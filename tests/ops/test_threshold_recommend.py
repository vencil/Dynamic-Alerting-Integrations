"""test_threshold_recommend.py — threshold_recommend.py 的單元測試。

測試涵蓋：
  - 百分位數計算（P50/P95/P99，已知分佈驗證）
  - 信心等級（樣本數門檻）
  - 推薦邏輯（正常/noisy/低樣本/非數值/delta < 5%）
  - Reserved key 過濾
  - Prometheus 查詢（mock HTTP）
  - Dry-run 模式
  - 完整管線（config-dir → recommendations）
  - JSON / Text / Markdown 輸出格式
  - CLI entry point
"""

import json
import math
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
_TOOLS_DIR = os.path.join(_REPO_ROOT, "scripts", "tools")
for _p in [_TOOLS_DIR, os.path.join(_TOOLS_DIR, "ops")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import threshold_recommend as tr  # noqa: E402
from factories import write_yaml, make_tenant_yaml  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════
# percentile + compute_percentiles
# ═══════════════════════════════════════════════════════════════════════
class TestPercentile:
    """百分位數計算測試。"""

    def test_single_value(self):
        """單一值的所有百分位數應相同。"""
        assert tr.percentile([42.0], 0.5) == 42.0
        assert tr.percentile([42.0], 0.95) == 42.0

    def test_two_values(self):
        """兩個值的中位數應為平均。"""
        assert tr.percentile([10.0, 20.0], 0.5) == 15.0

    def test_known_distribution(self):
        """100 個均勻分佈值的 P50/P95/P99。"""
        values = [float(i) for i in range(100)]
        assert tr.percentile(values, 0.50) == pytest.approx(49.5, abs=0.1)
        assert tr.percentile(values, 0.95) == pytest.approx(94.05, abs=0.1)
        assert tr.percentile(values, 0.99) == pytest.approx(98.01, abs=0.1)

    def test_empty_list(self):
        """空列表應返回 0.0。"""
        assert tr.percentile([], 0.5) == 0.0

    def test_compute_percentiles_filters_nan(self):
        """compute_percentiles 應過濾 NaN 和 Inf。"""
        values = [10.0, float('nan'), 20.0, float('inf'), 30.0]
        pcts = tr.compute_percentiles(values)
        assert pcts["p50"] == 20.0
        assert pcts["p95"] > 0
        assert pcts["p99"] > 0

    def test_compute_percentiles_empty(self):
        """全 NaN 列表應返回全零。"""
        pcts = tr.compute_percentiles([float('nan'), float('nan')])
        assert pcts == {"p50": 0.0, "p95": 0.0, "p99": 0.0}

    @pytest.mark.parametrize("values,q,expected", [
        ([1, 2, 3, 4, 5], 0.0, 1.0),
        ([1, 2, 3, 4, 5], 1.0, 5.0),
        ([1, 2, 3, 4, 5], 0.25, 2.0),
        ([1, 2, 3, 4, 5], 0.75, 4.0),
    ], ids=["p0", "p100", "p25", "p75"])
    def test_various_percentiles(self, values, q, expected):
        """各百分位數精確性。"""
        assert tr.percentile([float(v) for v in values], q) == pytest.approx(expected, abs=0.01)

    def test_p95_always_lte_p99(self):
        """P95 ≤ P99 invariant（property-like check）。"""
        import random
        random.seed(42)
        for _ in range(20):
            values = [random.uniform(0, 1000) for _ in range(50)]
            pcts = tr.compute_percentiles(values)
            assert pcts["p95"] <= pcts["p99"]


# ═══════════════════════════════════════════════════════════════════════
# grade_confidence
# ═══════════════════════════════════════════════════════════════════════
class TestConfidence:
    """信心等級測試。"""

    @pytest.mark.parametrize("count,expected", [
        (1500, tr.CONFIDENCE_HIGH),
        (1000, tr.CONFIDENCE_HIGH),
        (500, tr.CONFIDENCE_MEDIUM),
        (100, tr.CONFIDENCE_MEDIUM),
        (50, tr.CONFIDENCE_LOW),
        (0, tr.CONFIDENCE_LOW),
    ], ids=["1500-high", "1000-high", "500-med", "100-med", "50-low", "0-low"])
    def test_grade_thresholds(self, count, expected):
        """樣本數門檻正確對應信心等級。"""
        assert tr.grade_confidence(count, min_samples=100) == expected

    def test_custom_min_samples(self):
        """自訂 min_samples 影響 MEDIUM 門檻。"""
        # With min_samples=500, count=200 should be LOW
        assert tr.grade_confidence(200, min_samples=500) == tr.CONFIDENCE_LOW
        # With min_samples=50, count=200 should be MEDIUM
        assert tr.grade_confidence(200, min_samples=50) == tr.CONFIDENCE_MEDIUM


# ═══════════════════════════════════════════════════════════════════════
# is_reserved_key
# ═══════════════════════════════════════════════════════════════════════
class TestReservedKeys:
    """Reserved key 過濾測試。"""

    @pytest.mark.parametrize("key,expected", [
        ("_silent_mode", True),
        ("_severity_dedup", True),
        ("_routing", True),
        ("_state_maintenance", True),
        ("mysql_connections", False),
        ("cpu_threshold", False),
    ], ids=["silent", "dedup", "routing", "state", "mysql", "cpu"])
    def test_reserved_detection(self, key, expected):
        """正確辨識 reserved vs metric key。"""
        assert tr.is_reserved_key(key) == expected


# ═══════════════════════════════════════════════════════════════════════
# recommend_threshold
# ═══════════════════════════════════════════════════════════════════════
class TestRecommendThreshold:
    """推薦邏輯測試。"""

    def test_normal_recommendation_p95(self):
        """正常情況推薦 P95。"""
        pcts = {"p50": 50.0, "p95": 85.0, "p99": 95.0}
        rec = tr.recommend_threshold("cpu", 80, pcts, 500, 100)
        assert rec.recommended == 85
        assert rec.confidence == tr.CONFIDENCE_MEDIUM
        assert rec.delta_pct == pytest.approx(6.25, abs=0.1)

    def test_noisy_alert_recommends_p99(self):
        """BAD noise grade 推薦 P99（放寬）。"""
        pcts = {"p50": 50.0, "p95": 85.0, "p99": 95.0}
        rec = tr.recommend_threshold("cpu", 80, pcts, 500, 100, noise_grade="BAD")
        assert rec.recommended == 95
        assert "P99" in rec.reason

    def test_within_margin_no_change(self):
        """Delta < 5% 不建議變更。"""
        pcts = {"p50": 50.0, "p95": 82.0, "p99": 90.0}
        rec = tr.recommend_threshold("cpu", 80, pcts, 500, 100)
        assert "no change" in rec.reason

    def test_low_confidence_note(self):
        """低信心應在 reason 中標註。"""
        pcts = {"p50": 50.0, "p95": 120.0, "p99": 150.0}
        rec = tr.recommend_threshold("conn", 80, pcts, 30, 100)
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert "low confidence" in rec.reason.lower()

    def test_non_numeric_value(self):
        """非數值 current_value 需手動審核。"""
        pcts = {"p50": 50.0, "p95": 85.0, "p99": 95.0}
        rec = tr.recommend_threshold("mode", "enable", pcts, 500, 100)
        assert "non-numeric" in rec.reason

    def test_zero_current_value(self):
        """Current = 0 的 delta 計算。"""
        pcts = {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        rec = tr.recommend_threshold("idle", 0, pcts, 500, 100)
        assert rec.delta_pct == 0.0

    def test_integer_precision_preserved(self):
        """整數 current value 應推薦整數。"""
        pcts = {"p50": 50.3, "p95": 85.7, "p99": 95.2}
        rec = tr.recommend_threshold("cpu", 80, pcts, 500, 100)
        assert isinstance(rec.recommended, int)

    def test_float_precision_preserved(self):
        """浮點數 current value 應保留小數。"""
        pcts = {"p50": 50.3, "p95": 85.73, "p99": 95.21}
        rec = tr.recommend_threshold("ratio", 0.8, pcts, 500, 100)
        assert isinstance(rec.recommended, float)


# ═══════════════════════════════════════════════════════════════════════
# build_metric_query
# ═══════════════════════════════════════════════════════════════════════
class TestBuildQuery:
    """PromQL 查詢建構測試。"""

    def test_basic_query(self):
        """基本查詢格式。"""
        q = tr.build_metric_query("mysql_connections", "db-a", "7d")
        assert 'key="mysql_connections"' in q
        assert 'tenant="db-a"' in q
        assert "[7d]" in q


# ═══════════════════════════════════════════════════════════════════════
# query_prometheus_range (mocked)
# ═══════════════════════════════════════════════════════════════════════
class TestPrometheusQuery:
    """Prometheus 查詢測試（mock HTTP）。"""

    @patch("threshold_recommend.http_get_json")
    def test_range_vector_extraction(self, mock_get):
        """Range vector 應提取所有 values。"""
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{
                    "metric": {"key": "cpu"},
                    "values": [[1000, "80.5"], [1001, "82.3"], [1002, "79.1"]],
                }],
            },
        }, None)
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err is None
        assert len(values) == 3
        assert values[0] == 80.5

    @patch("threshold_recommend.http_get_json")
    def test_instant_vector_extraction(self, mock_get):
        """Instant vector 應提取 value。"""
        mock_get.return_value = ({
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {"key": "cpu"}, "value": [1000, "85.0"]}],
            },
        }, None)
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err is None
        assert 85.0 in values

    @patch("threshold_recommend.http_get_json")
    def test_query_error(self, mock_get):
        """查詢錯誤應返回 error。"""
        mock_get.return_value = (None, "connection refused")
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err is not None
        assert values == []

    @patch("threshold_recommend.http_get_json")
    def test_prometheus_error_status(self, mock_get):
        """Prometheus error 狀態應返回 error。"""
        mock_get.return_value = ({"status": "error", "error": "bad query"}, None)
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err == "bad query"


# ═══════════════════════════════════════════════════════════════════════
# analyze_tenant
# ═══════════════════════════════════════════════════════════════════════
class TestAnalyzeTenant:
    """租戶分析測試。"""

    def test_dry_run_generates_queries(self):
        """Dry-run 應為每個 metric key 產生 PromQL。"""
        config = {"mysql_connections": 100, "cpu_threshold": 80, "_routing": {"receiver": {}}}
        report = tr.analyze_tenant("db-a", config, dry_run=True)
        assert report.total_keys == 2  # _routing is reserved
        assert len(report.keys) == 2
        assert all("dry-run" in r.reason for r in report.keys)
        assert all(r.promql != "" for r in report.keys)

    def test_reserved_keys_filtered(self):
        """Reserved keys 不應被分析。"""
        config = {
            "mysql_connections": 100,
            "_silent_mode": True,
            "_routing": {"receiver": {}},
            "_severity_dedup": "enable",
        }
        report = tr.analyze_tenant("db-a", config, dry_run=True)
        assert report.total_keys == 1
        keys = [r.key for r in report.keys]
        assert "mysql_connections" in keys
        assert "_silent_mode" not in keys

    @patch("threshold_recommend.query_prometheus_range")
    def test_with_prometheus_data(self, mock_query):
        """有 Prometheus 資料時應產生推薦。"""
        mock_query.return_value = ([80, 82, 78, 85, 90, 88, 79, 83, 81, 86] * 50, None)
        config = {"mysql_connections": 70}
        report = tr.analyze_tenant("db-a", config, prometheus_url="http://prom:9090")
        assert report.total_keys == 1
        assert report.keys[0].p95 is not None
        assert report.keys[0].recommended is not None

    @patch("threshold_recommend.query_prometheus_range")
    def test_no_data_points(self, mock_query):
        """無資料點應返回 LOW confidence。"""
        mock_query.return_value = ([], None)
        config = {"cpu_threshold": 80}
        report = tr.analyze_tenant("db-a", config, prometheus_url="http://prom:9090")
        assert report.keys[0].confidence == tr.CONFIDENCE_LOW
        assert "no data" in report.keys[0].reason


# ═══════════════════════════════════════════════════════════════════════
# run_analysis — full pipeline
# ═══════════════════════════════════════════════════════════════════════
class TestRunAnalysis:
    """完整管線測試。"""

    def test_empty_config_dir(self, tmp_path):
        """空配置目錄應返回空結果。"""
        reports = tr.run_analysis(str(tmp_path), dry_run=True)
        assert reports == []

    def test_tenant_filter(self, tmp_path):
        """--tenant 過濾器正確運作。"""
        for name in ("db-a", "db-b"):
            yaml_content = make_tenant_yaml(name, keys={"cpu": 80})
            write_yaml(str(tmp_path), f"{name}.yaml", yaml_content)
        reports = tr.run_analysis(str(tmp_path), tenant_filter="db-b", dry_run=True)
        assert len(reports) == 1
        assert reports[0].tenant == "db-b"

    def test_multiple_tenants(self, tmp_path):
        """多租戶都應被分析。"""
        for name in ("db-a", "db-b", "db-c"):
            yaml_content = make_tenant_yaml(name, keys={"cpu": 80, "mem": 90})
            write_yaml(str(tmp_path), f"{name}.yaml", yaml_content)
        reports = tr.run_analysis(str(tmp_path), dry_run=True)
        assert len(reports) == 3


# ═══════════════════════════════════════════════════════════════════════
# Output formatting
# ═══════════════════════════════════════════════════════════════════════
class TestOutputFormatting:
    """輸出格式化測試。"""

    def _make_sample_reports(self):
        return [tr.TenantRecommendation(
            tenant="db-a",
            keys=[
                tr.KeyRecommendation("cpu", 80, p50=50.0, p95=85.0, p99=95.0,
                                     recommended=85, delta_pct=6.3, confidence="MEDIUM",
                                     sample_count=500, reason="recommended at P95"),
                tr.KeyRecommendation("mem", 90, p50=60.0, p95=88.0, p99=92.0,
                                     recommended=88, delta_pct=-2.2, confidence="HIGH",
                                     sample_count=1500, reason="within 5% margin, no change needed"),
            ],
            total_keys=2,
            recommended_changes=1,
        )]

    def test_text_format(self):
        """Text 輸出包含 tenant 和 key 資訊。"""
        reports = self._make_sample_reports()
        text = tr.format_text_report(reports)
        assert "db-a" in text
        assert "cpu" in text
        assert "1/2" in text

    def test_text_format_empty(self):
        """空結果顯示提示訊息。"""
        text = tr.format_text_report([])
        assert len(text) > 0

    def test_json_format(self):
        """JSON 輸出結構正確。"""
        reports = self._make_sample_reports()
        data = json.loads(tr.format_json_report(reports))
        assert data["tool"] == "threshold-recommend"
        assert data["summary"]["total_keys"] == 2
        assert data["summary"]["recommended_changes"] == 1

    def test_markdown_format(self):
        """Markdown 輸出包含表格標頭。"""
        reports = self._make_sample_reports()
        md = tr.format_markdown_report(reports)
        assert "| Key |" in md
        assert "db-a" in md

    def test_markdown_empty(self):
        """空 Markdown 輸出。"""
        md = tr.format_markdown_report([])
        assert "No recommendations" in md


# ═══════════════════════════════════════════════════════════════════════
# CLI main()
# ═══════════════════════════════════════════════════════════════════════
class TestCLI:
    """CLI 入口點測試。"""

    def test_missing_config_dir(self):
        """不存在的 config-dir 應 exit 1。"""
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", "/nonexistent"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
            assert exc_info.value.code == 1

    def test_invalid_lookback(self, tmp_path):
        """無效的 lookback 應 exit 1。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--lookback", "invalid"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
            assert exc_info.value.code == 1

    def test_dry_run_cli(self, tmp_path):
        """CLI --dry-run 正常完成。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path), "--dry-run"]):
            tr.main()

    def test_json_output_cli(self, tmp_path, capsys):
        """CLI --json 輸出合法 JSON。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--json", "--dry-run"]):
            tr.main()
            captured = capsys.readouterr()
            data = json.loads(captured.out)
            assert data["tool"] == "threshold-recommend"

    def test_markdown_output_cli(self, tmp_path, capsys):
        """CLI --markdown 輸出 Markdown 表格。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--markdown", "--dry-run"]):
            tr.main()
            captured = capsys.readouterr()
            assert "| Key |" in captured.out
