"""Tests for the 7 individual check_* functions in pr_preflight.py.

Follow-up to test_pr_preflight_orchestrator.py (which covered the
PreflightReport aggregation + main() orchestration). This file fills
the remaining coverage gap by exercising each individual check that
shells out to git / gh / pre-commit / python — all subprocess calls
are monkeypatched so no real external tools are invoked.

Covered:
  - check_branch_identity — feature / main / master / HEAD / unknown
  - check_behind_main     — synced / 1-5 / >5 / git failure
  - check_conflict        — synced / merge-tree pass+fail / fallback
                            merge --no-commit / FUSE-lock detection
  - check_local_hooks     — pass / failed hooks / subprocess error
  - check_scope_drift     — pass / fail with parsed FAIL line / no-output
  - check_ci_status       — SKIP / WARN / PASS / pending / fail+A/B
  - check_pr_mergeable    — SKIP / CONFLICTING / BLOCKED+review /
                            MERGEABLE+CLEAN
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import pr_preflight as pp  # noqa: E402


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a subprocess.CompletedProcess stub matching what pp.run returns."""
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _stub_run_sequence(monkeypatch, responses):
    """Replace pp.run with an iterator yielding stubbed CompletedProcess in order.

    Useful when the function under test makes multiple `run` calls.
    """
    it = iter(responses)

    def fake_run(*args, **kwargs):
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError(
                f"check made more `run` calls than mocked: cmd={args[0] if args else '?'}"
            ) from exc

    monkeypatch.setattr(pp, "run", fake_run)


def _stub_run_constant(monkeypatch, response):
    """Replace pp.run with a function that always returns the same response."""
    monkeypatch.setattr(pp, "run", lambda *a, **kw: response)


# ---------------------------------------------------------------------------
# check_branch_identity
# ---------------------------------------------------------------------------
class TestCheckBranchIdentity:
    def test_feature_branch_passes(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "feat/x\n"))
        result = pp.check_branch_identity()
        assert result.status == pp.Status.PASS
        assert "feat/x" in result.message

    def test_main_branch_fails(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "main\n"))
        result = pp.check_branch_identity()
        assert result.status == pp.Status.FAIL
        assert "main" in result.message

    def test_master_branch_fails(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "master\n"))
        result = pp.check_branch_identity()
        assert result.status == pp.Status.FAIL

    def test_detached_head_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "HEAD\n"))
        result = pp.check_branch_identity()
        assert result.status == pp.Status.WARN
        assert "Detached" in result.message

    def test_git_failure_treats_branch_as_unknown(self, monkeypatch):
        # rev-parse fails → branch becomes "unknown" → PASS (not main/master)
        _stub_run_constant(monkeypatch, _cp(128, "", "fatal: not a git repo"))
        result = pp.check_branch_identity()
        assert result.status == pp.Status.PASS
        assert "unknown" in result.message


# ---------------------------------------------------------------------------
# check_behind_main
# ---------------------------------------------------------------------------
class TestCheckBehindMain:
    def test_synced_passes(self, monkeypatch):
        _stub_run_sequence(monkeypatch, [
            _cp(0),                # git fetch (return value ignored)
            _cp(0, "0\n"),         # rev-list count = 0
        ])
        result = pp.check_behind_main()
        assert result.status == pp.Status.PASS
        assert "0 commits behind" in result.message

    def test_1_to_5_warns_with_recommendation(self, monkeypatch):
        _stub_run_sequence(monkeypatch, [_cp(0), _cp(0, "3\n")])
        result = pp.check_behind_main()
        assert result.status == pp.Status.WARN
        assert "落後 3" in result.message
        # The "<= 5" branch uses softer language
        assert "建議" in result.message

    def test_more_than_5_uses_stronger_language(self, monkeypatch):
        _stub_run_sequence(monkeypatch, [_cp(0), _cp(0, "10\n")])
        result = pp.check_behind_main()
        assert result.status == pp.Status.WARN
        assert "落後 10" in result.message
        assert "強烈" in result.message

    def test_rev_list_failure_warns(self, monkeypatch):
        _stub_run_sequence(monkeypatch, [
            _cp(0),                # fetch
            _cp(128, "", "no such ref"),   # rev-list fails
        ])
        result = pp.check_behind_main()
        assert result.status == pp.Status.WARN
        assert "無法計算" in result.message


# ---------------------------------------------------------------------------
# check_conflict
# ---------------------------------------------------------------------------
class TestCheckConflict:
    def test_synced_short_circuits_to_pass(self, monkeypatch):
        # First rev-list returns "0" → fast path, no merge-tree call.
        _stub_run_sequence(monkeypatch, [_cp(0, "0\n")])
        result = pp.check_conflict()
        assert result.status == pp.Status.PASS
        assert "已同步" in result.message

    def test_merge_tree_success_passes(self, monkeypatch):
        # rev-list non-zero → merge-tree returns 0 with no CONFLICT → PASS.
        _stub_run_sequence(monkeypatch, [
            _cp(0, "3\n"),         # rev-list count = 3 (behind, will check)
            _cp(0, "tree-id\n"),   # merge-tree clean
        ])
        result = pp.check_conflict()
        assert result.status == pp.Status.PASS
        assert "merge-tree" in result.message

    def test_merge_tree_conflict_fails(self, monkeypatch):
        merge_tree_output = "tree-id\nCONFLICT (content): Merge conflict in foo.go\n"
        _stub_run_sequence(monkeypatch, [
            _cp(0, "3\n"),
            _cp(1, merge_tree_output, ""),
        ])
        result = pp.check_conflict()
        assert result.status == pp.Status.FAIL
        assert "1 個檔案衝突" in result.message
        assert "foo.go" in result.detail

    def test_merge_tree_unavailable_with_fuse_lock_warns(
        self, monkeypatch, tmp_path,
    ):
        # merge-tree fails with no CONFLICT in output. Then ORIG_HEAD.lock
        # exists → graceful WARN about FUSE.
        monkeypatch.chdir(tmp_path)
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "ORIG_HEAD.lock").write_text("")

        _stub_run_sequence(monkeypatch, [
            _cp(0, "3\n"),
            _cp(1, "", "unknown subcommand merge-tree"),  # old git
        ])
        result = pp.check_conflict()
        assert result.status == pp.Status.WARN
        assert "FUSE" in result.message

    def test_merge_no_commit_fallback_passes(self, monkeypatch, tmp_path):
        # merge-tree fails AND no FUSE lock → falls through to merge --no-commit
        monkeypatch.chdir(tmp_path)
        _stub_run_sequence(monkeypatch, [
            _cp(0, "3\n"),
            _cp(1, "", "unknown subcommand"),     # merge-tree fails (old git)
            _cp(0, "", ""),                       # merge --no-commit succeeds
            _cp(0, "", ""),                       # merge --abort
        ])
        result = pp.check_conflict()
        assert result.status == pp.Status.PASS
        assert "dry-run" in result.message

    def test_merge_no_commit_fallback_conflict_fails(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        merge_output = (
            "Auto-merging file.txt\n"
            "CONFLICT (content): Merge conflict in file.txt\n"
        )
        _stub_run_sequence(monkeypatch, [
            _cp(0, "3\n"),
            _cp(1, "", "unknown"),                # merge-tree fails
            _cp(1, merge_output, ""),             # merge --no-commit conflict
            _cp(0, "", ""),                       # merge --abort
        ])
        result = pp.check_conflict()
        assert result.status == pp.Status.FAIL
        assert "1 個檔案衝突" in result.message
        assert "file.txt" in result.detail


# ---------------------------------------------------------------------------
# check_local_hooks
# ---------------------------------------------------------------------------
class TestCheckLocalHooks:
    def test_all_pass(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "hook-a..............Passed\n"))
        result = pp.check_local_hooks()
        assert result.status == pp.Status.PASS

    def test_failed_hooks_parsed(self, monkeypatch):
        output = (
            "hook-a..................Passed\n"
            "hook-b..................Failed\n"
            "hook-c..................Failed\n"
        )
        _stub_run_constant(monkeypatch, _cp(1, output))
        result = pp.check_local_hooks()
        assert result.status == pp.Status.FAIL
        assert "2 hook(s) 失敗" in result.message
        assert "1 通過" in result.message
        assert "hook-b" in result.detail
        assert "hook-c" in result.detail

    def test_subprocess_error_fails_with_stderr(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(127, "", "pre-commit: command not found"))
        result = pp.check_local_hooks()
        assert result.status == pp.Status.FAIL
        assert "command not found" in result.detail


# ---------------------------------------------------------------------------
# check_scope_drift
# ---------------------------------------------------------------------------
class TestCheckScopeDrift:
    def test_clean_passes(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, ""))
        result = pp.check_scope_drift()
        assert result.status == pp.Status.PASS

    def test_failure_parses_fail_headline(self, monkeypatch):
        output = (
            "PASS: working tree clean\n"
            "FAIL: tool-map drift detected\n"
            "         delta count: 3\n"
        )
        _stub_run_constant(monkeypatch, _cp(1, output, ""))
        result = pp.check_scope_drift()
        assert result.status == pp.Status.FAIL
        assert "FAIL: tool-map drift detected" in result.message

    def test_failure_with_no_output(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(1, "", ""))
        result = pp.check_scope_drift()
        assert result.status == pp.Status.FAIL
        assert "no output" in result.message


# ---------------------------------------------------------------------------
# check_ci_status
# ---------------------------------------------------------------------------
class TestCheckCIStatus:
    def test_no_pr_returns_skip(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(1, "", "no open pull request found"))
        result = pp.check_ci_status()
        assert result.status == pp.Status.SKIP

    def test_gh_unavailable_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(1, "", "gh: command not found"))
        result = pp.check_ci_status()
        assert result.status == pp.Status.WARN

    def test_empty_checks_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "[]", ""))
        result = pp.check_ci_status()
        assert result.status == pp.Status.WARN
        assert "無 CI checks" in result.message

    def test_all_pass(self, monkeypatch):
        checks = [
            {"name": "Lint", "state": "SUCCESS", "bucket": "pass"},
            {"name": "Tests", "state": "SUCCESS", "bucket": "pass"},
        ]
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(checks)))
        result = pp.check_ci_status()
        assert result.status == pp.Status.PASS
        assert "全部 2 個" in result.message

    def test_pending_warns(self, monkeypatch):
        checks = [
            {"name": "Lint", "state": "SUCCESS", "bucket": "pass"},
            {"name": "Tests", "state": "PENDING", "bucket": "pending"},
        ]
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(checks)))
        result = pp.check_ci_status()
        assert result.status == pp.Status.WARN
        assert "1 個 check 還在跑" in result.message

    def test_failure_triggers_ab_classification(self, monkeypatch):
        # check_ci_status returns FAIL and calls _classify_ci_failures.
        # Stub _classify to return a known string so we can assert detail.
        checks = [{"name": "Lint", "state": "FAILURE", "bucket": "fail"}]
        # 1st run: gh pr checks → fail. Then _classify_ci_failures will
        # call run() again, but we stub _classify_ci_failures directly.
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(checks)))
        monkeypatch.setattr(pp, "_classify_ci_failures",
                            lambda failed: "→ stubbed AB classification")
        result = pp.check_ci_status()
        assert result.status == pp.Status.FAIL
        assert "1 failed" in result.message
        assert "stubbed AB" in result.detail

    def test_invalid_json_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "{not json"))
        result = pp.check_ci_status()
        assert result.status == pp.Status.WARN
        assert "解析" in result.message


# ---------------------------------------------------------------------------
# check_pr_mergeable
# ---------------------------------------------------------------------------
class TestCheckPRMergeable:
    def test_no_pr_returns_skip(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(1, "", "no open pull request"))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.SKIP

    def test_gh_other_error_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(1, "", "API rate limit"))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.WARN
        assert "rate limit" in result.detail

    def test_conflicting_fails(self, monkeypatch):
        payload = {
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "DIRTY",
            "reviewDecision": "",
        }
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(payload)))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.FAIL
        assert "衝突" in result.message

    def test_blocked_without_review_warns(self, monkeypatch):
        payload = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "reviewDecision": "REVIEW_REQUIRED",
        }
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(payload)))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.WARN
        assert "review approval" in result.message

    def test_blocked_approved_means_other_protection(self, monkeypatch):
        payload = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "reviewDecision": "APPROVED",
        }
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(payload)))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.WARN
        assert "其他 branch protection" in result.message

    def test_clean_mergeable_passes(self, monkeypatch):
        payload = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
        }
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(payload)))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.PASS
        assert "可直接 merge" in result.message

    def test_unknown_state_warns(self, monkeypatch):
        payload = {
            "mergeable": "UNKNOWN",
            "mergeStateStatus": "UNKNOWN",
            "reviewDecision": "",
        }
        _stub_run_constant(monkeypatch, _cp(0, json.dumps(payload)))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.WARN
        assert "UNKNOWN" in result.message

    def test_invalid_json_warns(self, monkeypatch):
        _stub_run_constant(monkeypatch, _cp(0, "{not json"))
        result = pp.check_pr_mergeable()
        assert result.status == pp.Status.WARN
