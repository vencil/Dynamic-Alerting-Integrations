"""Tests for byo_check.py — BYO Prometheus & Alertmanager integration verification."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import byo_check as bc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _args(**kwargs):
    defaults = {
        "prometheus": "http://localhost:9090",
        "alertmanager": "http://localhost:9093",
        "json": False,
        "target": "all",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_http_get_json(mapping):
    """Return a mock for http_get_json that returns based on URL patterns."""
    def _get(url):
        for pattern, val in mapping.items():
            if pattern in url:
                return val
        return (None, "mock: no matching pattern")
    return _get


def _mock_query_prometheus(mapping):
    """Return a mock for query_prometheus that extracts results from the mapping.

    The mapping values are (data_dict, error) tuples in http_get_json format.
    This converts to query_prometheus's (results, error) format.
    """
    def _query(prom_url, promql):
        for pattern, (data, err) in mapping.items():
            if pattern in promql:
                if err:
                    return None, err
                if data and data.get("status") == "success":
                    return data.get("data", {}).get("result", []), None
                return [], None
        return None, "mock: no matching pattern"
    return _query


# ---------------------------------------------------------------------------
# query_prometheus
# ---------------------------------------------------------------------------
class TestQueryPrometheus:
    """Tests via monkeypatch — query_prometheus is now an alias to _lib_python.query_prometheus_instant."""

    def test_success(self, monkeypatch):
        fake = lambda prom_url, promql: ([{"metric": {}, "value": [1, "42"]}], None)
        monkeypatch.setattr(bc, "query_prometheus", fake)
        results, err = bc.query_prometheus("http://prom:9090", "up")
        assert err is None
        assert len(results) == 1
        assert results[0]["value"][1] == "42"

    def test_error(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "connection refused")
        monkeypatch.setattr(bc, "query_prometheus", fake)
        results, err = bc.query_prometheus("http://prom:9090", "up")
        assert results is None
        assert "connection refused" in err

    def test_non_success_status(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "bad query")
        monkeypatch.setattr(bc, "query_prometheus", fake)
        results, err = bc.query_prometheus("http://prom:9090", "bad{")
        assert results is None
        assert "bad query" in err


# ---------------------------------------------------------------------------
# check_prometheus
# ---------------------------------------------------------------------------
class TestCheckPrometheus:
    def test_unreachable_returns_single_fail(self):
        """If Prometheus is unreachable, return early with 1 fail check."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            checks = bc.check_prometheus(_args())
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"
        assert checks[0]["check"] == "prometheus_reachable"

    def test_all_pass(self):
        """Happy path: all checks pass."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        prom_mapping = {
            "tenant": (
                {"status": "success", "data": {"result": [
                    {"metric": {"tenant": "db-a"}, "value": [1, "1"]},
                ]}}, None),
            "threshold": (
                {"status": "success", "data": {"result": [
                    {"metric": {}, "value": [1, "1"]},
                ]}}, None),
            "user_threshold": (
                {"status": "success", "data": {"result": [
                    {"value": [1, "10"]},
                ]}}, None),
            "tenant:": (
                {"status": "success", "data": {"result": [
                    {"value": [1, "5"]},
                ]}}, None),
            "alert_threshold": (
                {"status": "success", "data": {"result": [
                    {"value": [1, "3"]},
                ]}}, None),
        }
        http_mapping = {
            "rules": (
                {"data": {"groups": [
                    {"name": "mariadb-alerts", "rules": [
                        {"name": "test", "lastError": ""},
                    ]},
                ]}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(bc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = bc.check_prometheus(_args())

        statuses = {c["check"]: c["status"] for c in checks}
        assert statuses["prometheus_reachable"] == "pass"
        assert statuses["step1_tenant_label"] == "pass"

    def test_no_tenant_label_warns(self):
        """When no tenant label found, returns warn."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        prom_mapping = {
            "tenant": (
                {"status": "success", "data": {"result": []}}, None),
            "threshold": (
                {"status": "success", "data": {"result": []}}, None),
            "user_threshold": (
                {"status": "success", "data": {"result": []}}, None),
        }
        http_mapping = {
            "rules": (
                {"data": {"groups": []}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(bc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = bc.check_prometheus(_args())

        tenant_check = next(c for c in checks if c["check"] == "step1_tenant_label")
        assert tenant_check["status"] == "warn"


# ---------------------------------------------------------------------------
# check_alertmanager
# ---------------------------------------------------------------------------
class TestCheckAlertmanager:
    def test_unreachable_returns_single_fail(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            checks = bc.check_alertmanager(_args())
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"
        assert checks[0]["check"] == "alertmanager_ready"

    def test_all_pass(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        mapping = {
            "status": (
                {"config": {"original": "route:\n  match:\n    tenant: db-a\ninhibit_rules:\n  - ..."}},
                None,
            ),
            "alerts": (
                [{"labels": {"alertname": "Test"}}], None,
            ),
            "silences": (
                [{"status": {"state": "active"}, "id": "1"}], None,
            ),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json", side_effect=_mock_http_get_json(mapping)):
                checks = bc.check_alertmanager(_args())

        statuses = {c["check"]: c["status"] for c in checks}
        assert statuses["alertmanager_ready"] == "pass"
        assert statuses["alertmanager_tenant_routes"] == "pass"
        assert statuses["alertmanager_inhibit_rules"] == "pass"
        assert statuses["alertmanager_alerts"] == "pass"
        assert statuses["alertmanager_silences"] == "pass"

    def test_no_tenant_routes_warns(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        mapping = {
            "status": ({"config": {"original": "route:\n  receiver: default"}}, None),
            "alerts": ([], None),
            "silences": ([], None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json", side_effect=_mock_http_get_json(mapping)):
                checks = bc.check_alertmanager(_args())

        routes_check = next(c for c in checks if c["check"] == "alertmanager_tenant_routes")
        assert routes_check["status"] == "warn"


# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------
class TestFormatOutput:
    def test_json_mode(self):
        checks = [{"check": "test", "status": "pass", "detail": "ok"}]
        result = bc.format_output("prometheus", checks, json_output=True)
        assert result["section"] == "prometheus"
        assert len(result["checks"]) == 1

    def test_text_mode(self, capsys):
        checks = [
            {"check": "test_pass", "status": "pass", "detail": "ok"},
            {"check": "test_fail", "status": "fail", "detail": "bad"},
        ]
        result = bc.format_output("test", checks, json_output=False)
        assert result is None
        captured = capsys.readouterr()
        assert "TEST" in captured.out
        assert "1/2 passed" in captured.out


# ---------------------------------------------------------------------------
# main (CLI)
# ---------------------------------------------------------------------------
class TestMain:
    def test_json_output(self, capsys):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["byo_check.py", "prometheus", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    bc.main()
        # #452/#737: unreachable Prometheus = transport caller-error → exit 2
        assert exc_info.value.code == 2
        output = json.loads(capsys.readouterr().out)
        assert output["tool"] == "byo-check"
        assert output["status"] == "fail"

    def test_all_target(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["byo_check.py", "all", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    bc.main()
        # #452/#737: unreachable endpoints = transport caller-error → exit 2
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Step 4: disk-recipe prerequisite (#692 P0③ W3) — kubelet volume-stats scraped
# AND tenant-attributed when a tenant declared a disk-fill custom alert.
# ---------------------------------------------------------------------------
def _disk_query(declaring, arriving, running, arriving_err=None):
    """Mock query_prometheus for Step 4 scenarios; declaring/arriving/running are
    lists of tenant names. Earlier steps' queries return [] (benign — those checks
    are warn/fail but the tests only assert the step4 result). Order matters: the
    declaring query is matched on its `metric=~` regex BEFORE the available_bytes
    substring it also contains. arriving_err simulates the volume-stats query itself
    erroring (transient) — distinct from a real empty result."""
    def _r(tenants):
        return [{"metric": {"tenant": t}, "value": [1, "1"]} for t in tenants]

    def _query(prom_url, promql):
        # declaring: the only query carrying BOTH user_threshold and volume-stats.
        if "user_threshold" in promql and "kubelet_volume_stats" in promql:
            return _r(declaring), None
        if "kubelet_volume_stats_available_bytes" in promql:
            return (None, arriving_err) if arriving_err else (_r(arriving), None)
        if "label_replace" in promql and "kube_pod_status_phase" in promql:
            return _r(running), None
        return [], None  # steps 1-3 benign
    return _query


class TestStep4DiskRecipePrereq:
    def _run(self, query_fn):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json",
                              side_effect=_mock_http_get_json({"rules": ({"data": {"groups": []}}, None)})):
                with patch.object(bc, "query_prometheus", side_effect=query_fn):
                    return bc.check_prometheus(_args())

    def _step4(self, checks):
        return next(c for c in checks if c["check"] == "step4_disk_recipe_prereq")

    def test_skip_when_no_disk_recipes(self):
        """No disk-fill recipe declared → step is N/A (skip), not a false alarm."""
        checks = self._run(_disk_query(declaring=[], arriving=[], running=[]))
        assert self._step4(checks)["status"] == "skip"

    def test_fail_platform_wide_no_volume_stats(self):
        """Disk recipes declared but ZERO tenant-attributed volume-stats arrive —
        the rollout-storm misconfiguration this step exists to catch."""
        checks = self._run(_disk_query(declaring=["db-a", "db-b"], arriving=[], running=["db-a", "db-b"]))
        c = self._step4(checks)
        assert c["status"] == "fail"
        assert "NO" in c["detail"]

    def test_warn_partial_missing_tenant(self):
        """Some tenants attributed, but a declaring+running tenant has none."""
        checks = self._run(_disk_query(declaring=["db-a", "db-b"], arriving=["db-a"], running=["db-a", "db-b"]))
        c = self._step4(checks)
        assert c["status"] == "warn"
        assert "db-b" in c["detail"]

    def test_pass_all_attributed(self):
        checks = self._run(_disk_query(declaring=["db-a"], arriving=["db-a"], running=["db-a"]))
        assert self._step4(checks)["status"] == "pass"

    def test_running_guard_excludes_unstarted_tenant(self):
        """db-b declared but NOT running yet → not flagged (running-pods guard),
        so a tenant mid-rollout with no workload up does not false-alarm."""
        checks = self._run(_disk_query(declaring=["db-a", "db-b"], arriving=["db-a"], running=["db-a"]))
        assert self._step4(checks)["status"] == "pass"

    def test_warn_not_fail_on_volume_stats_query_error(self):
        """A transient error on the volume-stats query must NOT be read as a real
        absence (false platform-wide fail) — degrade to advisory warn."""
        checks = self._run(_disk_query(declaring=["db-a"], arriving=[], running=["db-a"],
                                       arriving_err="query timeout"))
        c = self._step4(checks)
        assert c["status"] == "warn"
        assert "could not query" in c["detail"]

    def test_warn_not_fail_when_no_declaring_tenant_running_yet(self):
        """Onboarding window: disk recipe declared but the workload isn't deployed
        yet (no running pods) → advisory warn, NOT a false platform-wide fail (the
        running-pods guard must gate the fail, not just the partial-warn). Regression
        for the adversarial-review finding."""
        checks = self._run(_disk_query(declaring=["db-a", "db-b"], arriving=[], running=[]))
        c = self._step4(checks)
        assert c["status"] == "warn"
        assert "running pods yet" in c["detail"]

    def test_scope_mirrors_sentinel_exactly(self):
        """Step 4 MUST query the same scope as the CustomRecipeDiskInert sentinel —
        metric set (available_bytes OR used_bytes, exact, NO broad regex) AND db-.+
        namespace on the running leg — else onboarding/runtime split-brain (a non-db
        tenant the sentinel ignores would false-fail byo_check). Gemini adversarial
        finding; capture the issued PromQL and assert parity with rule-pack-kubernetes."""
        seen = []
        def _rec(prom_url, promql):
            seen.append(promql)
            if "user_threshold" in promql and "kubelet_volume_stats" in promql:
                return [{"metric": {"tenant": "db-a"}, "value": [1, "1"]}], None
            return [], None
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json",
                              side_effect=_mock_http_get_json({"rules": ({"data": {"groups": []}}, None)})):
                with patch.object(bc, "query_prometheus", side_effect=_rec):
                    bc.check_prometheus(_args())
        declaring = next(q for q in seen if "user_threshold" in q and "kubelet_volume_stats" in q)
        running = next(q for q in seen if "label_replace" in q and "kube_pod_status_phase" in q)
        # metric-set parity: exact-OR of available + used, NOT a broad regex.
        assert 'metric="kubelet_volume_stats_available_bytes"' in declaring
        assert 'metric="kubelet_volume_stats_used_bytes"' in declaring
        assert "=~" not in declaring
        # namespace-scope parity with the sentinel's pods-leg.
        assert 'namespace=~"db-.+"' in running


# ---------------------------------------------------------------------------
# Step 5: disk-IOPS-recipe prerequisite (#692 P0④) — container_fs scraped AND
# tenant-attributed; the codified FIDELITY GATE (blkio-bypass → fail-loud).
# ---------------------------------------------------------------------------
def _iops_query(declaring, arriving, running, arriving_err=None):
    """Mock query_prometheus for Step 5 scenarios. Earlier steps (incl Step 4's
    kubelet_volume_stats queries) return [] → Step 4 skips. The container_fs declaring
    query is matched before the container_fs_writes_total arriving substring."""
    def _r(tenants):
        return [{"metric": {"tenant": t}, "value": [1, "1"]} for t in tenants]

    def _query(prom_url, promql):
        if "user_threshold" in promql and "container_fs" in promql:
            return _r(declaring), None
        if "container_fs_writes_total" in promql:
            return (None, arriving_err) if arriving_err else (_r(arriving), None)
        if "label_replace" in promql and "kube_pod_status_phase" in promql:
            return _r(running), None
        return [], None  # steps 1-4 benign (no kubelet_volume_stats declared)
    return _query


class TestStep5DiskIopsRecipePrereq:
    def _run(self, query_fn):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json",
                              side_effect=_mock_http_get_json({"rules": ({"data": {"groups": []}}, None)})):
                with patch.object(bc, "query_prometheus", side_effect=query_fn):
                    return bc.check_prometheus(_args())

    def _step5(self, checks):
        return next(c for c in checks if c["check"] == "step5_disk_iops_recipe_prereq")

    def test_skip_when_no_iops_recipes(self):
        checks = self._run(_iops_query(declaring=[], arriving=[], running=[]))
        assert self._step5(checks)["status"] == "skip"

    def test_fail_blkio_bypass_no_container_fs(self):
        """The fidelity gate: IOPS recipes declared + pods running, but container_fs is
        0 (not scraped, or storage bypasses cgroup blkio — NFS/EFS) → fail-loud."""
        checks = self._run(_iops_query(declaring=["db-a", "db-b"], arriving=[], running=["db-a", "db-b"]))
        c = self._step5(checks)
        assert c["status"] == "fail"
        assert "blkio" in c["detail"]

    def test_warn_partial_missing_tenant(self):
        checks = self._run(_iops_query(declaring=["db-a", "db-b"], arriving=["db-a"], running=["db-a", "db-b"]))
        c = self._step5(checks)
        assert c["status"] == "warn"
        assert "db-b" in c["detail"]

    def test_pass_all_attributed(self):
        checks = self._run(_iops_query(declaring=["db-a"], arriving=["db-a"], running=["db-a"]))
        assert self._step5(checks)["status"] == "pass"

    def test_warn_not_fail_when_no_declaring_tenant_running_yet(self):
        checks = self._run(_iops_query(declaring=["db-a", "db-b"], arriving=[], running=[]))
        c = self._step5(checks)
        assert c["status"] == "warn"
        assert "running pods yet" in c["detail"]

    def test_warn_not_fail_on_container_fs_query_error(self):
        checks = self._run(_iops_query(declaring=["db-a"], arriving=[], running=["db-a"],
                                       arriving_err="query timeout"))
        c = self._step5(checks)
        assert c["status"] == "warn"
        assert "could not query" in c["detail"]


# ---------------------------------------------------------------------------
# --prometheus env-var fallback (add_prometheus_arg / README §6.1)
# ---------------------------------------------------------------------------
class TestPrometheusEnvFallback:
    """`--prometheus` resolves $PROMETHEUS_URL at the argparse layer.

    byo-check was previously hardcoded to http://localhost:9090 AND absent
    from the dispatcher's PROMETHEUS_COMMANDS, so a standalone run ignored
    $PROMETHEUS_URL entirely. add_prometheus_arg now resolves it as the
    argparse default (env → else localhost) for standalone + dispatcher.
    """

    def _run_main_capture_url(self, monkeypatch, argv):
        captured = {}

        def fake_check_prometheus(args):
            captured["url"] = args.prometheus
            return [{"check": "x", "status": "pass", "detail": ""}]

        monkeypatch.setattr(bc, "check_prometheus", fake_check_prometheus)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            bc.main()
        return captured["url"]

    def test_uses_env_when_flag_absent(self, monkeypatch):
        """--prometheus omitted + $PROMETHEUS_URL set → uses the env value."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._run_main_capture_url(
            monkeypatch, ["byo_check.py", "prometheus"])
        assert url == "http://test:1234"

    def test_falls_back_to_localhost_when_env_unset(self, monkeypatch):
        """--prometheus omitted + env unset → byte-identical old default."""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        url = self._run_main_capture_url(
            monkeypatch, ["byo_check.py", "prometheus"])
        assert url == "http://localhost:9090"

    def test_explicit_flag_overrides_env(self, monkeypatch):
        """Explicit --prometheus always wins over $PROMETHEUS_URL."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._run_main_capture_url(
            monkeypatch,
            ["byo_check.py", "prometheus", "--prometheus", "http://cli:9099"])
        assert url == "http://cli:9099"


# ---------------------------------------------------------------------------
# Pure judge helpers (da-tools ROI refactor W5). Each takes an already-fetched
# query result (+ error) and returns the check dict — no IO. These lock the
# per-step pass/warn/fail/skip boundaries, the caller_error tagging (transport
# failure → exit 2 downstream), and the detail-string formatting.
# ---------------------------------------------------------------------------
def _tenants(*names):
    return [{"metric": {"tenant": t}, "value": [1, "1"]} for t in names]


def _value(n):
    return [{"value": [1, str(n)]}]


class TestJudgeReachable:
    def test_pass_on_no_error(self):
        assert bc._judge_reachable(None) == {
            "check": "prometheus_reachable",
            "status": "pass",
            "detail": "Prometheus is healthy",
        }

    def test_fail_carries_caller_error(self):
        c = bc._judge_reachable(OSError("connection refused"))
        assert c["check"] == "prometheus_reachable"
        assert c["status"] == "fail"
        assert c["caller_error"] is True  # transport failure → exit 2
        assert c["detail"] == "Cannot reach Prometheus: connection refused"

    def test_detail_truncated_to_60(self):
        c = bc._judge_reachable(OSError("E" * 200))
        # "Cannot reach Prometheus: " + first 60 chars of str(err)
        assert c["detail"] == "Cannot reach Prometheus: " + "E" * 60


class TestJudgeStep1TenantLabel:
    def test_err_is_caller_error_fail(self):
        c = bc._judge_step1_tenant_label(None, "boom" * 40)
        assert c["status"] == "fail"
        assert c["caller_error"] is True
        assert c["detail"] == "Query failed: " + ("boom" * 40)[:60]

    def test_results_pass_sorted_and_capped(self):
        c = bc._judge_step1_tenant_label(_tenants("db-c", "db-a", "db-b"), None)
        assert c["status"] == "pass"
        assert "caller_error" not in c
        assert c["detail"] == "tenant label found on 3 tenant(s): db-a, db-b, db-c"

    def test_results_join_capped_at_10(self):
        many = ["db-%02d" % i for i in range(15)]
        c = bc._judge_step1_tenant_label(_tenants(*many), None)
        assert c["status"] == "pass"
        assert c["detail"].startswith("tenant label found on 15 tenant(s): ")
        # only the first 10 (sorted) tenant ids appear in the joined tail
        assert c["detail"].count("db-") == 10

    def test_empty_results_warn(self):
        c = bc._judge_step1_tenant_label([], None)
        assert c["status"] == "warn"
        assert "caller_error" not in c
        assert "relabel_configs" in c["detail"]


class TestJudgeStep2ThresholdExporterScrape:
    def test_err_caller_error(self):
        c = bc._judge_step2_threshold_exporter_scrape(None, "boom")
        assert c["status"] == "fail"
        assert c["caller_error"] is True

    def test_all_up_pass(self):
        c = bc._judge_step2_threshold_exporter_scrape(_value("1") + _value("1"), None)
        assert c["status"] == "pass"
        assert c["detail"] == "2 target(s), all UP"

    def test_some_down_warn(self):
        c = bc._judge_step2_threshold_exporter_scrape(_value("1") + _value("0"), None)
        assert c["status"] == "warn"
        assert c["detail"] == "2 target(s), some targets DOWN"

    def test_empty_fail_no_caller_error(self):
        c = bc._judge_step2_threshold_exporter_scrape([], None)
        assert c["status"] == "fail"
        assert "caller_error" not in c  # a missing scrape job is a real violation, not transport
        assert c["detail"] == "No threshold-exporter scrape job found"


class TestJudgeStep2UserThresholdMetrics:
    def test_err_caller_error(self):
        c = bc._judge_step2_user_threshold_metrics(None, "boom")
        assert c["status"] == "fail"
        assert c["caller_error"] is True

    def test_positive_count_pass(self):
        c = bc._judge_step2_user_threshold_metrics(_value("7"), None)
        assert c["status"] == "pass"
        assert c["detail"] == "7 user_threshold series found"

    def test_zero_count_warn(self):
        c = bc._judge_step2_user_threshold_metrics(_value("0"), None)
        assert c["status"] == "warn"
        assert c["detail"] == "0 user_threshold series found"

    def test_empty_fail(self):
        c = bc._judge_step2_user_threshold_metrics([], None)
        assert c["status"] == "fail"
        assert "caller_error" not in c


class TestJudgeStep3RulePacksLoaded:
    def test_err_caller_error(self):
        c = bc._judge_step3_rule_packs_loaded(None, "rules boom")
        assert c["status"] == "fail"
        assert c["caller_error"] is True
        assert c["detail"] == "Rules API failed: rules boom"

    def test_da_groups_pass(self):
        data = {"data": {"groups": [
            {"name": "mariadb-alerts", "rules": [{"name": "r1", "lastError": ""},
                                                 {"name": "r2", "lastError": ""}]},
        ]}}
        c = bc._judge_step3_rule_packs_loaded(data, None)
        assert c["status"] == "pass"
        assert c["detail"] == "1 rule groups, 2 rules"

    def test_eval_errors_warn(self):
        data = {"data": {"groups": [
            {"name": "redis-alerts", "rules": [{"name": "r1", "lastError": "kaboom"}]},
        ]}}
        c = bc._judge_step3_rule_packs_loaded(data, None)
        assert c["status"] == "warn"
        assert c["detail"] == "1 rule groups, 1 rules, 1 evaluation error(s)"

    def test_oversized_last_error_stays_out_of_detail(self):
        """A huge lastError must not leak into the check detail.

        The per-rule messages are collected internally with lastError[:40]
        truncation but only COUNTED in the emitted detail — lock that
        boundary so a refactor doesn't start dumping raw errors."""
        data = {"data": {"groups": [
            {"name": "redis-alerts", "rules": [{"name": "r1", "lastError": "E" * 200}]},
        ]}}
        c = bc._judge_step3_rule_packs_loaded(data, None)
        assert c["status"] == "warn"
        assert c["detail"] == "1 rule groups, 1 rules, 1 evaluation error(s)"
        assert "E" * 41 not in c["detail"]

    def test_no_da_groups_fail(self):
        data = {"data": {"groups": [{"name": "unrelated", "rules": []}]}}
        c = bc._judge_step3_rule_packs_loaded(data, None)
        assert c["status"] == "fail"
        assert "caller_error" not in c
        assert c["detail"] == "No Dynamic Alerting rule groups found"


class TestJudgeStep3bRecordingRulesOutput:
    def test_err_returns_none(self):
        """Query error → NO check appended (original had no else branch)."""
        assert bc._judge_step3b_recording_rules_output(None, "boom") is None

    def test_positive_count_pass(self):
        c = bc._judge_step3b_recording_rules_output(_value("5"), None)
        assert c["status"] == "pass"
        assert "5 tenant:* recording rule" in c["detail"]

    def test_zero_count_warn(self):
        c = bc._judge_step3b_recording_rules_output(_value("0"), None)
        assert c["status"] == "warn"

    def test_empty_results_warn(self):
        c = bc._judge_step3b_recording_rules_output([], None)
        assert c["status"] == "warn"
        assert "may not have evaluated yet" in c["detail"]


class TestJudgeE2eVectorMatching:
    def test_err_returns_none(self):
        assert bc._judge_e2e_vector_matching(None, "boom") is None

    def test_positive_count_pass(self):
        c = bc._judge_e2e_vector_matching(_value("3"), None)
        assert c["status"] == "pass"
        assert c["detail"] == "3 tenant(s) have threshold normalization output"

    def test_zero_count_warn(self):
        c = bc._judge_e2e_vector_matching(_value("0"), None)
        assert c["status"] == "warn"

    def test_empty_results_warn(self):
        c = bc._judge_e2e_vector_matching([], None)
        assert c["status"] == "warn"


class TestJudgeStep4DiskRecipePrereq:
    def test_err_caller_error(self):
        c = bc._judge_step4_disk_recipe_prereq(None, "boom", None, None, None)
        assert c["status"] == "fail"
        assert c["caller_error"] is True

    def test_no_declaring_skip(self):
        c = bc._judge_step4_disk_recipe_prereq([], None, None, None, None)
        assert c["status"] == "skip"
        assert "step N/A" in c["detail"]

    def test_arriving_err_warn(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a"), None, None, "vol timeout", _tenants("db-a"))
        assert c["status"] == "warn"
        assert "could not query volume-stats" in c["detail"]

    def test_no_running_candidates_warn(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a", "db-b"), None, [], None, [])
        assert c["status"] == "warn"
        assert "running pods yet" in c["detail"]

    def test_platform_wide_fail(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a", "db-b"), None, [], None, _tenants("db-a", "db-b"))
        assert c["status"] == "fail"
        assert "NO" in c["detail"]
        assert "caller_error" not in c  # a real absence, not a transport error

    def test_partial_missing_warn(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a", "db-b"), None, _tenants("db-a"), None, _tenants("db-a", "db-b"))
        assert c["status"] == "warn"
        assert "db-b" in c["detail"]

    def test_all_attributed_pass(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a"), None, _tenants("db-a"), None, _tenants("db-a"))
        assert c["status"] == "pass"

    def test_running_guard_excludes_unstarted(self):
        """db-b declared but not running → excluded from candidates → pass."""
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a", "db-b"), None, _tenants("db-a"), None, _tenants("db-a"))
        assert c["status"] == "pass"

    def test_arriving_err_truncated_to_50(self):
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants("db-a"), None, None, "E" * 200, _tenants("db-a"))
        assert c["status"] == "warn"
        assert c["detail"] == (
            "disk recipe(s) declared but could not query volume-stats: " + "E" * 50
        )

    def test_missing_join_capped_at_10(self):
        many = ["db-%02d" % i for i in range(16)]
        c = bc._judge_step4_disk_recipe_prereq(
            _tenants(*many), None, _tenants(many[0]), None, _tenants(*many))
        assert c["status"] == "warn"
        # 15 missing tenants but only the first 10 (sorted) are joined
        assert c["detail"].count("db-") == 10


class TestJudgeStep5DiskIopsRecipePrereq:
    def test_err_caller_error(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(None, "boom", None, None, None)
        assert c["status"] == "fail"
        assert c["caller_error"] is True

    def test_no_declaring_skip(self):
        c = bc._judge_step5_disk_iops_recipe_prereq([], None, None, None, None)
        assert c["status"] == "skip"
        assert "container_fs_*" in c["detail"]

    def test_arriving_err_warn(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a"), None, None, "cfs timeout", _tenants("db-a"))
        assert c["status"] == "warn"
        assert "could not query container_fs" in c["detail"]

    def test_no_running_candidates_warn(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a", "db-b"), None, [], None, [])
        assert c["status"] == "warn"
        assert "running pods yet" in c["detail"]

    def test_blkio_bypass_fail(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a", "db-b"), None, [], None, _tenants("db-a", "db-b"))
        assert c["status"] == "fail"
        assert "blkio" in c["detail"]

    def test_partial_missing_warn(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a", "db-b"), None, _tenants("db-a"), None, _tenants("db-a", "db-b"))
        assert c["status"] == "warn"
        assert "db-b" in c["detail"]

    def test_all_attributed_pass(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a"), None, _tenants("db-a"), None, _tenants("db-a"))
        assert c["status"] == "pass"

    def test_arriving_err_truncated_to_50(self):
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants("db-a"), None, None, "E" * 200, _tenants("db-a"))
        assert c["status"] == "warn"
        assert c["detail"] == (
            "IOPS recipe(s) declared but could not query container_fs: " + "E" * 50
        )

    def test_missing_join_capped_at_10(self):
        many = ["db-%02d" % i for i in range(16)]
        c = bc._judge_step5_disk_iops_recipe_prereq(
            _tenants(*many), None, _tenants(many[0]), None, _tenants(*many))
        assert c["status"] == "warn"
        # 15 missing tenants but only the first 10 (sorted) are joined
        assert c["detail"].count("db-") == 10


class TestCheckPrometheusControlFlow:
    """Orchestrator-level guards: the refactor must preserve the Step-0 early
    return and the append-nothing-on-error of Steps 3b / E2E."""

    def test_step0_unreachable_early_returns_single_element(self):
        """Prometheus down → exactly ONE check appended, then return (no later
        step runs). Regression for the early-return short-circuit."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            checks = bc.check_prometheus(_args())
        assert len(checks) == 1
        assert checks[0]["check"] == "prometheus_reachable"
        assert checks[0]["status"] == "fail"
        assert checks[0]["caller_error"] is True

    def test_step3b_and_e2e_omitted_on_query_error(self):
        """When every query errors, Steps 3b and E2E append no check dict — so
        their check names are absent from the result set."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        def _all_err(prom_url, promql):
            return None, "query error"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(bc, "http_get_json", side_effect=lambda url: (None, "err")):
                with patch.object(bc, "query_prometheus", side_effect=_all_err):
                    checks = bc.check_prometheus(_args())
        names = {c["check"] for c in checks}
        assert "step3_recording_rules_output" not in names
        assert "e2e_vector_matching" not in names
        # but the caller_error steps ARE present
        assert "step1_tenant_label" in names
        assert "step4_disk_recipe_prereq" in names
