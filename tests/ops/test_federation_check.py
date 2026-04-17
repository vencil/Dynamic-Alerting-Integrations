"""Tests for federation_check.py — Multi-cluster federation integration verification."""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import federation_check as fc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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
    This helper converts them to query_prometheus's (results, error) format.
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


def _mock_urlopen_ok():
    """Return a mock urlopen context manager that succeeds."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"OK"
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# query_prometheus
# ---------------------------------------------------------------------------
class TestQueryPrometheus:
    """Tests via monkeypatch — query_prometheus is now an alias to _lib_python.query_prometheus_instant."""

    def test_success(self, monkeypatch):
        fake = lambda prom_url, promql: ([{"metric": {}, "value": [1, "42"]}], None)
        monkeypatch.setattr(fc, "query_prometheus", fake)
        results, err = fc.query_prometheus("http://prom:9090", "up")
        assert err is None
        assert len(results) == 1

    def test_error(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "timeout")
        monkeypatch.setattr(fc, "query_prometheus", fake)
        results, err = fc.query_prometheus("http://prom:9090", "up")
        assert results is None
        assert "timeout" in err

    def test_non_success_status(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "parse error")
        monkeypatch.setattr(fc, "query_prometheus", fake)
        results, err = fc.query_prometheus("http://prom:9090", "bad{")
        assert results is None
        assert "parse error" in err


# ---------------------------------------------------------------------------
# check_edge
# ---------------------------------------------------------------------------
class TestCheckEdge:
    def test_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            checks = fc.check_edge("http://edge:9090")
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"
        assert checks[0]["check"] == "edge_prometheus_reachable"

    def test_all_pass(self):
        mock_resp = _mock_urlopen_ok()
        mock_federate = MagicMock()
        mock_federate.read.return_value = b"metric1{} 1\nmetric2{} 2\n"
        mock_federate.__enter__ = lambda s: s
        mock_federate.__exit__ = MagicMock(return_value=False)

        def mock_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if "federate" in url:
                return mock_federate
            return mock_resp

        http_mapping = {
            "config": ({"data": {"yaml": "global:\n  external_labels:\n    cluster: edge-1"}}, None),
        }
        prom_mapping = {
            "tenant": (
                {"status": "success", "data": {"result": [
                    {"metric": {"tenant": "db-a"}, "value": [1, "1"]},
                ]}}, None),
        }

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with patch.object(fc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(fc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = fc.check_edge("http://edge:9090")

        statuses = {c["check"]: c["status"] for c in checks}
        assert statuses["edge_prometheus_reachable"] == "pass"
        assert statuses["edge_external_labels"] == "pass"
        assert statuses["edge_tenant_label"] == "pass"
        assert statuses["edge_federate_endpoint"] == "pass"

    def test_no_external_labels(self):
        mock_resp = _mock_urlopen_ok()

        http_mapping = {
            "config": ({"data": {"yaml": "global:\n  scrape_interval: 15s"}}, None),
        }
        prom_mapping = {
            "tenant": ({"status": "success", "data": {"result": []}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(fc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(fc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = fc.check_edge("http://edge:9090")

        ext_check = next(c for c in checks if c["check"] == "edge_external_labels")
        assert ext_check["status"] == "fail"

    def test_external_labels_without_cluster(self):
        mock_resp = _mock_urlopen_ok()

        http_mapping = {
            "config": ({"data": {"yaml": "global:\n  external_labels:\n    env: prod"}}, None),
        }
        prom_mapping = {
            "tenant": ({"status": "success", "data": {"result": []}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(fc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(fc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = fc.check_edge("http://edge:9090")

        ext_check = next(c for c in checks if c["check"] == "edge_external_labels")
        assert ext_check["status"] == "warn"


# ---------------------------------------------------------------------------
# check_central
# ---------------------------------------------------------------------------
class TestCheckCentral:
    def test_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            checks = fc.check_central("http://central:9090")
        assert len(checks) == 1
        assert checks[0]["status"] == "fail"

    def test_all_pass(self):
        mock_resp = _mock_urlopen_ok()

        prom_mapping = {
            "cluster": (
                {"status": "success", "data": {"result": [
                    {"metric": {"cluster": "edge-1"}, "value": [1, "3"]},
                ]}}, None),
            "user_threshold": (
                {"status": "success", "data": {"result": [
                    {"value": [1, "10"]},
                ]}}, None),
            "tenant:": (
                {"status": "success", "data": {"result": [
                    {"value": [1, "5"]},
                ]}}, None),
        }
        http_mapping = {
            "rules": (
                {"data": {"groups": [
                    {"name": "test-alerts", "rules": [
                        {"name": "TestAlert", "alerts": [{"labels": {}}]},
                    ]},
                ]}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(fc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(fc, "query_prometheus", side_effect=_mock_query_prometheus(prom_mapping)):
                    checks = fc.check_central("http://central:9090")

        statuses = {c["check"]: c["status"] for c in checks}
        assert statuses["central_prometheus_reachable"] == "pass"
        assert statuses["central_edge_metrics"] == "pass"

    def test_no_cluster_label_fallback(self):
        mock_resp = _mock_urlopen_ok()

        def mock_query(prom_url, promql):
            if "cluster" in promql:
                return [], None
            if "count" in promql and "up" in promql:
                return [{"value": [1, "5"]}], None
            if "user_threshold" in promql:
                return [], None
            if "tenant" in promql:
                return [], None
            return None, "no match"

        http_mapping = {
            "rules": ({"data": {"groups": []}}, None),
        }

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(fc, "http_get_json", side_effect=_mock_http_get_json(http_mapping)):
                with patch.object(fc, "query_prometheus", side_effect=mock_query):
                    checks = fc.check_central("http://central:9090")

        edge_check = next(c for c in checks if c["check"] == "central_edge_metrics")
        assert edge_check["status"] == "warn"
        assert "single-cluster" in edge_check["detail"]


# ---------------------------------------------------------------------------
# check_e2e
# ---------------------------------------------------------------------------
class TestCheckE2E:
    def test_combines_edge_and_central(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch.object(fc, "http_get_json", return_value=(None, "refused")):
                with patch.object(fc, "query_prometheus", return_value=(None, "refused")):
                    checks = fc.check_e2e("http://central:9090", ["http://edge-1:9090"])

        check_names = [c["check"] for c in checks]
        # Should have edge checks with prefixed names
        assert any("edge(" in n for n in check_names)
        # Should have central checks
        assert any("central" in n for n in check_names)

    def test_skips_empty_urls(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch.object(fc, "http_get_json", return_value=(None, "refused")):
                with patch.object(fc, "query_prometheus", return_value=(None, "refused")):
                    checks = fc.check_e2e("http://central:9090", ["", "  "])

        # Should only have central checks (empty edge URLs skipped)
        check_names = [c["check"] for c in checks]
        assert not any("edge(" in n for n in check_names)


# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------
class TestFormatOutput:
    def test_text_output(self, capsys):
        checks = [
            {"check": "test_pass", "status": "pass", "detail": "ok"},
            {"check": "test_fail", "status": "fail", "detail": "bad"},
            {"check": "test_warn", "status": "warn", "detail": "maybe"},
        ]
        fc.format_output("edge", checks)
        captured = capsys.readouterr()
        assert "EDGE" in captured.out
        assert "1/3 passed" in captured.out
        assert "✓" in captured.out
        assert "✗" in captured.out
        assert "⚠" in captured.out


# ---------------------------------------------------------------------------
# main (CLI)
# ---------------------------------------------------------------------------
class TestMain:
    def test_edge_json(self, capsys):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["federation_check.py", "edge", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    fc.main()
        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["tool"] == "federation-check"
        assert output["section"] == "edge"

    def test_central_json(self, capsys):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["federation_check.py", "central", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    fc.main()
        assert exc_info.value.code == 1

    def test_e2e_requires_edge_urls(self, capsys):
        with patch("sys.argv", ["federation_check.py", "e2e"]):
            with pytest.raises(SystemExit) as exc_info:
                fc.main()
        assert exc_info.value.code == 1
        assert "edge-urls" in capsys.readouterr().err.lower()

    def test_e2e_with_urls(self, capsys):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch.object(fc, "http_get_json", return_value=(None, "refused")):
                with patch("sys.argv", [
                    "federation_check.py", "e2e",
                    "--edge-urls", "http://e1:9090,http://e2:9090",
                    "--json",
                ]):
                    with pytest.raises(SystemExit) as exc_info:
                        fc.main()
        assert exc_info.value.code == 1
        output = json.loads(capsys.readouterr().out)
        assert output["section"] == "e2e"
