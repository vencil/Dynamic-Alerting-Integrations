#!/usr/bin/env python3
"""Tests for alert_correlate.py — 告警關聯分析引擎。"""

import json
import os
import sys
import time

import pytest

# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))

import alert_correlate as ac  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_alert(name="TestAlert", tenant="db-a", severity="warning",
                namespace="db-a", starts_at=1000.0, ends_at=1300.0,
                labels=None):
    """Helper to create AlertEvent."""
    return ac.AlertEvent(
        alertname=name, tenant=tenant, severity=severity,
        namespace=namespace, starts_at=starts_at, ends_at=ends_at,
        labels=labels or {},
    )


@pytest.fixture
def sample_alerts():
    """Create a realistic set of alerts for testing."""
    base = 1700000000.0
    return [
        _make_alert("MariaDB_HighConnections", "db-a", "warning",
                     "db-a", base, base + 600),
        _make_alert("MariaDB_SlowQueries", "db-a", "warning",
                     "db-a", base + 60, base + 600),
        _make_alert("MariaDB_HighConnections", "db-b", "critical",
                     "db-b", base + 30, base + 600),
        # Unrelated alert far in the future
        _make_alert("Redis_MemoryHigh", "db-c", "warning",
                     "db-c", base + 7200, base + 7500),
    ]


# ---------------------------------------------------------------------------
# TestAlertEvent
# ---------------------------------------------------------------------------

class TestAlertEvent:
    def test_basic_construction(self):
        a = _make_alert()
        assert a.alertname == "TestAlert"
        assert a.tenant == "db-a"
        assert a.severity == "warning"

    def test_from_alertmanager(self):
        raw = {
            "labels": {
                "alertname": "HighCPU",
                "tenant": "db-a",
                "severity": "critical",
                "namespace": "db-a",
            },
            "startsAt": "2026-03-15T10:00:00Z",
            "endsAt": "2026-03-15T10:05:00Z",
        }
        alert = ac.AlertEvent.from_alertmanager(raw)
        assert alert.alertname == "HighCPU"
        assert alert.tenant == "db-a"
        assert alert.severity == "critical"
        assert alert.starts_at > 0

    def test_from_alertmanager_missing_fields(self):
        raw = {"labels": {}}
        alert = ac.AlertEvent.from_alertmanager(raw)
        assert alert.alertname == "unknown"
        assert alert.tenant == ""

    def test_from_alertmanager_namespace_fallback(self):
        """When tenant is missing, fall back to namespace."""
        raw = {
            "labels": {"alertname": "X", "namespace": "my-ns"},
            "startsAt": "",
        }
        alert = ac.AlertEvent.from_alertmanager(raw)
        assert alert.tenant == "my-ns"


# ---------------------------------------------------------------------------
# TestParseIsoTimestamp
# ---------------------------------------------------------------------------

class TestParseIsoTimestamp:
    def test_utc_z(self):
        ts = ac._parse_iso_timestamp("2026-03-15T10:00:00Z")
        assert ts > 0

    def test_with_offset(self):
        ts = ac._parse_iso_timestamp("2026-03-15T10:00:00+08:00")
        assert ts > 0

    def test_with_fractional(self):
        ts = ac._parse_iso_timestamp("2026-03-15T10:00:00.123456Z")
        assert ts > 0

    def test_empty_string(self):
        assert ac._parse_iso_timestamp("") == 0.0

    def test_invalid_string(self):
        assert ac._parse_iso_timestamp("not-a-date") == 0.0


# ---------------------------------------------------------------------------
# TestTimeOverlap
# ---------------------------------------------------------------------------

class TestTimeOverlap:
    def test_full_overlap(self):
        """Identical ranges = 1.0."""
        assert ac._time_overlap(100, 200, 100, 200) == 1.0

    def test_no_overlap(self):
        """Non-overlapping ranges = 0.0."""
        assert ac._time_overlap(100, 200, 300, 400) == 0.0

    def test_partial_overlap(self):
        """50% overlap."""
        score = ac._time_overlap(100, 200, 150, 250)
        assert 0.4 <= score <= 0.6

    def test_zero_start(self):
        """Zero start time returns 0.0."""
        assert ac._time_overlap(0, 200, 100, 200) == 0.0

    def test_zero_length_treated_as_firing(self):
        """Zero-length (end==start) is treated as still-firing → uses now."""
        # When end <= start, the code substitutes datetime.now(),
        # so the interval is actually [100, now] which overlaps [100, 200].
        result = ac._time_overlap(100, 100, 100, 200)
        assert result > 0.0  # overlap exists because "still firing"


# ---------------------------------------------------------------------------
# TestTimeWindowCluster
# ---------------------------------------------------------------------------

class TestTimeWindowCluster:
    def test_empty_input(self):
        assert ac.time_window_cluster([]) == []

    def test_single_alert(self):
        """Single alert creates no cluster (need 2+)."""
        alerts = [_make_alert(starts_at=100)]
        assert ac.time_window_cluster(alerts) == []

    def test_two_overlapping(self):
        """Two alerts within window form one cluster."""
        alerts = [
            _make_alert("A", starts_at=100, ends_at=200),
            _make_alert("B", starts_at=200, ends_at=400),
        ]
        clusters = ac.time_window_cluster(alerts, window_secs=300)
        assert len(clusters) == 1
        assert clusters[0].alert_count == 2

    def test_two_non_overlapping(self):
        """Two alerts far apart form no cluster."""
        alerts = [
            _make_alert("A", starts_at=100, ends_at=200),
            _make_alert("B", starts_at=10000, ends_at=10100),
        ]
        clusters = ac.time_window_cluster(alerts, window_secs=300)
        assert len(clusters) == 0

    def test_three_chained(self):
        """Three alerts where A→B→C chain within window."""
        alerts = [
            _make_alert("A", starts_at=100),
            _make_alert("B", starts_at=200),
            _make_alert("C", starts_at=350),
        ]
        clusters = ac.time_window_cluster(alerts, window_secs=300)
        assert len(clusters) == 1
        assert clusters[0].alert_count == 3

    def test_sample_alerts(self, sample_alerts):
        """Realistic alerts: 3 MariaDB close together, 1 Redis far away."""
        clusters = ac.time_window_cluster(sample_alerts, window_secs=300)
        assert len(clusters) == 1
        assert clusters[0].alert_count == 3


# ---------------------------------------------------------------------------
# TestCorrelationScore
# ---------------------------------------------------------------------------

class TestCorrelationScore:
    def test_identical_alerts(self):
        a = _make_alert("X", "db-a", "warning", "db-a", 100, 200)
        score = ac.compute_correlation_score(a, a)
        assert score >= 0.8

    def test_same_namespace_diff_name(self):
        a = _make_alert("A", "db-a", "warning", "ns-1", 100, 200)
        b = _make_alert("B", "db-a", "warning", "ns-1", 100, 200)
        score = ac.compute_correlation_score(a, b)
        # time overlap + namespace + severity = 0.4 + 0.3 + 0.1 = 0.8
        assert score >= 0.7

    def test_different_everything(self):
        a = _make_alert("X", "db-a", "warning", "ns-1", 100, 200)
        b = _make_alert("Y", "db-b", "critical", "ns-2", 5000, 5100)
        score = ac.compute_correlation_score(a, b)
        assert score < 0.3

    def test_same_prefix(self):
        """Alerts with same prefix score higher."""
        a = _make_alert("MariaDB_High", "db-a", "warning", "ns-1", 100, 200)
        b = _make_alert("MariaDB_Slow", "db-a", "warning", "ns-1", 100, 200)
        score = ac.compute_correlation_score(a, b)
        assert score >= 0.8  # overlap + ns + prefix + severity

    def test_score_range(self):
        """Score is always in [0, 1]."""
        a = _make_alert(starts_at=100, ends_at=200)
        b = _make_alert(starts_at=100, ends_at=200)
        score = ac.compute_correlation_score(a, b)
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# TestRootCauseInference
# ---------------------------------------------------------------------------

class TestRootCauseInference:
    def test_earliest_critical_wins(self):
        c = ac.CorrelationCluster(
            cluster_id=0, window_start=100, window_end=400,
            alerts=[
                _make_alert("Late", severity="critical", starts_at=200),
                _make_alert("Early", severity="critical", starts_at=100),
                _make_alert("Warn", severity="warning", starts_at=50),
            ],
        )
        ac.infer_root_cause(c)
        assert c.root_cause.alertname == "Early"  # earliest critical

    def test_higher_severity_wins(self):
        c = ac.CorrelationCluster(
            cluster_id=0, window_start=100, window_end=400,
            alerts=[
                _make_alert("Warn", severity="warning", starts_at=100),
                _make_alert("Crit", severity="critical", starts_at=150),
            ],
        )
        ac.infer_root_cause(c)
        assert c.root_cause.alertname == "Crit"

    def test_empty_cluster(self):
        c = ac.CorrelationCluster(
            cluster_id=0, window_start=0, window_end=0)
        ac.infer_root_cause(c)
        assert c.root_cause is None


# ---------------------------------------------------------------------------
# TestAnalyzeAlerts
# ---------------------------------------------------------------------------

class TestAnalyzeAlerts:
    def test_full_pipeline(self, sample_alerts):
        clusters = ac.analyze_alerts(sample_alerts, window_secs=300)
        assert len(clusters) >= 1
        for c in clusters:
            assert c.root_cause is not None

    def test_no_alerts(self):
        clusters = ac.analyze_alerts([])
        assert clusters == []


# ---------------------------------------------------------------------------
# TestBuildReport
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_report_structure(self, sample_alerts):
        clusters = ac.analyze_alerts(sample_alerts)
        report = ac.build_report(clusters, len(sample_alerts))
        assert "timestamp" in report
        assert report["total_alerts"] == 4
        assert report["cluster_count"] >= 1
        assert isinstance(report["clusters"], list)

    def test_empty_report(self):
        report = ac.build_report([], 0)
        assert report["cluster_count"] == 0
        assert report["clusters"] == []

    def test_cluster_fields(self, sample_alerts):
        clusters = ac.analyze_alerts(sample_alerts)
        report = ac.build_report(clusters, len(sample_alerts))
        c = report["clusters"][0]
        assert "cluster_id" in c
        assert "alert_count" in c
        assert "tenant_count" in c
        assert "root_cause" in c
        assert "alerts" in c
        assert "avg_correlation" in c


# ---------------------------------------------------------------------------
# TestOutputFormatting
# ---------------------------------------------------------------------------

class TestOutputFormatting:
    @pytest.fixture
    def report(self, sample_alerts):
        clusters = ac.analyze_alerts(sample_alerts)
        return ac.build_report(clusters, len(sample_alerts))

    def test_text_format(self, report):
        text = ac.format_text_report(report)
        assert "Alert Correlation Report" in text
        assert "Cluster #" in text
        assert "Root Cause" in text

    def test_text_empty(self):
        report = ac.build_report([], 0)
        text = ac.format_text_report(report)
        assert "No correlated" in text

    def test_json_format(self, report):
        text = ac.format_json_report(report)
        data = json.loads(text)
        assert data["cluster_count"] >= 1

    def test_markdown_format(self, report):
        text = ac.format_markdown_report(report)
        assert "# Alert Correlation Report" in text
        assert "| Severity |" in text


# ---------------------------------------------------------------------------
# TestLoadAlerts
# ---------------------------------------------------------------------------

class TestLoadAlerts:
    def test_load_from_json_array(self, tmp_path):
        data = [
            {"labels": {"alertname": "A", "tenant": "db-a"},
             "startsAt": "2026-03-15T10:00:00Z",
             "endsAt": "2026-03-15T10:05:00Z"},
        ]
        p = tmp_path / "alerts.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        alerts = ac.load_alerts_from_json(str(p))
        assert len(alerts) == 1
        assert alerts[0].alertname == "A"

    def test_load_from_json_wrapped(self, tmp_path):
        data = {"data": [
            {"labels": {"alertname": "B"}, "startsAt": ""},
        ]}
        p = tmp_path / "alerts.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        alerts = ac.load_alerts_from_json(str(p))
        assert len(alerts) == 1

    def test_load_from_json_invalid(self, tmp_path):
        p = tmp_path / "alerts.json"
        p.write_text('{"unexpected": true}', encoding="utf-8")
        alerts = ac.load_alerts_from_json(str(p))
        assert alerts == []


# ---------------------------------------------------------------------------
# TestCLI
# ---------------------------------------------------------------------------

class TestCLI:
    def test_parser_defaults(self):
        parser = ac.build_parser()
        args = parser.parse_args([])
        assert args.window == "5m"
        assert args.min_score == 0.3

    def test_main_with_input_file(self, monkeypatch, capsys, tmp_path):
        data = [
            {"labels": {"alertname": "A", "tenant": "db-a", "severity": "warning",
                         "namespace": "db-a"},
             "startsAt": "2026-03-15T10:00:00Z",
             "endsAt": "2026-03-15T10:05:00Z"},
            {"labels": {"alertname": "B", "tenant": "db-a", "severity": "warning",
                         "namespace": "db-a"},
             "startsAt": "2026-03-15T10:01:00Z",
             "endsAt": "2026-03-15T10:06:00Z"},
        ]
        p = tmp_path / "alerts.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "alert_correlate", "--input", str(p), "--json",
        ])
        ac.main()
        out = capsys.readouterr().out
        report = json.loads(out)
        assert report["total_alerts"] == 2

    def test_main_no_alerts(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "alert_correlate", "--prometheus", "http://fake:9090",
        ])
        monkeypatch.setattr(ac, "load_alerts_from_alertmanager",
                            lambda *a, **k: [])
        ac.main()
        out = capsys.readouterr().out
        assert "No correlated" in out

    def test_main_invalid_window(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", [
            "alert_correlate", "--window", "invalid",
        ])
        with pytest.raises(SystemExit) as exc_info:
            ac.main()
        assert exc_info.value.code == 1

    def test_main_ci_mode_critical(self, monkeypatch, tmp_path):
        """CI mode exits 1 when critical root cause found."""
        data = [
            {"labels": {"alertname": "A", "tenant": "db-a",
                         "severity": "critical", "namespace": "ns"},
             "startsAt": "2026-03-15T10:00:00Z",
             "endsAt": "2026-03-15T10:05:00Z"},
            {"labels": {"alertname": "B", "tenant": "db-b",
                         "severity": "critical", "namespace": "ns"},
             "startsAt": "2026-03-15T10:01:00Z",
             "endsAt": "2026-03-15T10:06:00Z"},
        ]
        p = tmp_path / "alerts.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "alert_correlate", "--input", str(p), "--ci",
        ])
        with pytest.raises(SystemExit) as exc_info:
            ac.main()
        assert exc_info.value.code == 1

    def test_main_markdown(self, monkeypatch, capsys, tmp_path):
        data = [
            {"labels": {"alertname": "A", "tenant": "db-a"},
             "startsAt": "2026-03-15T10:00:00Z", "endsAt": ""},
            {"labels": {"alertname": "B", "tenant": "db-a"},
             "startsAt": "2026-03-15T10:01:00Z", "endsAt": ""},
        ]
        p = tmp_path / "alerts.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "alert_correlate", "--input", str(p), "--markdown",
        ])
        ac.main()
        out = capsys.readouterr().out
        assert "# Alert Correlation Report" in out
