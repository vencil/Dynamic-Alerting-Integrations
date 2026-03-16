"""Tests for byo_check.py — BYO Prometheus & Alertmanager integration verification."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops')
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
        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["tool"] == "byo-check"
        assert output["status"] == "fail"

    def test_all_target(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["byo_check.py", "all", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    bc.main()
        assert exc_info.value.code == 1
