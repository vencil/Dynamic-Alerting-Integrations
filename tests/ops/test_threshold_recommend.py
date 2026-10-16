"""test_threshold_recommend.py — threshold_recommend.py 的單元測試。

測試涵蓋：
  - 百分位數計算（P50/P95/P99，已知分佈驗證）
  - 信心等級（樣本數門檻）
  - 推薦邏輯（正常/低樣本/非數值/delta < 5%）
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
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
from factories import write_yaml, make_tenant_yaml  # noqa: E402


def _ts_pairs(values, step=100.0, start=1_700_000_000.0):
    """(ts, value) pairs as query_prometheus_range_ts returns them; the
    upper-bound path reads timestamps too since #2941. The default step makes
    1000 points span ~27.7h, above the 24h floor."""
    return [(start + i * step, float(v)) for i, v in enumerate(values)]


def _range_result(values, step=100.0, start=1_700_000_000.0):
    """A raw Prometheus range result, values as the API spells them."""
    def spell(v):
        if v != v:
            return "NaN"
        if v in (float("inf"), float("-inf")):
            return "+Inf" if v > 0 else "-Inf"
        return str(v)
    return [{"values": [[start + i * step, spell(v)] for i, v in enumerate(values)]}]

# #2116: run_analysis reads the tree through da-guard (effective + served-values);
# the in-process calls find this repo's build via `$DA_GUARD_BINARY`.
pytestmark = pytest.mark.usefixtures("da_guard_env")


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
        # The MEDIUM floor of 100 cannot be lowered (#2941; the CLI refuses
        # such a value): 60 samples stay LOW under min_samples=50.
        assert tr.grade_confidence(60, min_samples=50) == tr.CONFIDENCE_LOW

    def test_min_samples_raises_high_too(self):
        """#2941: --min-samples above 1000 used to leave HIGH at 1000."""
        assert tr.grade_confidence(1500, min_samples=5000) == tr.CONFIDENCE_LOW
        assert tr.grade_confidence(5000, min_samples=5000) == tr.CONFIDENCE_HIGH
        assert tr.grade_confidence(1500, min_samples=1000) == tr.CONFIDENCE_HIGH

    @pytest.mark.parametrize("text", ["99", "0", "-1", "abc", "1.5"])
    def test_min_samples_arg_refuses(self, text):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError):
            tr.min_samples_arg(text)

    @pytest.mark.parametrize("text, expected", [("100", 100), ("5000", 5000)])
    def test_min_samples_arg_accepts(self, text, expected):
        assert tr.min_samples_arg(text) == expected


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

    def test_normal_recommendation_relaxes_to_p99(self):
        """P95 高於現值時放寬到 P99（#2964：放寬到 P95 仍有約 5% 的點超標）。"""
        pcts = {"p50": 50.0, "p95": 85.0, "p99": 95.0}
        rec = tr.recommend_threshold("cpu", 80, pcts, 500, 100)
        assert rec.recommended == 95
        assert rec.confidence == tr.CONFIDENCE_MEDIUM
        assert rec.delta_pct == pytest.approx(18.75, abs=0.1)

    def test_within_margin_no_change(self):
        """Delta < 5% 不建議變更。"""
        pcts = {"p50": 50.0, "p95": 82.0, "p99": 83.0}
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
        """Current = 0 且資料全 0：P95 非正，不給建議（#2964）。"""
        pcts = {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        rec = tr.recommend_threshold("idle", 0, pcts, 500, 100,
                                     values=[0.0] * 500)
        assert rec.recommended is None
        assert rec.delta_pct is None
        assert rec.reason == "P95 is non-positive (max in window 0) — no recommendation"

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
    """PromQL 查詢建構測試（#719：查觀測 recording rule，非 user_threshold）。"""

    def test_queries_observed_series_not_user_threshold(self):
        """#719：查 observed recording rule，帶 tenant label + lookback。"""
        q = tr.build_metric_query("tenant:mysql_threads_connected:max", "db-a", "7d")
        assert q == 'tenant:mysql_threads_connected:max{tenant="db-a"}[7d]'
        # regression guard: the old echo-chamber/broken query is gone.
        assert "user_threshold" not in q
        assert 'key=' not in q

    def test_tenant_promql_escaped(self):
        """CodeRabbit #3334234464: tenant 含引號/反斜線須轉義，產生合法 PromQL。"""
        q = tr.build_metric_query("tenant:x:max", 'ev"il\\t', "7d")
        # the embedded quote/backslash must be escaped, not raw
        assert 'tenant="ev\\"il\\\\t"' in q


# ═══════════════════════════════════════════════════════════════════════
# query_prometheus_range (mocked)
# ═══════════════════════════════════════════════════════════════════════
class TestPrometheusQuery:
    """Prometheus 查詢測試（mock HTTP）。

    W1: fetch core 收斂進 _lib_prometheus.query_prometheus_instant，
    HTTP seam 改 patch ``_lib_prometheus.http_get_json``。
    """

    @patch("_lib_prometheus.http_get_json")
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

    @patch("_lib_prometheus.http_get_json")
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

    @patch("_lib_prometheus.http_get_json")
    def test_query_error(self, mock_get):
        """查詢錯誤應返回 error。"""
        mock_get.return_value = (None, "connection refused")
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err is not None
        assert values == []

    @patch("_lib_prometheus.http_get_json")
    def test_prometheus_error_status(self, mock_get):
        """Prometheus error 狀態應返回 error。"""
        mock_get.return_value = ({"status": "error", "error": "bad query"}, None)
        values, err = tr.query_prometheus_range("http://prom:9090", "test_query")
        assert err == "bad query"


# ═══════════════════════════════════════════════════════════════════════
# analyze_tenant
# ═══════════════════════════════════════════════════════════════════════
class TestAnalyzeTenant:
    """租戶分析測試（#719：以注入的 observed_map 隔離 rule-pack 真實內容）。"""

    # Hermetic observed-map: one upper-bound mapped key, one lower-bound key,
    # one unsupported-scope key. Unmapped keys are anything not listed here.
    HERMETIC_MAP = {
        "connections": {
            "scope": "tenant",
            "direction": ">",
            "observed_series": "tenant:mysql_threads_connected:max",
        },
        "broker_count": {
            "scope": "tenant",
            "direction": "<",
            "candidates": ["tenant:broker_count:max"],
            "needs_review": True,
            "reason": "lower-bound (<) metric — #916",
        },
        "container_cpu": {
            "scope": "tenant_version",
            "candidates": ["tenant_version:pod_weakest_cpu_percent:vlabeled"],
            "needs_review": True,
            "reason": "unsupported scope — #916",
        },
    }

    def test_dry_run_maps_to_observed_series(self):
        """Dry-run 對 mapped key 產生觀測 series 查詢。"""
        config = {"connections": 100, "_routing": {"receiver": {}}}
        report = tr.analyze_tenant("db-a", config, dry_run=True, observed_map=self.HERMETIC_MAP)
        assert report.total_keys == 1  # _routing is reserved
        rec = report.keys[0]
        assert rec.key == "connections"
        assert "tenant:mysql_threads_connected:max" in rec.promql
        assert "dry-run" in rec.reason

    def test_unmapped_key_skipped(self):
        """未對映的 key fail-loud skip。"""
        config = {"totally_unknown": 5}
        report = tr.analyze_tenant("db-a", config, dry_run=True, observed_map=self.HERMETIC_MAP)
        rec = report.keys[0]
        assert rec.promql == ""
        assert "not in observed-map" in rec.reason

    def test_lower_bound_key_skipped(self):
        """下界 (<) key skip（#916）。"""
        config = {"broker_count": 3}
        report = tr.analyze_tenant("db-a", config, dry_run=True, observed_map=self.HERMETIC_MAP)
        rec = report.keys[0]
        assert rec.promql == ""
        assert "skipped" in rec.reason
        assert "lower-bound" in rec.reason  # semantic, not a brittle issue-ref pin

    def test_unsupported_scope_key_skipped(self):
        """version-aware (tenant_version scope) key skip（#916）。"""
        config = {"container_cpu": 80}
        report = tr.analyze_tenant("db-a", config, dry_run=True, observed_map=self.HERMETIC_MAP)
        rec = report.keys[0]
        assert rec.promql == ""
        assert "skipped" in rec.reason

    def test_reserved_keys_filtered(self):
        """Reserved keys 不應被分析。"""
        config = {
            "connections": 100,
            "_silent_mode": True,
            "_routing": {"receiver": {}},
            "_severity_dedup": "enable",
        }
        report = tr.analyze_tenant("db-a", config, dry_run=True, observed_map=self.HERMETIC_MAP)
        assert report.total_keys == 1
        keys = [r.key for r in report.keys]
        assert "connections" in keys
        assert "_silent_mode" not in keys

    @patch("threshold_recommend.query_prometheus_range_ts")
    def test_with_prometheus_data(self, mock_query):
        """mapped key 有 Prometheus 資料時應產生推薦。"""
        mock_query.return_value = (_ts_pairs([80, 82, 78, 85, 90, 88, 79, 83, 81, 86] * 50), None)
        config = {"connections": 70}
        report = tr.analyze_tenant(
            "db-a", config, prometheus_url="http://prom:9090", observed_map=self.HERMETIC_MAP
        )
        assert report.total_keys == 1
        assert report.keys[0].p95 is not None
        assert report.keys[0].recommended is not None
        # confirm it queried the observed series, not user_threshold
        assert "tenant:mysql_threads_connected:max" in report.keys[0].promql

    @patch("threshold_recommend.query_prometheus_range_ts")
    def test_no_data_points(self, mock_query):
        """mapped key 無資料點應返回 LOW confidence。"""
        mock_query.return_value = ([], None)
        config = {"connections": 80}
        report = tr.analyze_tenant(
            "db-a", config, prometheus_url="http://prom:9090", observed_map=self.HERMETIC_MAP
        )
        assert report.keys[0].confidence == tr.CONFIDENCE_LOW
        assert "no data" in report.keys[0].reason


# ═══════════════════════════════════════════════════════════════════════
# run_analysis — full pipeline
# ═══════════════════════════════════════════════════════════════════════
class TestRunAnalysis:
    """完整管線測試。"""

    def test_empty_config_dir(self, tmp_path):
        """空配置目錄：da-guard 拒收（沒有任何設定檔），fail-closed 而非回空結果（#2116）。"""
        import _lib_tenant_values as tv
        with pytest.raises(tv.DaGuardError):
            tr.run_analysis(str(tmp_path), dry_run=True)

    def test_no_tenant_config_dir(self, tmp_path):
        """只有 `_defaults.yaml`、沒有租戶的樹應返回空結果。"""
        write_yaml(str(tmp_path), "_defaults.yaml", "defaults:\n  mysql_connections: 80\n")
        assert tr.run_analysis(str(tmp_path), dry_run=True) == []

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
# --export-patch (#720 STAGE-1)
# ═══════════════════════════════════════════════════════════════════════
class TestExportPatch:
    """conf.d override fragment 輸出測試（#720 STAGE-1）。"""

    def _reports(self):
        # cpu: +6.3% actionable; mem: -2.2% within-margin; lag: skipped (no rec)
        return [tr.TenantRecommendation(
            tenant="db-a",
            keys=[
                tr.KeyRecommendation("mysql_threads_running", "80", p95=85.0, recommended=85,
                                     delta_pct=6.3, confidence="MEDIUM",
                                     reason="recommended at P95 (increase 6.3%)"),
                tr.KeyRecommendation("mysql_connections", "90", p95=88.0, recommended=88,
                                     delta_pct=-2.2, confidence="HIGH",
                                     reason="within 5% margin, no change needed"),
                tr.KeyRecommendation("kafka_broker_count", "3", recommended=None,
                                     reason="skipped: lower-bound (<) metric — #916"),
            ],
            total_keys=3,
            recommended_changes=1,
        )]

    def test_emits_only_actionable_key_as_valid_yaml(self):
        """只含 |delta|>=5% 且有建議的 key；輸出是合法 conf.d YAML。"""
        yaml = pytest.importorskip("yaml")
        out = tr.format_export_patch(self._reports())
        doc = yaml.safe_load(out)
        # only the actionable key, quoted-string value, under tenants:<name>
        assert doc == {"tenants": {"db-a": {"mysql_threads_running": "85"}}}

    def test_within_margin_and_skipped_not_patched(self):
        """within-margin / skipped key 不進 patch（只當註解列出）。"""
        out = tr.format_export_patch(self._reports())
        # the YAML data must NOT carry these keys
        yaml = pytest.importorskip("yaml")
        doc = yaml.safe_load(out) or {}
        emitted = doc.get("tenants", {}).get("db-a", {})
        assert "mysql_connections" not in emitted   # within-margin
        assert "kafka_broker_count" not in emitted   # skipped (no rec)
        # but they ARE surfaced as transparency comments
        assert "(skipped)" in out
        assert "mysql_connections" in out  # appears in a comment line

    def test_value_is_quoted_string_integer(self):
        """整數建議值渲染為不帶小數點的 quoted string（對齊 conf.d 慣例）。"""
        out = tr.format_export_patch(self._reports())
        assert '"85"' in out
        assert '"85.0"' not in out

    def test_no_actionable_recommendations(self):
        """全 skipped / 空 reports → 合法 YAML + 明確 no-actionable 標記，不崩。"""
        yaml = pytest.importorskip("yaml")
        skipped = [tr.TenantRecommendation(tenant="db-a", total_keys=1, keys=[
            tr.KeyRecommendation("x", "1", recommended=None, reason="skipped: unmapped"),
        ])]
        for reports in (skipped, []):
            out = tr.format_export_patch(reports)
            assert "no actionable" in out
            assert yaml.safe_load(out) is None   # comments only → parses to None

    def test_force_manual_reason_with_newline_is_sanitized(self):
        """guardrail_reason 含換行（來自 str(exc)）不得破壞 export-patch 註解流。

        未清洗時，內嵌的 `\\n` 會讓後半段脫離 `#` 註解、變成 YAML 裸文字
        （safe_load 吐出 mapping 而非 None，operator apply 時 parse error）。
        """
        yaml = pytest.importorskip("yaml")
        reports = [tr.TenantRecommendation(tenant="t", total_keys=1, keys=[
            tr.KeyRecommendation("db2_bufferpool_hit_ratio", "0.95", recommended=None,
                                 force_manual=True,
                                 guardrail_reason="boom\nevil: injected\nmore"),
        ])]
        out = tr.format_export_patch(reports)
        assert yaml.safe_load(out) is None            # 註入文字沒逃成 YAML mapping
        assert all(l.lstrip().startswith("#")         # 每個非空行仍是註解
                   for l in out.splitlines() if l.strip())

    def test_float_value_and_boundary_and_negative(self):
        """float 帶小數 / 邊界 5.0% 含入 / 負 delta（decrease）皆正確 emit。"""
        yaml = pytest.importorskip("yaml")
        reports = [tr.TenantRecommendation(tenant="t", total_keys=3, keys=[
            tr.KeyRecommendation("pg_replication_lag", "30", recommended=45.5,
                                 delta_pct=51.7, confidence="HIGH", reason="P95"),
            tr.KeyRecommendation("boundary", "100", recommended=105,
                                 delta_pct=5.0, confidence="HIGH", reason="P95"),  # inclusive
            tr.KeyRecommendation("decrease", "100", recommended=80,
                                 delta_pct=-20.0, confidence="HIGH", reason="decrease"),
        ])]
        doc = yaml.safe_load(tr.format_export_patch(reports))
        assert doc == {"tenants": {"t": {
            "pg_replication_lag": "45.5",  # float keeps decimals
            "boundary": "105",             # 5.0% boundary is included (>=)
            "decrease": "80",              # negative delta still actionable
        }}}

    def test_mixed_tenants_preserve_skip_transparency(self):
        """一 tenant 有建議、另一 tenant 全 skip → YAML 只含前者，但後者 skip-context 仍以註解保留。"""
        yaml = pytest.importorskip("yaml")
        reports = [
            tr.TenantRecommendation(tenant="db-a", total_keys=1, keys=[
                tr.KeyRecommendation("mysql_threads_running", "80", recommended=90, delta_pct=12.5,
                                     confidence="HIGH", reason="recommended at P95"),
            ]),
            tr.TenantRecommendation(tenant="db-b", total_keys=1, keys=[
                tr.KeyRecommendation("redis_connected_clients", "500", recommended=None,
                                     reason="skipped: unmapped"),
            ]),
        ]
        out = tr.format_export_patch(reports)
        doc = yaml.safe_load(out)
        # applyable YAML carries ONLY the actionable tenant
        assert doc == {"tenants": {"db-a": {"mysql_threads_running": "90"}}}
        # but db-b's skip context is NOT silently dropped — present as a comment
        assert "db-b" in out
        assert "(skipped)" in out
        assert "redis_connected_clients" in out

    def test_export_patch_cli(self, tmp_path):
        """CLI --export-patch dry-run 路徑跑得通（無 Prometheus → 全 skip）。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"mysql_threads_running": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--export-patch", "--dry-run"]):
            tr.main()  # must not raise


# ═══════════════════════════════════════════════════════════════════════
# CLI main()
# ═══════════════════════════════════════════════════════════════════════
class TestCLI:
    """CLI 入口點測試。"""

    def test_missing_config_dir(self):
        """不存在的 config-dir 應 exit caller error。"""
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", "/nonexistent"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_invalid_lookback(self, tmp_path):
        """無效的 lookback 應 exit caller error。"""
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--lookback", "invalid"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_missing_observed_map_exits_caller_error(self, tmp_path, monkeypatch, capsys):
        """#3017：map 缺檔不再把每個 key 靜默 skip、rc 0；改 rc 2 並點名檔案。"""
        missing = tmp_path / "metric_observed_map.yaml"
        monkeypatch.setattr(tr.observed_map_lib, "DEFAULT_MAP_PATH", str(missing))
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path), "--dry-run"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "ERROR:" in err and str(missing) in err

    def test_missing_observed_map_with_unmatched_tenant_still_fails(self, tmp_path, monkeypatch):
        """--tenant 對不到任何租戶時也先載入 map：「什麼都沒找到」不能掩蓋缺 map。"""
        monkeypatch.setattr(tr.observed_map_lib, "DEFAULT_MAP_PATH", str(tmp_path / "absent.yaml"))
        write_yaml(str(tmp_path), "db-a.yaml", make_tenant_yaml("db-a", keys={"cpu": 80}))
        with patch("sys.argv", ["threshold_recommend.py", "--config-dir", str(tmp_path),
                                 "--dry-run", "--tenant", "no-such-tenant"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
        assert exc_info.value.code == EXIT_CALLER_ERROR

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

    def test_missing_config_dir_without_generate_exits_caller_error(self):
        """#719: 無 --config-dir 且非 generate → exit caller error。"""
        with patch("sys.argv", ["threshold_recommend.py"]):
            with pytest.raises(SystemExit) as exc_info:
                tr.main()
            assert exc_info.value.code == EXIT_CALLER_ERROR

    def test_generate_observed_map_cli(self, tmp_path, capsys, monkeypatch):
        """#719: --generate-observed-map 寫出 map 並印摘要，不需 --config-dir。"""
        called = {}

        def fake_write(out_path=None, pack_paths=None):
            called["yes"] = True
            # #916 Item B: summary now carries merge stats (preserved/demoted/
            # dropped) alongside the #719 count keys.
            return {
                "path": str(tmp_path / "m.yaml"),
                "total": 3,
                "clean": 2,
                "needs_review": 1,
                "preserved": 1,
                "demoted": 0,
                "dropped": 0,
            }

        monkeypatch.setattr(tr.observed_map_lib, "write_observed_map", fake_write)
        with patch("sys.argv", ["threshold_recommend.py", "--generate-observed-map"]):
            tr.main()  # must NOT raise (no --config-dir required)
        captured = capsys.readouterr()
        assert called.get("yes") is True
        # #1112: the summary is human-readable prose → stderr (stdout is reserved
        # for the one JSON document `--json` promises; see the next test).
        assert "observed-map" in captured.err
        assert "3" in captured.err  # total keys echoed
        assert "preserved 1" in captured.err  # merge stats surfaced
        assert captured.out == ""

    def test_generate_observed_map_json_envelope(self, tmp_path, capsys, monkeypatch):
        """#1112: --generate-observed-map --json → stdout 恰好一份 JSON。

        保留本工具的頂層 schema（tool / tenants / summary），寫檔結果放在
        `observed_map`，並以 `status` 與推薦報告區分。
        """
        def fake_write(out_path=None, pack_paths=None):
            return {
                "path": str(tmp_path / "m.yaml"),
                "total": 3, "clean": 2, "needs_review": 1,
                "preserved": 1, "demoted": 0, "dropped": 0,
            }

        monkeypatch.setattr(tr.observed_map_lib, "write_observed_map", fake_write)
        with patch("sys.argv",
                   ["threshold_recommend.py", "--generate-observed-map", "--json"]):
            tr.main()
        doc = json.loads(capsys.readouterr().out)
        assert doc["status"] == "observed_map_written"
        assert doc["observed_map"]["total"] == 3
        assert doc["tenants"] == []
        assert doc["summary"]["recommended_changes"] == 0
        assert doc["summary"]["low_confidence_changes"] == 0   # #2969: same shape


# ═══════════════════════════════════════════════════════════════════════
# #916 Item A — lower-bound (percentile-lower) engine
# ═══════════════════════════════════════════════════════════════════════
from datetime import datetime, timezone, timedelta  # noqa: E402


def _floor_samples(floor, *, days=9, pts_per_day=200, base_day=0, above=None):
    """(ts, val) samples across `days` UTC days whose per-day P5 == `floor`.

    ~10% of each day's points sit AT the floor, the rest just above → the 5th
    percentile lands in the floor band. `days` calendar days give days-2 FULL days
    (the engine drops the two partial boundary days), so days=9 → 7 full days.
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    hi = above if above is not None else min(0.9999, floor + 0.02)
    step = 86400 // pts_per_day
    out = []
    for d in range(days):
        for i in range(pts_per_day):
            ts = (base + timedelta(days=base_day + d, seconds=i * step)).timestamp()
            out.append((ts, floor if i < pts_per_day * 0.10 else hi))
    return out


def _rho_target(current, rho):
    """A floor value whose 4dp-floored target gives (approximately) miss-ratio rho."""
    return round(1.0 - rho * (1.0 - current), 4)


class TestQueryPrometheusRangeTs:
    @patch("_lib_prometheus.http_get_json")
    def test_keeps_timestamps(self, mock_get):
        mock_get.return_value = ({
            "status": "success",
            "data": {"resultType": "matrix", "result": [{
                "metric": {}, "values": [[1000, "0.95"], [1060, "0.96"]]}]},
        }, None)
        pairs, err = tr.query_prometheus_range_ts("http://prom:9090", "q")
        assert err is None
        assert pairs == [(1000.0, 0.95), (1060.0, 0.96)]

    @patch("_lib_prometheus.http_get_json")
    def test_error_propagates(self, mock_get):
        mock_get.return_value = (None, "connection refused")
        pairs, err = tr.query_prometheus_range_ts("http://prom:9090", "q")
        assert pairs == [] and err == "connection refused"

    @patch("_lib_prometheus.http_get_json")
    def test_filters_nan_and_inf(self, mock_get):
        # a hit-ratio rule can emit NaN while idle (0/0); an unfiltered NaN would
        # sort to an arbitrary index and corrupt the percentile.
        mock_get.return_value = ({
            "status": "success",
            "data": {"resultType": "matrix", "result": [{
                "metric": {}, "values": [
                    [1000, "0.95"], [1060, "NaN"], [1120, "0.96"], [1180, "Inf"]]}]},
        }, None)
        pairs, err = tr.query_prometheus_range_ts("http://prom:9090", "q")
        assert err is None
        assert pairs == [(1000.0, 0.95), (1120.0, 0.96)]   # NaN + Inf dropped


class TestRecommendThresholdLowerGuard0:
    """guard 0 (domain, first): non-numeric / out-of-(0,1) current all force_manual,
    never export, never crash (esp. current==1.0 → no divide-by-zero)."""

    @pytest.mark.parametrize("cur", ["1", "1.0", "disable", "95", "0", "1.2"])
    def test_domain_guard_force_manual_no_export(self, cur):
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", cur,
                                           _floor_samples(0.97), 100)
        assert rec.force_manual is True
        assert rec.recommended is None
        assert not tr._exportable(rec)
        assert rec.guardrail_reason  # a reason is always attached


class TestRecommendThresholdLowerGuardOrder:
    """The blocker: relaxation is checked BEFORE margin, so a sub-10% loosen is
    force_manual (never slips through as within-margin 'no change')."""

    @pytest.mark.parametrize("rho", [1.04, 1.05, 1.07, 1.10])
    def test_relaxation_band_never_exports(self, rho):
        floor = _rho_target(0.95, rho)          # target just BELOW current 0.95
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(floor), 100)
        assert rec.force_manual is True, f"rho={rho} floor={floor} should be manual"
        assert rec.recommended is None
        # and it must not appear as an applyable line in the export patch YAML
        yaml = pytest.importorskip("yaml")
        rep = tr.TenantRecommendation(tenant="t", total_keys=1, keys=[rec])
        doc = yaml.safe_load(tr.format_export_patch([rep]))
        emitted = (doc or {}).get("tenants", {}).get("t", {}) if doc else {}
        assert "db2_bufferpool_hit_ratio" not in emitted

    def test_tighten_happy_path_exports(self):
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(0.97), 100)
        assert rec.force_manual is False
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert rec.recommended > 0.95          # raises the floor
        assert tr._exportable(rec)
        assert "miss-rate" in rec.reason       # miss-space delta signal

    def test_within_margin_recommended_none(self):
        # tighten side, |rho-1| < 0.10 → recommended=None (not just a reason string)
        floor = _rho_target(0.95, 0.96)        # rho 0.96 → |rho-1|=0.04 < 0.10
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(floor), 100)
        assert rec.force_manual is False
        assert rec.recommended is None
        assert not tr._exportable(rec)
        assert "no change" in rec.reason

    def test_clamp_to_quarter_miss_then_refloor(self):
        # a P5 hugging 1.0 clamps to 1 - 0.25*m_c and re-floors to 4dp.
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(0.999), 100)
        # m_c = 0.05 → clamp target = 1 - 0.25*0.05 = 0.9875
        assert rec.recommended == pytest.approx(0.9875, abs=1e-9)
        assert not rec.force_manual

    def test_4dp_floor_truncates_not_rounds(self):
        # candidate 0.976543 must floor DOWN to 0.9765 (never 0.9766) — floor keeps
        # the recommendation from silently loosening via round-half-up.
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(0.976543), 100)
        assert f"{rec.recommended:.4f}" == "0.9765"


class TestRecommendThresholdLowerSamples:
    def test_thin_lookback_force_manual(self):
        # only 3 full days (5 calendar days) → force_manual with the lookback reason
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(0.97, days=5), 100)
        assert rec.force_manual is True
        assert "lookback" in rec.guardrail_reason

    def test_insufficient_total_samples_force_manual(self):
        # enough calendar days but too few points overall → force_manual
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95",
                                           _floor_samples(0.97, days=9, pts_per_day=5), 100)
        assert rec.force_manual is True
        assert "sample" in rec.guardrail_reason.lower()


class TestRecommendThresholdLowerContamination:
    @staticmethod
    def _trough_samples(current_ignored, weekday, weekend, trough_days=(3, 6),
                        days=9, pts=200):
        """9 cal days (7 full); `trough_days` sit genuinely at `weekend` floor, the
        rest at `weekday` floor (each day ~10% at its floor, rest just above)."""
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        out = []
        for d in range(days):
            f = weekend if d in trough_days else weekday
            for i in range(pts):
                ts = (base + timedelta(days=d, seconds=i * 432)).timestamp()
                out.append((ts, f if i < pts * 0.10 else min(0.9999, f + 0.005)))
        return out

    def test_outage_echo_transient_does_not_trip_divergence(self):
        # A short degraded window crossing UTC midnight pollutes 2 daily buckets'
        # P5 but is <5% of total → pooled ≈ daily (ratio ≈ 1 < K) so the divergence
        # gate does NOT fire → the engine tightens to the clean floor.
        s = _floor_samples(0.97, days=9, pts_per_day=200)
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for m in range(16):
            s.append(((base + timedelta(days=4, seconds=23*3600 + m*120)).timestamp(), 0.80))
            s.append(((base + timedelta(days=5, seconds=m*120)).timestamp(), 0.80))
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95", s, 100)
        assert rec.force_manual is False
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)

    def test_shallow_trough_above_current_trips_divergence(self):
        # THE MAJOR-BUG REGRESSION: a recurring trough ABOVE current (weekday floor
        # 0.98, weekend trough 0.96, current 0.95). daily-median resists (~0.98) but
        # pooled-P5 drops into the trough (~0.96) → without the divergence gate the
        # engine auto-tightens to ~0.98 and false-alarms on the 0.96 trough every
        # week. The gate ((1-pooled) >= 1.5*(1-daily)) must force_manual instead.
        s = self._trough_samples("0.95", weekday=0.980, weekend=0.960)
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95", s, 100)
        assert rec.force_manual is True
        assert "diverge" in rec.guardrail_reason
        assert rec.recommended is None                 # never auto-tightens

    def test_deep_trough_below_current_goes_relaxation_not_divergence(self):
        # A trough BELOW current (weekend 0.90 < current 0.95): candidate=min lands
        # below current → the RELAXATION guard claims it (checked before divergence),
        # so the reason is relaxation, not divergence.
        s = self._trough_samples("0.95", weekday=0.98, weekend=0.90)
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.95", s, 100)
        assert rec.force_manual is True
        assert "relax" in rec.guardrail_reason
        assert "diverge" not in rec.guardrail_reason    # relaxation wins the race

    def test_oscillation_uses_p5_floor_not_peaks(self):
        # Intra-day oscillation 0.90<->0.99 with a stable daily-P5 ~0.90: the engine
        # keys off the P5 floor (raise floor toward 0.90), not the 0.99 peaks.
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        s = []
        for d in range(9):
            for i in range(200):
                ts = (base + timedelta(days=d, seconds=i*432)).timestamp()
                s.append((ts, 0.90 if i % 2 == 0 else 0.99))
        rec = tr.recommend_threshold_lower("db2_bufferpool_hit_ratio", "0.85", s, 100)
        # current 0.85 floor, P5 ~0.90 → tighten toward 0.90, not toward 0.99
        assert rec.recommended is not None and not rec.force_manual
        assert 0.89 <= rec.recommended <= 0.91

    def test_exportable_rejects_force_manual(self):
        rec = tr.KeyRecommendation("k", "0.95", recommended=None, delta_pct=8.0,
                                   force_manual=True, guardrail_reason="relax")
        assert not tr._exportable(rec)


class TestAnalyzeTenantLowerBound:
    """analyze_tenant routes a percentile-lower entry to the lower engine and a
    N/A entry to a by-design skip."""

    LOWER_MAP = {
        "db2_bufferpool_hit_ratio": {
            "scope": "tenant", "direction": "<",
            "observed_series": "tenant:db2_bufferpool_hit_ratio:min",
            "recommendation_mode": "percentile-lower",
        },
        "kafka_active_controllers": {
            "scope": "tenant", "direction": "<",
            "candidates": ["tenant:kafka_active_controllers:max"],
            "recommendation_mode": "not-applicable",
            "reason": "by-design not-applicable — invariant (#916)",
        },
    }

    @patch("threshold_recommend.query_prometheus_range_ts")
    def test_percentile_lower_routes_to_lower_engine(self, mock_ts):
        mock_ts.return_value = (_floor_samples(0.97), None)
        report = tr.analyze_tenant("db-x", {"db2_bufferpool_hit_ratio": "0.95"},
                                   prometheus_url="http://p:9090", observed_map=self.LOWER_MAP)
        rec = report.keys[0]
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert "miss-rate" in rec.reason
        assert report.recommended_changes == 1

    def test_not_applicable_key_skipped_by_design(self):
        report = tr.analyze_tenant("db-x", {"kafka_active_controllers": "1"},
                                   dry_run=False, prometheus_url="http://p:9090",
                                   observed_map=self.LOWER_MAP)
        rec = report.keys[0]
        assert rec.recommended is None
        assert "by-design not-applicable" in rec.reason

    @patch("threshold_recommend.query_prometheus_range_ts")
    def test_bad_value_forces_manual_not_crash(self, mock_ts):
        # one tenant's garbage value must degrade to force_manual, not sink the run
        mock_ts.return_value = (_floor_samples(0.97), None)
        report = tr.analyze_tenant("db-x", {"db2_bufferpool_hit_ratio": "not-a-ratio"},
                                   prometheus_url="http://p:9090", observed_map=self.LOWER_MAP)
        rec = report.keys[0]
        assert rec.force_manual is True and rec.recommended is None

    @patch("threshold_recommend.query_prometheus_range_ts")
    def test_upper_bound_exception_reason_not_lower_labelled(self, mock_q):
        # item 4: an UPPER-bound key blowing up in the per-key try/except must NOT
        # be mislabelled "lower-bound floor" — the reason follows direction.
        mock_q.side_effect = RuntimeError("boom")
        upper_map = {"connections": {"scope": "tenant", "direction": ">",
                                     "observed_series": "tenant:x:max"}}
        report = tr.analyze_tenant("db-x", {"connections": "100"},
                                   prometheus_url="http://p:9090", observed_map=upper_map)
        rec = report.keys[0]
        assert rec.force_manual is True
        assert "recommendation → manual review" in rec.reason
        assert "lower-bound" not in rec.reason


class TestAnalyzeTenantUpperBoundFiniteSamples:
    """#2956: the upper-bound path counts only finite samples."""

    _UPPER_MAP = {"connections": {"scope": "tenant", "direction": ">",
                                  "observed_series": "tenant:x:max"}}

    def _upper(self, values, step=100.0):
        """Through the raw Prometheus response, so the finite filter that
        query_prometheus_range_ts applies is the one under test."""
        with patch("threshold_recommend.query_prometheus_instant",
                   return_value=(_range_result(values, step), None)):
            report = tr.analyze_tenant("db-x", {"connections": "2000"},
                                       prometheus_url="http://p:9090",
                                       observed_map=self._UPPER_MAP)
        return report.keys[0]

    def test_upper_bound_counts_only_finite_samples(self):
        """#2956: NaN / Inf are dropped from the percentiles, so they must not
        count towards confidence either: 999 NaN + 1 point is one sample."""
        nan, inf = float("nan"), float("inf")
        rec = self._upper([nan] * 600 + [inf] * 200 + [-inf] * 199 + [55.0])
        assert rec.sample_count == 1
        assert rec.confidence == tr.CONFIDENCE_LOW

    def test_upper_bound_all_nan_is_no_data(self):
        """#2956: all-NaN used to grade HIGH and recommend 0 (-100%)."""
        rec = self._upper([float("nan")] * 1500)
        assert rec.sample_count == 0
        assert rec.recommended is None
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert rec.reason == "no data points found"

    def test_upper_bound_finite_samples_unchanged(self):
        """Control: the same count of finite points still grades HIGH."""
        rec = self._upper([50.0 + (i % 10) for i in range(1000)])
        assert rec.sample_count == 1000
        assert rec.confidence == tr.CONFIDENCE_HIGH

    # ── #2941: the 24-distinct-hour coverage floor ─────────────────────
    _BIG_DELTA = [50.0 + (i % 10) for i in range(1000)]   # current 2000 → -50% (#2964 floor)
    _T0 = 1_700_000_000.0 - (1_700_000_000.0 % 3600)      # on an hour boundary

    def _upper_pairs(self, pairs):
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(pairs, None)):
            report = tr.analyze_tenant("db-x", {"connections": "2000"},
                                       prometheus_url="http://p:9090",
                                       observed_map=self._UPPER_MAP)
        return report.keys[0]

    def test_short_history_is_low_and_not_governed(self):
        """1000 points of a 15s rule are ~4.2h of data: HIGH by count, LOW
        by coverage, so threshold_govern opens no PR for it."""
        import threshold_govern as tg
        rec = self._upper(self._BIG_DELTA, step=15.0)
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert "distinct hour(s) < 24" in rec.reason
        assert rec.recommended is not None          # still shown, only not trusted
        assert tg.is_governance_actionable(rec, 25.0) is False

    def test_short_history_downgrades_medium_too(self):
        """A 1m rule's 16h is ~960 points: MEDIUM by count, still too short."""
        rec = self._upper_pairs(_ts_pairs(self._BIG_DELTA[:960], step=60.0,
                                          start=self._T0))
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert "distinct hour(s) < 24" in rec.reason

    def test_a_stray_early_sample_does_not_stretch_the_history(self):
        """A first-to-last span would read 6 days here; 5 hours are covered."""
        pairs = ([(self._T0 - 6 * 86400, 55.0)]
                 + _ts_pairs(self._BIG_DELTA, step=15.0, start=self._T0))
        rec = self._upper_pairs(pairs)
        assert rec.confidence == tr.CONFIDENCE_LOW

    def test_two_short_series_far_apart_are_still_short(self):
        pairs = (_ts_pairs(self._BIG_DELTA[:240], step=15.0, start=self._T0)
                 + _ts_pairs(self._BIG_DELTA[:240], step=15.0,
                             start=self._T0 + 30 * 3600))
        rec = self._upper_pairs(pairs)
        assert rec.confidence == tr.CONFIDENCE_LOW

    def test_24_distinct_hours_are_enough(self):
        """Boundary: one sample in each of 24 consecutive hours, many samples
        in total, is not short; 23 hours are."""
        def hours(n):
            return [(self._T0 + h * 3600 + k * 60, 50.0 + (k % 10))
                    for h in range(n) for k in range(60)]
        import threshold_govern as tg
        rec = self._upper_pairs(hours(24))
        assert rec.confidence == tr.CONFIDENCE_HIGH
        assert "distinct hour" not in rec.reason
        assert tg.is_governance_actionable(rec, 25.0) is True
        assert self._upper_pairs(hours(23)).confidence == tr.CONFIDENCE_LOW

    def test_short_history_does_not_touch_an_already_low_grade(self):
        rec = self._upper([50.0] * 10, step=15.0)
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert "distinct hour" not in rec.reason


# ═══════════════════════════════════════════════════════════════════════
# #2969 — a LOW recommendation is listed, never exported or counted
# ═══════════════════════════════════════════════════════════════════════
_HEADER_LINES = 7   # format_export_patch's fixed `#` header


def _low_rec(key="k", reason="thin sample", **kw):
    base = dict(current_value="100", recommended=50.0, delta_pct=-50.0,
                confidence=tr.CONFIDENCE_LOW, reason=reason)
    base.update(kw)
    return tr.KeyRecommendation(key=key, **base)


class TestLowConfidenceNotExported:
    _UPPER_MAP = TestAnalyzeTenantUpperBoundFiniteSamples._UPPER_MAP
    _LOWER_MAP = TestAnalyzeTenantLowerBound.LOWER_MAP
    _BIG_DELTA = TestAnalyzeTenantUpperBoundFiniteSamples._BIG_DELTA

    def _analyze(self, pairs, *, current="70", source_file="", **kw):
        # current 70 over 50-59 data: P95 59, and 59 is also the largest
        # in-window value <= current and above ceil(70/2) — the same 59 /
        # -15.7% whether or not the upper engine clamps a tightening (#2964).
        # current 40: a loosening to 59 (P95 and P99 alike), +47.5%.
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(pairs, None)):
            return tr.analyze_tenant("db-x", {"connections": current},
                                     prometheus_url="http://p:9090",
                                     observed_map=self._UPPER_MAP,
                                     source_file=source_file, **kw)

    # ── _exportable and threshold_govern agree at the same margin ──────
    @pytest.mark.parametrize("conf", [tr.CONFIDENCE_HIGH, tr.CONFIDENCE_MEDIUM,
                                      tr.CONFIDENCE_LOW, tr.CONFIDENCE_NOT_QUERIED,
                                      "", None])
    @pytest.mark.parametrize("delta", [None, 0.0, 4.99, -4.99, 5.0, -5.0, 25.0, -25.0])
    @pytest.mark.parametrize("force_manual", [True, False])
    @pytest.mark.parametrize("recommended", [None, 50.0, 0.0])
    def test_exportable_matches_govern_at_the_same_margin(
            self, recommended, force_manual, delta, conf):
        import threshold_govern as tg
        rec = tr.KeyRecommendation(key="k", current_value="100",
                                   recommended=recommended, delta_pct=delta,
                                   confidence=conf, force_manual=force_manual)
        assert tr._exportable(rec) is tg.is_governance_actionable(rec, 5.0)

    @pytest.mark.parametrize("conf,expected", [
        (tr.CONFIDENCE_HIGH, True), (tr.CONFIDENCE_MEDIUM, True),
        (tr.CONFIDENCE_LOW, False), (tr.CONFIDENCE_NOT_QUERIED, False),
        ("", False), (None, False)])
    def test_exportable_confidence_floor(self, conf, expected):
        """Pins the direction on its own: the grid above passes as long as
        the two sides agree, even if both changed."""
        assert tr._exportable(_low_rec(confidence=conf)) is expected

    # ── analyze_tenant: counted apart, never as a change ───────────────
    def test_upper_low_is_counted_apart_and_only_a_comment(self):
        yaml = pytest.importorskip("yaml")
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=15.0))
        rec = rep.keys[0]
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert "distinct hour(s) < 24" in rec.reason
        assert (rep.recommended_changes, rep.low_confidence_changes) == (0, 1)
        out = tr.format_export_patch([rep])
        assert yaml.safe_load(out) is None
        assert (f"# [db-x] (low confidence) connections: recommended 59, was 70, "
                f"-15.7% — {rec.reason}") in out.splitlines()

    def test_upward_low_is_counted_apart_and_only_a_comment(self):
        """A LOW loosening (positive delta) is held back just like a LOW
        tightening."""
        yaml = pytest.importorskip("yaml")
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=15.0), current="40")
        rec = rep.keys[0]
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert rec.delta_pct > 0
        assert (rep.recommended_changes, rep.low_confidence_changes) == (0, 1)
        out = tr.format_export_patch([rep])
        assert yaml.safe_load(out) is None
        assert (f"# [db-x] (low confidence) connections: recommended 59, was 40, "
                f"+47.5% — {rec.reason}") in out.splitlines()

    def test_low_comment_names_the_source_file(self):
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=15.0),
                            source_file="tenants/db-x.yaml")
        rec = rep.keys[0]
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert (f"# [db-x] (low confidence) connections: recommended 59, was 70 "
                f"(from tenants/db-x.yaml), -15.7% — {rec.reason}"
                ) in tr.format_export_patch([rep]).splitlines()

    def test_high_is_still_counted_and_exported(self):
        yaml = pytest.importorskip("yaml")
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=100.0))
        assert rep.keys[0].confidence == tr.CONFIDENCE_HIGH
        assert (rep.recommended_changes, rep.low_confidence_changes) == (1, 0)
        doc = yaml.safe_load(tr.format_export_patch([rep]))
        assert doc == {"tenants": {"db-x": {"connections": "59"}}}

    def test_lower_bound_low_under_raised_min_samples_is_excluded(self):
        """560 points with --min-samples 1000: a percentile-lower floor is LOW."""
        yaml = pytest.importorskip("yaml")
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(_floor_samples(0.97, days=8, pts_per_day=70), None)):
            rep = tr.analyze_tenant("db-x", {"db2_bufferpool_hit_ratio": "0.95"},
                                    prometheus_url="http://p:9090",
                                    observed_map=self._LOWER_MAP, min_samples=1000)
        rec = rep.keys[0]
        assert rec.sample_count == 560
        assert rec.confidence == tr.CONFIDENCE_LOW
        assert rec.recommended == pytest.approx(0.97, abs=1e-9)
        assert not tr._exportable(rec)
        assert (rep.recommended_changes, rep.low_confidence_changes) == (0, 1)
        out = tr.format_export_patch([rep])
        assert yaml.safe_load(out) is None
        assert (f"# [db-x] (low confidence) db2_bufferpool_hit_ratio: recommended 0.97, "
                f"was 0.95, {tr._delta_str(rec)} — {rec.reason}") in out.splitlines()
        assert tr._delta_str(rec).endswith("% miss")

    # ── format_export_patch ────────────────────────────────────────────
    def test_all_low_first_line_says_so(self):
        yaml = pytest.importorskip("yaml")
        rep = tr.TenantRecommendation(tenant="t", total_keys=2, keys=[
            _low_rec("a"), _low_rec("b")], low_confidence_changes=2)
        out = tr.format_export_patch([rep])
        lines = out.splitlines()
        assert lines[_HEADER_LINES] == (
            "# (no value line: 2 low-confidence recommendation(s) listed below as "
            "comments only; a value line needs HIGH/MEDIUM confidence)")
        assert "no actionable" not in out
        assert yaml.safe_load(out) is None

    def test_all_low_across_tenants_counts_every_tenant(self):
        reps = [tr.TenantRecommendation(tenant="t1", keys=[_low_rec("a")]),
                tr.TenantRecommendation(tenant="t2", keys=[_low_rec("b"), _low_rec("c")])]
        lines = tr.format_export_patch(reps).splitlines()
        assert lines[_HEADER_LINES] == (
            "# (no value line: 3 low-confidence recommendation(s) listed below as "
            "comments only; a value line needs HIGH/MEDIUM confidence)")
        assert sum("(low confidence)" in l for l in lines[_HEADER_LINES + 1:]) == 3

    def test_all_blocked_plus_low_names_both(self):
        """No value line because the one exportable key's name is not plain,
        and a LOW one beside it: the first line names both."""
        rep = tr.TenantRecommendation(tenant="t", keys=[
            _low_rec("a"), _low_rec("bad key", confidence=tr.CONFIDENCE_HIGH)])
        lines = tr.format_export_patch([rep]).splitlines()
        assert lines[_HEADER_LINES] == (
            "# (no value line: every actionable key is listed as (not written); "
            "1 low-confidence recommendation(s) listed below as comments only; "
            "a value line needs HIGH/MEDIUM confidence)")

    def test_no_low_keeps_the_no_actionable_line(self):
        rep = tr.TenantRecommendation(tenant="t", total_keys=1, keys=[
            tr.KeyRecommendation("x", "1", recommended=None, reason="skipped: unmapped")])
        lines = tr.format_export_patch([rep]).splitlines()
        assert lines[_HEADER_LINES] == "# (no actionable recommendations)"

    def test_low_beside_high_is_an_in_block_comment(self):
        yaml = pytest.importorskip("yaml")
        rep = tr.TenantRecommendation(tenant="t", total_keys=2, keys=[
            _low_rec("a", reason="few hours"),
            _low_rec("b", confidence=tr.CONFIDENCE_HIGH, reason="P95")])
        out = tr.format_export_patch([rep])
        assert yaml.safe_load(out) == {"tenants": {"t": {"b": "50"}}}
        assert ("    # (low confidence) a: recommended 50, was 100, -50.0% — few hours"
                in out.splitlines())

    @pytest.mark.parametrize("with_value_line", [False, True])
    def test_hostile_reason_stays_one_comment_line(self, with_value_line):
        yaml = pytest.importorskip("yaml")
        keys = [_low_rec("a", reason="boom\nevil: x\r'q\"\x85y: z")]
        if with_value_line:
            keys.append(_low_rec("b", confidence=tr.CONFIDENCE_HIGH, reason="P95"))
        out = tr.format_export_patch([tr.TenantRecommendation(tenant="t", keys=keys)])
        expected = ({"tenants": {"t": {"b": "50"}}} if with_value_line else None)
        assert yaml.safe_load(out) == expected
        low = [l for l in out.splitlines() if "(low confidence) a:" in l]
        assert len(low) == 1
        assert low[0].lstrip() == ("# " if with_value_line else "# [t] ") + (
            "(low confidence) a: recommended 50, was 100, -50.0% — "
            "boom?evil: x?'q\"?y: z")
        assert all(l.lstrip().startswith("#") or l.startswith(("tenants:", "  t:", "    b:"))
                   for l in out.splitlines() if l.strip())

    # ── text / JSON reports ────────────────────────────────────────────
    def test_text_report_counts_low_apart(self):
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=15.0))
        text = tr.format_text_report([rep])
        assert "Tenant: db-x (0/1 changes recommended, 1 low-confidence)" in text
        assert "Total: 0/1 keys with recommended changes; 1 low-confidence" in text

    def test_text_report_unchanged_without_low(self):
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=100.0))
        text = tr.format_text_report([rep])
        assert "Tenant: db-x (1/1 changes recommended)" in text
        assert "low-confidence" not in text

    def test_json_report_carries_low_confidence_changes(self):
        rep = self._analyze(_ts_pairs(self._BIG_DELTA, step=15.0))
        doc = json.loads(tr.format_json_report([rep]))
        assert doc["summary"]["recommended_changes"] == 0
        assert doc["summary"]["low_confidence_changes"] == 1
        assert doc["tenants"][0]["low_confidence_changes"] == 1
        assert doc["tenants"][0]["recommended_changes"] == 0


# ═══════════════════════════════════════════════════════════════════════
# --prometheus env-var fallback (W1: add_prometheus_arg / README §6.1)
# ═══════════════════════════════════════════════════════════════════════
class TestPrometheusEnvFallback:
    """`--prometheus` 走 _lib_io.add_prometheus_arg 的 canonical env fallback。

    回歸鎖（形狀照 tests/ops/test_byo_check.py::TestPrometheusEnvFallback）：
    修正前是 `os.environ.get("PROMETHEUS_URL", default)` —— 對
    present-but-empty 的 $PROMETHEUS_URL 會回空字串（key 存在），把空 URL
    塞進查詢，與 entrypoint.inject_prometheus_env 的 `if prom_url:` 語意
    分歧。help 文字（_HELP dict）逐字保留。
    """

    def _run_main_capture_url(self, monkeypatch, tmp_path, extra=()):
        captured = {}

        def fake_run_analysis(config_dir, *, prometheus_url=None, **kwargs):
            captured["url"] = prometheus_url
            return []

        monkeypatch.setattr(tr, "run_analysis", fake_run_analysis)
        monkeypatch.setattr(tr, "format_text_report", lambda reports: "")
        monkeypatch.setattr(sys, "argv", [
            "threshold_recommend.py", "--config-dir", str(tmp_path), *extra])
        tr.main()
        return captured["url"]

    @pytest.mark.parametrize("value", ["99", "0", "-1"])
    def test_min_samples_below_100_is_caller_error(self, monkeypatch, tmp_path,
                                                    capsys, value):
        """#2941: used to be accepted and change nothing."""
        monkeypatch.setattr(tr, "run_analysis", lambda *a, **k: pytest.fail("ran"))
        monkeypatch.setattr(sys, "argv", [
            "threshold_recommend.py", "--config-dir", str(tmp_path),
            "--min-samples", value])
        with pytest.raises(SystemExit) as exc:
            tr.main()
        assert exc.value.code == EXIT_CALLER_ERROR
        assert "--min-samples" in capsys.readouterr().err

    def test_uses_env_when_flag_absent(self, monkeypatch, tmp_path):
        """--prometheus 省略 + $PROMETHEUS_URL 有值 → 用 env 值。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        url = self._run_main_capture_url(monkeypatch, tmp_path)
        assert url == "http://env-prom:9090"

    def test_empty_env_falls_back_to_localhost(self, monkeypatch, tmp_path):
        """PROMETHEUS_URL 設定但為空字串（如 ConfigMap 缺鍵）→ 回退 localhost。"""
        monkeypatch.setenv("PROMETHEUS_URL", "")
        url = self._run_main_capture_url(monkeypatch, tmp_path)
        assert url == "http://localhost:9090"

    def test_explicit_flag_overrides_env(self, monkeypatch, tmp_path):
        """顯式 --prometheus 永遠蓋過 $PROMETHEUS_URL。"""
        monkeypatch.setenv("PROMETHEUS_URL", "http://env-prom:9090")
        url = self._run_main_capture_url(
            monkeypatch, tmp_path, extra=("--prometheus", "http://cli:9099"))
        assert url == "http://cli:9099"


# ═══════════════════════════════════════════════════════════════════════
# #2964: upper-bound floor — non-positive P95, tighten floor, relax to P99
# ═══════════════════════════════════════════════════════════════════════
_N_7D = 2016                         # one point per 5 min over 7 days


def _gauss(n=_N_7D, mu=100.0, sigma=10.0, seed=42):
    rnd = __import__("random").Random(seed)
    return [rnd.gauss(mu, sigma) for _ in range(n)]


def _firing(values, threshold):
    """Points an upper-bound alert fires on (value > threshold)."""
    return sum(v > float(threshold) for v in values)


class TestUpperBoundFloor2964:
    """#2964: P95 used to be the recommendation as-is. An idle metric was
    tightened to 0, a P95 the 2-dp rounding turned into 0 likewise, a
    tighten could start alerts that never fired under the current value, and
    a relax to P95 still left ~5% of points over the new threshold."""

    _MAP = {"connections": {"scope": "tenant", "direction": ">",
                            "observed_series": "tenant:x:max"}}

    def _rec(self, current, values):
        """Through analyze_tenant, so the raw points reach the engine the way
        the CLI passes them (7 days, so the 24h coverage floor never bites)."""
        pairs = _ts_pairs(values, step=7 * 86400 / len(values))
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(pairs, None)):
            report = tr.analyze_tenant("db-x", {"connections": current},
                                       prometheus_url="http://p:9090",
                                       observed_map=self._MAP)
        return report.keys[0]

    # ── ① non-positive P95: no recommendation ─────────────────────────
    def test_all_zero_gets_no_recommendation(self):
        import threshold_govern as tg
        rec = self._rec("30", [0.0] * _N_7D)
        assert rec.recommended is None
        assert rec.delta_pct is None
        assert rec.reason == "P95 is non-positive (max in window 0) — no recommendation"
        assert "idle" not in rec.reason
        assert not tr._exportable(rec)
        assert tg.is_governance_actionable(rec, 25.0) is False

    def test_p95_zero_names_the_window_max(self):
        """P95 = 0 is not idle: the current alert may have fired. The reason
        carries the window max so an operator can tell."""
        rec = self._rec("30", [0.0] * (_N_7D - 50) + [45.0] * 50)
        assert rec.recommended is None
        assert "(max in window 45)" in rec.reason

    def test_negative_values_get_no_recommendation(self):
        rec = self._rec("100", [-5.0] * _N_7D)
        assert rec.recommended is None
        assert rec.reason == "P95 is non-positive (max in window -5) — no recommendation"

    def test_unrounded_p95_decides_not_the_2dp_display(self):
        """compute_percentiles rounds 0.004 to 0.0; that display value used
        to become the recommendation (0, every point over it)."""
        values = [0.004] * _N_7D
        assert tr.compute_percentiles(values)["p95"] == 0.0
        rec = self._rec("30", values)
        assert rec.recommended == 15
        assert _firing(values, rec.recommended) == 0

    def test_direct_call_with_values_uses_unrounded_p95(self):
        values = [0.004] * 500
        rec = tr.recommend_threshold("k", 30, tr.compute_percentiles(values),
                                     500, 100, values=values)
        assert rec.recommended == 15

    def test_direct_call_without_values_judges_the_given_p95(self):
        rec = tr.recommend_threshold("k", 30, {"p50": 0.0, "p95": 0.0, "p99": 0.0},
                                     500, 100)
        assert rec.recommended is None
        assert rec.reason == "P95 is non-positive — no recommendation"

    # ── ② tighten floor ────────────────────────────────────────────────
    def test_tighten_starts_no_new_firing(self):
        """F4: a steady normal(100, 10) under current 200. P95 (~116) sits
        below ~5% of the points, so a PR to it would start alerts that never
        fired; the floor is the highest point at or below current."""
        values = _gauss()
        rec = self._rec("200", values)
        assert rec.recommended == math.ceil(max(values))
        assert _firing(values, rec.recommended) == _firing(values, 200) == 0
        assert rec.recommended > rec.p95

    def test_one_spike_above_current_does_not_block_the_tighten(self):
        values = _gauss() + [9999.0]
        rec = self._rec("200", values)
        assert rec.recommended == math.ceil(max(values[:-1]))
        assert _firing(values, rec.recommended) == _firing(values, 200) == 1

    def test_heavy_tail_tightens_without_new_firing(self):
        rnd = __import__("random").Random(3)
        values = [rnd.lognormvariate(3.0, 1.2) for _ in range(_N_7D)]
        current = 400
        rec = self._rec(str(current), values)
        assert 200 <= rec.recommended < current
        assert _firing(values, rec.recommended) == _firing(values, current)

    def test_one_round_tightens_at_most_half(self):
        rec = self._rec("30", [0.0] * int(_N_7D * 0.94)
                        + [2.0] * (_N_7D - int(_N_7D * 0.94)))
        assert rec.recommended == 15
        assert rec.delta_pct == -50.0

    def test_half_of_an_odd_integer_rounds_up(self):
        rec = self._rec("15", [2.0] * _N_7D)
        assert rec.recommended == 8
        assert isinstance(rec.recommended, int)

    @pytest.mark.parametrize("current,expected", [
        ("0.85", 0.43),     # 0.425 → up at 2 dp
        ("2.2", 1.1),       # 2.2 * 0.5 * 100 is 110.00000000000001 in float
    ])
    def test_decimal_current_floors_at_two_dp(self, current, expected):
        rec = self._rec(current, [0.01] * _N_7D)
        assert rec.recommended == expected
        assert isinstance(rec.recommended, float)

    def test_decimal_current_window_max_rounds_up(self):
        values = _gauss(mu=0.4, sigma=0.05)
        rec = self._rec("0.8", values)
        assert rec.recommended == math.ceil(max(values) * 100) / 100
        assert _firing(values, rec.recommended) == 0

    def test_direct_call_without_values_floors_at_half(self):
        rec = tr.recommend_threshold("k", 80, {"p50": 5.0, "p95": 10.0, "p99": 12.0},
                                     500, 100)
        assert rec.recommended == 40
        assert rec.delta_pct == -50.0

    def test_floor_keeps_currents_precision(self):
        """A 3-dp current rounds at 3 dp: at 2 dp the window max 0.054 would
        round up to 0.06, a +9% relax dressed as a tighten."""
        rec = self._rec("0.055", [0.054] * _N_7D)
        assert rec.recommended == 0.054
        assert rec.recommended <= 0.055
        assert not tr._exportable(rec)

    def test_points_at_current_count_as_not_firing(self):
        """A point equal to current does not fire (alert is value > threshold),
        so it holds the floor: dropping to 15 would start 60 new alerts."""
        values = [5.0] * 1956 + [30.0] * 60
        rec = self._rec("30", values)
        assert rec.recommended == 30
        assert rec.delta_pct == 0.0
        assert _firing(values, rec.recommended) == _firing(values, 30) == 0

    def test_p95_equal_to_current_is_not_a_relax(self):
        values = [10.0] * 1800 + [50.0] * 150 + [100.0] * 66
        assert tr.percentile(sorted(values), 0.95) == 50.0      # premise
        assert tr.percentile(sorted(values), 0.99) > 50.0
        rec = self._rec("50", values)
        assert rec.recommended == 50
        assert rec.reason == "within 5% margin, no change needed"

    # ── current written as a small decimal or in scientific notation ──
    @pytest.mark.parametrize("current", ["0.001", "1e-3"])
    def test_tiny_current_relax_rounds_up(self, current):
        """Rounding P99 to the nearest used to give 0.0: a "-100% relax"
        that govern and export both took."""
        values = _gauss(mu=0.002, sigma=0.0002)
        p99 = tr.percentile(sorted(values), 0.99)
        rec = self._rec(current, values)
        assert rec.recommended == math.ceil(p99 * 1000) / 1000
        assert rec.recommended >= p99 > 0.001
        assert rec.delta_pct > 0
        assert rec.reason.startswith("relaxed to P99 (increase ")

    @pytest.mark.parametrize("current", ["0.001", "1e-3"])
    def test_tiny_current_tighten_never_reaches_zero(self, current):
        """At current's own 3 dp the window max 0.0002 rounds up to 0.001:
        no change, where 2-dp rounding of P95 used to recommend 0."""
        rec = self._rec(current, [0.0002] * _N_7D)
        assert rec.recommended == 0.001
        assert rec.delta_pct == 0.0
        assert not tr._exportable(rec)

    def test_small_decimal_tightens_at_its_precision(self):
        rec = self._rec("0.004", [0.001] * _N_7D)
        assert rec.recommended == 0.002
        assert rec.delta_pct == -50.0

    def test_sci_half_is_not_an_integer(self):
        """"5e-1" has no '.', but it is 0.5, not a whole number."""
        values = _gauss(mu=1.0, sigma=0.15)
        p99 = tr.percentile(sorted(values), 0.99)
        rec = self._rec("5e-1", values)
        assert rec.recommended == math.ceil(p99 * 100) / 100
        assert isinstance(rec.recommended, float)
        assert self._rec("5e-1", [0.1] * _N_7D).recommended == 0.25

    def test_sci_whole_number_stays_integer(self):
        values = _gauss(mu=2000.0, sigma=100.0)
        rec = self._rec("1e3", values)
        assert rec.recommended == math.ceil(tr.percentile(sorted(values), 0.99))
        assert isinstance(rec.recommended, int)
        tight = self._rec("1e3", [100.0] * _N_7D)
        assert tight.recommended == 500 and isinstance(tight.recommended, int)

    # ── the exported value is the recommendation, digit for digit ─────
    @pytest.mark.parametrize("current,base,top", [
        ("0.123456789", 0.05, 0.0987654321),    # :g wrote 0.0987654
        ("200000.55", 100000.0, 150000.366),    # :g wrote 150000
    ])
    def test_export_patch_value_starts_no_new_firing(self, current, base, top):
        import yaml
        values = [base] * (_N_7D - 10) + [top] * 10
        pairs = _ts_pairs(values, step=7 * 86400 / len(values))
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(pairs, None)):
            report = tr.analyze_tenant("db-x", {"connections": current},
                                       prometheus_url="http://p:9090",
                                       observed_map=self._MAP)
        rec = report.keys[0]
        assert rec.recommended >= top                          # premise: floored
        written = yaml.safe_load(tr.format_export_patch([report]))
        value = written["tenants"]["db-x"]["connections"]
        assert float(value) == rec.recommended
        assert _firing(values, value) == _firing(values, current) == 0
        assert f"tighten floored at {value} above" in rec.reason

    @pytest.mark.parametrize("value,text", [
        (80, "80"), (80.0, "80"), (0.43, "0.43"), (0.098765433, "0.098765433"),
        (150000.37, "150000.37"), (0.00001, "0.00001"), (1e20, "100000000000000000000")])
    def test_format_threshold_value_is_lossless(self, value, text):
        assert tr._format_threshold_value(value) == text

    # ── a non-finite current (YAML .inf / .nan) ────────────────────────
    @pytest.mark.parametrize("current", [float("inf"), float("-inf"), float("nan"),
                                         "inf", "nan"])
    def test_non_finite_current_is_manual_review(self, current):
        values = _gauss()
        rec = tr.recommend_threshold("k", current, tr.compute_percentiles(values),
                                     len(values), 100, values=values)
        assert rec.recommended is None and rec.delta_pct is None
        assert rec.reason == "non-finite current value — manual review needed"

    # ── defences: unreachable today, so a broken rounding helper stands in ──
    def test_defence_relax_never_comes_out_as_a_decrease(self, monkeypatch):
        monkeypatch.setattr(tr, "_ceil_to", lambda x, places: 0.0)
        rec = self._rec("80", _gauss())
        assert rec.recommended == 80 and rec.delta_pct == 0.0
        assert "decrease" not in rec.reason and "relaxed" not in rec.reason

    def test_defence_non_positive_target_is_no_recommendation(self, monkeypatch):
        monkeypatch.setattr(tr, "_ceil_to", lambda x, places: -1)
        rec = self._rec("30", [0.004] * _N_7D)      # P95 > 0, rounds to 0
        assert rec.recommended is None
        assert rec.reason == "recommended value 0 is non-positive — no recommendation"

    def test_defence_floor_is_capped_at_current(self, monkeypatch):
        monkeypatch.setattr(tr, "_ceil_to", lambda x, places: 35)
        rec = self._rec("30", [2.0] * _N_7D)
        assert rec.recommended == 30 and rec.delta_pct == 0.0

    @pytest.mark.parametrize("current,places", [
        (80, 0), ("80", 0), ("1e3", 0), ("0.8", 2), ("0.85", 2), (0.8, 2),
        ("80.0", 2), ("5e-1", 2), ("0.001", 3), ("1e-3", 3), ("0.0005", 4),
        ("1.5e2", 2), (__import__("decimal").Decimal("1E+3"), 2)])
    def test_decimals_follow_how_current_is_written(self, current, places):
        assert tr._decimals(current) == places

    def test_floor_at_current_is_within_margin(self):
        rec = self._rec("70", [69.9] * _N_7D)
        assert rec.delta_pct == 0.0
        assert not tr._exportable(rec)

    # ── ③ relax to P99 ─────────────────────────────────────────────────
    def test_relax_uses_p99(self):
        values = _gauss()
        rec = self._rec("80", values)
        assert rec.recommended == math.ceil(tr.percentile(sorted(values), 0.99))
        assert "P99" in rec.reason and "P95" not in rec.reason
        # P99 rounded up: at most ~1% over, not P95's ~5%.
        assert _firing(values, rec.recommended) / len(values) <= 0.01

    def test_direct_call_relax_uses_p99(self):
        rec = tr.recommend_threshold("k", 80, {"p50": 50.0, "p95": 85.0, "p99": 95.0},
                                     500, 100)
        assert rec.recommended == 95

    # ── reason copy ────────────────────────────────────────────────────
    def test_reason_when_held_by_half(self):
        rec = self._rec("30", [2.0] * _N_7D)
        assert rec.reason.startswith("tighten floored at 15 above P95 2 (decrease 50.0%)")
        assert "half of current" in rec.reason
        assert "recommended at P95" not in rec.reason

    def test_reason_when_held_by_window_max(self):
        values = _gauss()
        rec = self._rec("200", values)
        top = max(values)
        assert f"highest in-window value at or below current is {top!r}" in rec.reason
        assert "half of current" not in rec.reason
        assert "recommended at P95" not in rec.reason

    def test_reason_at_p95_when_nothing_holds_it(self):
        rec = self._rec("100", [50.0] * _N_7D)
        assert rec.recommended == 50
        assert rec.reason == "recommended at P95 (decrease 50.0%)"

    @pytest.mark.parametrize("current,value", [
        ("70", 69.9),       # floor reaches current
        ("100", 96.5),      # P95 rounds to 96, the floor holds it at 97 (-3%)
    ])
    def test_reason_within_margin_has_no_tighten_note(self, current, value):
        rec = self._rec(current, [value] * _N_7D)
        assert rec.reason == "within 5% margin, no change needed"

    def test_reason_relax(self):
        rec = self._rec("80", _gauss())
        assert rec.reason.startswith("relaxed to P99 (increase ")

    # ── current = 0 (#2997: manual review, never a proposed value) ────
    @staticmethod
    def _zero_why(raw, up, top=None):
        shown = raw if raw == up else f"{raw} (rounded up to current's precision: {up})"
        seen = f", max {top}" if top is not None else ""
        return ("current threshold is 0 (fires on any positive value) — manual "
                f"review; observed P99 {shown}{seen}")

    def test_zero_current_with_activity_goes_to_manual_review(self):
        """#2997: 0 → P95 at a made-up delta of 100% always passed the
        --min-delta-pct gate. A 0 threshold fires on any positive value and
        is often meant, so it is routed to manual review instead."""
        import threshold_govern as tg
        values = _gauss()
        rec = self._rec("0", values)
        p99 = tr.percentile(sorted(values), 0.99)
        why = self._zero_why(f"{p99:.6g}", str(math.ceil(p99)),
                             tr._format_threshold_value(max(values)))
        assert rec.force_manual is True
        assert rec.recommended is None
        assert rec.delta_pct is None
        assert rec.reason == why
        assert rec.guardrail_reason == why
        # Confidence is kept as the sample-count grade (not used for gating).
        assert rec.confidence == tr.grade_confidence(len(values), tr.SAMPLE_THRESHOLD_MEDIUM)
        assert rec.confidence == tr.CONFIDENCE_HIGH
        assert not tr._exportable(rec)
        assert tg.is_governance_actionable(rec, 0.0) is False

    @pytest.mark.parametrize("current,up", [
        ("0", "1"),        # whole-number current → an int, rounded UP (not 0)
        ("0.0", "0.13"),   # 2 places, rounded UP (not 0.12)
        ("0.000", "0.124"),
    ])
    def test_zero_current_p99_is_rounded_up_in_current_format(self, current, up):
        values = [0.0] * (_N_7D - 300) + [0.1234] * 300    # P95 > 0
        rec = self._rec(current, values)
        assert rec.reason == self._zero_why("0.1234", up, "0.1234")

    def test_zero_current_p99_already_at_precision_shown_once(self):
        """No `(rounded up …)` note when rounding up changes nothing."""
        values = [0.0] * (_N_7D - 300) + [0.25] * 300
        rec = self._rec("0.0", values)
        assert rec.reason == ("current threshold is 0 (fires on any positive value) "
                              "— manual review; observed P99 0.25, max 0.25")

    def test_zero_current_direct_call_without_values_names_no_max(self):
        rec = tr.recommend_threshold("k", 0, {"p50": 0.0, "p95": 0.3, "p99": 0.42},
                                     500, 100)
        assert rec.force_manual is True and rec.delta_pct is None
        assert rec.reason == ("current threshold is 0 (fires on any positive value) "
                              "— manual review; observed P99 0.42 (rounded up to "
                              "current's precision: 1)")

    def test_zero_current_skips_the_coverage_low_suffix(self):
        """#2941 appends `→ LOW` to a short-history key's reason; a force_manual
        key carries no recommendation to downgrade, and the suffix would land
        in the text govern shows as the manual-review reason."""
        values = [0.0] * 900 + [0.3] * 100
        pairs = _ts_pairs(values, step=36.0)          # 1000 points in 10 hours
        with patch("threshold_recommend.query_prometheus_range_ts",
                   return_value=(pairs, None)):
            rec = tr.analyze_tenant("db-x", {"connections": "0"},
                                    prometheus_url="http://p:9090",
                                    observed_map=self._MAP).keys[0]
        assert rec.force_manual is True
        assert "→ LOW" not in rec.reason
        assert rec.reason == rec.guardrail_reason == self._zero_why("0.3", "1", "0.3")
        # Not downgraded either: the confidence stays the sample-count grade.
        assert rec.confidence == tr.grade_confidence(1000, tr.SAMPLE_THRESHOLD_MEDIUM)
        assert rec.confidence != tr.CONFIDENCE_LOW

    def test_zero_current_all_zero_unchanged(self):
        """#2964's all-zero case keeps its own reason and is not force_manual."""
        rec = self._rec("0", [0.0] * _N_7D)
        assert rec.force_manual is False
        assert rec.recommended is None and rec.delta_pct is None
        assert rec.reason == "P95 is non-positive (max in window 0) — no recommendation"

    def test_zero_current_in_govern_manual_review_section(self):
        """Listed and counted under manual review, with no `% miss` tail
        (that suffix is for lower-bound miss-rate deltas)."""
        import threshold_govern as tg
        report = tr.TenantRecommendation(tenant="db-x", total_keys=1,
                                         keys=[self._rec("0", _gauss())])
        fm = tg.collect_force_manual([report])
        assert [(u.tenant, u.key) for u in fm] == [("db-x", "connections")]
        assert fm[0].delta_pct is None
        assert tg.build_governance_plan([report], 0.0) == []
        text = tg.format_text_report([], [], applied=False, force_manual=fm)
        line = next(ln for ln in text.splitlines() if "db-x / connections" in ln)
        assert line.strip() == f"db-x / connections: {fm[0].guardrail_reason}"
        assert "miss" not in text
        # The header names no cause: a zero current is no guardrail trip.
        head = next(ln for ln in text.splitlines()
                    if "need manual review" in ln or "需人工 review" in ln)
        assert head in ("⚠ 1 threshold(s) need manual review "
                        "(no value proposed — reason per line):",
                        "⚠ 1 個閾值需人工 review（不提供建議值，原因見各行）：")
        out = json.loads(tg.format_json_report([], [], False, force_manual=fm))
        assert out["summary"]["force_manual"] == 1
        assert out["force_manual"][0]["delta_pct"] is None

    def test_zero_current_in_reports_and_export_patch(self):
        rec = self._rec("0", _gauss())
        report = tr.TenantRecommendation(tenant="db-x", total_keys=1, keys=[rec])
        assert f"└─ {rec.reason}" in tr.format_text_report([report])
        assert f"| {rec.reason} |" in tr.format_markdown_report([report])
        assert (f"(manual) connections: {rec.guardrail_reason}"
                in tr.format_export_patch([report]))
        data = json.loads(tr.format_json_report([report]))
        assert set(data["tenants"][0]["keys"][0]) == set(
            tr.asdict(tr.KeyRecommendation(key="k", current_value=1)))

    # ── multi-round: each recommendation becomes the next current ─────
    @staticmethod
    def _rounds(rec_fn, current, values, rounds=30):
        import threshold_govern as tg
        path = [current]
        for _ in range(rounds):
            rec = rec_fn(current, values)
            if not tg.is_governance_actionable(rec, 25.0):
                break
            current = tr._format_threshold_value(rec.recommended)
            path.append(current)
        return path

    def test_rounds_idle_opens_no_pr(self):
        assert self._rounds(self._rec, "1000", [0.0] * _N_7D) == ["1000"]

    def test_rounds_steady_metric_settles_in_one(self):
        path = self._rounds(self._rec, "200", _gauss())
        assert len(path) == 2

    def test_rounds_mostly_zero_steps_down_by_half(self):
        values = [0.0] * int(_N_7D * 0.94) + [2.0] * (_N_7D - int(_N_7D * 0.94))
        assert self._rounds(self._rec, "30", values) == ["30", "15", "8", "4", "2"]

    # ── backtest agrees: the tighten starts no firing ──────────────────
    def test_backtest_rates_the_tighten_low(self, monkeypatch):
        import backtest_threshold as bt
        values = _gauss() + [9999.0]
        rec = self._rec("200", values)
        monkeypatch.setattr(bt, "query_range", lambda *a, **kw: [
            {"values": [[i, str(v)] for i, v in enumerate(values)]}])
        # #3017: a key a shipped alert compares (`>`); a made-up key is not backtested.
        change = {"tenant": "db-x", "metric": "mysql_connections", "old_value": "200",
                  "new_value": tr._format_threshold_value(rec.recommended)}
        result = bt.backtest_change("http://prom", change, 7 * 86400)
        assert result["status"] == "analyzed"
        assert result["risk"] == "LOW"
        assert result["new_breach_count"] == result["old_breach_count"] == 1
