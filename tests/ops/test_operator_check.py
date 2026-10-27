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
import _lib_prometheus  # noqa: E402  (#3133: the targets are read by the lib)


def _make_args(**overrides) -> SimpleNamespace:
    """Build a minimal argparse-shaped Namespace for OperatorChecker."""
    defaults = dict(
        namespace="monitoring",
        rule_packs_dir="rule-packs",
        prometheus=None,
        kubeconfig=None,
        json=False,
        ci=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


_ALL_CRDS = (oc._CRD_RULES, oc._CRD_MONITORS, oc._CRD_AM_CONFIGS)


def _stub_cluster(monkeypatch, crds=_ALL_CRDS, lists=None, fail=None):
    """Replace `kubectl_list` (the shared reader, #3207) with an in-memory
    cluster: `crds` names the installed CRDs, `lists` maps kind → items,
    `fail` maps kind → the KubectlError message its read raises. Returns
    the list of (kind, kwargs) calls."""
    lists, fail, calls = dict(lists or {}), dict(fail or {}), []

    def fake(kind, **kw):
        calls.append((kind, kw))
        if kind in fail:
            raise oc.KubectlError(fail[kind])
        return lists.get(kind, [])

    def fake_get(kind, name, **kw):
        assert kind == "crd", kind
        calls.append(("crd", {"name": name, **kw}))
        if "crd" in fail:
            raise oc.KubectlError(fail["crd"])
        return {"metadata": {"name": name}} if name in crds else None

    monkeypatch.setattr(oc, "kubectl_list", fake)
    monkeypatch.setattr(oc, "kubectl_get", fake_get)
    return calls


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
class TestClusterReads:
    """#3207: every read is `kubectl_list` — a read with no answer is a caller
    error (rc 2), never "not found"; a missing CRD is an answer."""

    def test_kubeconfig_namespace_and_selector_reach_the_reader(self, monkeypatch):
        calls = _stub_cluster(monkeypatch)
        oc.OperatorChecker(_make_args(kubeconfig="/path/kc", namespace="ns1")).check_servicemonitor_status()
        assert ("servicemonitor", {"kubeconfig": "/path/kc", "namespace": "ns1",
                                   "selector": oc._PART_OF}) in calls

    def test_each_crd_is_read_once_by_name_even_when_it_fails(self, monkeypatch):
        """A CRD is a `get` by name (needs only `get`, review of #3207), once
        per CRD, a failure raised again rather than re-asked."""
        calls = _stub_cluster(monkeypatch, fail={"crd": "unreachable"})
        checker = oc.OperatorChecker(_make_args(kubeconfig="/kc"))
        for check in (checker.check_operator_detection, checker.check_prometheus_rule_status,
                      checker.check_servicemonitor_status, checker.check_alertmanager_config):
            check()
        assert calls == [("crd", {"name": n, "kubeconfig": "/kc"})
                         for n in (oc._CRD_RULES, oc._CRD_MONITORS, oc._CRD_AM_CONFIGS)]

    @pytest.mark.parametrize("failing", ["crd", "prometheusrule", "servicemonitor", "alertmanagerconfig"])
    def test_a_read_with_no_answer_is_a_caller_error(self, monkeypatch, failing):
        _stub_cluster(monkeypatch, fail={failing: f"`kubectl get {failing}` failed (exit 1): Forbidden"})
        checker = oc.OperatorChecker(_make_args(ci=False))
        checker.run_all_checks()
        hit = [c for c in checker.checks if c.caller_error]
        assert hit and all(c.status == "fail" and "Forbidden" in c.detail for c in hit), checker.checks
        assert checker.exit_code() == 2


class TestCheckOperatorDetection:
    def test_crd_found_passes(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_operator_detection()
        assert result.status == "pass" and not result.caller_error

    def test_crd_missing_is_a_finding(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=())
        result = oc.OperatorChecker(_make_args()).check_operator_detection()
        assert result.status == "fail" and not result.caller_error
        assert oc._CRD_RULES in result.detail


# ---------------------------------------------------------------------------
# check_prometheus_rule_status
# ---------------------------------------------------------------------------
class TestCheckPrometheusRuleStatus:
    def test_loaded_equals_expected_passes(self, monkeypatch, tmp_path):
        # 2 rule packs on disk; the cluster lists 2 → pass.
        (tmp_path / "rule-pack-database.yaml").write_text("x", encoding="utf-8")
        (tmp_path / "rule-pack-mongodb.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}, {"x": 2}]})
        result = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path))).check_prometheus_rule_status()
        assert result.status == "pass"
        assert "2/2" in result.detail

    def test_partial_load_warns(self, monkeypatch, tmp_path):
        for i in range(3):
            (tmp_path / f"rule-pack-{i}.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}]})
        result = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path))).check_prometheus_rule_status()
        assert result.status == "warn"
        assert "1/3" in result.detail

    def test_nothing_loaded_fails(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path))).check_prometheus_rule_status()
        assert result.status == "fail" and not result.caller_error

    def test_crd_not_installed_is_a_finding_and_is_not_listed(self, monkeypatch, tmp_path):
        (tmp_path / "rule-pack-x.yaml").write_text("x", encoding="utf-8")
        calls = _stub_cluster(monkeypatch, crds=(), fail={"prometheusrule": "must not be read"})
        result = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path))).check_prometheus_rule_status()
        assert result.status == "fail" and not result.caller_error
        assert oc._CRD_RULES in result.detail and [k for k, _ in calls] == ["crd"]
        assert "prometheusrule" not in [k for k, _ in calls]

    def test_missing_rule_packs_dir_treated_as_zero_expected(self, monkeypatch, tmp_path):
        # No directory → expected = 0; with loaded > 0 → warn.
        _stub_cluster(monkeypatch, lists={"prometheusrule": [{"x": 1}]})
        result = oc.OperatorChecker(_make_args(rule_packs_dir=str(tmp_path / "ghost"))).check_prometheus_rule_status()
        assert result.status == "warn"
        assert result.detail == "1"


# ---------------------------------------------------------------------------
# check_servicemonitor_status
# ---------------------------------------------------------------------------
class TestCheckServiceMonitorStatus:
    def test_found_passes(self, monkeypatch):
        _stub_cluster(monkeypatch, lists={"servicemonitor": [{"x": 1}]})
        assert oc.OperatorChecker(_make_args()).check_servicemonitor_status().status == "pass"

    def test_empty_fails(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail" and not result.caller_error

    def test_crd_not_installed_is_a_finding(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=(oc._CRD_RULES,))
        result = oc.OperatorChecker(_make_args()).check_servicemonitor_status()
        assert result.status == "fail" and not result.caller_error and oc._CRD_MONITORS in result.detail


# ---------------------------------------------------------------------------
# check_alertmanager_config
# ---------------------------------------------------------------------------
class TestCheckAlertmanagerConfig:
    def test_with_tenants_passes(self, monkeypatch):
        _stub_cluster(monkeypatch, lists={"alertmanagerconfig": [{"x": 1}, {"x": 2}, {"x": 3}]})
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "pass"
        assert "3" in result.detail

    def test_no_tenants_warns(self, monkeypatch):
        _stub_cluster(monkeypatch)
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "warn" and result.detail == "no tenants"

    def test_crd_not_installed_warns_and_says_so(self, monkeypatch):
        _stub_cluster(monkeypatch, crds=(oc._CRD_RULES, oc._CRD_MONITORS))
        result = oc.OperatorChecker(_make_args()).check_alertmanager_config()
        assert result.status == "warn" and oc._CRD_AM_CONFIGS in result.detail


# ---------------------------------------------------------------------------
# check_target_health
# ---------------------------------------------------------------------------
class TestCheckTargetHealth:
    def test_no_prometheus_url_skips(self):
        result = oc.OperatorChecker(_make_args()).check_target_health()
        assert result.status == "skip"

    def test_api_error_is_a_caller_error(self, monkeypatch):
        """#3133 (owner 2026-10-23): --prometheus was given, so targets that
        cannot be read are a caller error (exit 2) — it used to be a warn."""
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: (None, "connection refused"))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert (result.status, result.caller_error) == ("fail", True)
        assert "connection refused" in result.detail

    def test_no_targets_warns(self, monkeypatch):
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": []}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_no_exporter_targets_warns(self, monkeypatch):
        # All targets exist but none have "threshold" or "exporter" in job.
        targets = [
            {"labels": {"job": "kubernetes-nodes"}, "health": "up"},
            {"labels": {"job": "prometheus"}, "health": "up"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"
        assert "no targets" in result.detail

    def test_all_exporters_healthy_passes(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "UP"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "pass"

    def test_unhealthy_exporter_fails(self, monkeypatch):
        targets = [
            {"labels": {"job": "threshold-exporter"}, "health": "up"},
            {"labels": {"job": "threshold-exporter-2"}, "health": "down"},
        ]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "fail"

    def test_target_with_missing_labels_treated_as_no_match(self, monkeypatch):
        # Targets present but each has empty labels — `.get("job", "")`
        # returns "" so neither "threshold" nor "exporter" matches → no
        # exporter targets → warn.
        targets = [{"labels": {}, "health": "up"}]
        monkeypatch.setattr(_lib_prometheus, "http_get_json",
                            lambda *a, **kw: ({"status": "success", "data": {"activeTargets": targets}}, None))
        checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
        result = checker.check_target_health()
        assert result.status == "warn"

    def test_malformed_response_is_a_caller_error(self, monkeypatch):
        """#3133: a body that is not a target list is not an answer — it used
        to degrade to the "no targets found" warn (PR #291 kept it from crashing)."""
        for malformed in (None, "not a dict", ["list-not-dict"], 42,
                          {"status": "success", "data": {}},
                          {"status": "success", "data": {"activeTargets": [{"labels": {"job": 1}}]}}):
            monkeypatch.setattr(_lib_prometheus, "http_get_json",
                                lambda *a, _body=malformed, **kw: (_body, None))
            checker = oc.OperatorChecker(_make_args(prometheus="http://localhost:9090"))
            result = checker.check_target_health()
            assert (result.status, result.caller_error) == ("fail", True), malformed


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


# ---------------------------------------------------------------------------
# #3137 — `--config-dir` was removed (it was declared but never read). Every
# spelling of it is now an argparse caller error, and removing it changed
# what the `--c` abbreviation resolves to.
# ---------------------------------------------------------------------------
_OPERATOR_CHECK = os.path.join(_TOOLS_DIR, "operator_check.py")


def _never_check(self):
    raise AssertionError("a rejected argv must not reach the checks")


class TestConfigDirRemoved:
    @pytest.mark.parametrize("argv, unrecognized", [
        (["--config-dir", "X"], "--config-dir X"),
        (["--config-dir=X"], "--config-dir=X"),
        # Prefixes that used to abbreviate --config-dir match nothing now.
        (["--co", "X"], "--co X"),
        (["--config", "X"], "--config X"),
    ])
    def test_every_spelling_is_an_argparse_caller_error(
        self, monkeypatch, cli_argv, capsys, argv, unrecognized,
    ):
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", _never_check)
        cli_argv("operator_check.py", *argv)
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert f"unrecognized arguments: {unrecognized}" in err

    def test_json_mode_rejects_before_any_envelope(self, tmp_path):
        # Through the real entry point: argparse refuses before --json arms
        # the crash envelope, so stdout stays empty (no JSON on stdout).
        proc = subprocess.run(
            [sys.executable, _OPERATOR_CHECK, "--json", "--config-dir", "X"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60, cwd=tmp_path,
        )
        assert proc.returncode == 2, proc.stderr
        assert proc.stdout == ""
        assert "unrecognized arguments: --config-dir X" in proc.stderr

    @pytest.mark.parametrize("argv, rc", [
        (["--c"], 1),   # --c is now an unambiguous prefix of --ci
        ([], 0),        # control: same failing check without --ci
    ])
    def test_c_abbreviation_now_resolves_to_ci(
        self, monkeypatch, cli_argv, argv, rc,
    ):
        def one_failure(self):
            self.checks = [oc.CheckResult("X", "fail")]
        monkeypatch.setattr(oc.OperatorChecker, "run_all_checks", one_failure)
        monkeypatch.setattr(oc.OperatorChecker, "print_human_report", lambda self: None)
        cli_argv("operator_check.py", *argv)
        with pytest.raises(SystemExit) as exc:
            oc.main()
        assert exc.value.code == rc
