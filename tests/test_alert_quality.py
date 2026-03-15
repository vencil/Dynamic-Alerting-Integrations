#!/usr/bin/env python3
"""test_alert_quality.py — alert_quality.py pytest 風格測試。

驗證:
  1. compute_noise_score() — 震盪分數計算
  2. compute_stale_score() — 閒置分數計算
  3. compute_resolution_latency() — 解決延遲計算
  4. compute_suppression_ratio() — 壓制比例計算
  5. compute_overall_grade() — 綜合評級
  6. compute_tenant_score() — 租戶綜合分數
  7. generate_report() — 報告生成
  8. generate_markdown() — Markdown 輸出
  9. print_text_report() — 文字輸出
  10. CLI — 參數解析
  11. Prometheus / Alertmanager query — mock 測試
"""

import json
import sys
import time
from unittest.mock import patch

import pytest

import alert_quality as aq  # noqa: E402


# ── compute_noise_score ─────────────────────────────────────────

class TestComputeNoiseScore:
    """震盪分數計算測試。"""

    @pytest.mark.parametrize("fire_count,period_days,expected_grade", [
        (0, 30, "GOOD"),
        (5, 30, "GOOD"),
        (10, 30, "WARN"),
        (15, 30, "WARN"),
        (20, 30, "BAD"),
        (50, 30, "BAD"),
    ], ids=["zero", "low", "medium-boundary", "medium", "high-boundary", "very-high"])
    def test_grades(self, fire_count, period_days, expected_grade):
        """各種 fire count 正確評級。"""
        _, grade = aq.compute_noise_score(fire_count, period_days)
        assert grade == expected_grade

    def test_normalization_to_30d(self):
        """15 天 10 次 → 標準化為 30 天 20 次 → BAD。"""
        normalized, grade = aq.compute_noise_score(10, 15)
        assert normalized == 20
        assert grade == "BAD"

    def test_zero_period(self):
        """零天期間不應除零。"""
        count, grade = aq.compute_noise_score(5, 0)
        assert count == 5
        assert grade == "GOOD"


# ── compute_stale_score ─────────────────────────────────────────

class TestComputeStaleScore:
    """閒置分數計算測試。"""

    def test_recently_fired(self):
        """最近 fire 過 → GOOD。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 3600, now, 30)
        assert days < 1
        assert grade == "GOOD"

    def test_stale_alert(self):
        """超過 14 天未 fire → WARN。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 20 * 86400, now, 30)
        assert days >= 14
        assert grade == "WARN"

    def test_never_fired(self):
        """從未 fire（last_fired_ts=0） → WARN。"""
        now = time.time()
        days, grade = aq.compute_stale_score(0, now, 30)
        assert days == 30.0
        assert grade == "WARN"

    def test_just_under_threshold(self):
        """剛好 13 天 → GOOD。"""
        now = time.time()
        days, grade = aq.compute_stale_score(now - 13 * 86400, now, 30)
        assert grade == "GOOD"


# ── compute_resolution_latency ──────────────────────────────────

class TestComputeResolutionLatency:
    """解決延遲計算測試。"""

    @pytest.mark.parametrize("durations,expected_grade", [
        ([], "GOOD"),
        ([3600, 7200, 1800], "GOOD"),
        ([60, 120, 90], "BAD"),
        ([100000, 200000], "WARN"),
    ], ids=["empty", "normal-range", "flapping", "slow-resolution"])
    def test_grades(self, durations, expected_grade):
        """各種解決延遲正確評級。"""
        _, grade = aq.compute_resolution_latency(durations)
        assert grade == expected_grade

    def test_avg_calculation(self):
        """平均值正確計算。"""
        avg, _ = aq.compute_resolution_latency([100, 200, 300])
        assert abs(avg - 200.0) < 0.01


# ── compute_suppression_ratio ──────────────────────────────────

class TestComputeSuppressionRatio:
    """壓制比例計算測試。"""

    @pytest.mark.parametrize("total,suppressed,expected_grade", [
        (0, 0, "GOOD"),
        (100, 10, "GOOD"),
        (100, 50, "WARN"),
        (100, 80, "WARN"),
        (10, 0, "GOOD"),
    ], ids=["no-alerts", "low-ratio", "boundary-50pct", "high-ratio", "zero-suppressed"])
    def test_grades(self, total, suppressed, expected_grade):
        """各種壓制比例正確評級。"""
        _, grade = aq.compute_suppression_ratio(total, suppressed)
        assert grade == expected_grade

    def test_ratio_value(self):
        """比例值正確計算。"""
        ratio, _ = aq.compute_suppression_ratio(100, 25)
        assert abs(ratio - 0.25) < 0.01


# ── compute_overall_grade ──────────────────────────────────────

class TestComputeOverallGrade:
    """綜合評級測試。"""

    def _make_metrics(self, noise="GOOD", stale="GOOD",
                      resolution="GOOD", suppression="GOOD"):
        m = aq.AlertQualityMetrics(alertname="test", tenant="t")
        m.noise_grade = noise
        m.stale_grade = stale
        m.resolution_grade = resolution
        m.suppression_grade = suppression
        return m

    def test_all_good(self):
        """全 GOOD → GOOD。"""
        assert aq.compute_overall_grade(self._make_metrics()) == "GOOD"

    def test_one_warn(self):
        """一項 WARN → WARN。"""
        m = self._make_metrics(noise="WARN")
        assert aq.compute_overall_grade(m) == "WARN"

    def test_two_warn_becomes_bad(self):
        """兩項 WARN → BAD。"""
        m = self._make_metrics(noise="WARN", stale="WARN")
        assert aq.compute_overall_grade(m) == "BAD"

    def test_one_bad(self):
        """一項 BAD → BAD。"""
        m = self._make_metrics(resolution="BAD")
        assert aq.compute_overall_grade(m) == "BAD"


# ── compute_tenant_score ────────────────────────────────────────

class TestComputeTenantScore:
    """租戶綜合分數測試。"""

    def test_empty_list(self):
        """空清單 → 100 分。"""
        assert aq.compute_tenant_score([]) == 100.0

    def test_all_good(self):
        """全 GOOD → 100 分。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="GOOD"),
            aq.AlertQualityMetrics(alertname="b", tenant="t", overall_grade="GOOD"),
        ]
        assert aq.compute_tenant_score(alerts) == 100.0

    def test_mixed_grades(self):
        """混合評級正確加權平均。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="GOOD"),
            aq.AlertQualityMetrics(alertname="b", tenant="t", overall_grade="BAD"),
        ]
        # (100 + 0) / 2 = 50
        assert aq.compute_tenant_score(alerts) == 50.0

    def test_all_bad(self):
        """全 BAD → 0 分。"""
        alerts = [
            aq.AlertQualityMetrics(alertname="a", tenant="t", overall_grade="BAD"),
        ]
        assert aq.compute_tenant_score(alerts) == 0.0


# ── generate_report ────────────────────────────────────────────

class TestGenerateReport:
    """報告生成測試。"""

    def test_empty_metrics(self):
        """空指標清單仍產生有效報告。"""
        report = aq.generate_report([], "30d")
        assert report.period == "30d"
        assert report.summary["total_tenants"] == 0
        assert report.summary["total_alertnames"] == 0

    def test_multi_tenant(self):
        """多租戶指標正確分組。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="HighConn", tenant="db-a", overall_grade="GOOD",
            ),
            aq.AlertQualityMetrics(
                alertname="HighCPU", tenant="db-b", overall_grade="WARN",
            ),
        ]
        report = aq.generate_report(metrics, "7d")
        assert report.summary["total_tenants"] == 2
        assert report.summary["good"] == 1
        assert report.summary["warn"] == 1

    def test_has_timestamp(self):
        """報告包含時間戳。"""
        report = aq.generate_report([], "30d")
        assert report.timestamp != ""

    def test_score_in_tenants(self):
        """每個 tenant 有 score 欄位。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        assert len(report.tenants) == 1
        assert report.tenants[0]["score"] == 100.0


# ── generate_markdown ──────────────────────────────────────────

class TestGenerateMarkdown:
    """Markdown 輸出測試。"""

    def test_contains_header(self):
        """Markdown 包含報告標題。"""
        report = aq.generate_report([], "30d")
        md = aq.generate_markdown(report)
        assert "## Alert Quality Report" in md

    def test_contains_table(self):
        """Markdown 包含表格。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        md = aq.generate_markdown(report)
        assert "| db-a |" in md

    def test_problem_details(self):
        """Markdown 包含問題警報詳細資訊。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="BadAlert", tenant="db-a",
                overall_grade="BAD", noise_grade="BAD",
                fire_count=30,
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        md = aq.generate_markdown(report)
        assert "`BadAlert`" in md
        assert "BAD" in md


# ── print_text_report ──────────────────────────────────────────

class TestPrintTextReport:
    """文字報告輸出測試。"""

    def test_contains_title(self, capsys):
        """報告包含標題。"""
        report = aq.generate_report([], "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "Alert Quality Report" in out or "警報品質評估報告" in out

    def test_shows_score(self, capsys):
        """報告顯示分數。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="a", tenant="db-a", overall_grade="GOOD",
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "100" in out

    def test_problem_alerts_shown(self, capsys):
        """問題警報顯示詳細資訊。"""
        metrics = [
            aq.AlertQualityMetrics(
                alertname="NoisyAlert", tenant="db-a",
                overall_grade="BAD", noise_grade="BAD",
                fire_count=50,
            ),
        ]
        report = aq.generate_report(metrics, "30d")
        aq.print_text_report(report)
        out = capsys.readouterr().out
        assert "NoisyAlert" in out
        assert "BAD" in out


# ── Prometheus query mock ──────────────────────────────────────

class TestQueryPrometheusAlerts:
    """Prometheus 查詢 mock 測試。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳結果。"""
        def mock_get(url, timeout=10):
            return {
                "status": "success",
                "data": {"result": [{"metric": {"alertname": "X"}, "values": [[1, "1"]]}]},
            }, None
        monkeypatch.setattr(aq, "http_get_json", mock_get)
        result = aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)
        assert len(result) == 1

    def test_error_returns_empty(self, monkeypatch):
        """HTTP 錯誤回傳空清單。"""
        monkeypatch.setattr(aq, "http_get_json", lambda url, timeout=10: (None, "refused"))
        result = aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)
        assert result == []

    def test_api_error(self, monkeypatch):
        """API 錯誤狀態回傳空清單。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=10: ({"status": "error"}, None))
        result = aq.query_prometheus_alerts("http://prom", "ALERTS", 86400)
        assert result == []


# ── Alertmanager query mock ────────────────────────────────────

class TestQueryAlertmanagerAlerts:
    """Alertmanager 查詢 mock 測試。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳清單。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=15: ([{"labels": {"alertname": "X"}}], None))
        result = aq.query_alertmanager_alerts("http://am")
        assert len(result) == 1

    def test_error_returns_empty(self, monkeypatch):
        """錯誤回傳空清單。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=15: (None, "refused"))
        result = aq.query_alertmanager_alerts("http://am")
        assert result == []


class TestQueryAlertmanagerSilences:
    """Alertmanager silence 查詢測試。"""

    def test_filters_active_only(self, monkeypatch):
        """只回傳 active 狀態的 silence。"""
        silences = [
            {"id": "1", "status": {"state": "active"}},
            {"id": "2", "status": {"state": "expired"}},
        ]
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=15: (silences, None))
        result = aq.query_alertmanager_silences("http://am")
        assert len(result) == 1
        assert result[0]["id"] == "1"


# ── analyze_from_prometheus ────────────────────────────────────

class TestAnalyzeFromPrometheus:
    """端到端分析 mock 測試。"""

    def test_basic_analysis(self, monkeypatch):
        """基本分析流程。"""
        now = time.time()
        values = [
            [now - 600, "0"], [now - 540, "1"], [now - 480, "1"],
            [now - 420, "0"], [now - 360, "1"], [now - 300, "0"],
        ]
        result_data = {
            "status": "success",
            "data": {"result": [{
                "metric": {"alertname": "HighConn", "tenant": "db-a"},
                "values": values,
            }]},
        }
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: (result_data, None))
        metrics = aq.analyze_from_prometheus("http://prom", 86400)
        assert len(metrics) == 1
        assert metrics[0].alertname == "HighConn"
        assert metrics[0].tenant == "db-a"
        assert metrics[0].fire_count >= 1

    def test_no_data(self, monkeypatch):
        """無資料回傳空清單。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: (None, "refused"))
        metrics = aq.analyze_from_prometheus("http://prom", 86400)
        assert metrics == []


# ── CLI ────────────────────────────────────────────────────────

class TestCLI:
    """CLI 參數測試。"""

    def test_parser_defaults(self):
        """預設參數正確。"""
        parser = aq.build_parser()
        args = parser.parse_args(["--prometheus", "http://prom"])
        assert args.prometheus == "http://prom"
        assert args.period == "30d"
        assert args.json_output is False
        assert args.ci is False
        assert args.min_score == 0

    def test_all_flags(self):
        """所有 flag 正確解析。"""
        parser = aq.build_parser()
        args = parser.parse_args([
            "--prometheus", "http://prom",
            "--alertmanager", "http://am",
            "--period", "7d",
            "--tenant", "db-a",
            "--json",
            "--ci",
            "--min-score", "80",
        ])
        assert args.alertmanager == "http://am"
        assert args.period == "7d"
        assert args.tenant == "db-a"
        assert args.json_output is True
        assert args.ci is True
        assert args.min_score == 80


# ── main() CLI entry ───────────────────────────────────────────

class TestMain:
    """main() CLI 進入點測試。"""

    def test_json_output(self, monkeypatch, capsys):
        """--json 輸出有效 JSON。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--json",
        ])
        aq.main()
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "summary" in data
        assert "tenants" in data

    def test_markdown_output(self, monkeypatch, capsys):
        """--markdown 輸出包含表格。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--markdown",
        ])
        aq.main()
        out = capsys.readouterr().out
        assert "## Alert Quality Report" in out

    def test_ci_mode_passes(self, monkeypatch):
        """CI 模式：無 BAD 時 exit code 0。"""
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"result": []}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--ci",
        ])
        # 應該正常結束，不 raise
        aq.main()

    def test_ci_mode_fails_on_bad(self, monkeypatch):
        """CI 模式：有 BAD 時 exit code 1。"""
        now = time.time()
        # 製造大量震盪 → BAD noise score
        values = []
        for i in range(100):
            values.append([now - (100 - i) * 60, str(i % 2)])  # 交替 0/1
        monkeypatch.setattr(aq, "http_get_json",
                            lambda url, timeout=30: ({"status": "success",
                                                      "data": {"result": [{
                                                          "metric": {"alertname": "Noisy", "tenant": "t"},
                                                          "values": values,
                                                      }]}}, None))
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--ci",
        ])
        with pytest.raises(SystemExit) as exc_info:
            aq.main()
        assert exc_info.value.code == 1

    def test_invalid_period_exits(self, monkeypatch):
        """無效 period 字串應 exit 1。"""
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom", "--period", "xyz",
        ])
        with pytest.raises(SystemExit) as exc_info:
            aq.main()
        assert exc_info.value.code == 1

    def test_tenant_filter(self, monkeypatch, capsys):
        """--tenant 篩選傳遞到查詢。"""
        queries_made = []

        def mock_get(url, timeout=30):
            queries_made.append(url)
            return {"status": "success", "data": {"result": []}}, None

        monkeypatch.setattr(aq, "http_get_json", mock_get)
        monkeypatch.setattr("sys.argv", [
            "alert_quality", "--prometheus", "http://prom",
            "--tenant", "db-a", "--json",
        ])
        aq.main()
        assert any("db-a" in q for q in queries_made)
