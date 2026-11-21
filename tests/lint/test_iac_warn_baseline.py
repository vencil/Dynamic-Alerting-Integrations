"""The Layer 1 WARN register and its generated table (#3863).

`scripts/tools/lint/_iac_warn_baseline.py` holds a rationale per (Dockerfile,
rule code); `check_iac_vibe_rules.py --ci` fails on the three ways the
register and the doc can disagree with hadolint — drift, unregistered,
stale — and `--write-baseline` regenerates the doc's table. The engine is
stubbed here (`run_hadolint`); the run against the pinned hadolint is the
`iac-sast-check` hook itself, in the `Lint` job.
"""
from __future__ import annotations

import errno
import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_DIR = str(REPO_ROOT / "scripts" / "tools" / "lint")
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

import _iac_warn_baseline as wb  # noqa: E402
import check_iac_vibe_rules as iac  # noqa: E402

A = "a/Dockerfile"
B = "b/Dockerfile"
ENTRY = wb.Entry(summary="s", rationale="r", exit="e")


def _warn(path, code="DL3018", line=1):
    return {"file": path, "line": line, "code": code, "level": "warning", "message": "pin"}


def _doc(block: str) -> str:
    return f"# T\n\nbefore\n\n{block}\n\nafter\n"


# ---------------------------------------------------------------------------
# pure functions
# ---------------------------------------------------------------------------
def test_registered_warns_have_no_problem():
    assert wb.problems([_warn(A), _warn(A, line=9)], {(A, "DL3018"): ENTRY}) == []


def test_an_unregistered_warn_is_a_problem_naming_its_lines():
    out = wb.problems([_warn(A, line=3), _warn(A, line=7)], {})
    assert len(out) == 1 and out[0].startswith("unregistered"), out
    assert f"{A}:3" in out[0] and f"{A}:7" in out[0], out


def test_a_register_entry_no_warn_produced_is_stale():
    out = wb.problems([_warn(A)], {(A, "DL3018"): ENTRY, (B, "DL3018"): ENTRY})
    assert out == [f"stale register entry ({B}, DL3018): hadolint no longer "
                   f"reports it — delete the entry"], out


def test_the_key_is_file_and_code_not_line():
    """A moved line keeps its registration; another code in the same file
    does not inherit it."""
    reg = {(A, "DL3018"): ENTRY}
    assert wb.problems([_warn(A, line=500)], reg) == []
    assert wb.problems([_warn(A, line=500), _warn(A, "DL3019")], reg)[0].startswith(
        "unregistered WARN DL3019")


def test_render_counts_per_key_and_has_no_line_numbers():
    block = wb.render([_warn(A, line=11), _warn(A, line=42)], {(A, "DL3018"): ENTRY})
    assert block.splitlines()[0] == wb.BEGIN and block.splitlines()[-1] == wb.END
    row = [ln for ln in block.splitlines() if ln.startswith("| 1 ")]
    assert row == [f"| 1 | `{A}` | DL3018 | 2 | s | r | e |"], block
    assert "11" not in block and "42" not in block


def test_render_escapes_a_pipe_in_the_text():
    block = wb.render([_warn(A)], {(A, "DL3018"): wb.Entry("a|b", "r", "e")})
    assert "a\\|b" in block


def test_render_with_no_warn_says_so():
    assert wb.render([], {}) == f"{wb.BEGIN}\n（目前沒有 hadolint WARN。）\n{wb.END}"


def test_splice_replaces_only_the_block():
    old = _doc(f"{wb.BEGIN}\nstale row\n{wb.END}")
    new = wb.splice(old, wb.render([], {}))
    assert new == _doc(wb.render([], {}))


@pytest.mark.parametrize("text", [
    "no markers at all\n",
    f"{wb.BEGIN}\n",
    f"{wb.END}\n{wb.BEGIN}\n",
    f"{wb.BEGIN}\n{wb.END}\n{wb.BEGIN}\n{wb.END}\n",
], ids=["none", "no-end", "end-first", "twice"])
def test_a_malformed_block_is_an_error_not_an_append(text):
    with pytest.raises(wb.BaselineDocError):
        wb.splice(text, wb.render([], {}))


@pytest.mark.parametrize("entry", [
    wb.Entry("", "r", "e"), wb.Entry("s", "  ", "e"), wb.Entry("s", "r", "line\nbreak")])
def test_the_register_refuses_an_empty_or_multiline_field(entry):
    with pytest.raises(ValueError):
        wb._check_register({(A, "DL3018"): entry})


def test_the_committed_register_is_well_formed():
    wb._check_register(wb.REGISTER)
    assert wb.REGISTER, "an empty register would make the committed table vacuous"


# ---------------------------------------------------------------------------
# through check_iac_vibe_rules.main (engine stubbed)
# ---------------------------------------------------------------------------
@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A doc whose table matches one registered WARN on A; returns the doc."""
    reg = {(A, "DL3018"): ENTRY}
    warns = [_warn(A, line=5)]
    doc = tmp_path / "baseline.md"
    doc.write_text(_doc(wb.render(warns, reg)), encoding="utf-8")
    monkeypatch.setattr(wb, "REGISTER", reg)
    monkeypatch.setattr(wb, "DOC", str(doc))   # absolute: REPO_ROOT / DOC is DOC
    monkeypatch.setattr(iac, "find_dockerfiles", lambda: [])
    monkeypatch.setattr(iac, "collect_findings", _fake_collect(warns))
    monkeypatch.delenv("PR_BODY", raising=False)
    return doc


def _fake_collect(warns, block=()):
    def collect(dockerfiles, warn_out=None):
        if warn_out is not None:
            warn_out.extend(warns)
        return {"BLOCK": list(block), "WARN": [f"{w['file']}:{w['line']}" for w in warns],
                "INFO": []}
    return collect


def _main(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    rc = iac.main()
    return rc, capsys.readouterr().out


def test_green_registered_warn_is_rc_0(tree, monkeypatch, capsys):
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 0, out
    assert "[BASELINE]" not in out and "1 WARN" in out, out


def test_red_drift(tree, monkeypatch, capsys):
    tree.write_text(tree.read_text(encoding="utf-8").replace("| s |", "| hand-edited |"),
                    encoding="utf-8")
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 1, out
    assert "generated WARN table" in out and "out of date" in out, out


def test_red_unregistered(tree, monkeypatch, capsys):
    monkeypatch.setattr(iac, "collect_findings", _fake_collect([_warn(A), _warn(B)]))
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 1, out
    assert f"[BASELINE] unregistered WARN DL3018 in {B}" in out, out


def test_red_stale(tree, monkeypatch, capsys):
    monkeypatch.setattr(wb, "REGISTER", {**wb.REGISTER, (B, "DL3018"): ENTRY})
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 1, out
    assert f"[BASELINE] stale register entry ({B}, DL3018)" in out, out


def test_a_missing_doc_is_red_not_skipped(tree, monkeypatch, capsys):
    tree.unlink()
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 1 and "[BASELINE] cannot use" in out, out


def test_without_ci_a_problem_is_printed_but_rc_0(tree, monkeypatch, capsys):
    monkeypatch.setattr(wb, "REGISTER", {})
    rc, out = _main(monkeypatch, capsys)
    assert rc == 0 and "[BASELINE] unregistered" in out, out


def test_the_pr_body_bypass_does_not_waive_a_register_problem(tree, monkeypatch, capsys):
    """The bypass downgrades BLOCK findings only; an unwritten reason is not
    a legitimate exception, it is a missing reason."""
    monkeypatch.setattr(iac, "collect_findings",
                        _fake_collect([_warn(A), _warn(B)], block=["x [V1] boom"]))
    monkeypatch.setenv("PR_BODY", "bypass-lint: iac-vibe-rules\nreason: " + "word " * 31)
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert "BYPASSED" in out and rc == 1, out


def test_write_baseline_regenerates_the_table(tree, monkeypatch, capsys):
    tree.write_text(_doc(f"{wb.BEGIN}\nold\n{wb.END}"), encoding="utf-8")
    rc, out = _main(monkeypatch, capsys, "--write-baseline")
    assert rc == 0, out
    assert tree.read_text(encoding="utf-8") == _doc(wb.render([_warn(A)], wb.REGISTER))
    rc, out = _main(monkeypatch, capsys, "--ci")
    assert rc == 0, out


def test_write_baseline_refuses_while_a_warn_is_unregistered(tree, monkeypatch, capsys):
    before = _doc(f"{wb.BEGIN}\nold\n{wb.END}")
    tree.write_text(before, encoding="utf-8")
    monkeypatch.setattr(iac, "collect_findings", _fake_collect([_warn(A), _warn(B)]))
    rc, out = _main(monkeypatch, capsys, "--write-baseline")
    assert rc == 1 and "unregistered" in out, out
    assert tree.read_text(encoding="utf-8") == before


def test_write_baseline_that_cannot_write_is_rc_2_and_keeps_the_doc(
        tree, monkeypatch, capsys):
    """A rename nobody falls back from (EIO): rc 2, one ERROR line naming
    the doc, no traceback, the old doc intact (#3981 artifact entry)."""
    before = _doc(f"{wb.BEGIN}\nold\n{wb.END}")
    tree.write_text(before, encoding="utf-8")
    real_replace, where, fired = os.replace, os.path.realpath(tree), []

    def replace(src, dst, *a, **k):
        if os.path.realpath(dst) == where:
            fired.append(src)
            raise OSError(errno.EIO, "Input/output error (simulated)", src, dst)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(sys, "argv", ["prog", "--write-baseline"])
    with pytest.raises(SystemExit) as exc:
        iac.main()
    err = capsys.readouterr().err
    assert fired, "the injected failure never ran"
    assert exc.value.code == 2, err
    assert f"ERROR: cannot write {tree}:" in err and "Traceback" not in err, err
    assert tree.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------
def test_the_committed_doc_holds_the_block():
    text = (REPO_ROOT / "docs/internal/iac-lint-baseline.md").read_text(encoding="utf-8")
    wb.splice(text, wb.render([], {}))   # raises unless exactly one block


def test_the_hook_runs_when_the_register_or_the_doc_changes():
    """A PR that edits only the register or the table must still be judged."""
    cfg = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    m = re.search(r"- id: iac-sast-check\n(?:\s+\w.*\n)*?\s+files: '([^']+)'", cfg)
    assert m, "iac-sast-check hook or its files: pattern not found"
    files = re.compile(m.group(1))
    for path in ("docs/internal/iac-lint-baseline.md",
                 "scripts/tools/lint/_iac_warn_baseline.py",
                 "scripts/tools/lint/check_iac_vibe_rules.py",
                 "try-local/generator/Dockerfile"):
        assert files.search(path), path
