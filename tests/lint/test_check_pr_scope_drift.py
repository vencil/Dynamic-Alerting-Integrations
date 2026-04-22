"""Smoke tests for check_pr_scope_drift.py — PR-level drift gate.

Covers:
  - `run` passes encoding="utf-8" + errors="replace" (regression guard for H3-class
    Windows cp950/cp932 UnicodeDecodeError on git stderr)
  - `check_tool_map` parses generate_tool_map.py --check output correctly
  - `check_working_tree_clean` treats both staged and unstaged diffs as FAIL
  - `check_working_tree_clean` ignores untracked files (`??`)
  - `main` returns 0 when both checks pass
  - `main` returns 1 when any check fails
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_pr_scope_drift as cpsd  # noqa: E402


# ---------------------------------------------------------------------------
# run — subprocess encoding contract
# ---------------------------------------------------------------------------
class TestRun:
    def test_uses_utf8_encoding(self, tmp_path):
        """The whole point of this helper (vs stdlib subprocess.run) is the
        `encoding="utf-8", errors="replace"` contract. Guard against regression."""
        with patch("check_pr_scope_drift.subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""}
            )()
            cpsd.run(["echo", "hi"], cwd=tmp_path)
            _, kwargs = mock_run.call_args
            assert kwargs["encoding"] == "utf-8"
            assert kwargs["errors"] == "replace"
            assert kwargs["capture_output"] is True


# ---------------------------------------------------------------------------
# check_tool_map
# ---------------------------------------------------------------------------
class TestCheckToolMap:
    def test_pass_when_no_outdated_keyword(self, tmp_path):
        with patch("check_pr_scope_drift.run",
                   return_value=(0, "tool-map is up to date", "")):
            ok, msg = cpsd.check_tool_map(tmp_path)
            assert ok is True
            assert "PASS" in msg

    def test_fail_when_outdated_in_stdout(self, tmp_path):
        with patch("check_pr_scope_drift.run",
                   return_value=(0, "tool-map outdated: 3 entries", "")):
            ok, msg = cpsd.check_tool_map(tmp_path)
            assert ok is False
            assert "drift" in msg.lower()

    def test_fail_when_generator_exit_nonzero(self, tmp_path):
        with patch("check_pr_scope_drift.run",
                   return_value=(1, "", "some error")):
            ok, _ = cpsd.check_tool_map(tmp_path)
            assert ok is False


# ---------------------------------------------------------------------------
# check_working_tree_clean
# ---------------------------------------------------------------------------
class TestWorkingTreeClean:
    def test_clean_both_passes(self, tmp_path):
        def fake_run(cmd, cwd):
            return (0, "", "")
        with patch("check_pr_scope_drift.run", side_effect=fake_run):
            ok, msg = cpsd.check_working_tree_clean(tmp_path)
            assert ok is True
            assert "clean" in msg

    def test_unstaged_changes_fails(self, tmp_path):
        calls = {"i": 0}
        def fake_run(cmd, cwd):
            calls["i"] += 1
            if cmd[:2] == ["git", "diff"] and "--cached" not in cmd and "--quiet" in cmd:
                return (1, "", "")  # unstaged dirty
            if cmd[:3] == ["git", "diff", "--cached"]:
                return (0, "", "")
            if cmd[:2] == ["git", "status"]:
                return (0, " M file.txt\n", "")
            return (0, "", "")
        with patch("check_pr_scope_drift.run", side_effect=fake_run):
            ok, msg = cpsd.check_working_tree_clean(tmp_path)
            assert ok is False
            assert "uncommitted" in msg

    def test_staged_changes_fails(self, tmp_path):
        def fake_run(cmd, cwd):
            if cmd[:2] == ["git", "diff"] and "--cached" not in cmd:
                return (0, "", "")
            if cmd[:3] == ["git", "diff", "--cached"]:
                return (1, "", "")  # staged dirty
            if cmd[:2] == ["git", "status"]:
                return (0, "M  staged.txt\n", "")
            return (0, "", "")
        with patch("check_pr_scope_drift.run", side_effect=fake_run):
            ok, _ = cpsd.check_working_tree_clean(tmp_path)
            assert ok is False

    def test_untracked_ignored(self, tmp_path):
        """`?? untracked_file` lines should be filtered out of the preview, and
        if diff --quiet returns 0, we're clean regardless of untracked files."""
        def fake_run(cmd, cwd):
            if cmd[:2] == ["git", "diff"]:
                return (0, "", "")
            return (0, "?? scratch.txt\n", "")
        with patch("check_pr_scope_drift.run", side_effect=fake_run):
            ok, _ = cpsd.check_working_tree_clean(tmp_path)
            assert ok is True


# ---------------------------------------------------------------------------
# main — end-to-end exit code
# ---------------------------------------------------------------------------
class TestMain:
    def test_all_pass_returns_zero(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["check_pr_scope_drift.py"])
        with patch("check_pr_scope_drift.check_tool_map",
                   return_value=(True, "tool-map: PASS")), \
             patch("check_pr_scope_drift.check_working_tree_clean",
                   return_value=(True, "clean")):
            rc = cpsd.main()
            assert rc == 0

    def test_tool_map_fail_returns_one(self, capsys, monkeypatch):
        monkeypatch.setattr("sys.argv", ["check_pr_scope_drift.py"])
        with patch("check_pr_scope_drift.check_tool_map",
                   return_value=(False, "tool-map drift")), \
             patch("check_pr_scope_drift.check_working_tree_clean",
                   return_value=(True, "clean")):
            rc = cpsd.main()
            assert rc == 1
            err = capsys.readouterr().err
            assert "FAIL" in err
