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
        lineno, n, preview = offenders[0]
        assert n > 2000
        assert "xxxx" in preview

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
