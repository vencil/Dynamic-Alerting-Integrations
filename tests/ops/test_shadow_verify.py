"""Tests for shadow_verify.py — Shadow Monitoring readiness and convergence verification."""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import shadow_verify as sv  # noqa: E402
import _lib_prometheus  # noqa: E402  (#3133: queries are read by the lib)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _args(**kwargs):
    defaults = {
        "mapping": None,
        "report_csv": None,
        "readiness_json": None,
        "prometheus": "http://localhost:9090",
        "alertmanager": None,  # the CLI default: not given (#3133)
        "json": False,
        "phase": "all",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _write_mapping(tmp_path, content):
    p = tmp_path / "prefix-mapping.yaml"
    p.write_text(yaml.dump(content), encoding="utf-8")
    return str(p)


def _write_csv(tmp_path, rows, name="report.csv"):
    p = tmp_path / name
    with open(p, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Tenant", "Status", "Timestamp"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return str(p)


def _write_readiness(tmp_path, data):
    p = tmp_path / "cutover-readiness.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# query_prometheus
# ---------------------------------------------------------------------------
class TestQueryPrometheus:
    """Test query_prometheus via direct replacement to avoid sys.modules aliasing."""

    def test_success(self, monkeypatch):
        data = {"status": "success", "data": {"result": [{"value": [0, "42"]}]}}
        fake = lambda prom_url, promql: (data.get("data", {}).get("result", []), None)
        monkeypatch.setattr(sv, "query_prometheus", fake)
        results, err = sv.query_prometheus("http://prom:9090", "up")
        assert err is None
        assert results[0]["value"][1] == "42"

    def test_http_error(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "connection refused")
        monkeypatch.setattr(sv, "query_prometheus", fake)
        results, err = sv.query_prometheus("http://prom:9090", "up")
        assert results is None
        assert "connection refused" in err

    def test_non_success_status(self, monkeypatch):
        fake = lambda prom_url, promql: (None, "bad query")
        monkeypatch.setattr(sv, "query_prometheus", fake)
        results, err = sv.query_prometheus("http://prom:9090", "bad{")
        assert results is None
        assert "bad query" in err


# ---------------------------------------------------------------------------
# run_cmd
# ---------------------------------------------------------------------------
class TestRunCmd:
    def test_requires_list(self):
        with pytest.raises(TypeError, match="list argument"):
            sv.run_cmd("echo hello")

    def test_success(self):
        result = sv.run_cmd(["echo", "hello"])
        assert result == "hello"


# ---------------------------------------------------------------------------
# check_preflight
# ---------------------------------------------------------------------------
class TestCheckPreflight:
    def test_mapping_file_found(self, tmp_path):
        mapping = {"metric_a": {"original_metric": "old_a"}, "metric_b": {"original_metric": "old_b"}}
        mp = _write_mapping(tmp_path, mapping)
        args = _args(mapping=mp)

        with patch.object(_lib_prometheus, "http_get_json", return_value=(None, "unreachable")):
            with patch("urllib.request.urlopen", side_effect=OSError("fail")):
                checks = sv.check_preflight(args)

        mapping_check = next(c for c in checks if c["check"] == "mapping_file")
        assert mapping_check["status"] == "pass"
        assert "2 comparison pairs" in mapping_check["detail"]

    def test_mapping_file_missing(self, tmp_path):
        args = _args(mapping=str(tmp_path / "nonexistent.yaml"))
        with patch.object(_lib_prometheus, "http_get_json", return_value=(None, "unreachable")):
            with patch("urllib.request.urlopen", side_effect=OSError("fail")):
                checks = sv.check_preflight(args)

        mapping_check = next(c for c in checks if c["check"] == "mapping_file")
        assert mapping_check["status"] == "fail"

    def test_no_mapping_provided(self):
        args = _args(mapping=None)
        with patch.object(_lib_prometheus, "http_get_json", return_value=(None, "unreachable")):
            with patch("urllib.request.urlopen", side_effect=OSError("fail")):
                checks = sv.check_preflight(args)

        mapping_check = next(c for c in checks if c["check"] == "mapping_file")
        assert mapping_check["status"] == "skip"

    def test_prometheus_healthy(self):
        args = _args()
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"Prometheus Server is Healthy."
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        prom_data = {"status": "success", "data": {"result": [{"value": [0, "5"]}]}}

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(_lib_prometheus, "http_get_json", return_value=(prom_data, None)):
                checks = sv.check_preflight(args)

        health_check = next(c for c in checks if c["check"] == "prometheus_healthy")
        assert health_check["status"] == "pass"

    def test_prometheus_unreachable(self):
        args = _args()
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            with patch.object(_lib_prometheus, "http_get_json", return_value=(None, "unreachable")):
                checks = sv.check_preflight(args)

        health_check = next(c for c in checks if c["check"] == "prometheus_healthy")
        assert health_check["status"] == "fail"


# ---------------------------------------------------------------------------
# check_runtime
# ---------------------------------------------------------------------------
class TestCheckRuntime:
    def test_csv_no_mismatches(self, tmp_path):
        rows = [
            {"Tenant": "db-a", "Status": "match", "Timestamp": "2026-03-15 10:00:00"},
            {"Tenant": "db-b", "Status": "match", "Timestamp": "2026-03-15 10:00:00"},
        ]
        csv_path = _write_csv(tmp_path, rows)
        args = _args(report_csv=csv_path)

        with patch.object(sv, "query_prometheus", return_value=([], None)):
            checks = sv.check_runtime(args)

        ratio = next(c for c in checks if c["check"] == "csv_mismatch_ratio")
        assert ratio["status"] == "pass"
        assert "0/2" in ratio["detail"]

    def test_csv_with_mismatches(self, tmp_path):
        rows = [
            {"Tenant": "db-a", "Status": "mismatch", "Timestamp": "2026-03-15 10:00:00"},
            {"Tenant": "db-a", "Status": "match", "Timestamp": "2026-03-15 10:00:00"},
        ]
        csv_path = _write_csv(tmp_path, rows)
        args = _args(report_csv=csv_path)

        with patch.object(sv, "query_prometheus", return_value=([], None)):
            checks = sv.check_runtime(args)

        ratio = next(c for c in checks if c["check"] == "csv_mismatch_ratio")
        assert ratio["status"] == "fail"
        assert "1/2" in ratio["detail"]

    def test_csv_tenant_coverage(self, tmp_path):
        rows = [
            {"Tenant": "db-a", "Status": "match", "Timestamp": "2026-03-15 10:00:00"},
            {"Tenant": "db-b", "Status": "match", "Timestamp": "2026-03-15 10:00:00"},
        ]
        csv_path = _write_csv(tmp_path, rows)
        args = _args(report_csv=csv_path)

        with patch.object(sv, "query_prometheus", return_value=([], None)):
            checks = sv.check_runtime(args)

        coverage = next(c for c in checks if c["check"] == "csv_tenant_coverage")
        assert coverage["status"] == "pass"
        assert "2 tenants" in coverage["detail"]

    def test_csv_file_missing(self, tmp_path):
        args = _args(report_csv=str(tmp_path / "nonexistent.csv"))

        with patch.object(sv, "query_prometheus", return_value=([], None)):
            checks = sv.check_runtime(args)

        skip = next((c for c in checks if c["check"] == "csv_report"), None)
        assert skip is not None
        assert skip["status"] == "skip"

    def test_maintenance_mode_detected(self, tmp_path):
        args = _args(report_csv=None)
        maint_results = [{"metric": {"tenant": "db-a"}, "value": [0, "1"]}]

        with patch.object(sv, "query_prometheus") as mock_qp:
            mock_qp.side_effect = [
                (maint_results, None),  # maintenance query
                ([], None),             # silent mode query
            ]
            checks = sv.check_runtime(args)

        maint = next(c for c in checks if c["check"] == "maintenance_mode")
        assert maint["status"] == "warn"
        assert "db-a" in maint["detail"]

    def test_no_maintenance_mode(self, tmp_path):
        args = _args(report_csv=None)

        with patch.object(sv, "query_prometheus", return_value=([], None)):
            checks = sv.check_runtime(args)

        maint = next(c for c in checks if c["check"] == "maintenance_mode")
        assert maint["status"] == "pass"


# ---------------------------------------------------------------------------
# check_convergence
# ---------------------------------------------------------------------------
class TestCheckConvergence:
    def test_readiness_ready(self, tmp_path):
        rj = _write_readiness(tmp_path, {
            "ready": True,
            "convergence_percentage": 100,
            "unconverged_pairs": [],
        })
        args = _args(readiness_json=rj, report_csv=None)
        checks = sv.check_convergence(args)

        readiness = next(c for c in checks if c["check"] == "cutover_readiness_json")
        assert readiness["status"] == "pass"
        assert "READY" in readiness["detail"]

    def test_readiness_not_ready(self, tmp_path):
        rj = _write_readiness(tmp_path, {
            "ready": False,
            "convergence_percentage": 85,
            "unconverged_pairs": ["metric_a", "metric_b"],
        })
        args = _args(readiness_json=rj, report_csv=None)
        checks = sv.check_convergence(args)

        readiness = next(c for c in checks if c["check"] == "cutover_readiness_json")
        assert readiness["status"] == "fail"
        assert "2 pairs" in readiness["detail"]

    def test_readiness_file_missing(self, tmp_path):
        args = _args(readiness_json=str(tmp_path / "missing.json"), report_csv=None)
        checks = sv.check_convergence(args)

        readiness = next(c for c in checks if c["check"] == "cutover_readiness_json")
        assert readiness["status"] == "skip"

    def test_seven_day_zero_mismatch(self, tmp_path):
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            {"Tenant": "db-a", "Status": "match", "Timestamp": recent_ts},
            {"Tenant": "db-b", "Status": "match", "Timestamp": recent_ts},
        ]
        csv_path = _write_csv(tmp_path, rows)
        args = _args(report_csv=csv_path, readiness_json=None)
        checks = sv.check_convergence(args)

        zero_check = next(c for c in checks if c["check"] == "seven_day_zero_mismatch")
        assert zero_check["status"] == "pass"
        assert "0 mismatches" in zero_check["detail"]

    def test_seven_day_has_mismatches(self, tmp_path):
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            {"Tenant": "db-a", "Status": "mismatch", "Timestamp": recent_ts},
            {"Tenant": "db-a", "Status": "match", "Timestamp": recent_ts},
        ]
        csv_path = _write_csv(tmp_path, rows)
        args = _args(report_csv=csv_path, readiness_json=None)
        checks = sv.check_convergence(args)

        zero_check = next(c for c in checks if c["check"] == "seven_day_zero_mismatch")
        assert zero_check["status"] == "fail"
        assert "1 mismatches" in zero_check["detail"]


# ---------------------------------------------------------------------------
# format_output
# ---------------------------------------------------------------------------
class TestFormatOutput:
    def test_json_output(self):
        checks = [{"check": "test_check", "status": "pass", "detail": "ok"}]
        result = sv.format_output("preflight", checks, json_output=True)
        assert result["phase"] == "preflight"
        assert result["checks"] == checks

    def test_text_output(self, capsys):
        checks = [
            {"check": "test_pass", "status": "pass", "detail": "all good"},
            {"check": "test_fail", "status": "fail", "detail": "broken"},
        ]
        result = sv.format_output("runtime", checks, json_output=False)
        assert result is None
        captured = capsys.readouterr()
        assert "RUNTIME" in captured.out
        assert "1/2 passed" in captured.out


# ---------------------------------------------------------------------------
# main CLI
# ---------------------------------------------------------------------------
class TestMainCLI:
    def test_json_output_all_phases(self, tmp_path, capsys):
        mapping = {"m": {"original_metric": "old_m"}}
        mp = _write_mapping(tmp_path, mapping)
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        csv_path = _write_csv(tmp_path, [
            {"Tenant": "db-a", "Status": "match", "Timestamp": recent_ts}
        ])
        rj = _write_readiness(tmp_path, {"ready": True, "convergence_percentage": 100, "unconverged_pairs": []})

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"OK"
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        prom_data = {"status": "success", "data": {"result": [{"value": [0, "5"]}]}}

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(_lib_prometheus, "http_get_json", return_value=(prom_data, None)):
                with patch.object(sv, "query_prometheus", return_value=([], None)):
                    with patch("sys.argv", ["shadow_verify.py", "all",
                                            "--mapping", mp,
                                            "--report-csv", csv_path,
                                            "--readiness-json", rj,
                                            "--json"]):
                        with pytest.raises(SystemExit) as exc_info:
                            sv.main()

        # Should exit 0 (all pass) or at least produce JSON
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["tool"] == "shadow-verify"
        assert len(output["phases"]) == 3


# ---------------------------------------------------------------------------
# #3133: answers are read by the lib — only HTTP is faked, so the shape check
# and the count reading run for real; a query not answered is a failed check.
# ---------------------------------------------------------------------------
def _vector(*samples):
    return ({"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": {"tenant": f"db-{i}"}, "value": [1, v]} for i, v in enumerate(samples)]}},
        None)


def _run(check, answer_for):
    import contextlib
    import urllib.parse as up
    import _lib_prometheus

    def _get(url, **_kw):
        if "/api/v2/status" in url:
            return {"config": {"original": "migration_status"}}, None
        return answer_for(up.parse_qs(up.urlsplit(url).query).get("query", [""])[0])
    healthy = MagicMock()
    healthy.read.return_value = b"Prometheus is Healthy."
    healthy.__enter__ = lambda s: s
    healthy.__exit__ = MagicMock(return_value=False)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("urllib.request.urlopen", return_value=healthy))
        stack.enter_context(patch.object(_lib_prometheus, "http_get_json", side_effect=_get))
        return {c["check"]: c for c in check(_args())}


class TestQueryNotAnsweredIsACheck:
    @pytest.mark.parametrize("query,check", [
        ('user_state_filter{filter="maintenance"}', "maintenance_mode"),
        ("user_silent_mode", "silent_mode"),
    ])
    def test_a_mode_query_error_is_a_caller_error(self, query, check):
        """It used to append nothing."""
        c = _run(sv.check_runtime, lambda q: (None, "HTTP 503") if q == query
                 else _vector())[check]
        assert (c["status"], c.get("caller_error")) == ("fail", True)
        assert c["detail"] == "Query failed: HTTP 503"

    @pytest.mark.parametrize("value", ["NaN", "+Inf"])
    def test_a_count_that_is_not_finite_is_a_caller_error(self, value):
        """It used to crash on int(float('nan'))."""
        c = _run(sv.check_preflight, lambda q: _vector(value) if "shadow" in q
                 else _vector("3"))["shadow_rules_present"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)

    def test_a_malformed_answer_is_a_caller_error(self):
        bad = ({"status": "success", "data": {"resultType": "vector",
                                              "result": [{"value": "3"}]}}, None)
        c = _run(sv.check_preflight, lambda q: bad if "tenant:.*" in q
                 else _vector("3"))["recording_rules_loaded"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)

    def test_an_empty_count_still_reads_as_zero(self):
        c = _run(sv.check_preflight, lambda q: _vector())["recording_rules_loaded"]
        assert (c["status"], c["detail"]) == ("warn", "0 tenant:* recording rule metric names found")


@pytest.mark.parametrize("marker,check", [
    ("tenant:.*", "recording_rules_loaded"),
    ("shadow", "shadow_rules_present"),
])
@pytest.mark.parametrize("value", ["NaN", "+Inf"])
def test_every_count_refuses_a_non_finite_count(marker, check, value):
    """#3133: both count positions are a named caller_error, not a crash."""
    c = _run(sv.check_preflight, lambda q: _vector(value) if marker in q
             else _vector("3"))[check]
    assert (c["status"], c.get("caller_error")) == ("fail", True)


class TestAlertmanagerIsReadOnlyWhenGiven:
    """#3133 (owner 2026-10-23): an optional source counts only when given."""

    @pytest.mark.parametrize("am", [(None, "HTTP 503"), ([], None),
                                    ({"config": {"original": 5}}, None)],
                             ids=["unreachable", "not-an-object", "original-not-string"])
    def test_a_given_alertmanager_not_answering_is_a_caller_error(self, monkeypatch, am):
        seen = []

        def get(url, **_kw):
            seen.append(url)
            return am if "/api/v2/status" in url else _vector("3")
        args = _args(alertmanager="http://am:9093")
        monkeypatch.setattr(_lib_prometheus, "http_get_json", get)
        with patch("urllib.request.urlopen", side_effect=OSError("fail")):
            c = {c["check"]: c for c in sv.check_preflight(args)}["alertmanager_shadow_route"]
        assert (c["status"], c.get("caller_error")) == ("fail", True)
        # the URL given is the one read — not the default (review of fc7596cd3)
        assert [u for u in seen if "/api/v2/status" in u] == ["http://am:9093/api/v2/status"]

    def test_the_default_alertmanager_not_answering_is_a_skip(self, monkeypatch):
        seen = []

        def get(url, **_kw):
            seen.append(url)
            return (None, "refused") if "/api/v2/status" in url else _vector("3")
        monkeypatch.setattr(_lib_prometheus, "http_get_json", get)
        with patch("urllib.request.urlopen", side_effect=OSError("fail")):
            c = {c["check"]: c for c in sv.check_preflight(_args())}["alertmanager_shadow_route"]
        assert c["status"] == "skip"
        assert "http://localhost:9093/api/v2/status" in seen

    def test_a_given_alertmanager_with_the_route_passes(self, monkeypatch):
        def get(url, **_kw):
            return (({"config": {"original": "route: {matchers: [migration_status=shadow]}"}}, None)
                    if url == "http://am:9093/api/v2/status" else
                    (None, "refused") if "/api/v2/status" in url else _vector("3"))
        monkeypatch.setattr(_lib_prometheus, "http_get_json", get)
        with patch("urllib.request.urlopen", side_effect=OSError("fail")):
            c = {c["check"]: c for c in sv.check_preflight(_args(alertmanager="http://am:9093"))}
        assert c["alertmanager_shadow_route"]["status"] == "pass"
