"""
test_cardinality_forecasting.py — 基數預測工具測試。

覆蓋範圍：
- 線性回歸（純 Python 實作）
- 趨勢分類 / 風險分類
- 觸頂天數計算
- Tenant 分析
- 報告生成（text/JSON/Markdown）
- Prometheus 查詢 mock
- CLI 整合
"""
import json
import os
import sys
import time
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops"))

import cardinality_forecasting as cf


# ═══════════════════════════════════════════════════════════════════
# TestLinearRegression
# ═══════════════════════════════════════════════════════════════════

class TestLinearRegression:
    """純 Python 線性回歸測試。"""

    def test_perfect_positive_slope(self):
        """完美正斜率線性關係。"""
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 20.0, 30.0, 40.0, 50.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert abs(slope - 10.0) < 0.01
        assert abs(intercept - 10.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_flat_line(self):
        """水平線（斜率 0）。"""
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [50.0, 50.0, 50.0, 50.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert abs(slope) < 0.01
        assert abs(intercept - 50.0) < 0.01

    def test_negative_slope(self):
        """負斜率。"""
        xs = [0.0, 1.0, 2.0, 3.0]
        ys = [100.0, 90.0, 80.0, 70.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert slope < 0
        assert abs(slope - (-10.0)) < 0.01

    def test_single_point(self):
        """單一資料點 → 斜率 0。"""
        xs = [5.0]
        ys = [42.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert slope == 0.0
        assert intercept == 42.0

    def test_two_points(self):
        """兩個資料點 → 完美擬合。"""
        xs = [0.0, 10.0]
        ys = [100.0, 200.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert abs(slope - 10.0) < 0.01
        assert abs(intercept - 100.0) < 0.01
        assert abs(r_sq - 1.0) < 0.01

    def test_noisy_data(self):
        """有雜訊的資料。"""
        xs = [0.0, 1.0, 2.0, 3.0, 4.0]
        ys = [10.0, 22.0, 28.0, 42.0, 48.0]
        slope, intercept, r_sq = cf.linear_regression(xs, ys)
        assert slope > 0
        assert 0.9 < r_sq <= 1.0

    def test_empty_lists(self):
        """空清單 → 安全處理。"""
        slope, intercept, r_sq = cf.linear_regression([], [])
        assert slope == 0.0


# ═══════════════════════════════════════════════════════════════════
# TestClassifyTrend
# ═══════════════════════════════════════════════════════════════════

class TestClassifyTrend:
    """趨勢分類測試。"""

    @pytest.mark.parametrize("slope,expected", [
        (5.0, "growing"),
        (1.0, "growing"),
        (0.5, "stable"),
        (0.3, "stable"),
        (0.0, "stable"),
        (-0.3, "stable"),
        (-0.5, "stable"),
        (-1.0, "declining"),
        (-5.0, "declining"),
    ], ids=[
        "fast-grow", "slow-grow", "edge-stable-pos", "near-zero-pos",
        "zero", "near-zero-neg", "edge-stable-neg",
        "slow-decline", "fast-decline",
    ])
    def test_trend(self, slope, expected):
        """正確分類成長趨勢。"""
        assert cf.classify_trend(slope) == expected


# ═══════════════════════════════════════════════════════════════════
# TestClassifyRisk
# ═══════════════════════════════════════════════════════════════════

class TestClassifyRisk:
    """風險分類測試。"""

    @pytest.mark.parametrize("current,dtl,warn,limit,expected", [
        (500, None, 7, 500, "critical"),     # At limit
        (600, None, 7, 500, "critical"),     # Over limit
        (400, 5.0, 7, 500, "critical"),      # Within warn_days
        (400, 7.0, 7, 500, "critical"),      # At warn_days boundary
        (300, 14.0, 7, 500, "warning"),      # Within 3x warn_days
        (410, None, 7, 500, "warning"),      # >80% capacity
        (200, 100.0, 7, 500, "safe"),        # Far from limit
        (200, None, 7, 500, "safe"),         # Not growing
        (100, None, 7, 500, "safe"),         # Low cardinality
    ], ids=[
        "at-limit", "over-limit", "within-warn", "at-warn-boundary",
        "within-3x-warn", "80pct-capacity",
        "far-from-limit", "not-growing", "low-cardinality",
    ])
    def test_risk(self, current, dtl, warn, limit, expected):
        """正確分類風險等級。"""
        assert cf.classify_risk(current, dtl, warn, limit) == expected


# ═══════════════════════════════════════════════════════════════════
# TestComputeDaysToLimit
# ═══════════════════════════════════════════════════════════════════

class TestComputeDaysToLimit:
    """觸頂天數計算測試。"""

    @pytest.mark.parametrize("current,slope,limit,expected", [
        (400.0, 10.0, 500, 10.0),
        (0.0, 50.0, 500, 10.0),
        (490.0, 1.0, 500, 10.0),
        (500.0, 10.0, 500, 0.0),     # Already at limit
        (600.0, 10.0, 500, 0.0),     # Over limit
        (200.0, 0.0, 500, None),     # Not growing
        (200.0, -5.0, 500, None),    # Declining
    ], ids=[
        "normal", "from-zero", "near-limit",
        "at-limit", "over-limit",
        "flat", "declining",
    ])
    def test_days_to_limit(self, current, slope, limit, expected):
        """正確計算觸頂天數。"""
        result = cf.compute_days_to_limit(current, slope, limit)
        assert result == expected


# ═══════════════════════════════════════════════════════════════════
# TestAnalyzeTenant
# ═══════════════════════════════════════════════════════════════════

class TestAnalyzeTenant:
    """單一 tenant 分析測試。"""

    def test_growing_tenant(self):
        """穩定增長的 tenant。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        ts = [(now - 10 * day + i * day, 100.0 + i * 10.0) for i in range(11)]
        forecast = cf.analyze_tenant("db-a", ts, limit=500, warn_days=7)
        assert forecast.tenant == "db-a"
        assert forecast.current_cardinality == 200
        assert forecast.slope_per_day > 0
        assert forecast.trend == "growing"
        assert forecast.data_points == 11

    def test_stable_tenant(self):
        """穩定的 tenant（基數不變）。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        ts = [(now - 10 * day + i * day, 100.0) for i in range(11)]
        forecast = cf.analyze_tenant("db-b", ts, limit=500, warn_days=7)
        assert forecast.trend == "stable"
        assert forecast.risk_level == "safe"
        assert forecast.days_to_limit is None

    def test_empty_time_series(self):
        """空時序 → safe 預設。"""
        forecast = cf.analyze_tenant("empty", [], limit=500, warn_days=7)
        assert forecast.current_cardinality == 0
        assert forecast.risk_level == "safe"
        assert forecast.data_points == 0

    def test_critical_tenant(self):
        """即將觸頂的 tenant。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        # 480 at day 0, growing by ~5/day → 3-4 days to 500
        ts = [(now - 5 * day + i * day, 460.0 + i * 5.0) for i in range(6)]
        forecast = cf.analyze_tenant("db-crit", ts, limit=500, warn_days=7)
        assert forecast.current_cardinality == 485
        assert forecast.risk_level in ("critical", "warning")

    def test_declining_tenant(self):
        """基數下降的 tenant。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        ts = [(now - 5 * day + i * day, 300.0 - i * 20.0) for i in range(6)]
        forecast = cf.analyze_tenant("db-dec", ts, limit=500, warn_days=7)
        assert forecast.trend == "declining"
        assert forecast.days_to_limit is None


# ═══════════════════════════════════════════════════════════════════
# TestGenerateForecast
# ═══════════════════════════════════════════════════════════════════

class TestGenerateForecast:
    """預測報告生成測試。"""

    def test_multi_tenant(self):
        """多 tenant 報告。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        data = {
            "db-a": [(now - 5 * day + i * day, 100.0 + i * 10.0) for i in range(6)],
            "db-b": [(now - 5 * day + i * day, 50.0) for i in range(6)],
        }
        report = cf.generate_forecast(data, limit=500, warn_days=7)
        assert len(report.tenants) == 2
        assert report.cardinality_limit == 500

    def test_tenant_filter(self):
        """Tenant 過濾。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        data = {
            "db-a": [(now - day + i * 3600, 100.0) for i in range(24)],
            "db-b": [(now - day + i * 3600, 200.0) for i in range(24)],
        }
        report = cf.generate_forecast(data, tenant_filter="db-a")
        assert len(report.tenants) == 1
        assert report.tenants[0].tenant == "db-a"

    def test_empty_data(self):
        """空資料。"""
        report = cf.generate_forecast({})
        assert len(report.tenants) == 0
        assert report.safe_count == 0


# ═══════════════════════════════════════════════════════════════════
# TestReports
# ═══════════════════════════════════════════════════════════════════

class TestReports:
    """報告格式測試。"""

    @pytest.fixture
    def sample_report(self):
        return cf.ForecastReport(
            tenants=[
                cf.TenantForecast(
                    tenant="db-a", current_cardinality=300,
                    cardinality_limit=500, slope_per_day=5.0,
                    intercept=250.0, r_squared=0.95,
                    days_to_limit=40.0, predicted_date="2026-04-23",
                    trend="growing", risk_level="safe", data_points=30,
                ),
                cf.TenantForecast(
                    tenant="db-b", current_cardinality=480,
                    cardinality_limit=500, slope_per_day=10.0,
                    intercept=450.0, r_squared=0.98,
                    days_to_limit=2.0, predicted_date="2026-03-16",
                    trend="growing", risk_level="critical", data_points=30,
                ),
            ],
            generated_at="2026-03-14 12:00:00",
            lookback_days=30,
            cardinality_limit=500,
            warn_days=7,
        )

    def test_text_report_en(self, sample_report):
        """英文文字報告。"""
        text = cf.generate_text_report(sample_report, "en")
        assert "Cardinality Forecast Report" in text
        assert "db-a" in text
        assert "db-b" in text
        assert "Critical: 1" in text
        assert "Safe: 1" in text

    def test_text_report_zh(self, sample_report):
        """中文文字報告。"""
        text = cf.generate_text_report(sample_report, "zh")
        assert "基數預測報告" in text
        assert "危急: 1" in text

    def test_text_report_empty(self):
        """空報告。"""
        report = cf.ForecastReport()
        text = cf.generate_text_report(report, "en")
        assert "No tenant data available" in text

    def test_json_report(self, sample_report):
        """JSON 報告結構。"""
        data = cf.generate_json_report(sample_report)
        assert data["cardinality_limit"] == 500
        assert data["summary"]["critical"] == 1
        assert data["summary"]["safe"] == 1
        assert len(data["tenants"]) == 2
        assert data["tenants"][0]["tenant"] == "db-a"

    def test_markdown_report(self, sample_report):
        """Markdown 報告。"""
        md = cf.generate_markdown(sample_report)
        assert "# Cardinality Forecast Report" in md
        assert "| db-a |" in md
        assert "| db-b |" in md
        assert "critical" in md

    def test_text_at_limit(self):
        """已觸頂的 tenant 報告。"""
        report = cf.ForecastReport(
            tenants=[cf.TenantForecast(
                tenant="full", current_cardinality=500,
                cardinality_limit=500, slope_per_day=5.0,
                intercept=480.0, r_squared=0.9,
                days_to_limit=0.0, predicted_date=None,
                trend="growing", risk_level="critical", data_points=10,
            )],
            generated_at="now",
        )
        text = cf.generate_text_report(report, "en")
        assert "Limit reached!" in text

    def test_text_not_projected(self):
        """不會觸頂的 tenant。"""
        report = cf.ForecastReport(
            tenants=[cf.TenantForecast(
                tenant="safe", current_cardinality=100,
                cardinality_limit=500, slope_per_day=-1.0,
                intercept=120.0, r_squared=0.8,
                days_to_limit=None, predicted_date=None,
                trend="declining", risk_level="safe", data_points=10,
            )],
            generated_at="now",
        )
        text = cf.generate_text_report(report, "en")
        assert "not projected to reach" in text


# ═══════════════════════════════════════════════════════════════════
# TestQueryPrometheus
# ═══════════════════════════════════════════════════════════════════

class TestQueryPrometheus:
    """Prometheus 查詢 mock 測試。"""

    @patch("cardinality_forecasting.http_get_json")
    def test_query_range_success(self, mock_get):
        """正常查詢回傳 per-tenant 資料。"""
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"tenant": "db-a"},
                        "values": [[1000, "100"], [2000, "110"], [3000, "120"]],
                    },
                    {
                        "metric": {"tenant": "db-b"},
                        "values": [[1000, "50"], [2000, "55"]],
                    },
                ]
            },
        }, None)
        result = cf.query_cardinality_range("http://prom:9090")
        assert "db-a" in result
        assert "db-b" in result
        assert len(result["db-a"]) == 3

    @patch("cardinality_forecasting.http_get_json")
    def test_query_range_empty(self, mock_get):
        """查詢失敗回傳空 dict。"""
        mock_get.return_value = (None, "connection error")
        result = cf.query_cardinality_range("http://prom:9090")
        assert result == {}

    @patch("cardinality_forecasting.http_get_json")
    def test_query_range_error_status(self, mock_get):
        """查詢 status != success。"""
        mock_get.return_value = ({"status": "error", "error": "bad query"}, None)
        result = cf.query_cardinality_range("http://prom:9090")
        assert result == {}

    @patch("cardinality_forecasting.http_get_json")
    def test_query_scrape_series(self, mock_get):
        """scrape_series_added 查詢。"""
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "result": [
                    {"metric": {"tenant": "db-a"}, "value": [1000, "42"]},
                ]
            },
        }, None)
        result = cf.query_scrape_series_added("http://prom:9090")
        assert result["db-a"] == 42.0

    @patch("cardinality_forecasting.http_get_json")
    def test_query_scrape_series_empty(self, mock_get):
        """scrape_series_added 查詢失敗。"""
        mock_get.return_value = (None, "connection error")
        result = cf.query_scrape_series_added("http://prom:9090")
        assert result == {}


# ═══════════════════════════════════════════════════════════════════
# TestCLI
# ═══════════════════════════════════════════════════════════════════

class TestCLI:
    """CLI 整合測試。"""

    def _make_mock_data(self):
        """產生 mock Prometheus 回應。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        return ({
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"tenant": "db-a"},
                        "values": [
                            [now - 5 * day + i * day, str(100 + i * 10)]
                            for i in range(6)
                        ],
                    },
                ]
            },
        }, None)

    @patch("cardinality_forecasting.http_get_json")
    def test_main_text_output(self, mock_get, capsys):
        """文字輸出。"""
        mock_get.return_value = self._make_mock_data()
        exit_code = cf.main(["--prometheus", "http://prom:9090"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "db-a" in output

    @patch("cardinality_forecasting.http_get_json")
    def test_main_json_output(self, mock_get, capsys):
        """JSON 輸出。"""
        mock_get.return_value = self._make_mock_data()
        exit_code = cf.main(["--prometheus", "http://prom:9090", "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert "tenants" in data
        assert data["tenants"][0]["tenant"] == "db-a"

    @patch("cardinality_forecasting.http_get_json")
    def test_main_markdown_output(self, mock_get, capsys):
        """Markdown 輸出。"""
        mock_get.return_value = self._make_mock_data()
        exit_code = cf.main(["--prometheus", "http://prom:9090", "--markdown"])
        assert exit_code == 0
        output = capsys.readouterr().out
        assert "# Cardinality Forecast Report" in output

    @patch("cardinality_forecasting.http_get_json")
    def test_main_ci_safe(self, mock_get, capsys):
        """CI 模式 — 安全 → exit 0。"""
        mock_get.return_value = self._make_mock_data()
        exit_code = cf.main(["--prometheus", "http://prom:9090", "--ci"])
        assert exit_code == 0

    @patch("cardinality_forecasting.http_get_json")
    def test_main_ci_critical(self, mock_get, capsys):
        """CI 模式 — critical → exit 1。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"tenant": "db-crit"},
                        "values": [
                            [now - 5 * day + i * day, str(480 + i * 5)]
                            for i in range(6)
                        ],
                    },
                ]
            },
        }, None)
        exit_code = cf.main(["--prometheus", "http://prom:9090", "--ci",
                            "--warn-days", "30"])
        assert exit_code == 1

    @patch("cardinality_forecasting.http_get_json")
    def test_main_no_data(self, mock_get, capsys):
        """無資料 → exit 1。"""
        mock_get.return_value = (None, "connection error")
        exit_code = cf.main(["--prometheus", "http://prom:9090"])
        assert exit_code == 1

    @patch("cardinality_forecasting.http_get_json")
    def test_main_tenant_filter(self, mock_get, capsys):
        """Tenant 過濾。"""
        now = time.time()
        day = cf.SECONDS_PER_DAY
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "result": [
                    {
                        "metric": {"tenant": "db-a"},
                        "values": [[now - day + i * 3600, str(100)]
                                   for i in range(24)],
                    },
                    {
                        "metric": {"tenant": "db-b"},
                        "values": [[now - day + i * 3600, str(200)]
                                   for i in range(24)],
                    },
                ]
            },
        }, None)
        exit_code = cf.main(["--prometheus", "http://prom:9090",
                            "--tenant", "db-a", "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data["tenants"]) == 1
        assert data["tenants"][0]["tenant"] == "db-a"

    @patch("cardinality_forecasting.http_get_json")
    def test_main_custom_limit(self, mock_get, capsys):
        """自訂基數上限。"""
        mock_get.return_value = self._make_mock_data()
        exit_code = cf.main(["--prometheus", "http://prom:9090",
                            "--limit", "1000", "--json"])
        assert exit_code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["cardinality_limit"] == 1000


# ═══════════════════════════════════════════════════════════════════
# TestForecastReportProperties
# ═══════════════════════════════════════════════════════════════════

class TestForecastReportProperties:
    """ForecastReport 屬性測試。"""

    def test_counts(self):
        """risk level 計數正確。"""
        report = cf.ForecastReport(tenants=[
            cf.TenantForecast(
                tenant="a", current_cardinality=0, cardinality_limit=500,
                slope_per_day=0, intercept=0, r_squared=0,
                days_to_limit=None, predicted_date=None,
                trend="stable", risk_level="critical", data_points=0,
            ),
            cf.TenantForecast(
                tenant="b", current_cardinality=0, cardinality_limit=500,
                slope_per_day=0, intercept=0, r_squared=0,
                days_to_limit=None, predicted_date=None,
                trend="stable", risk_level="warning", data_points=0,
            ),
            cf.TenantForecast(
                tenant="c", current_cardinality=0, cardinality_limit=500,
                slope_per_day=0, intercept=0, r_squared=0,
                days_to_limit=None, predicted_date=None,
                trend="stable", risk_level="safe", data_points=0,
            ),
        ])
        assert report.critical_count == 1
        assert report.warning_count == 1
        assert report.safe_count == 1
