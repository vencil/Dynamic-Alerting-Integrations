"""Tests for scripts/tools/dx/agent_output_metrics.py.

The tool is the BEFORE/AFTER instrument for the agent-harness plan (evidence
contract, CHANGELOG entry cap, PR template). Nothing else re-derives these
numbers, so a parser that silently miscounts entries would make every later
"it helped" claim unfalsifiable. Coverage:

  - section_lines: bracketed and bare headings, block ends at next ``## ``,
    ``### `` stays inside, missing section -> None
  - iter_entries: nested bullets and wrapped lines are continuation; blank
    lines do not close; a column-0 non-bullet line does; line numbers are
    1-based and point at the bullet
  - nearest_rank: the pinned convention (ceil(p*n), 1-based), empty -> 0
  - has_evidence_fence: positive; ``$`` in prose is not evidence; a fence
    whose first line is not a command is not evidence; ``~~~`` fences count
  - CLI: --help 0; no subcommand 2; missing file 2; missing section 2;
    --json stdout is exactly one JSON document; pr-bodies with gh missing
    -> 2, with gh output stubbed -> 0 and the fence count is right
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

DX_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, DX_DIR)
import agent_output_metrics as aom  # noqa: E402

SCRIPT = os.path.join(DX_DIR, "agent_output_metrics.py")

SAMPLE = """# Changelog

## [Unreleased]

<!-- staging note -->

### Fixed

- **entry one** short
- **entry two** first line
  second line of two
  - nested bullet of two

  after a blank, still two
- **entry three** only line

### Added

- **entry four** in Added
```
- not an entry: inside a column-0 fence
```
- **entry five** after the fence

## [v2.9.0] - 2026-06-06

- old entry
"""


# ============================================================
# section_lines
# ============================================================


def test_section_bracketed_and_bare_heading():
    assert aom.section_lines(SAMPLE, "Unreleased") is not None
    assert aom.section_lines("## Notes\n- a\n", "Notes") == (1, ["- a"])


def test_section_ends_at_next_h2_and_keeps_h3():
    no, block = aom.section_lines(SAMPLE, "Unreleased")
    assert no == 3
    joined = "\n".join(block)
    assert "### Added" in joined
    assert "old entry" not in joined


def test_section_missing_is_none():
    assert aom.section_lines(SAMPLE, "v3.0.0") is None


# ============================================================
# iter_entries
# ============================================================


def _entries():
    no, block = aom.section_lines(SAMPLE, "Unreleased")
    return list(aom.iter_entries(block, no + 1))


def test_entry_count_and_line_numbers_are_bullets():
    ents = _entries()
    assert [e[1].splitlines()[0] for e in ents] == [
        "- **entry one** short",
        "- **entry two** first line",
        "- **entry three** only line",
        "- **entry four** in Added",
        "- **entry five** after the fence",
    ]
    lines = SAMPLE.splitlines()
    for no, body in ents:
        assert lines[no - 1] == body.splitlines()[0]


def test_continuation_nested_wrapped_and_blank_do_not_close():
    two = _entries()[1][1]
    assert "second line of two" in two
    assert "nested bullet of two" in two
    assert "after a blank, still two" in two


def test_column0_non_bullet_closes_entry():
    four = _entries()[3][1]
    assert "inside a column-0 fence" not in four
    assert four == "- **entry four** in Added"


# ============================================================
# nearest_rank
# ============================================================


@pytest.mark.parametrize("p,expected", [(0.5, 2), (0.75, 3), (0.9, 4), (1.0, 4)])
def test_nearest_rank_convention(p, expected):
    assert aom.nearest_rank([1, 2, 3, 4], p) == expected


def test_nearest_rank_empty_is_zero():
    assert aom.nearest_rank([], 0.5) == 0
    assert aom.length_stats([]) == {"median": 0, "p75": 0, "p90": 0, "max": 0}


# ============================================================
# has_evidence_fence
# ============================================================


def test_evidence_fence_positive_and_tilde():
    assert aom.has_evidence_fence("claim\n```\n$ make test\nok\n```\n")
    assert aom.has_evidence_fence("claim\n~~~\n\n  $ pytest -q\n~~~\n")


def test_dollar_in_prose_is_not_evidence():
    assert not aom.has_evidence_fence("cost is $ 5 and\n$ nope outside fence\n")


def test_fence_whose_first_line_is_not_a_command_is_not_evidence():
    assert not aom.has_evidence_fence("```\nplain output\n$ later line\n```\n")
    assert not aom.has_evidence_fence("```python\nx = 1\n```\n")


# ============================================================
# measure_changelog
# ============================================================


def test_measure_changelog_counts_cap_and_longest():
    r = aom.measure_changelog(SAMPLE, "Unreleased", cap=40, top=2)
    assert r["entries"] == 5
    assert r["over_cap"] == sum(1 for _, b in _entries() if len(b) > 40) == 1
    assert [e["head"] for e in r["longest"]][0].startswith("- **entry two**")
    assert len(r["longest"]) == 2


# ============================================================
# CLI
# ============================================================


def _run(args, **kw):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True,
                          text=True, encoding="utf-8", timeout=60, **kw)


def test_help_exits_zero():
    assert _run(["--help"]).returncode == 0


def test_no_subcommand_is_caller_error():
    p = _run([])
    assert p.returncode == 2
    assert "subcommand" in p.stderr


def test_missing_file_and_missing_section_are_caller_errors(tmp_path):
    assert _run(["changelog", "--path", str(tmp_path / "nope.md")]).returncode == 2
    f = tmp_path / "CHANGELOG.md"
    f.write_text(SAMPLE, encoding="utf-8")
    p = _run(["changelog", "--path", str(f), "--section", "v9.9.9"])
    assert p.returncode == 2
    assert "v9.9.9" in p.stderr


def test_json_stdout_is_one_document(tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(SAMPLE, encoding="utf-8")
    p = _run(["--json", "changelog", "--path", str(f), "--cap", "40"])
    assert p.returncode == 0, p.stderr
    doc = json.loads(p.stdout)
    assert doc["status"] == "ok" and doc["entries"] == 5 and doc["over_cap"] == 1
    assert p.stdout.strip().count("\n") == 0


def test_text_output_mentions_counts(tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(SAMPLE, encoding="utf-8")
    p = _run(["changelog", "--path", str(f)])
    assert p.returncode == 0
    assert "5 entries" in p.stdout


def test_pr_bodies_gh_missing_is_caller_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("gh")
    monkeypatch.setattr(aom.subprocess, "run", boom)
    assert aom.main(["pr-bodies"]) == 2


def test_pr_bodies_counts_fences(monkeypatch, capsys):
    fake = [
        {"number": 1, "body": "## Evidence\n```\n$ make test\n3 passed\n```\n"},
        {"number": 2, "body": "no evidence here"},
        {"number": 3, "body": None},
    ]
    monkeypatch.setattr(aom, "_run_gh", lambda state, limit: json.dumps(fake))
    assert aom.main(["--json", "pr-bodies", "--limit", "3"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["prs"] == 3
    assert doc["with_evidence_fence"] == 1
    assert doc["evidence_fence_prs"] == [1]
    assert doc["chars"]["max"] == len(fake[0]["body"])


def test_pr_bodies_non_json_is_caller_error(monkeypatch):
    monkeypatch.setattr(aom, "_run_gh", lambda state, limit: "not json")
    assert aom.main(["pr-bodies"]) == 2
