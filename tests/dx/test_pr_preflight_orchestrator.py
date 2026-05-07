"""Tests for pr_preflight.py orchestrator + uncovered helpers.

Audit flagged the 7-check orchestration + exit-code aggregation as
untested (38% coverage; sub-checks marker/msg-validator/pass-gate are
covered separately by their own test files but the orchestrator itself
isn't). This file fills the orchestrator-shaped gap.

Covers:
  - PreflightReport: has_failure / has_warning aggregation, print_summary
  - validate_conventional_header: type/scope enum, length, format
  - validate_commit_msg_body: post-header line-length + blank-line rule
  - detect_commit_msg_bom: UTF-8 / UTF-16 LE/BE BOM detection + clean files
  - _classify_ci_failures: A/B classification with mocked gh
  - main() exit codes: --check-commit-msg / --check-pr-title fast paths,
    --ci with passing/failing checks, --skip-hooks SKIPs the hooks check
"""
from __future__ import annotations

import os
import sys
import subprocess as _subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import pr_preflight as pp  # noqa: E402


# ---------------------------------------------------------------------------
# PreflightReport — aggregation + summary
# ---------------------------------------------------------------------------
class TestPreflightReport:
    def test_empty_report_no_failure_no_warning(self):
        r = pp.PreflightReport()
        assert r.has_failure is False
        assert r.has_warning is False

    def test_pass_only_no_failure_no_warning(self):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.PASS, "ok"))
        r.add(pp.CheckResult("c2", pp.Status.PASS, "ok"))
        assert r.has_failure is False
        assert r.has_warning is False

    def test_warning_only_marks_warning_not_failure(self):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.PASS, "ok"))
        r.add(pp.CheckResult("c2", pp.Status.WARN, "caution"))
        assert r.has_failure is False
        assert r.has_warning is True

    def test_failure_marks_failure(self):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.PASS, "ok"))
        r.add(pp.CheckResult("c2", pp.Status.FAIL, "broken"))
        assert r.has_failure is True

    def test_skip_neither_failure_nor_warning(self):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.SKIP, "skipped"))
        assert r.has_failure is False
        assert r.has_warning is False

    def test_print_summary_blocked_message(self, capsys):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.FAIL, "bad"))
        r.print_summary()
        out = capsys.readouterr().out
        assert "BLOCKED" in out
        assert "c1" in out
        assert "bad" in out

    def test_print_summary_caution_on_warning_only(self, capsys):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.WARN, "warn"))
        r.print_summary()
        out = capsys.readouterr().out
        assert "CAUTION" in out

    def test_print_summary_ready_on_clean(self, capsys):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.PASS, "ok"))
        r.print_summary()
        out = capsys.readouterr().out
        assert "READY" in out

    def test_print_summary_includes_detail_lines(self, capsys):
        r = pp.PreflightReport()
        r.add(pp.CheckResult("c1", pp.Status.FAIL, "msg", detail="line-a\nline-b"))
        r.print_summary()
        out = capsys.readouterr().out
        assert "line-a" in out
        assert "line-b" in out


# ---------------------------------------------------------------------------
# validate_conventional_header — pure
# ---------------------------------------------------------------------------
class TestValidateConventionalHeader:
    def test_minimal_valid_header(self):
        assert pp.validate_conventional_header("feat: add thing") == []

    def test_with_scope(self):
        assert pp.validate_conventional_header("fix(ci): typo") == []

    def test_empty_header_errors(self):
        errs = pp.validate_conventional_header("")
        assert any("empty" in e for e in errs)

    def test_too_long_errors(self):
        long = "feat: " + ("x" * 200)
        errs = pp.validate_conventional_header(long, max_length=100)
        assert any("too long" in e for e in errs)

    def test_no_colon_format_error(self):
        errs = pp.validate_conventional_header("just a sentence")
        assert any("conventional-commits" in e for e in errs)

    def test_type_enum_violation(self):
        errs = pp.validate_conventional_header(
            "wat: stuff", type_enum=["feat", "fix"]
        )
        assert any("not in allowed enum" in e and "wat" in e for e in errs)

    def test_scope_enum_violation(self):
        errs = pp.validate_conventional_header(
            "fix(rogue): stuff", scope_enum=["ci", "docs"]
        )
        assert any("scope 'rogue' not in allowed enum" in e for e in errs)

    def test_empty_subject_errors(self):
        errs = pp.validate_conventional_header("feat:    ")
        assert any("subject is empty" in e for e in errs)


# ---------------------------------------------------------------------------
# validate_commit_msg_body — pure
# ---------------------------------------------------------------------------
class TestValidateCommitMsgBody:
    def test_empty_input_no_errors(self):
        assert pp.validate_commit_msg_body([]) == []

    def test_only_comments_no_header_no_errors(self):
        assert pp.validate_commit_msg_body(["# comment", "# more"]) == []

    def test_header_only_no_errors(self):
        assert pp.validate_commit_msg_body(["feat: add thing"]) == []

    def test_short_body_with_blank_line_passes(self):
        lines = ["feat: x", "", "Body line."]
        assert pp.validate_commit_msg_body(lines) == []

    def test_body_too_long_errors(self):
        long = "x" * 200
        lines = ["feat: x", "", long]
        errs = pp.validate_commit_msg_body(lines, max_line_length=100)
        assert any("too long" in e for e in errs)

    def test_missing_blank_line_after_header_warns(self):
        # Body line directly after header without blank separator.
        lines = ["feat: x", "Body line."]
        errs = pp.validate_commit_msg_body(lines)
        assert any("body-leading-blank" in e for e in errs)


# ---------------------------------------------------------------------------
# detect_commit_msg_bom — pure (file-bound)
# ---------------------------------------------------------------------------
class TestDetectCommitMsgBom:
    def test_clean_file_returns_none(self, tmp_path):
        f = tmp_path / "msg"
        f.write_bytes(b"feat: clean\n\nbody\n")
        assert pp.detect_commit_msg_bom(f) is None

    def test_utf8_bom_detected(self, tmp_path):
        f = tmp_path / "msg"
        f.write_bytes(b"\xef\xbb\xbffeat: bom\n")
        result = pp.detect_commit_msg_bom(f)
        assert result is not None
        assert "UTF-8 BOM" in result

    def test_utf16_le_bom_detected(self, tmp_path):
        f = tmp_path / "msg"
        f.write_bytes(b"\xff\xfef\x00e\x00")
        result = pp.detect_commit_msg_bom(f)
        assert result is not None
        assert "UTF-16 LE" in result

    def test_utf16_be_bom_detected(self, tmp_path):
        f = tmp_path / "msg"
        f.write_bytes(b"\xfe\xff\x00f\x00e")
        result = pp.detect_commit_msg_bom(f)
        assert result is not None
        assert "UTF-16 BE" in result

    def test_missing_file_returns_none(self, tmp_path):
        ghost = tmp_path / "no-such-file"
        # OSError caught → None.
        assert pp.detect_commit_msg_bom(ghost) is None

    def test_empty_file_returns_none(self, tmp_path):
        f = tmp_path / "msg"
        f.write_bytes(b"")
        assert pp.detect_commit_msg_bom(f) is None


# ---------------------------------------------------------------------------
# _classify_ci_failures — A/B classifier with mocked gh
# ---------------------------------------------------------------------------
class TestClassifyCIFailures:
    def _stub_run(self, monkeypatch, sequence):
        """Replace pr_preflight.run with a function that yields stubbed
        CompletedProcess objects in order."""
        calls = iter(sequence)

        def fake_run(*args, **kwargs):
            try:
                return next(calls)
            except StopIteration:
                raise AssertionError("more `run` calls than mocked")

        monkeypatch.setattr(pp, "run", fake_run)

    def test_gh_unavailable_returns_empty(self, monkeypatch):
        self._stub_run(monkeypatch, [
            _subprocess.CompletedProcess([], 1, stdout="", stderr="not found"),
        ])
        assert pp._classify_ci_failures([{"name": "x"}]) == ""

    def test_main_success_means_pr_introduced(self, monkeypatch):
        import json as _json
        self._stub_run(monkeypatch, [
            _subprocess.CompletedProcess(
                [], 0,
                stdout=_json.dumps([{"conclusion": "success", "headBranch": "main", "databaseId": 99}]),
                stderr="",
            ),
        ])
        result = pp._classify_ci_failures([{"name": "Lint"}])
        assert "main CI 目前是" in result
        assert "本 PR 引入的" in result

    def test_main_failure_with_overlap_classifies_shared_and_only_pr(self, monkeypatch):
        import json as _json
        self._stub_run(monkeypatch, [
            _subprocess.CompletedProcess(
                [], 0,
                stdout=_json.dumps([{"conclusion": "failure", "headBranch": "main", "databaseId": 42}]),
                stderr="",
            ),
            _subprocess.CompletedProcess(
                [], 0,
                stdout=_json.dumps({"jobs": [
                    {"name": "Lint", "conclusion": "failure"},
                    {"name": "Tests", "conclusion": "success"},
                ]}),
                stderr="",
            ),
        ])
        result = pp._classify_ci_failures([
            {"name": "Lint"},     # shared with main
            {"name": "Smoke"},    # only PR
        ])
        assert "pre-existing" in result
        assert "Lint" in result
        assert "本 PR 引入" in result
        assert "Smoke" in result

    def test_invalid_json_returns_empty(self, monkeypatch):
        self._stub_run(monkeypatch, [
            _subprocess.CompletedProcess([], 0, stdout="{not json", stderr=""),
        ])
        assert pp._classify_ci_failures([]) == ""


# ---------------------------------------------------------------------------
# main() — orchestrator entry points
# ---------------------------------------------------------------------------
class TestMainOrchestrator:
    def _stub_all_checks(self, monkeypatch, fail_check=None, warn_check=None):
        """Replace every check_* in pr_preflight with a stub.

        `fail_check` (str): name of the check whose stub returns FAIL.
        `warn_check` (str): name of the check whose stub returns WARN.
        Everything else returns PASS.
        """
        check_specs = [
            ("check_branch_identity", "Branch identity"),
            ("check_behind_main", "Behind main"),
            ("check_conflict", "Conflict"),
            ("check_local_hooks", "Local hooks"),
            ("check_scope_drift", "Scope drift"),
            ("check_ci_status", "CI status"),
            ("check_pr_mergeable", "PR mergeable"),
        ]
        for fn_name, label in check_specs:
            if label == fail_check:
                status = pp.Status.FAIL
                msg = "stubbed-fail"
            elif label == warn_check:
                status = pp.Status.WARN
                msg = "stubbed-warn"
            else:
                status = pp.Status.PASS
                msg = "stubbed-pass"
            # check_ci_status / check_pr_mergeable take args; wrap accordingly.
            if fn_name in {"check_ci_status", "check_pr_mergeable"}:
                monkeypatch.setattr(
                    pp, fn_name,
                    lambda *a, _label=label, _status=status, _msg=msg, **kw:
                        pp.CheckResult(_label, _status, _msg),
                )
            else:
                monkeypatch.setattr(
                    pp, fn_name,
                    lambda _label=label, _status=status, _msg=msg:
                        pp.CheckResult(_label, _status, _msg),
                )

    def _stub_repo_root_and_marker(self, monkeypatch, tmp_path):
        """Avoid touching real git state: stub repo-root + marker writers."""
        monkeypatch.setattr(pp, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(pp, "write_marker", lambda repo_root: None)
        monkeypatch.setattr(pp, "clear_markers", lambda repo_root: 0)
        monkeypatch.setattr(os, "chdir", lambda p: None)

    def test_check_commit_msg_path_short_circuits(self, monkeypatch, tmp_path, capsys):
        # Provide a valid commit-msg file → check_commit_msg_file returns 0.
        f = tmp_path / "msg"
        f.write_text("feat: ok\n", encoding="utf-8")
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        # Stub commitlint enum readers so check_commit_msg_file doesn't go
        # through the full validation (we just want orchestrator dispatch).
        monkeypatch.setattr(pp, "check_commit_msg_file",
                            lambda path, repo_root: 0)
        monkeypatch.setattr(sys, "argv",
                            ["pr_preflight.py", "--check-commit-msg", str(f)])
        assert pp.main() == 0

    def test_check_pr_title_path_short_circuits(self, monkeypatch, tmp_path):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        monkeypatch.setattr(pp, "check_pr_title",
                            lambda title, repo_root, max_length=70: 0)
        monkeypatch.setattr(sys, "argv",
                            ["pr_preflight.py", "--check-pr-title", "feat: x"])
        assert pp.main() == 0

    def test_all_pass_returns_zero(self, monkeypatch, tmp_path):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["pr_preflight.py", "--ci"])
        assert pp.main() == 0

    def test_failure_with_ci_flag_returns_one(self, monkeypatch, tmp_path):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch, fail_check="Conflict")
        monkeypatch.setattr(sys, "argv", ["pr_preflight.py", "--ci"])
        assert pp.main() == 1

    def test_failure_without_ci_flag_returns_zero(self, monkeypatch, tmp_path):
        # Without --ci, even a FAIL exits 0 (interactive mode shows summary
        # but doesn't gate).
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch, fail_check="Conflict")
        monkeypatch.setattr(sys, "argv", ["pr_preflight.py"])
        assert pp.main() == 0

    def test_skip_hooks_records_skip_status(self, monkeypatch, tmp_path, capsys):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch)

        # Sentinel: if check_local_hooks IS called, fail loudly.
        def fail_if_called():
            raise AssertionError("check_local_hooks should be skipped")
        monkeypatch.setattr(pp, "check_local_hooks", fail_if_called)

        monkeypatch.setattr(sys, "argv", ["pr_preflight.py", "--skip-hooks", "--ci"])
        assert pp.main() == 0
        out = capsys.readouterr().out
        assert "Local hooks" in out
        assert "已跳過" in out  # SKIP message
