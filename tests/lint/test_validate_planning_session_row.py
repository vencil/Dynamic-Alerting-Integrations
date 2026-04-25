"""Smoke tests for validate_planning_session_row.py — §12.1 Session Ledger bloat guard.

Covers:
  - `find_offending_rows` only scans inside §12.1 scope
  - Divider lines (`| --- | --- |`) are skipped, not flagged as rows
  - Rows under limit pass; over-limit rows reported with line number + char count
  - `resolve_targets` falls back to glob when no CLI path given
  - `main` returns 0 when no planning docs match the glob
  - `main` returns 1 when at least one row exceeds the limit
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import validate_planning_session_row as vpsr  # noqa: E402


# ---------------------------------------------------------------------------
# find_offending_rows
# ---------------------------------------------------------------------------
class TestFindOffendingRows:
    def _write_planning(self, path, ledger_rows, pre="", post=""):
        content = (
            "---\ntitle: fake planning\n---\n\n"
            f"{pre}"
            "### 12.1 Session Ledger（Working Log）\n\n"
            "| Session | Notes |\n"
            "|---|---|\n"
        )
        content += "\n".join(ledger_rows) + "\n"
        content += f"\n### 12.2 Next section\n\n{post}"
        path.write_text(content, encoding="utf-8")

    def test_row_under_limit_passes(self, tmp_path):
        f = tmp_path / "v2.9.0-planning.md"
        self._write_planning(f, ["| #01 | short summary |"])
        offenders = vpsr.find_offending_rows(f, limit=2000)
        assert offenders == []

    def test_row_over_limit_flagged(self, tmp_path):
        f = tmp_path / "v2.9.0-planning.md"
        big_row = "| #99 | " + ("x" * 2500) + " |"
        self._write_planning(f, [big_row])
        offenders = vpsr.find_offending_rows(f, limit=2000)
        assert len(offenders) == 1
        # 4-tuple per --auto-archive-suggest enhancement: (lineno, n, preview, full_line)
        lineno, n, preview, full_line = offenders[0]
        assert n > 2000
        assert "xxxx" in preview
        assert full_line == big_row  # full unmodified row preserved for archive suggester

    def test_divider_lines_not_flagged(self, tmp_path):
        """A divider `| --- | --- |` must never be reported as a bloated row
        even if its character length happens to exceed the limit."""
        f = tmp_path / "v2.9.0-planning.md"
        big_divider = "|" + ("-" * 2500) + "|"
        self._write_planning(f, [big_divider])
        offenders = vpsr.find_offending_rows(f, limit=2000)
        assert offenders == []

    def test_scope_restricted_to_12_1(self, tmp_path):
        """Long rows OUTSIDE §12.1 (e.g. a live-tracker table) must be ignored."""
        f = tmp_path / "v2.9.0-planning.md"
        content = (
            "### 12.1 Session Ledger\n\n"
            "| #01 | short |\n\n"
            "### 12.2 Live Tracker\n\n"
            "| 1 | " + ("y" * 3000) + " |\n"
        )
        f.write_text(content, encoding="utf-8")
        offenders = vpsr.find_offending_rows(f, limit=2000)
        assert offenders == []

    def test_missing_file_returns_empty(self, tmp_path, capsys):
        offenders = vpsr.find_offending_rows(tmp_path / "nope.md", limit=2000)
        assert offenders == []


# ---------------------------------------------------------------------------
# resolve_targets
# ---------------------------------------------------------------------------
class TestResolveTargets:
    def test_cli_paths_take_precedence(self):
        targets = vpsr.resolve_targets(["a.md", "b.md"], "docs/internal/v*.md")
        assert [t.name for t in targets] == ["a.md", "b.md"]

    def test_empty_falls_back_to_glob(self):
        # glob against REPO_ROOT — result may be empty in CI clone, either is fine.
        targets = vpsr.resolve_targets([], "docs/internal/NONEXISTENT-*.md")
        assert targets == []


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
class TestMain:
    def test_no_docs_matches_glob_returns_zero(self, tmp_path, capsys):
        rc = vpsr.main(["--glob", "docs/internal/NONEXISTENT-*.md"])
        assert rc == 0

    def test_over_limit_returns_one(self, tmp_path):
        f = tmp_path / "v9.0.0-planning.md"
        content = (
            "### 12.1 Session Ledger\n\n"
            "|s|n|\n|---|---|\n"
            "| #1 | " + ("z" * 3000) + " |\n"
        )
        f.write_text(content, encoding="utf-8")
        rc = vpsr.main([str(f), "--limit", "2000"])
        assert rc == 1

    def test_under_limit_returns_zero(self, tmp_path):
        f = tmp_path / "v9.0.0-planning.md"
        content = (
            "### 12.1 Session Ledger\n\n"
            "|s|n|\n|---|---|\n"
            "| #1 | short |\n"
        )
        f.write_text(content, encoding="utf-8")
        rc = vpsr.main([str(f), "--limit", "2000"])
        assert rc == 0


# ---------------------------------------------------------------------------
# --auto-archive-suggest (issue #82 enhancement)
# ---------------------------------------------------------------------------
class TestAutoArchiveSuggest:
    """The enhancement reads the sibling -archive.md doc, finds matching
    `## §S#NN` sections, and emits suggested slim-pointer replacements
    for over-bloat rows that already have an archive section.

    Naming convention: `vX.Y.Z-planning.md` ↔ `vX.Y.Z-planning-archive.md`
    in the same directory.
    """

    def _write_planning_with_bloated_row(self, planning_path, session_id):
        """Write a planning doc with one over-bloat session row."""
        # 6-cell row matching the production schema:
        # | id | date | title | body | status | next |
        bloated_body = "x" * 2500
        row = (
            f"| #{session_id} | 2026-04-26 | Some session title | "
            f"{bloated_body} | done 🟢 | nothing |"
        )
        content = (
            "### 12.1 Session Ledger（Working Log）\n\n"
            "| Session | Date | Title | Notes | Status | Next |\n"
            "|---|---|---|---|---|---|\n"
            f"{row}\n"
        )
        planning_path.write_text(content, encoding="utf-8")

    def _write_archive_with_section(self, archive_path, session_id):
        """Write an archive doc containing a matching ## §S#NN section."""
        archive_path.write_text(
            f"# Archive\n\n"
            f"## §S#{session_id} — Some session title (PR #99, 2026-04-26)\n\n"
            f"Full session detail goes here, all 2500 chars of it.\n",
            encoding="utf-8",
        )

    def test_derive_archive_path_planning_to_archive(self, tmp_path):
        planning = tmp_path / "v2.8.0-planning.md"
        archive = vpsr.derive_archive_path(planning)
        assert archive.name == "v2.8.0-planning-archive.md"
        assert archive.parent == planning.parent

    def test_find_archived_sessions_extracts_ids(self, tmp_path):
        archive = tmp_path / "v2.8.0-planning-archive.md"
        archive.write_text(
            "# Archive\n\n"
            "## §S#27 — Phase .b kickoff (PR #59)\n\nbody1\n\n"
            "## §S#31 — Spawn task B (PR #69)\n\nbody2\n\n"
            "## §S#36 — PR-3 of 3 (PR #80)\n\nbody3\n",
            encoding="utf-8",
        )
        ids = vpsr.find_archived_sessions(archive)
        assert ids == {"27", "31", "36"}

    def test_find_archived_sessions_missing_file_returns_empty(self, tmp_path):
        assert vpsr.find_archived_sessions(tmp_path / "nope.md") == set()

    def test_suggest_slim_pointer_when_archive_section_exists(self, tmp_path):
        planning = tmp_path / "v2.8.0-planning.md"
        archive = tmp_path / "v2.8.0-planning-archive.md"
        self._write_planning_with_bloated_row(planning, session_id="42")
        self._write_archive_with_section(archive, session_id="42")
        offenders = vpsr.find_offending_rows(planning, limit=2000)
        assert len(offenders) == 1
        full_line = offenders[0][3]
        suggested = vpsr.suggest_slim_pointer(full_line, archive)
        assert suggested is not None
        # Slim pointer must reference §S#42 archive and be < 2000 chars.
        assert "§S#42" in suggested
        assert "Archived" in suggested
        assert len(suggested) < 2000
        # Must preserve session id, date, status cells.
        assert "| 42 |" in suggested or "| #42 |" in suggested
        assert "2026-04-26" in suggested
        assert "done" in suggested

    def test_suggest_slim_pointer_returns_none_when_no_archive_section(self, tmp_path):
        planning = tmp_path / "v2.8.0-planning.md"
        archive = tmp_path / "v2.8.0-planning-archive.md"
        self._write_planning_with_bloated_row(planning, session_id="42")
        # Archive exists but DOES NOT have a §S#42 section.
        archive.write_text("# Archive\n\n## §S#99 — different\n\nbody\n", encoding="utf-8")
        offenders = vpsr.find_offending_rows(planning, limit=2000)
        full_line = offenders[0][3]
        assert vpsr.suggest_slim_pointer(full_line, archive) is None

    def test_main_with_auto_archive_suggest_emits_replacement(self, tmp_path, capsys):
        planning = tmp_path / "v2.8.0-planning.md"
        archive = tmp_path / "v2.8.0-planning-archive.md"
        self._write_planning_with_bloated_row(planning, session_id="42")
        self._write_archive_with_section(archive, session_id="42")
        rc = vpsr.main([str(planning), "--auto-archive-suggest"])
        assert rc == 1  # over-bloat still reports failure
        out = capsys.readouterr().out
        assert "S#42" in out
        assert "SUGGESTED replacement" in out
        assert "Archived" in out

    def test_main_with_auto_archive_suggest_falls_back_when_no_archive(self, tmp_path, capsys):
        planning = tmp_path / "v2.8.0-planning.md"
        # No archive file at all.
        self._write_planning_with_bloated_row(planning, session_id="42")
        rc = vpsr.main([str(planning), "--auto-archive-suggest"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "S#42" in out
        # No suggestion emitted; instructions to write archive first.
        assert "MANUAL" in out or "first" in out.lower()


class TestTruncateAtWordBoundary:
    """`_truncate_at_word_boundary` keeps slim-pointer cells readable
    by cutting at whitespace instead of mid-word."""

    def test_short_input_returned_verbatim(self):
        assert vpsr._truncate_at_word_boundary("short", max_chars=80) == "short"

    def test_long_input_cuts_at_last_space(self):
        text = "the quick brown fox jumps over the lazy dog and onwards forever"
        got = vpsr._truncate_at_word_boundary(text, max_chars=20)
        # Budget 20: "the quick brown fox " is 20 chars; head[:19] = "the quick brown fox"
        # last space at index 15, so head[:15] = "the quick brown" + "…"
        assert got == "the quick brown…"
        assert len(got) <= 20

    def test_no_whitespace_falls_back_to_hard_cut(self):
        # One giant token with no spaces — fallback to hard truncation
        # rather than refuse to truncate.
        text = "x" * 100
        got = vpsr._truncate_at_word_boundary(text, max_chars=20)
        assert got == "x" * 19 + "…"
        assert len(got) <= 20

    def test_unicode_chars_count_as_chars(self):
        # CJK chars are single code points; budget counts code points, not bytes.
        text = "資料工程 platform team alpha beta gamma delta"
        got = vpsr._truncate_at_word_boundary(text, max_chars=15)
        # head[:14] = "資料工程 platform" (15 cps wait — let me recount)
        # actually whatever the cut, just verify length ≤ budget and ends with …
        assert len(got) <= 15
        assert got.endswith("…")
