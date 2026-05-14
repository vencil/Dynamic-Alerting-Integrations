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

    def test_check_commit_msg_path_short_circuits(self, monkeypatch, tmp_path, capsys, cli_argv):
        # Provide a valid commit-msg file → check_commit_msg_file returns 0.
        f = tmp_path / "msg"
        f.write_text("feat: ok\n", encoding="utf-8")
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        # Stub commitlint enum readers so check_commit_msg_file doesn't go
        # through the full validation (we just want orchestrator dispatch).
        monkeypatch.setattr(pp, "check_commit_msg_file",
                            lambda path, repo_root: 0)
        cli_argv("pr_preflight.py", "--check-commit-msg", str(f))
        assert pp.main() == 0

    def test_check_pr_title_path_short_circuits(self, monkeypatch, tmp_path, cli_argv):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        monkeypatch.setattr(pp, "check_pr_title",
                            lambda title, repo_root, max_length=70: 0)
        cli_argv("pr_preflight.py", "--check-pr-title", "feat: x")
        assert pp.main() == 0

    def test_all_pass_returns_zero(self, monkeypatch, tmp_path, cli_argv):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch)
        cli_argv("pr_preflight.py", "--ci")
        assert pp.main() == 0

    def test_failure_with_ci_flag_returns_one(self, monkeypatch, tmp_path, cli_argv):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch, fail_check="Conflict")
        cli_argv("pr_preflight.py", "--ci")
        assert pp.main() == 1

    def test_failure_without_ci_flag_returns_zero(self, monkeypatch, tmp_path, cli_argv):
        # Without --ci, even a FAIL exits 0 (interactive mode shows summary
        # but doesn't gate).
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch, fail_check="Conflict")
        cli_argv("pr_preflight.py")
        assert pp.main() == 0

    def test_skip_hooks_records_skip_status(self, monkeypatch, tmp_path, capsys, cli_argv):
        self._stub_repo_root_and_marker(monkeypatch, tmp_path)
        self._stub_all_checks(monkeypatch)

        # Sentinel: if check_local_hooks IS called, fail loudly.
        def fail_if_called():
            raise AssertionError("check_local_hooks should be skipped")
        monkeypatch.setattr(pp, "check_local_hooks", fail_if_called)

        cli_argv("pr_preflight.py", "--skip-hooks", "--ci")
        assert pp.main() == 0
        out = capsys.readouterr().out
        assert "Local hooks" in out
        assert "已跳過" in out  # SKIP message


# ---------------------------------------------------------------------------
# check_pass2_trailer_strict — issue #454
# ---------------------------------------------------------------------------
# This is the CI strict-mode gate for Self-Review-Pass-2: trailers. Tests
# stub subprocess.run so no real git state is touched. We rely on git's
# native --format=%(trailers:key=...) parser, so we don't need to test
# case-insensitivity / multi-line folding ourselves — those are git's
# responsibility, and re-asserting them here would only verify our mock.
class TestCheckPass2TrailerStrict:
    @staticmethod
    def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
        return _subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr,
        )

    def _stub_subprocess(self, monkeypatch, count_result, log_result=None):
        """Stub pr_preflight.run() (the helper that wraps subprocess.run with
        uniform timeout / FileNotFoundError handling) with a 2-call sequence.

        First call = `git rev-list --count`; second = `git log --format=...`.
        Pass log_result=None to verify the second call wasn't reached
        (used for the empty-range SKIP path).
        """
        calls = {"i": 0}
        results = [count_result] + ([log_result] if log_result is not None else [])

        def fake_run(cmd, *args, **kwargs):
            i = calls["i"]
            calls["i"] += 1
            if i >= len(results):
                raise AssertionError(
                    f"Unexpected pp.run call #{i + 1}: cmd={cmd}"
                )
            return results[i]

        monkeypatch.setattr(pp, "run", fake_run)
        return calls

    def test_trailer_present_returns_zero(self, monkeypatch, tmp_path):
        """At least one commit in range carries the trailer → exit 0."""
        self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(0, "3\n"),
            log_result=self._cp(0, "dogfood mutated foo(), test_bar caught (✓)\n"),
        )
        assert pp.check_pass2_trailer_strict(tmp_path) == 0

    def test_no_trailer_in_any_commit_fails_with_amend_hint(self, monkeypatch, tmp_path, capsys):
        """Range non-empty but trailer absent → exit 1 + helpful stderr."""
        self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(0, "2\n"),
            log_result=self._cp(0, ""),  # empty stdout = no trailers found
        )
        rc = pp.check_pass2_trailer_strict(tmp_path)
        assert rc == 1
        err = capsys.readouterr().err
        assert "Self-Review-Pass-2" in err
        assert "git commit --amend" in err  # actionable recovery hint
        assert "force-with-lease" in err  # safe push variant

    def test_empty_range_skips_with_zero(self, monkeypatch, tmp_path, capsys):
        """No commits in <base>..HEAD → SKIP (exit 0), don't false-fail.

        Catches the case where HEAD is on base or behind it — should be
        caught by check_branch_identity / check_behind_main, not this gate.
        """
        calls = self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(0, "0\n"),
            log_result=None,  # log call must NOT happen
        )
        assert pp.check_pass2_trailer_strict(tmp_path) == 0
        assert calls["i"] == 1, "git log should not be invoked when range is empty"
        out = capsys.readouterr().out
        assert "skipping" in out.lower()

    def test_rev_list_failure_returns_one_with_checkout_hint(self, monkeypatch, tmp_path, capsys):
        """Common CI failure: `actions/checkout@v4` shallow clone makes
        origin/main unresolvable. Error message must point at fetch-depth: 0.
        """
        self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(
                128, "",
                "fatal: ambiguous argument 'origin/main..HEAD': "
                "unknown revision or path not in the working tree.",
            ),
            log_result=None,  # log call must NOT happen if rev-list failed
        )
        rc = pp.check_pass2_trailer_strict(tmp_path)
        assert rc == 1
        err = capsys.readouterr().err
        assert "fetch-depth: 0" in err  # actionable CI guidance
        assert "actions/checkout@v4" in err

    def test_log_failure_returns_one(self, monkeypatch, tmp_path, capsys):
        """Less common: rev-list succeeds but log fails (e.g. corrupt repo).
        Still surface the underlying git error."""
        self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(0, "1\n"),
            log_result=self._cp(128, "", "fatal: bad revision"),
        )
        rc = pp.check_pass2_trailer_strict(tmp_path)
        assert rc == 1
        err = capsys.readouterr().err
        assert "git log" in err
        assert "bad revision" in err

    def test_custom_base_ref_threaded_through(self, monkeypatch, tmp_path):
        """The base_ref kwarg must reach the git commands (so the workflow
        can pass origin/<base_branch> from pull_request.base.ref)."""
        captured_cmds = []

        def fake_run(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            return self._cp(0, "1\n" if len(captured_cmds) == 1 else "trailer\n")

        monkeypatch.setattr(pp, "run", fake_run)
        pp.check_pass2_trailer_strict(tmp_path, base_ref="origin/develop")
        # Both git invocations should embed origin/develop, not origin/main.
        assert any("origin/develop..HEAD" in arg for arg in captured_cmds[0])
        assert any("origin/develop..HEAD" in arg for arg in captured_cmds[1])

    def test_git_not_on_path_surfaces_uniform_error(self, monkeypatch, tmp_path, capsys):
        """`pp.run()` collapses FileNotFoundError into rc=127 synthetic
        CompletedProcess. Verify the check surfaces this without crashing."""
        # rc=127 = "command not found" (POSIX convention pp.run encodes).
        self._stub_subprocess(
            monkeypatch,
            count_result=self._cp(127, "", "command not found: git"),
            log_result=None,
        )
        rc = pp.check_pass2_trailer_strict(tmp_path)
        assert rc == 1
        err = capsys.readouterr().err
        assert "git rev-list" in err
        assert "command not found" in err

    def test_cli_flag_dispatches_to_function(self, monkeypatch, tmp_path, cli_argv):
        """`--check-pass2-trailer-strict` short-circuits main() like the
        sibling --check-commit-msg / --check-pr-title fast paths."""
        monkeypatch.setattr(pp, "find_repo_root", lambda: tmp_path)
        monkeypatch.setattr(os, "chdir", lambda p: None)
        called = {"n": 0, "base_ref": None}

        def fake_check(repo_root, base_ref="origin/main"):
            called["n"] += 1
            called["base_ref"] = base_ref
            return 0

        monkeypatch.setattr(pp, "check_pass2_trailer_strict", fake_check)
        cli_argv("pr_preflight.py", "--check-pass2-trailer-strict",
                 "--base-ref", "origin/release-2.9")
        assert pp.main() == 0
        assert called["n"] == 1
        assert called["base_ref"] == "origin/release-2.9"
