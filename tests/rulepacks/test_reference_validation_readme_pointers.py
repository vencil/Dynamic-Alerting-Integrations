"""reference-validation/README.md 的外部指路必須解析得到（TRK-339 WS4a-1 後續）.

WHY this exists
---------------
The README's 「重跑（迴歸基線）」 section deliberately refuses to copy the VM
install commands and points at named CI workflow steps instead — "版本與 SHA 只能
有一份，手抄第二份正是這個 repo 在消滅的病". That is the right call, but as
written the pointer table was itself an ungated hand-copy: three step names and
two relative links, transcribed by hand, with NOTHING checking them.

`scripts/tools/lint/check_doc_links.py` does not close that gap and must not be
asked to: its ``scan_dirs`` is ``["docs", "rule-packs"]`` (plus four root-level
Markdown files), so everything under ``tests/`` is invisible to it. Measured, not
assumed — appending a link to a file that does not exist to this README and
running ``check_doc_links.py --ci`` still exits 0 with "Broken links found: 0"
over the same 265 files. Widening ``scan_dirs`` is a separate change with its own
fallout (tests/ carries Markdown that was never written to that linter's rules),
so the pointer table gets a targeted pytest gate here instead.

WHERE it lives
--------------
``tests/rulepacks/`` — next to ``test_reference_validation_sync.py``, the other
gate that owns the ``reference-validation/`` fixture directory, and inside the
tree the CI ``python-tests-run`` job already runs wholesale (``pytest tests/``).
A new file rather than a section appended to that one: its docstring declares a
single narrow contract (candidate rules ↔ shipped packs, via the divergence
ledger), and a doc-pointer guard is a different subject.

SHAPE
-----
Nothing here hardcodes a step name or a path — that would recreate the hand-copy
the README exists to avoid, one layer down. The README is PARSED, every claim it
makes is extracted, and each extracted claim is resolved against reality:

  * every ```<name>` step`` claim in the table must equal a real
    ``- name:`` of a step in the workflow that same row names;
  * every relative Markdown link in the README must resolve to a file on disk;
  * where a row both links a workflow and names it in prose, the two must agree.

Vacuity floors are explicit: a regex that silently matched nothing would
otherwise turn this whole module green while checking zero claims.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_README = _REPO / "tests" / "rulepacks" / "reference-validation" / "README.md"
_WORKFLOWS = _REPO / ".github" / "workflows"

_SECTION = "## 重跑（迴歸基線）"

# `Some Step Name` step  — the README's own phrasing for "this step, over there".
_STEP_CLAIM = re.compile(r"`([^`]+)`\s*step")
# A backticked workflow filename, e.g. `ci.yml` / `nightly-vm-replay.yaml`.
_WORKFLOW_TOKEN = re.compile(r"`([\w.-]+\.ya?ml)`")
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
# A table's `|---|---|` separator row (also tolerates `:--` alignment markers).
_TABLE_RULE = re.compile(r"\|[\s:|-]+\|")


def _lines_fence_blanked(md: str) -> list[str]:
    """Split into lines, replacing fenced-code lines with "".

    Fence-awareness is not decoration: a ``|``-bearing or backticked line inside
    the section's ```sh block is not a table row and not a pointer, and a scanner
    that cannot tell the difference reports confident nonsense in both
    directions. The fence markers themselves are blanked too.
    """
    out: list[str] = []
    in_fence = False
    for line in md.split("\n"):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append("")
            continue
        out.append("" if in_fence else line)
    return out


def _section_lines(md: str, heading: str) -> list[str]:
    """The lines under ``heading`` up to the next ``## `` heading."""
    lines = _lines_fence_blanked(md)
    inside = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            inside = stripped == heading
            continue
        if inside:
            out.append(line)
    return out


def _table_body_rows(lines: list[str]) -> list[str]:
    """Body rows of the first pipe table in ``lines`` (header + rule dropped)."""
    rows = [ln.strip() for ln in lines if ln.strip().startswith("|")]
    rows = [r for r in rows if not _TABLE_RULE.fullmatch(r)]
    return rows[1:]


@pytest.fixture(scope="module")
def readme_text() -> str:
    return _README.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def install_rows(readme_text: str) -> list[str]:
    rows = _table_body_rows(_section_lines(readme_text, _SECTION))
    assert len(rows) >= 3, (
        f"expected the 「{_SECTION}」 install-pointer table to still have at "
        f"least its three rows (vmalert / vmalert-tool / vmsingle); parsed "
        f"{len(rows)}: {rows}. If the table moved or was reshaped, re-anchor "
        "this guard rather than deleting it — an unparsed table means the "
        "pointers below are checked by nothing.")
    return rows


def _workflow_step_names(path: Path) -> set[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    names: set[str] = set()
    for job in (doc.get("jobs") or {}).values():
        for step in (job.get("steps") or []):
            if isinstance(step, dict) and step.get("name"):
                names.add(str(step["name"]).strip())
    assert names, f"{path} parsed to zero named steps — re-anchor this guard"
    return names


def test_each_row_names_exactly_one_workflow(install_rows: list[str]):
    """Per-row vacuity floor for the test below.

    A row whose workflow token stopped matching would make its step-name claim
    unverifiable, and a loop that just `continue`d past it would report success
    for a pointer it never looked at.
    """
    for row in install_rows:
        found = set(_WORKFLOW_TOKEN.findall(row))
        assert len(found) == 1, (
            f"expected exactly one backticked workflow filename in this row, "
            f"got {sorted(found)}: {row}")
        assert (_WORKFLOWS / found.pop()).is_file(), row


def test_every_named_step_exists_verbatim_in_the_workflow_it_points_at(
        install_rows: list[str]):
    """The whole point of the table: the reader searches CI for that step name.

    A step renamed on the workflow side — or transcribed loosely here, e.g.
    dropping a parenthetical suffix — leaves the reader searching a string that
    is not there, and the README claims to be the SSOT for VM installation.
    """
    checked = 0
    for row in install_rows:
        workflow = _WORKFLOWS / _WORKFLOW_TOKEN.search(row).group(1)
        real = _workflow_step_names(workflow)
        claims = _STEP_CLAIM.findall(row)
        assert claims, (
            "row names a workflow but makes no `<name>` step claim, so nothing "
            f"here is verifiable: {row}")
        for claim in claims:
            assert claim.strip() in real, (
                f"{_README.relative_to(_REPO)} points at a step named "
                f"{claim.strip()!r} in {workflow.relative_to(_REPO)}, which has "
                f"no such step. Quote the step name VERBATIM — the reader's only "
                f"move is to search for it. Real names:\n  "
                + "\n  ".join(sorted(real)))
            checked += 1
    assert checked >= 3, (
        f"only {checked} step-name claims were checked; the table is supposed "
        "to carry at least three (vmalert / vmalert-tool / vmsingle)")


def test_linked_workflow_matches_the_one_named_in_the_same_row(
        install_rows: list[str]):
    """Where a row both links and names a workflow, the two must be the same
    file — otherwise the link and the prose send the reader to different CI
    files and only one of them can be right."""
    for row in install_rows:
        named = _WORKFLOW_TOKEN.search(row).group(1)
        for _text, target in _MD_LINK.findall(row):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            if target.split("#")[0].endswith((".yml", ".yaml")):
                assert Path(target.split("#")[0]).name == named, (
                    f"row links {target!r} but names {named!r}: {row}")


def test_every_relative_link_in_the_readme_resolves(readme_text: str):
    """The gap check_doc_links.py leaves open for tests/.

    ``check_doc_links.py``'s ``scan_dirs`` is ``["docs", "rule-packs"]``; this
    README is under ``tests/`` and is therefore never opened by it. Verified by
    experiment, not by reading: a link to a nonexistent file appended here still
    leaves ``check_doc_links.py --ci`` at exit 0 / "Broken links found: 0".
    """
    checked = 0
    for _text, target in _MD_LINK.findall("\n".join(
            _lines_fence_blanked(readme_text))):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        resolved = (_README.parent / path).resolve()
        assert resolved.exists(), (
            f"{_README.relative_to(_REPO)} links {target!r}, which resolves to "
            f"{resolved} — no such file. (check_doc_links.py does not scan "
            "tests/, so this assertion is the only thing that catches it.)")
        checked += 1
    assert checked >= 5, (
        f"only {checked} relative links were checked — the README carries more "
        "than that; the link regex has probably stopped matching")
