"""Tests for operator_check.py — Prometheus Operator CRD deployment verifier.

Audit flagged this as a 0% covered tool. Every kubectl / HTTP call is
monkeypatched so no real cluster / Prometheus is contacted.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import operator_check as oc  # noqa: E402


def _make_args(**overrides) -> SimpleNamespace:
    """Build a minimal argparse-shaped Namespace for OperatorChecker."""
    defaults = dict(
        namespace="monitoring",
        rule_packs_dir="rule-packs",
        config_dir="conf.d",
        prometheus=None,
        kubeconfig=None,
        json=False,
        ci=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _stub_kubectl(monkeypatch, responses):
    """Replace OperatorChecker.run_kubectl with a sequence-yielding stub.

    `responses` is a list of (stdout, stderr, returncode) tuples.
    """
    it = iter(responses)

    def fake(self, *cmd):
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError(
                f"run_kubectl called more times than mocked: {cmd}"
            ) from exc

    monkeypatch.setattr(oc.OperatorChecker, "run_kubectl", fake)


# ---------------------------------------------------------------------------
# i18n — pure helper
# ---------------------------------------------------------------------------
class TestI18n:
    def test_known_key_en(self):
        assert oc.i18n("title", "en") == oc.STRINGS["en"]["title"]

    def test_known_key_zh(self):
        assert oc.i18n("title", "zh") == oc.STRINGS["zh"]["title"]

    def test_unknown_lang_falls_back_to_en(self):
        assert oc.i18n("title", "fr") == oc.STRINGS["en"]["title"]

    def test_unknown_key_returns_key_as_fallback(self):
        assert oc.i18n("nonexistent_key", "en") == "nonexistent_key"


# ---------------------------------------------------------------------------
# CheckResult — data class
# ---------------------------------------------------------------------------
class TestCheckResult:
    def test_to_dict_shape(self):
        r = oc.CheckResult("Test", "pass", "ok")
        assert r.to_dict() == {
            "check": "Test",
            "status": "pass",
            "detail": "ok",
            "caller_error": False,  # #452/#737: additive caller-error marker
        }

    def test_default_detail_empty(self):
        r = oc.CheckResult("Test", "fail")
        assert r.detail == ""
        assert r.to_dict()["detail"] == ""


# ---------------------------------------------------------------------------
# OperatorChecker.run_kubectl — subprocess wrapper
# ---------------------------------------------------------------------------
class TestRunKubectl:
    def test_success_returns_stdout_stderr_rc(self, monkeypatch):
        proc = MagicMock()
        proc.stdout = "ok\n"
        proc.stderr = ""
        proc.returncode = 0
        monkeypatch.setattr(oc.subprocess, "run", lambda *a, **kw: proc)
        checker = oc.OperatorChecker(_make_args())
        out, err, rc = checker.run_kubectl("get", "pods")
        assert (out, err, rc) == ("ok\n", "", 0)

    def test_kubectl_missing_returns_127(self, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("kubectl: command not found")
        monkeypatch.setattr(oc.subprocess, "run", boom)
        checker = oc.OperatorChecker(_make_args())
        _, err, rc = checker.run_kubectl("get", "pods")
        assert rc == 127
        # i18n message — just verify non-empty.
        assert err

    def test_timeout_returns_124(self, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="kubectl", timeout=10)
        monkeypatch.setattr(oc.subprocess, "run", boom)
        checker = oc.OperatorChecker(_make_args())
        _, err, rc = checker.run_kubectl("get", "pods")
        assert rc == 124
        assert err

    def test_kubeconfig_flag_passed_to_kubectl(self, monkeypatch):
        captured = {}

        def fake(cmd, **kw):
            captured["cmd"] = cmd
            proc = MagicMock(stdout="", stderr="", returncode=0)
            return proc
        monkeypatch.setattr(oc.subprocess, "run", fake)
        checker = oc.OperatorChecker(_make_args(kubeconfig="/path/kc"))
        checker.run_kubectl("get", "pods")
        assert "--kubeconfig" in captured["cmd"]
        assert "/path/kc" in captured["cmd"]


# ---------------------------------------------------------------------------
# check_operator_detection
# ---------------------------------------------------------------------------
class TestCheckOperatorDetection:
    def test_crd_found_passes(self, monkeypatch):
        _stub_kubectl(monkeypatch, [("...", "", 0)])
        checker = oc.OperatorChecker(_make_args())
        result = checker.check_operator_detection()
        assert result.status == "pass"

    def test_crd_missing_fails(self, monkeypatch):
        _stub_kubectl(monkeypatch, [("", "Error: not found", 1)])
        checker = oc.OperatorChecker(_make_args())
        result = checker.check_operator_detection()
        assert result.status == "fail"


# ---------------------------------------------------------------------------
# check_prometheus_rule_status
# ---------------------------------------------------------------------------
class TestCheckPrometheusRuleStatus:
    def test_loaded_equals_expected_passes(self, monkeypatch, tmp_path):
        # 2 rule packs on disk; kubectl returns 2 items → pass.
        (tmp_path / "rule-pack-database.yaml").write_text("x", encoding="utf-8")
        (tmp_path / "rule-pack-mongodb.yaml").write_text("x", encoding="utf-8")
        kubectl_resp = json.dumps({"items": [{"x": 1}, {"x": 2}]})
        _stub_kubectl(monkeypatch, [(kubectl_resp, "", 0)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "pass"
        assert "2/2" in result.detail

    def test_partial_load_warns(self, monkeypatch, tmp_path):
        # 3 rule packs on disk; kubectl returns 1 → warn.
        for i in range(3):
            (tmp_path / f"rule-pack-{i}.yaml").write_text("x", encoding="utf-8")
        _stub_kubectl(monkeypatch, [(json.dumps({"items": [{"x": 1}]}), "", 0)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "warn"
        assert "1/3" in result.detail

    def test_nothing_loaded_fails(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        _stub_kubectl(monkeypatch, [(json.dumps({"items": []}), "", 0)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "fail"

    def test_kubectl_error_treated_as_zero_loaded(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        _stub_kubectl(monkeypatch, [("", "no permission", 1)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "fail"  # 0 loaded, 1 expected

    def test_invalid_json_kubectl_output(self, monkeypatch, tmp_path):
        # JSON parse error → loaded = 0.
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        _stub_kubectl(monkeypatch, [("{not json", "", 0)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "fail"

    def test_missing_rule_packs_dir_treated_as_zero_expected(self, monkeypatch, tmp_path):
        # No directory → expected = 0; with loaded > 0 → warn.
        ghost = tmp_path / "ghost"
        kubectl_resp = json.dumps({"items": [{"x": 1}]})
        _stub_kubectl(monkeypatch, [(kubectl_resp, "", 0)])
        checker = oc.OperatorChecker(_make_args(rule_packs_dir=str(ghost)))
        result = checker.check_prometheus_rule_status()
        assert result.status == "warn"
        assert result.detail == "1"


# ---------------------------------------------------------------------------
# check_servicemonitor_status
# ---------------------------------------------------------------------------
class TestCheckServiceMonitorStatus:
    def test_found_passes(self, monkeypatch):
        _stub_kubectl(monkeypatch,
                      [(json.dumps({"items": [{"x": 1}]}), "", 0)])
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "pass"

    def test_empty_fails(self, monkeypatch):
        _stub_kubectl(monkeypatch, [(json.dumps({"items": []}), "", 0)])
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail"

    def test_kubectl_error_fails(self, monkeypatch):
        _stub_kubectl(monkeypatch, [("", "denied", 1)])
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail"

    def test_invalid_json_fails(self, monkeypatch):
        _stub_kubectl(monkeypatch, [("{not json", "", 0)])
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail"


# ---------------------------------------------------------------------------
# check_alertmanager_config
# ---------------------------------------------------------------------------
class TestCheckAlertmanagerConfig:
    def test_with_tenants_passes(self, monkeypatch):
        items = [{"x": 1}, {"x": 2}, {"x": 3}]
        _stub_kubectl(monkeypatch, [(json.dumps({"items": items}), "", 0)])
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "pass"
        assert "3" in result.detail

    def test_no_tenants_warns(self, monkeypatch):
        _stub_kubectl(monkeypatch, [(json.dumps({"items": []}), "", 0)])
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "warn"

    def test_invalid_json_warns(self, monkeypatch):
        _stub_kubectl(monkeypatch, [("{not json", "", 0)])
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "warn"


# ---------------------------------------------------------------------------
# check_target_health
# ---------------------------------------------------------------------------
class TestCheckTargetHealth:
    def test_no_prometheus_url_skips(self):
        result = oc.OperatorChecker(_make_args()).check_target_health()
        assert result.status == "skip"

    def test_api_error_warns(self, monkeypatch):
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: (None, "connection refused"))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_no_targets_warns(self, monkeypatch):
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: ({"data": {"activeTargets": []}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_no_exporter_targets_warns(self, monkeypatch):
        # All targets exist but none have "threshold" or "exporter" in job.
        targets = [
            {"labels": {"job": "kubernetes-nodes"}, "health": "up"},
            {"labels": {"job": "prometheus"}, "health": "up"},
        ]
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: ({"data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"
        assert "no targets" in result.detail

    def test_all_exporters_healthy_passes(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "UP"},
        ]
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: ({"data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "pass"

    def test_unhealthy_exporter_fails(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "down"},
        ]
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: ({"data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "fail"

    def test_target_with_missing_labels_treated_as_no_match(self, monkeypatch):
        # Targets present but each has empty labels — `.get("job", "")`
        # returns "" so neither "threshold" nor "exporter" matches → no
        # exporter targets → warn.
        targets = [{"labels": {}, "health": "up"}]
        monkeypatch.setattr(oc, "http_get_json",
                            lambda *a, **kw: ({"data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_malformed_response_does_not_crash(self, monkeypatch):
        # PR #291 audit follow-up: when http_get_json returns a non-dict
        # body (None / string / list), `data.get(...)` raises AttributeError.
        # The except clause now catches AttributeError too, so the function
        # degrades gracefully to the "no targets found" warn path.
        for malformed in (None, "not a dict", ["list-not-dict"], 42):
            monkeypatch.setattr(oc, "http_get_json",
                                lambda *a, _body=malformed, **kw: (_body, None))
            checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
            result = checker.check_target_health()
            assert result.status == "warn"
            assert "no targets" in result.detail


# ---------------------------------------------------------------------------
# run_all_checks + reports + exit_code
# ---------------------------------------------------------------------------
class TestRunAllChecks:
    def test_appends_five_checks(self, monkeypatch):
        # Stub each individual check_* to return a known PASS result.
        for fn_name in (
            "check_operator_detection",
            "check_prometheus_rule_status",
            "check_servicemonitor_status",
            "check_alertmanager_config",
            "check_target_health",
        ):
            monkeypatch.setattr(
                oc.OperatorChecker, fn_name,
                lambda self, _name=fn_name: oc.CheckResult(_name, "pass"),
            )
        checker = oc.OperatorChecker(_make_args())
        checker.run_all_checks()
        assert len(checker.checks) == 5
        assert all(c.status == "pass" for c in checker.checks)


class TestReports:
    def _make_filled(self):
        c = oc.OperatorChecker(_make_args())
        c.checks = [
            oc.CheckResult("Operator", "pass", "found"),
            oc.CheckResult("Rules", "warn", "1/2"),
            oc.CheckResult("Targets", "fail", "broken"),
            oc.CheckResult("Skipped", "skip", "no url"),
        ]
        return c

    def test_human_report_mentions_each_check_and_summary(self, capsys):
        self._make_filled().print_human_report()
        out = capsys.readouterr().out
        for name in ("Operator", "Rules", "Targets"):
            assert name in out
        # Status symbols for each tier appear.
        for sym in ("✓", "⚠", "✗", "—"):
            assert sym in out

    def test_json_report_has_summary_counts(self, capsys):
        self._make_filled().print_json_report()
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["pass"] == 1
        assert payload["summary"]["warn"] == 1
        assert payload["summary"]["fail"] == 1
        assert payload["summary"]["total"] == 4
        assert len(payload["checks"]) == 4


class TestExitCode:
    def test_non_ci_always_returns_zero_even_with_failures(self):
        c = oc.OperatorChecker(_make_args(ci=False))
        c.checks = [oc.CheckResult("X", "fail")]
        assert c.exit_code() == 0

    def test_ci_with_no_failures_returns_zero(self):
        c = oc.OperatorChecker(_make_args(ci=True))
        c.checks = [oc.CheckResult("X", "pass"), oc.CheckResult("Y", "warn")]
        assert c.exit_code() == 0

    def test_ci_with_failure_returns_one(self):
        c = oc.OperatorChecker(_make_args(ci=True))
        c.checks = [oc.CheckResult("X", "fail"), oc.CheckResult("Y", "pass")]
        assert c.exit_code() == 1


# ---------------------------------------------------------------------------
# main — CLI entry
# ---------------------------------------------------------------------------
class TestMain:
    def test_default_run_exits_zero(self, monkeypatch, cli_argv):
        # Stub everything so main() doesn't actually contact a cluster.
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: 0)
        cli_argv("operator_check.py")
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 0

    def test_json_flag_picks_json_reporter(self, monkeypatch, cli_argv):
        called = {"json": False, "human": False}
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", lambda self: None)
        monkeypatch.setattr(
            oc.OperatorChecker, "print_json_report",
            lambda self: called.__setitem__("json", True),
        )
        monkeypatch.setattr(
            oc.OperatorChecker, "print_human_report",
            lambda self: called.__setitem__("human", True),
        )
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: 0)
        cli_argv("operator_check.py", "--json")
        with pytest.raises(SystemExit):
            oc.main()
        assert called["json"] is True
        assert called["human"] is False

    def test_ci_flag_propagates_to_exit_code(self, monkeypatch, cli_argv):
        # When --ci is set and exit_code returns 1, main() exits 1.
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        monkeypatch.setattr(oc.OperatorChecker, "exit_code", lambda self: 1)
        cli_argv("operator_check.py", "--ci")
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 1
