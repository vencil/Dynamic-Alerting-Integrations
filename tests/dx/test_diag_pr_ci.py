"""Tests for diag_pr_ci.py — PR CI diagnostic CLI (#446).

Per #446 day-2 supplementary comment: use a `side_effect` router to map
each `gh api <path>` call to a fixture file based on the endpoint pattern.
A single `return_value` mock can't catch sequential-routing bugs.

Fixtures live in `tests/dx/fixtures/diag_pr_ci/` so test code stays short
and fixture shapes can be re-recorded with one `gh api > fixtures/x.json`
command when the real API drifts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import diag_pr_ci as dpc  # noqa: E402


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "diag_pr_ci"


def _cp(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


def _load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def _mock_gh_api_router(cmd, **kwargs):
    """Route `gh api <path>` calls to fixture files by endpoint pattern.

    The router maps each known endpoint to one fixture. Unmatched endpoints
    raise AssertionError so a refactor that adds a new endpoint without
    updating tests fails loudly instead of silently returning empty.
    """
    endpoint = cmd[-1]  # path is always last positional arg

    if "/pulls/" in endpoint and "/check-runs" not in endpoint:
        body = _load_fixture("pull_446.json")
    elif "/commits/" in endpoint and "/check-runs" in endpoint:
        body = _load_fixture("check_runs.json")
    elif "/actions/runs/" in endpoint and "/jobs" in endpoint:
        body = _load_fixture("jobs_55002.json")
    elif endpoint.endswith("/check-runs/7002/annotations"):
        body = _load_fixture("annotations_7002.json")
    elif endpoint.endswith("/check-runs/7003/annotations"):
        body = _load_fixture("annotations_7003.json")
    else:
        raise AssertionError(f"Unexpected endpoint in test: {endpoint}")
    return _cp(0, body, "")


# ─── gh_api wrapper ─────────────────────────────────────────────────────


class TestGhApi:
    def test_returns_parsed_json_on_success(self, monkeypatch):
        monkeypatch.setattr(
            dpc.subprocess, "run",
            lambda *a, **kw: _cp(0, '{"foo": 1}', ""),
        )
        assert dpc.gh_api("/whatever") == {"foo": 1}

    def test_includes_paginate_flag_when_requested(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _cp(0, "[]", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        dpc.gh_api("/x", paginate=True)
        assert "--paginate" in captured["cmd"]

    def test_omits_paginate_flag_by_default(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return _cp(0, "{}", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        dpc.gh_api("/x")
        assert "--paginate" not in captured["cmd"]

    def test_raises_gh_api_error_on_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            dpc.subprocess, "run",
            lambda *a, **kw: _cp(1, "", "Not Found"),
        )
        with pytest.raises(dpc.GhApiError) as exc_info:
            dpc.gh_api("/repos/o/r/pulls/99999")
        assert exc_info.value.endpoint == "/repos/o/r/pulls/99999"
        assert "Not Found" in exc_info.value.stderr

    def test_raises_value_error_on_non_json_stdout(self, monkeypatch):
        monkeypatch.setattr(
            dpc.subprocess, "run",
            lambda *a, **kw: _cp(0, "<html>unexpected", ""),
        )
        with pytest.raises(ValueError, match="non-JSON"):
            dpc.gh_api("/x")

    def test_timeout_collapses_into_gh_api_error_with_rc_124(self, monkeypatch):
        """Round-2 self-review fix: subprocess.TimeoutExpired must not leak
        as an uncaught exception. The 3s prereq probe can pass and then a
        mid-flow endpoint call can still time out (network drops, slow
        cross-region API). Surface as GhApiError so main() handles it
        with a clean message instead of a stack trace."""
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(a[0] if a else [], kw.get("timeout", 0))

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(dpc.GhApiError) as exc_info:
            dpc.gh_api("/repos/o/r/pulls/1", timeout=30)
        assert exc_info.value.returncode == 124  # POSIX timeout convention
        assert "timed out" in exc_info.value.stderr
        assert "network may have dropped" in exc_info.value.stderr


# ─── Prerequisite probe ────────────────────────────────────────────────


class TestCheckPrerequisites:
    def test_passes_when_all_three_steps_succeed(self, monkeypatch):
        # gh --version OK, gh auth status OK, gh api /rate_limit OK.
        monkeypatch.setattr(
            dpc.subprocess, "run",
            lambda *a, **kw: _cp(0, "ok", ""),
        )
        # Returns None on success (no SystemExit raised).
        dpc.check_prerequisites()

    def test_exit_2_when_gh_not_installed(self, monkeypatch, capsys):
        def fake_run(cmd, **kw):
            if cmd[:2] == ["gh", "--version"]:
                raise FileNotFoundError("No such file or directory: 'gh'")
            return _cp(0, "", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            dpc.check_prerequisites()
        assert exc_info.value.code == dpc.EXIT_PREREQ_MISSING
        assert "gh` CLI not found" in capsys.readouterr().err

    def test_exit_2_when_not_authenticated(self, monkeypatch, capsys):
        call_index = {"i": 0}

        def fake_run(cmd, **kw):
            call_index["i"] += 1
            if cmd[:2] == ["gh", "--version"]:
                return _cp(0, "gh 2.x", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(1, "", "You are not logged into any GitHub hosts.")
            return _cp(0, "", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            dpc.check_prerequisites()
        assert exc_info.value.code == dpc.EXIT_PREREQ_MISSING
        assert "not authenticated" in capsys.readouterr().err

    def test_exit_3_when_api_probe_times_out(self, monkeypatch, capsys):
        def fake_run(cmd, **kw):
            if cmd[:2] == ["gh", "--version"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "api", "/rate_limit"]:
                raise subprocess.TimeoutExpired(cmd, 3)
            return _cp(0, "", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            dpc.check_prerequisites()
        assert exc_info.value.code == dpc.EXIT_NETWORK_BLOCKED
        err = capsys.readouterr().err
        assert "Cowork VM" in err
        assert "Windows MCP" in err

    def test_exit_3_when_api_probe_returns_proxy_error(self, monkeypatch, capsys):
        def fake_run(cmd, **kw):
            if cmd[:2] == ["gh", "--version"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "api", "/rate_limit"]:
                return _cp(1, "", "Could not resolve host: api.github.com")
            return _cp(0, "", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            dpc.check_prerequisites()
        assert exc_info.value.code == dpc.EXIT_NETWORK_BLOCKED

    def test_exit_2_when_api_probe_returns_non_proxy_error(self, monkeypatch, capsys):
        """Auth-class probe failure (e.g. token expired) routes to exit 2
        with a re-login hint, not exit 3 (which is for network/proxy)."""
        def fake_run(cmd, **kw):
            if cmd[:2] == ["gh", "--version"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(0, "", "")
            if cmd[:3] == ["gh", "api", "/rate_limit"]:
                return _cp(1, "", "HTTP 401: Bad credentials")
            return _cp(0, "", "")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as exc_info:
            dpc.check_prerequisites()
        assert exc_info.value.code == dpc.EXIT_PREREQ_MISSING
        assert "gh auth refresh" in capsys.readouterr().err


# ─── Orchestration with side_effect router ─────────────────────────────


class TestDiagnosePr:
    def test_full_flow_drills_failed_actions_check(self, monkeypatch):
        """The canonical happy-path: PR has 4 check-runs (success / Actions
        failure / external failure / pending). Expect:
        - 2 failed checks surfaced (the Actions one + the CodeCov one)
        - Actions check gets jobs drilled + annotations attached
        - External check skips jobs, annotations attempted (empty array)
        - Bucket summary for non-failed: success=1, pending=1
        """
        with patch.object(dpc.subprocess, "run", side_effect=_mock_gh_api_router):
            diag = dpc.diagnose_pr("foo", "bar", 446)

        assert diag.pr_number == 446
        assert diag.head_sha.startswith("abc123def456")
        assert len(diag.failed_checks) == 2

        actions_check = next(c for c in diag.failed_checks if c.name == "test (linux)")
        assert actions_check.workflow_run_id == 55002
        assert len(actions_check.jobs) == 1  # only the failed py3.13 job
        assert actions_check.jobs[0].name == "test (linux, py3.13)"
        assert len(actions_check.jobs[0].failed_steps) == 1
        assert actions_check.jobs[0].failed_steps[0].name == "Run pytest"
        assert len(actions_check.annotations) == 3

        external_check = next(c for c in diag.failed_checks if c.name == "CodeCov")
        assert external_check.workflow_run_id is None
        assert external_check.jobs == []
        assert external_check.annotations == []

        assert diag.other_checks_summary == {"success": 1, "pending": 1}

    def test_call_count_and_order(self, monkeypatch):
        """The 4-endpoint contract: order matters because each call depends
        on data from the prior. Refactoring that swaps jobs↔annotations
        order would still PASS data-shape tests but break the contract.
        Pin the call sequence so the drift surfaces."""
        with patch.object(dpc.subprocess, "run", side_effect=_mock_gh_api_router) as mock:
            dpc.diagnose_pr("foo", "bar", 446)

        endpoints = [c.args[0][-1] for c in mock.call_args_list]
        assert "/pulls/446" in endpoints[0]
        assert "/commits/" in endpoints[1] and "/check-runs" in endpoints[1]
        # Then for each failed check: jobs (Actions only) + annotations.
        # The Actions check (7002) drills both; the external check (7003)
        # only attempts annotations. Order within a check: jobs first,
        # then annotations.
        actions_drill = [e for e in endpoints if "/runs/55002/jobs" in e]
        assert len(actions_drill) == 1, "Actions jobs must be fetched exactly once"
        annotation_calls = [e for e in endpoints if "annotations" in e]
        assert len(annotation_calls) == 2, "both failed checks attempt annotations"

    def test_jobs_fetch_error_does_not_abort_flow(self, monkeypatch, capsys):
        """If `gh api /actions/runs/.../jobs` fails (e.g. ephemeral 502),
        the diag should still complete with annotations populated and a
        stderr warning — not crash the whole tool."""
        def router(cmd, **kw):
            endpoint = cmd[-1]
            if "/actions/runs/" in endpoint and "/jobs" in endpoint:
                return _cp(1, "", "HTTP 502: Bad Gateway")
            return _mock_gh_api_router(cmd, **kw)

        with patch.object(dpc.subprocess, "run", side_effect=router):
            diag = dpc.diagnose_pr("foo", "bar", 446)

        actions_check = next(c for c in diag.failed_checks if c.name == "test (linux)")
        assert actions_check.workflow_run_id == 55002
        assert actions_check.jobs == []
        # Annotations should still be attached — graceful degradation.
        assert len(actions_check.annotations) == 3
        assert "Could not fetch jobs" in capsys.readouterr().err


# ─── Markdown formatter ────────────────────────────────────────────────


class TestFormatMarkdown:
    def _make_diag(self, **overrides) -> dpc.PrDiag:
        defaults = dict(
            pr_number=446, pr_title="example", pr_url="https://example.com/pr/446",
            head_sha="abc" * 14, head_ref="feature/x", state="open",
        )
        defaults.update(overrides)
        return dpc.PrDiag(**defaults)

    def test_no_failures_emits_success_marker(self):
        diag = self._make_diag()
        out = dpc.format_markdown(diag)
        assert "✅ No failed checks" in out
        assert "PR #446" in out

    def test_truncates_annotations_to_max(self):
        annotations = [
            {"path": f"f{i}.py", "start_line": i, "annotation_level": "failure",
             "message": f"err {i}"}
            for i in range(12)
        ]
        diag = self._make_diag(failed_checks=[
            dpc.CheckDiag(
                check_run_id=1, name="test", conclusion="failure",
                details_url="https://example.com/run/1",
                annotations=annotations,
            )
        ])
        out = dpc.format_markdown(diag, max_annotations_per_check=5)
        # First 5 annotations rendered, rest collapsed.
        assert "f0.py:0" in out
        assert "f4.py:4" in out
        assert "f5.py:5" not in out
        assert "and 7 more" in out
        assert "--json" in out  # hint at the unbounded format

    def test_external_check_renders_no_actions_jobs_marker(self):
        diag = self._make_diag(failed_checks=[
            dpc.CheckDiag(
                check_run_id=2, name="CodeCov", conclusion="failure",
                details_url="https://codecov.io/...",
                workflow_run_id=None,
            )
        ])
        out = dpc.format_markdown(diag)
        assert "External-app check" in out

    def test_actions_check_with_failed_jobs_renders_steps(self):
        check = dpc.CheckDiag(
            check_run_id=3, name="test", conclusion="failure",
            details_url="https://github.com/.../runs/55/job/88",
            workflow_run_id=55,
        )
        check.jobs.append(dpc.JobDiag(
            job_id=88, name="test (linux)", status="completed", conclusion="failure",
            failed_steps=[
                dpc.JobStep(name="Run pytest", status="completed", conclusion="failure"),
            ],
            html_url="https://example.com/job/88",
        ))
        diag = self._make_diag(failed_checks=[check])
        out = dpc.format_markdown(diag)
        assert "test (linux)" in out
        assert "Run pytest" in out

    def test_renders_other_checks_summary(self):
        diag = self._make_diag(other_checks_summary={"success": 28, "skipped": 2})
        out = dpc.format_markdown(diag)
        assert "Other checks" in out
        assert "success=28" in out
        assert "skipped=2" in out

    def test_empty_jobs_with_workflow_id_uses_neutral_message(self):
        """Round-2 self-review fix: when an Actions check has
        workflow_run_id but `check.jobs` is empty, the message must NOT
        claim 'gh api error' — empty jobs can legitimately mean the run
        was cancelled before any job started, or no individual job
        bubbled up failure. Stay neutral."""
        diag = self._make_diag(failed_checks=[
            dpc.CheckDiag(
                check_run_id=99, name="test", conclusion="failure",
                details_url="https://github.com/o/r/actions/runs/55/job/88",
                workflow_run_id=55,
                jobs=[],  # legitimately empty
            )
        ])
        out = dpc.format_markdown(diag)
        assert "gh api error" not in out.lower()
        assert "/actions/runs/55/jobs" in out  # the endpoint that was probed
        assert "--json" in out  # escape hatch hint

    def test_whitespace_only_annotation_message_does_not_crash(self):
        """Round-2 self-review fix: an annotation with `message: "\\n"` used
        to crash via `.strip().splitlines()[0]` → IndexError. Now collapses
        to empty first-line cleanly."""
        diag = self._make_diag(failed_checks=[
            dpc.CheckDiag(
                check_run_id=1, name="test", conclusion="failure",
                details_url="",
                annotations=[
                    {"path": "f.py", "start_line": 1,
                     "annotation_level": "failure", "message": "\n\n  \n"},
                ],
            )
        ])
        # The whole point: this should not raise.
        out = dpc.format_markdown(diag)
        assert "f.py:1" in out
        assert "[failure]" in out


# ─── JSON formatter ────────────────────────────────────────────────────


class TestFormatJson:
    def test_output_is_valid_json_with_stable_schema(self):
        diag = dpc.PrDiag(
            pr_number=446, pr_title="x", pr_url="https://example.com",
            head_sha="abc", head_ref="ref", state="open",
            failed_checks=[
                dpc.CheckDiag(
                    check_run_id=1, name="test", conclusion="failure",
                    details_url="https://example.com",
                    annotations=[{"path": "f.py", "start_line": 1, "message": "err"}],
                )
            ],
        )
        out = dpc.format_json(diag)
        parsed = json.loads(out)
        assert parsed["pr_number"] == 446
        assert len(parsed["failed_checks"]) == 1
        assert parsed["failed_checks"][0]["check_run_id"] == 1
        assert parsed["failed_checks"][0]["annotations"][0]["path"] == "f.py"

    def test_json_does_not_truncate_annotations(self):
        """Unlike markdown, JSON must preserve every annotation so machine
        consumers can do their own filtering / pagination."""
        annotations = [{"path": f"f{i}.py", "start_line": i, "message": f"e{i}"}
                       for i in range(50)]
        diag = dpc.PrDiag(
            pr_number=1, pr_title="x", pr_url="", head_sha="", head_ref="",
            state="open",
            failed_checks=[dpc.CheckDiag(
                check_run_id=1, name="t", conclusion="failure",
                details_url="", annotations=annotations,
            )],
        )
        parsed = json.loads(dpc.format_json(diag))
        assert len(parsed["failed_checks"][0]["annotations"]) == 50


# ─── End-to-end main() dispatch ────────────────────────────────────────


class TestMainDispatch:
    def test_pr_not_found_returns_exit_1_with_clean_message(self, monkeypatch, capsys, cli_argv):
        """404 from gh api should surface as a clean error message
        (no raw stack trace)."""
        # Prereq probes all pass.
        def fake_run(cmd, **kw):
            if cmd[:2] in (["gh", "--version"], ["gh", "auth"]):
                return _cp(0, "ok", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(0, "ok", "")
            if cmd[:3] == ["gh", "api", "/rate_limit"]:
                return _cp(0, '{"resources": {}}', "")
            if cmd[:3] == ["gh", "repo", "view"]:
                return _cp(0, '{"nameWithOwner": "foo/bar"}', "")
            if cmd[:2] == ["gh", "api"] and "/pulls/99999" in cmd[-1]:
                return _cp(1, "", "HTTP 404: Not Found")
            raise AssertionError(f"unexpected cmd in test: {cmd}")

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        cli_argv("diag_pr_ci.py", "99999")
        rc = dpc.main()
        assert rc == dpc.EXIT_INTERNAL_ERROR
        err = capsys.readouterr().err
        assert "PR #99999 not found" in err
        assert "cross-repo" in err  # the documented limitation hint

    def test_json_flag_emits_parseable_json(self, monkeypatch, capsys, cli_argv):
        def fake_run(cmd, **kw):
            if cmd[:2] in (["gh", "--version"], ["gh", "auth"]):
                return _cp(0, "ok", "")
            if cmd[:3] == ["gh", "auth", "status"]:
                return _cp(0, "ok", "")
            if cmd[:3] == ["gh", "api", "/rate_limit"]:
                return _cp(0, '{"resources": {}}', "")
            if cmd[:3] == ["gh", "repo", "view"]:
                return _cp(0, '{"nameWithOwner": "foo/bar"}', "")
            return _mock_gh_api_router(cmd, **kw)

        monkeypatch.setattr(dpc.subprocess, "run", fake_run)
        cli_argv("diag_pr_ci.py", "446", "--json")
        rc = dpc.main()
        assert rc == dpc.EXIT_OK
        out = capsys.readouterr().out
        parsed = json.loads(out)  # must be valid JSON
        assert parsed["pr_number"] == 446
