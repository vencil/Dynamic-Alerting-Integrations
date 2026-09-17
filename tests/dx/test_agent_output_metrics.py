"""Tests for scripts/tools/dx/agent_output_metrics.py: the entry / fence /
percentile definitions in its module docstring, and the CLI exit-code contract."""
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


def test_section_does_not_end_at_a_fenced_h2_line():
    text = "## [Unreleased]\n- a\n```md\n## [v1.0.0]\n```\n- b\n## [v2.0.0]\n- c\n"
    no, block = aom.section_lines(text, "Unreleased")
    assert no == 1 and block == ["- a", "```md", "## [v1.0.0]", "```", "- b"]


def test_heading_is_found_even_below_an_unclosed_fence():
    """Fence tracking while looking for the heading let one unclosed fence
    above it hide the whole section (blind review, PR-C round 2)."""
    text = "```\n## [Unreleased]\n- a\n"
    assert aom.section_lines(text, "Unreleased") == (2, ["- a"])


def test_a_fenced_example_above_the_real_heading_is_not_the_heading():
    """Round 3: a fence-blind heading lookup took the fenced example as the
    heading and then read the example's closing fence as an opener, so the
    section ran to EOF (false red, and `new` stuck at 0)."""
    text = ("```md\n## [Unreleased]\n- example\n```\n\n## [Unreleased]\n- real\n\n"
            "## [v1.0.0] (2026-01-01)\n- released\n")
    assert aom.section_lines(text, "Unreleased") == (6, ["- real", ""])


def test_fallback_to_a_fenced_heading_keeps_the_fence_state():
    """When every match is fenced (unclosed fence above), the first match
    counts AND the end scan starts inside that fence, so the fence's closer
    closes it and the next `## ` really ends the section."""
    text = "```\n## [Unreleased]\n- a\n```\n## [v1.0.0]\n- b\n"
    assert aom.section_lines(text, "Unreleased") == (2, ["- a", "```"])


def test_longest_head_is_the_real_first_line():
    text = "## [Unreleased]\n- head\u2028tail " + "x" * 30 + "\n"
    r = aom.measure_changelog(text, "Unreleased", 1000, 1)
    assert r["longest"][0]["head"].startswith("- head\u2028tail")


def test_prose_len_splits_only_on_real_line_breaks():
    entry = "- head\u2028  ```\u2028hidden\u2028  ```"
    assert aom.prose_len(entry) == len(entry)


def test_section_splits_only_on_real_line_breaks():
    text = "## [Unreleased]\n- a\u2028## [v1.0.0]\n- b\n"
    no, block = aom.section_lines(text, "Unreleased")
    assert block == ["- a\u2028## [v1.0.0]", "- b"]
    assert aom.split_lines("a\r\nb\rc\nd\n") == ["a", "b", "c", "d"]


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


def test_column0_fence_content_is_no_entry_and_closes_the_previous_one():
    ents = _entries()
    assert all("inside a column-0 fence" not in body for _, body in ents)
    assert ents[3][1] == "- **entry four** in Added"


def test_column0_heading_closes_entry():
    """`### Added` follows entry three at column 0 -- it must not be absorbed."""
    assert _entries()[2][1] == "- **entry three** only line"


def test_tilde_fence_at_column0_hides_bullets_and_only_its_own_marker_closes_it():
    block = ["- e1", "~~~", "- not an entry", "```", "- still not", "~~~", "- e2"]
    got = [b for _, b in aom.iter_entries(block, 1)]
    assert got == ["- e1", "- e2"]


def test_one_space_indent_closes_entry():
    block = ["- one", "  cont", " single", "- two"]
    assert [b for _, b in aom.iter_entries(block, 1)] == ["- one\n  cont", "- two"]


def test_section_name_with_spaces_is_reachable():
    assert aom.section_lines("## [Release Notes] - x\n- a\n", "Release Notes") == (1, ["- a"])


# ============================================================
# prose_len
# ============================================================


def test_prose_len_drops_indented_fences_and_table_rows():
    entry = "- head\n  prose\n  ```\n  $ cmd\n  out\n  ```\n  | a | b |\n  |---|---|\n  tail"
    assert aom.prose_len(entry) == len("- head\n  prose\n  tail")
    assert aom.prose_len("- only") == len("- only")


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


def test_evidence_commands_lists_every_command_line_of_an_evidence_fence():
    body = "claim\n```\n$ make test\nok\n  $ pytest -q\n3 passed\n```\n"
    assert aom.evidence_commands(body) == ["make test", "pytest -q"]


def test_evidence_commands_ignores_fences_that_do_not_open_with_a_command():
    # `$ later` inside an output-first block is output, not a command
    assert aom.evidence_commands("```\nplain\n$ later\n```\n") == []
    # an evidence fence after a non-evidence one is still found
    assert aom.evidence_commands("```\nplain\n```\n```\n$ ls\n```\n") == ["ls"]
    # a `$ ` line after a non-`$` output line in an evidence fence is still a command
    assert aom.evidence_commands("```\n$ a\nout\n$ b\n```\n") == ["a", "b"]


def test_fence_commands_reads_every_fence_at_the_fence_indent():
    # output-first fence: no evidence for shape, but its `$ ` lines ARE citations
    body = "```\nlog\n$ make test\n```\n- item\n  ```\n  $ pytest -q\n    $ nested prompt in output\n  ```\n"
    assert aom.has_evidence_fence(body), "the indented fence opens with a command"
    assert aom.fence_commands(body) == ["make test", "pytest -q"]
    assert aom.fence_commands("prose $ not in a fence\n") == []


def test_evidence_commands_and_has_evidence_fence_are_one_ruler():
    for body in ("```\n$ a\n```\n", "```\nx\n```\n", "prose $ a\n", "~~~\n\n$ a\n~~~\n",
                 "```\nx\n~~~\n~~~\n$ ls\n```\n"):
        assert aom.has_evidence_fence(body) is bool(aom.evidence_commands(body)), body


def test_fence_whose_first_line_is_not_a_command_is_not_evidence():
    assert not aom.has_evidence_fence("```\nplain output\n$ later line\n```\n")
    assert not aom.has_evidence_fence("```python\nx = 1\n```\n")


def test_tilde_inside_backtick_fence_does_not_close_it():
    # Discriminating case: an any-marker toggle would close on the first
    # `~~~`, reopen on the second, and read `$ ls` as a first line -> True.
    assert aom.has_evidence_fence("```\nx\n~~~\n~~~\n$ ls\n```\n") is False
    assert aom.has_evidence_fence("~~~\nx\n```\n```\n$ ls\n~~~\n") is False


def test_fence_closer_must_be_bare_and_backtick_info_string_may_not_contain_backtick():
    # "``` trailing" is content, not a closer -> `$ ls` is still inside the block
    assert aom.has_evidence_fence("```\nx\n``` trailing\n$ ls\n```\n") is False
    # "```a`b" cannot open a backtick fence -> `$ ls` is prose, not evidence
    assert aom.has_evidence_fence("```a`b\n$ ls\n```\n") is False
    assert aom.has_evidence_fence("```bash\n$ ls\n```   \n") is True
    block = ["- e1", "```", "- x", "``` not a closer", "- y", "```", "- e2"]
    assert [b for _, b in aom.iter_entries(block, 1)] == ["- e1", "- e2"]


def test_blank_lines_inside_an_entry_count_but_trailing_ones_do_not():
    block = ["- a", "", "  b", "", "", "- c", ""]
    assert [b for _, b in aom.iter_entries(block, 1)] == ["- a\n\n  b", "- c"]


def test_longer_fence_may_contain_shorter_one():
    assert aom.has_evidence_fence("````\n```\n$ ls\n```\n````\n") is False
    assert aom.has_evidence_fence("````\n$ ls\n```\n````\n") is True
    entry = "- head\n  ````\n  ```\n  $ out\n  ```\n  ````\n  tail"
    assert aom.prose_len(entry) == len("- head\n  tail")
    block = ["- e1", "````", "```", "- inner", "```", "````", "- e2"]
    assert [b for _, b in aom.iter_entries(block, 1)] == ["- e1", "- e2"]


# ============================================================
# measure_changelog
# ============================================================


def test_measure_changelog_counts_cap_and_longest():
    r = aom.measure_changelog(SAMPLE, "Unreleased", cap=40, top=2)
    assert r["entries"] == 5
    assert r["over_cap"] == sum(1 for _, b in _entries() if len(b) > 40) == 1
    assert [e["head"] for e in r["longest"]][0].startswith("- **entry two**")
    assert len(r["longest"]) == 2
    assert r["prose_chars"]["max"] == r["chars"]["max"]   # SAMPLE has no evidence lines
    assert r["over_cap_prose"] == 1
    assert r["longest"][0]["prose_chars"] == r["longest"][0]["chars"]


def test_top_zero_lists_nothing():
    assert aom.measure_changelog(SAMPLE, "Unreleased", cap=40, top=0)["longest"] == []


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


def test_json_flag_is_accepted_after_the_subcommand(tmp_path):
    """The module docstring's own example puts --json last; it must work."""
    f = tmp_path / "CHANGELOG.md"
    f.write_text(SAMPLE, encoding="utf-8")
    p = _run(["changelog", "--path", str(f), "--json"])
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["entries"] == 5


def test_negative_top_or_cap_is_a_caller_error(tmp_path):
    f = tmp_path / "CHANGELOG.md"
    f.write_text(SAMPLE, encoding="utf-8")
    assert _run(["changelog", "--path", str(f), "--top", "-1"]).returncode == 2
    assert _run(["changelog", "--path", str(f), "--cap", "-5"]).returncode == 2
    assert _run(["pr-bodies", "--limit", "-1"]).returncode == 2


def test_pr_bodies_gh_failure_reports_the_first_stderr_line(monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="invalid value for --limit: 0\n\nUsage: ...\n  -w, --web\n")
    monkeypatch.setattr(aom.subprocess, "run", boom)
    assert aom.main(["pr-bodies", "--limit", "0"]) == 2
    err = capsys.readouterr().err
    assert "invalid value for --limit: 0" in err
    assert "--web" not in err


def test_pr_bodies_wrong_element_shape_is_a_caller_error(monkeypatch, capsys):
    monkeypatch.setattr(aom, "_run_gh", lambda state, limit: json.dumps([1, 2]))
    assert aom.main(["--json", "pr-bodies"]) == 2
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(aom, "_run_gh", lambda state, limit: json.dumps([{"body": "x"}]))
    assert aom.main(["pr-bodies"]) == 2
    assert aom.pr_shape_error([{"number": True, "body": "x"}]) is not None
    assert aom.pr_shape_error([{"number": 7, "body": None}]) is None


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
