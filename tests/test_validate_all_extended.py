"""Extended tests for validate_all.py — coverage boost.

Targets: _smart_detect, main() with --parallel, --baseline, --compare,
--fix, --profile, --notify, --smart, --diff-report flags.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import validate_all as va
from validate_all import (
    _smart_detect,
    TOOLS,
    FIX_COMMANDS,
)


# ============================================================
# _smart_detect
# ============================================================
class TestSmartDetect:
    """_smart_detect() git-diff based check selection."""

    def _mock_git(self, monkeypatch, diff_files="", staged_files="",
                  untracked_files="", fail=False):
        """Mock subprocess.run for git commands."""
        def mock_run(cmd, **kwargs):
            result = MagicMock()
            if fail:
                raise subprocess.TimeoutExpired(cmd, 30)
            result.returncode = 0
            if "diff" in cmd and "--cached" in cmd:
                result.stdout = staged_files
            elif "diff" in cmd:
                result.stdout = diff_files
            elif "ls-files" in cmd:
                result.stdout = untracked_files
            else:
                result.stdout = ""
            return result

        monkeypatch.setattr(subprocess, "run", mock_run)

    def test_no_changes_returns_empty(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch)
        result = _smart_detect(tmp_path)
        assert result == []

    def test_docs_change_triggers_doc_checks(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="docs/guide.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result
        assert "versions" in result

    def test_rule_packs_change(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="rule-packs/mariadb.yaml\n")
        result = _smart_detect(tmp_path)
        assert "alerts" in result
        assert "platform_data" in result

    def test_unknown_file_runs_all(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, diff_files="unknown_file.txt\n")
        result = _smart_detect(tmp_path)
        all_names = sorted(n for n, _, _, _ in TOOLS)
        assert result == all_names

    def test_timeout_returns_none(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, fail=True)
        result = _smart_detect(tmp_path)
        assert result is None

    def test_staged_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, staged_files="scripts/tools/ops/new.py\n")
        result = _smart_detect(tmp_path)
        assert "tool_map" in result

    def test_untracked_files_detected(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch, untracked_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "changelog" in result

    def test_result_is_sorted(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/a.md\nrule-packs/b.yaml\n")
        result = _smart_detect(tmp_path)
        assert result == sorted(result)

    def test_combined_changes(self, monkeypatch, tmp_path):
        self._mock_git(monkeypatch,
                       diff_files="docs/guide.md\n",
                       staged_files="CHANGELOG.md\n")
        result = _smart_detect(tmp_path)
        assert "links" in result
        assert "changelog" in result


# ============================================================
# main() extended CLI modes
# ============================================================
class TestMainExtended:
    """Extended main() CLI mode tests for coverage boost."""

    def _mock_run_one_pass(self, short_name, script_path, tool_args,
                           project_root):
        return (short_name, "pass", 0.1, "ok", "output text")

    def _mock_run_one_fail(self, short_name, script_path, tool_args,
                           project_root):
        return (short_name, "fail", 0.2, "error detail", "failure output")

    def test_parallel_json(self, monkeypatch, capsys):
        """--parallel --json mode."""
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--parallel", "--json", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["mode"] == "parallel"

    def test_parallel_text(self, monkeypatch, capsys):
        """--parallel text mode."""
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--parallel", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "PARALLEL" in out

    def test_parallel_verbose(self, monkeypatch, capsys):
        """--parallel --verbose shows full output."""
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--parallel", "--verbose", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "output text" in out or "VERSIONS" in out

    def test_baseline_mode(self, monkeypatch, capsys, tmp_path):
        """--baseline saves JSON baseline file."""
        bf = tmp_path / ".validation-baseline.json"
        monkeypatch.setattr(va, "BASELINE_FILE", bf)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--baseline", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert bf.exists()
        data = json.loads(bf.read_text(encoding="utf-8"))
        assert "passed" in data

    def test_compare_mode(self, monkeypatch, capsys, tmp_path):
        """--compare against baseline."""
        bf = tmp_path / ".validation-baseline.json"
        baseline = {
            "results": {"versions": {"status": "pass", "elapsed": 1.0}},
            "passed": 1, "failed": 0,
        }
        bf.write_text(json.dumps(baseline), encoding="utf-8")
        monkeypatch.setattr(va, "BASELINE_FILE", bf)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--compare", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0

    def test_profile_mode(self, monkeypatch, capsys, tmp_path):
        """--profile appends timing to CSV."""
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--profile", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert csv_file.exists()
        content = csv_file.read_text(encoding="utf-8")
        assert "timestamp" in content
        assert "versions" in content

    def test_profile_appends(self, monkeypatch, capsys, tmp_path):
        """--profile appends (not overwrites) on second run."""
        csv_file = tmp_path / ".validation-profile.csv"
        monkeypatch.setattr(va, "PROFILE_CSV", csv_file)

        for _ in range(2):
            monkeypatch.setattr(sys, "argv", [
                "validate_all", "--profile", "--only", "versions",
            ])
            monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
            with pytest.raises(SystemExit):
                va.main()

        lines = csv_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows

    def test_notify_pass(self, monkeypatch, capsys):
        """--notify on successful run."""
        calls = []
        monkeypatch.setattr(va, "_send_notification",
                            lambda t, m: calls.append((t, m)))
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--notify", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        assert len(calls) == 1
        assert "Passed" in calls[0][0]

    def test_notify_fail(self, monkeypatch, capsys):
        """--notify on failed run."""
        calls = []
        monkeypatch.setattr(va, "_send_notification",
                            lambda t, m: calls.append((t, m)))
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--notify", "--only", "versions",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_fail)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        assert len(calls) == 1
        assert "Failed" in calls[0][0]

    def test_fix_mode(self, monkeypatch, capsys):
        """--fix auto-fixes failed checks."""
        fix_calls = []

        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        def mock_subprocess_run(cmd, **kwargs):
            fix_calls.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = "Fixed something"
            return result

        monkeypatch.setattr(va, "_run_one", mock_run_one)
        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

        # Find a tool that's in FIX_COMMANDS
        fix_name = next(iter(FIX_COMMANDS.keys()))
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--fix", "--only", fix_name,
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "Auto-fixing" in out

    def test_fix_no_auto_fix_available(self, monkeypatch, capsys):
        """--fix with a tool that has no auto-fix."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        monkeypatch.setattr(va, "_run_one", mock_run_one)

        # Find a tool NOT in FIX_COMMANDS
        tool_names = {t[0] for t in TOOLS}
        no_fix = next(n for n in tool_names if n not in FIX_COMMANDS)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--fix", "--only", no_fix,
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "no auto-fix" in out

    def test_smart_mode(self, monkeypatch, capsys):
        """--smart mode derives checks from git diff."""
        def mock_smart(project_root):
            return ["versions"]
        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--smart",
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Smart mode" in out

    def test_smart_mode_none(self, monkeypatch, capsys):
        """--smart with None (git unavailable) runs all."""
        def mock_smart(project_root):
            return None
        monkeypatch.setattr(va, "_smart_detect", mock_smart)
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--smart", "--only", "versions",
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0

    def test_diff_report_mode(self, monkeypatch, capsys):
        """--diff-report shows diff output."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        monkeypatch.setattr(va, "_run_one", mock_run_one)

        def mock_gen_diff(failed_checks, tools_dir, project_root):
            return "=== DIFF REPORT ===\nversions: diff output"

        monkeypatch.setattr(va, "_generate_diff_report", mock_gen_diff)

        fix_name = next(iter(FIX_COMMANDS.keys()))
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--diff-report", "--only", fix_name,
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "DIFF REPORT" in out

    def test_all_skipped_text_output(self, monkeypatch, capsys):
        """All tools skipped shows appropriate message."""
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--only", "nonexistent_check",
        ])
        monkeypatch.setattr(va, "_run_one", self._mock_run_one_pass)
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "skipped" in out.lower() or "0" in out

    def test_fix_error_handling(self, monkeypatch, capsys):
        """--fix handles fix command errors."""
        def mock_run_one(short_name, script_path, tool_args, project_root):
            return (short_name, "fail", 0.1, "error", "output")

        def mock_subprocess_run(cmd, **kwargs):
            raise OSError("Command not found")

        monkeypatch.setattr(va, "_run_one", mock_run_one)
        monkeypatch.setattr(subprocess, "run", mock_subprocess_run)

        fix_name = next(iter(FIX_COMMANDS.keys()))
        monkeypatch.setattr(sys, "argv", [
            "validate_all", "--fix", "--only", fix_name,
        ])
        with pytest.raises(SystemExit) as exc:
            va.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "fix error" in out or "Auto-fixing" in out
