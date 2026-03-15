#!/usr/bin/env python3
"""pytest style tests for baseline_discovery.py — pure logic functions.

Covers:
- extract_scalar(): Prometheus result parsing
- percentile(): linear interpolation
- compute_stats(): statistical summary
- suggest_threshold(): threshold recommendation logic
- query_prometheus(): error handling (mocked)
- main(): CLI dry-run mode
"""

import math
import sys

import pytest

# Make baseline_discovery importable

import baseline_discovery  # noqa: E402


# ── extract_scalar ─────────────────────────────────────────────────


class TestExtractScalar:
    """extract_scalar() extracts first numeric value from Prometheus results."""

    def test_valid_result(self):
        """Normal Prometheus vector result returns float."""
        results = [{"metric": {}, "value": [1234567890, "42.5"]}]
        assert abs(baseline_discovery.extract_scalar(results) - 42.5) < 1e-5

    def test_empty_results(self):
        """Empty result list returns None."""
        assert baseline_discovery.extract_scalar([]) is None

    def test_none_results(self):
        """None input returns None."""
        assert baseline_discovery.extract_scalar(None) is None

    def test_nan_value(self):
        """NaN value returns None."""
        results = [{"metric": {}, "value": [0, "NaN"]}]
        assert baseline_discovery.extract_scalar(results) is None

    def test_inf_value(self):
        """Inf value returns None."""
        results = [{"metric": {}, "value": [0, "Inf"]}]
        assert baseline_discovery.extract_scalar(results) is None

    def test_non_numeric_value(self):
        """Non-numeric string returns None."""
        results = [{"metric": {}, "value": [0, "not-a-number"]}]
        assert baseline_discovery.extract_scalar(results) is None

    def test_missing_value_key(self):
        """Result without 'value' key returns None."""
        results = [{"metric": {}}]
        assert baseline_discovery.extract_scalar(results) is None

    def test_zero_value(self):
        """Zero is a valid value, not None."""
        results = [{"metric": {}, "value": [0, "0"]}]
        assert baseline_discovery.extract_scalar(results) == 0.0


# ── percentile ─────────────────────────────────────────────────────


class TestPercentile:
    """percentile() computes percentile via linear interpolation."""

    @pytest.mark.parametrize("values,p,expected", [
        ([1, 2, 3, 4, 5], 50, 3.0),
        ([1, 2, 3, 4], 50, 2.5),
        ([10, 20, 30], 0, 10.0),
        ([10, 20, 30], 100, 30.0),
        ([42], 50, 42.0),
        ([42], 99, 42.0),
    ], ids=["p50-odd", "p50-even", "p0-min", "p100-max",
            "single-p50", "single-p99"])
    def test_percentile_values(self, values, p, expected):
        """各種百分位數正確計算。"""
        assert abs(baseline_discovery.percentile(values, p) - expected) < 1e-5

    def test_empty_list(self):
        """空清單回傳 None。"""
        assert baseline_discovery.percentile([], 50) is None

    def test_p95_interpolation(self):
        """p95 百元素序列正確內插。"""
        values = list(range(1, 101))  # 1..100
        result = baseline_discovery.percentile(values, 95)
        expected = 95 + 0.05 * (96 - 95)
        assert abs(result - expected) < 1e-5


# ── compute_stats ──────────────────────────────────────────────────


class TestComputeStats:
    """compute_stats() computes statistical summary from samples。"""

    def test_normal_samples(self):
        """10 valid samples produce correct stats。"""
        samples = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        stats = baseline_discovery.compute_stats(samples)
        assert stats["count"] == 10
        assert stats["min"] == 10
        assert stats["max"] == 100
        assert abs(stats["avg"] - 55.0) < 1e-5
        assert abs(stats["p50"] - 55.0) < 1e-5
        assert stats["p90"] is not None
        assert stats["p95"] is not None
        assert stats["p99"] is not None

    def test_with_none_values(self):
        """None values are filtered out before computation。"""
        samples = [10, None, 30, None, 50]
        stats = baseline_discovery.compute_stats(samples)
        assert stats["count"] == 3
        assert stats["min"] == 10
        assert stats["max"] == 50

    def test_all_none(self):
        """All None returns count=0 and None for all stats。"""
        stats = baseline_discovery.compute_stats([None, None, None])
        assert stats["count"] == 0
        assert stats["min"] is None
        assert stats["max"] is None
        assert stats["avg"] is None

    def test_empty_list(self):
        """Empty list returns count=0。"""
        stats = baseline_discovery.compute_stats([])
        assert stats["count"] == 0

    def test_single_sample(self):
        """Single valid sample: min == max == avg == all percentiles。"""
        stats = baseline_discovery.compute_stats([42])
        assert stats["count"] == 1
        assert stats["min"] == 42
        assert stats["max"] == 42
        assert abs(stats["avg"] - 42.0) < 1e-5
        assert abs(stats["p50"] - 42.0) < 1e-5


# ── suggest_threshold ──────────────────────────────────────────────


class TestSuggestThreshold:
    """suggest_threshold() recommends warning/critical thresholds。"""

    def test_sufficient_samples(self):
        """With >=10 samples, warning = p95*1.2, critical = p99*1.5。"""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 90, "p99": 95,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        assert abs(result["warning"] - round(90 * 1.2, 2)) < 1e-5
        assert abs(result["critical"] - round(95 * 1.5, 2)) < 1e-5

    def test_insufficient_samples(self):
        """With <10 samples, returns None thresholds。"""
        stats = {"count": 5, "p95": 90, "p99": 95}
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        assert result["warning"] is None
        assert result["critical"] is None
        assert "樣本不足" in result["note"]

    def test_connections_rounds_up(self):
        """Connections metric rounds thresholds to int (ceil)。"""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 83.3, "p99": 91.7,
        }
        result = baseline_discovery.suggest_threshold(stats, "connections")
        # 83.3 * 1.2 = 99.96 -> ceil -> 100
        assert isinstance(result["warning"], int)
        assert result["warning"] == math.ceil(83.3 * 1.2)
        # 91.7 * 1.5 = 137.55 -> ceil -> 138
        assert isinstance(result["critical"], int)
        assert result["critical"] == math.ceil(91.7 * 1.5)

    def test_zero_p95_returns_none(self):
        """p95 == 0 means metric not active, warning should be None。"""
        stats = {
            "count": 100, "min": 0, "max": 0, "avg": 0,
            "p50": 0, "p90": 0, "p95": 0, "p99": 0,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        assert result["warning"] is None

    def test_note_present(self):
        """Result always includes a note field。"""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 90, "p99": 95,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        assert "note" in result
        assert len(result["note"]) > 0


# ── DEFAULT_METRICS ────────────────────────────────────────────────


class TestDefaultMetrics:
    """DEFAULT_METRICS structure and format-string safety。"""

    def test_all_metrics_have_required_keys(self):
        """Every metric must have query, unit, description。"""
        for key, info in baseline_discovery.DEFAULT_METRICS.items():
            assert "query" in info, f"{key} missing 'query'"
            assert "unit" in info, f"{key} missing 'unit'"
            assert "description" in info, f"{key} missing 'description'"

    def test_queries_have_tenant_placeholder(self):
        """Every query must have {tenant} placeholder。"""
        for key, info in baseline_discovery.DEFAULT_METRICS.items():
            # format should work without error
            try:
                formatted = info["query"].format(tenant="test-tenant")
            except KeyError as e:
                raise AssertionError(f"{key} query has unexpected placeholder: {e}")
            assert "test-tenant" in formatted, \
                f"{key} query doesn't embed tenant after format"

    def test_known_metric_keys(self):
        """Expected metric keys are present。"""
        expected = {"connections", "cpu", "slow_queries", "memory", "disk_io"}
        actual = set(baseline_discovery.DEFAULT_METRICS.keys())
        assert expected == actual


# ── query_prometheus（mock http_get_json）─────────────────────────


class TestQueryPrometheus:
    """query_prometheus() Prometheus API 查詢（mock）。"""

    def test_success(self, monkeypatch):
        """成功查詢回傳 results。"""
        def mock_get(url):
            return {
                "status": "success",
                "data": {"result": [{"metric": {}, "value": [0, "42"]}]},
            }, None
        monkeypatch.setattr(baseline_discovery, "http_get_json", mock_get)
        results, err = baseline_discovery.query_prometheus(
            "http://localhost:9090", "up")
        assert err is None
        assert len(results) == 1

    def test_http_error(self, monkeypatch):
        """HTTP 錯誤回傳 error。"""
        def mock_get(url):
            return None, "connection refused"
        monkeypatch.setattr(baseline_discovery, "http_get_json", mock_get)
        results, err = baseline_discovery.query_prometheus(
            "http://localhost:9090", "up")
        assert results is None
        assert "connection refused" in err

    def test_api_error_status(self, monkeypatch):
        """Prometheus API 回傳非 success 狀態。"""
        def mock_get(url):
            return {"status": "error", "error": "bad query"}, None
        monkeypatch.setattr(baseline_discovery, "http_get_json", mock_get)
        results, err = baseline_discovery.query_prometheus(
            "http://localhost:9090", "bad{")
        assert results is None
        assert "bad query" in err

    def test_empty_result(self, monkeypatch):
        """查詢成功但無資料回傳空 list。"""
        def mock_get(url):
            return {"status": "success", "data": {"result": []}}, None
        monkeypatch.setattr(baseline_discovery, "http_get_json", mock_get)
        results, err = baseline_discovery.query_prometheus(
            "http://localhost:9090", "nonexistent_metric")
        assert err is None
        assert results == []

    def test_url_encoding(self, monkeypatch):
        """PromQL 查詢正確 URL 編碼。"""
        captured_urls = []
        def mock_get(url):
            captured_urls.append(url)
            return {"status": "success", "data": {"result": []}}, None
        monkeypatch.setattr(baseline_discovery, "http_get_json", mock_get)
        baseline_discovery.query_prometheus(
            "http://localhost:9090", 'rate(metric{label="val"}[5m])')
        assert len(captured_urls) == 1
        assert "api/v1/query" in captured_urls[0]


# ── main() CLI ────────────────────────────────────────────────────


class TestMainCLI:
    """main() CLI 整合測試。"""

    def test_dry_run(self, capsys, monkeypatch):
        """--dry-run 模式列出指標但不實際查詢。"""
        monkeypatch.setattr(sys, "argv", [
            "baseline_discovery", "--tenant", "db-a", "--dry-run"])
        baseline_discovery.main()
        out = capsys.readouterr().out
        assert "Dry Run" in out
        assert "db-a" in out
        assert "connections" in out

    def test_dry_run_with_specific_metrics(self, capsys, monkeypatch):
        """--dry-run + --metrics 只顯示指定指標。"""
        monkeypatch.setattr(sys, "argv", [
            "baseline_discovery", "--tenant", "db-a",
            "--metrics", "cpu,memory", "--dry-run"])
        baseline_discovery.main()
        out = capsys.readouterr().out
        assert "cpu" in out
        assert "memory" in out

    def test_unknown_metric_warning(self, capsys, monkeypatch):
        """未知指標顯示警告到 stderr。"""
        monkeypatch.setattr(sys, "argv", [
            "baseline_discovery", "--tenant", "db-a",
            "--metrics", "cpu,nonexistent", "--dry-run"])
        baseline_discovery.main()
        err = capsys.readouterr().err
        assert "未知指標" in err
        assert "nonexistent" in err

    def test_all_unknown_metrics_exits(self, capsys, monkeypatch):
        """全部指標未知時 exit(1)。"""
        monkeypatch.setattr(sys, "argv", [
            "baseline_discovery", "--tenant", "db-a",
            "--metrics", "bogus1,bogus2", "--dry-run"])
        with pytest.raises(SystemExit) as exc_info:
            baseline_discovery.main()
        assert exc_info.value.code == 1
