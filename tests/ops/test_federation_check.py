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
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


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
        # #452/#737: unreachable edge Prometheus = transport caller-error
        assert exc_info.value.code == EXIT_CALLER_ERROR
        output = json.loads(capsys.readouterr().out)
        assert output["tool"] == "federation-check"
        assert output["section"] == "edge"

    def test_central_json(self, capsys):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            with patch("sys.argv", ["federation_check.py", "central", "--json"]):
                with pytest.raises(SystemExit) as exc_info:
                    fc.main()
        # #452/#737: unreachable central Prometheus = transport caller-error
        assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_e2e_requires_edge_urls(self, capsys):
        with patch("sys.argv", ["federation_check.py", "e2e"]):
            with pytest.raises(SystemExit) as exc_info:
                fc.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
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
        # #452/#737: unreachable edges / transport failures = caller-error
        assert exc_info.value.code == EXIT_CALLER_ERROR
        output = json.loads(capsys.readouterr().out)
        assert output["section"] == "e2e"


# ---------------------------------------------------------------------------
# --prometheus env-var fallback (add_prometheus_arg / README §6.1)
# ---------------------------------------------------------------------------
class TestPrometheusEnvFallback:
    """`--prometheus` resolves $PROMETHEUS_URL at the argparse layer.

    federation-check was previously hardcoded to http://localhost:9090 AND
    absent from the dispatcher's PROMETHEUS_COMMANDS, so a standalone run
    (`python3 federation_check.py ...`) ignored $PROMETHEUS_URL entirely.
    add_prometheus_arg now resolves it as the argparse default (env → else
    localhost), so the same env-var works standalone and via da-tools.
    """

    def _run_main_capture_url(self, monkeypatch, argv):
        captured = {}

        def fake_check_edge(prom_url):
            captured["url"] = prom_url
            return [{"check": "x", "status": "pass", "detail": ""}]

        monkeypatch.setattr(fc, "check_edge", fake_check_edge)
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit):
            fc.main()
        return captured["url"]

    def test_uses_env_when_flag_absent(self, monkeypatch):
        """--prometheus omitted + $PROMETHEUS_URL set → uses the env value."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._run_main_capture_url(
            monkeypatch, ["federation_check.py", "edge"])
        assert url == "http://test:1234"

    def test_falls_back_to_localhost_when_env_unset(self, monkeypatch):
        """--prometheus omitted + env unset → byte-identical old default."""
        monkeypatch.delenv("PROMETHEUS_URL", raising=False)
        url = self._run_main_capture_url(
            monkeypatch, ["federation_check.py", "edge"])
        assert url == "http://localhost:9090"

    def test_explicit_flag_overrides_env(self, monkeypatch):
        """Explicit --prometheus always wins over $PROMETHEUS_URL."""
        monkeypatch.setenv("PROMETHEUS_URL", "http://test:1234")
        url = self._run_main_capture_url(
            monkeypatch,
            ["federation_check.py", "edge", "--prometheus", "http://cli:9099"])
        assert url == "http://cli:9099"


# ---------------------------------------------------------------------------
# #3133: answers are read by the lib — only HTTP is faked, so the shape check
# and the count reading run for real; a query not answered is a failed check.
# ---------------------------------------------------------------------------
def _vector(*samples):
    return ({"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"cluster": f"c{i}"}, "value": [1, v]} for i, v in enumerate(samples)]}},
        None)


def _central(answer_for):
    """check_central with every query answered by ``answer_for(promql)``."""
    import contextlib
    import urllib.parse as up
    import _lib_prometheus

    def _get(url, **_kw):
        if "/api/v1/rules" in url:
            return {"status": "success", "data": {"groups": []}}, None
        return answer_for(up.parse_qs(up.urlsplit(url).query).get("query", [""])[0])
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("urllib.request.urlopen", return_value=_mock_urlopen_ok()))
        stack.enter_context(patch.object(fc, "http_get_json", side_effect=_get))
        stack.enter_context(patch.object(_lib_prometheus, "http_get_json", side_effect=_get))
        return {c["check"]: c for c in fc.check_central("http://prom:9090")}


class TestQueryNotAnsweredIsACheck:
    def test_the_fallback_query_error_is_a_caller_error(self):
        """No cluster label, then the fallback errors: it used to be dropped
        and read as "No tenant-labeled metrics found" — a finding, rc 1."""
        c = _central(lambda q: (None, "HTTP 503") if q == 'count(up{tenant!=""})'
                     else _vector())["central_edge_metrics"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)
        assert c["detail"] == "Query failed: HTTP 503"

    def test_the_recording_rules_query_error_is_a_check(self):
        """It used to append nothing."""
        c = _central(lambda q: (None, "HTTP 503") if "tenant:.*" in q
                     else _vector("1"))["central_recording_rules"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)

    @pytest.mark.parametrize("value", ["NaN", "+Inf"])
    def test_a_count_that_is_not_finite_is_a_caller_error(self, value):
        """It used to crash on int(float('nan'))."""
        c = _central(lambda q: _vector(value) if q == "count(user_threshold)"
                     else _vector("1"))["central_threshold_exporter"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)

    def test_a_malformed_answer_is_a_caller_error(self):
        bad = ({"status": "success", "data": {"resultType": "matrix", "result": []}}, None)
        c = _central(lambda q: bad if q == "count(user_threshold)"
                     else _vector("1"))["central_threshold_exporter"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)
        assert "resultType is 'matrix'" in c["detail"]

    def test_the_cross_cluster_query_error_is_a_check(self):
        """e2e: it used to append nothing."""
        with patch.object(fc, "check_edge", return_value=[]), \
                patch.object(fc, "check_central", return_value=[]), \
                patch.object(fc, "query_prometheus", return_value=(None, "HTTP 503")):
            checks = fc.check_e2e("http://prom:9090", ["http://edge:9090"])
        assert checks == [{"check": "e2e_cross_cluster_matching", "status": "fail",
                           "caller_error": True, "detail": "Query failed: HTTP 503"}]

    def test_the_fallback_error_ends_the_run_at_rc2(self, monkeypatch):
        """Was rc 1 (#3133)."""
        monkeypatch.setattr(sys, "argv", ["federation_check", "central", "--json"])
        with patch.object(fc, "check_central", return_value=list(_central(
                lambda q: (None, "HTTP 503") if q == 'count(up{tenant!=""})'
                else _vector()).values())):
            with pytest.raises(SystemExit) as exc:
                fc.main()
        assert exc.value.code == EXIT_CALLER_ERROR
