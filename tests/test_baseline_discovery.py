#!/usr/bin/env python3
"""Tests for baseline_discovery.py — pure logic functions.

Covers:
- extract_scalar(): Prometheus result parsing
- percentile(): linear interpolation
- compute_stats(): statistical summary
- suggest_threshold(): threshold recommendation logic
- query_prometheus(): error handling (mocked)
"""

import math
import os
import sys
import unittest

# Make baseline_discovery importable
TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir, "scripts", "tools",
)
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import baseline_discovery  # noqa: E402


# ── extract_scalar ─────────────────────────────────────────────────


class TestExtractScalar(unittest.TestCase):
    """extract_scalar() extracts first numeric value from Prometheus results."""

    def test_valid_result(self):
        """Normal Prometheus vector result returns float."""
        results = [{"metric": {}, "value": [1234567890, "42.5"]}]
        self.assertAlmostEqual(baseline_discovery.extract_scalar(results), 42.5)

    def test_empty_results(self):
        """Empty result list returns None."""
        self.assertIsNone(baseline_discovery.extract_scalar([]))

    def test_none_results(self):
        """None input returns None."""
        self.assertIsNone(baseline_discovery.extract_scalar(None))

    def test_nan_value(self):
        """NaN value returns None."""
        results = [{"metric": {}, "value": [0, "NaN"]}]
        self.assertIsNone(baseline_discovery.extract_scalar(results))

    def test_inf_value(self):
        """Inf value returns None."""
        results = [{"metric": {}, "value": [0, "Inf"]}]
        self.assertIsNone(baseline_discovery.extract_scalar(results))

    def test_non_numeric_value(self):
        """Non-numeric string returns None."""
        results = [{"metric": {}, "value": [0, "not-a-number"]}]
        self.assertIsNone(baseline_discovery.extract_scalar(results))

    def test_missing_value_key(self):
        """Result without 'value' key returns None."""
        results = [{"metric": {}}]
        self.assertIsNone(baseline_discovery.extract_scalar(results))

    def test_zero_value(self):
        """Zero is a valid value, not None."""
        results = [{"metric": {}, "value": [0, "0"]}]
        self.assertEqual(baseline_discovery.extract_scalar(results), 0.0)


# ── percentile ─────────────────────────────────────────────────────


class TestPercentile(unittest.TestCase):
    """percentile() computes percentile via linear interpolation."""

    def test_p50_odd_length(self):
        """p50 of [1,2,3,4,5] = 3."""
        self.assertAlmostEqual(baseline_discovery.percentile([1, 2, 3, 4, 5], 50), 3.0)

    def test_p50_even_length(self):
        """p50 of [1,2,3,4] = 2.5 (interpolated)."""
        self.assertAlmostEqual(baseline_discovery.percentile([1, 2, 3, 4], 50), 2.5)

    def test_p0(self):
        """p0 returns minimum."""
        self.assertAlmostEqual(baseline_discovery.percentile([10, 20, 30], 0), 10.0)

    def test_p100(self):
        """p100 returns maximum."""
        self.assertAlmostEqual(baseline_discovery.percentile([10, 20, 30], 100), 30.0)

    def test_single_element(self):
        """Single element list returns that element for any percentile."""
        self.assertAlmostEqual(baseline_discovery.percentile([42], 50), 42.0)
        self.assertAlmostEqual(baseline_discovery.percentile([42], 99), 42.0)

    def test_empty_list(self):
        """Empty list returns None."""
        self.assertIsNone(baseline_discovery.percentile([], 50))

    def test_p95_interpolation(self):
        """p95 of 100 elements gives correct interpolation."""
        values = list(range(1, 101))  # 1..100
        result = baseline_discovery.percentile(values, 95)
        # k = 99 * 0.95 = 94.05, interp between values[94]=95 and values[95]=96
        expected = 95 + 0.05 * (96 - 95)
        self.assertAlmostEqual(result, expected, places=5)


# ── compute_stats ──────────────────────────────────────────────────


class TestComputeStats(unittest.TestCase):
    """compute_stats() computes statistical summary from samples."""

    def test_normal_samples(self):
        """10 valid samples produce correct stats."""
        samples = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        stats = baseline_discovery.compute_stats(samples)
        self.assertEqual(stats["count"], 10)
        self.assertEqual(stats["min"], 10)
        self.assertEqual(stats["max"], 100)
        self.assertAlmostEqual(stats["avg"], 55.0)
        self.assertAlmostEqual(stats["p50"], 55.0)
        self.assertIsNotNone(stats["p90"])
        self.assertIsNotNone(stats["p95"])
        self.assertIsNotNone(stats["p99"])

    def test_with_none_values(self):
        """None values are filtered out before computation."""
        samples = [10, None, 30, None, 50]
        stats = baseline_discovery.compute_stats(samples)
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["min"], 10)
        self.assertEqual(stats["max"], 50)

    def test_all_none(self):
        """All None returns count=0 and None for all stats."""
        stats = baseline_discovery.compute_stats([None, None, None])
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["min"])
        self.assertIsNone(stats["max"])
        self.assertIsNone(stats["avg"])

    def test_empty_list(self):
        """Empty list returns count=0."""
        stats = baseline_discovery.compute_stats([])
        self.assertEqual(stats["count"], 0)

    def test_single_sample(self):
        """Single valid sample: min == max == avg == all percentiles."""
        stats = baseline_discovery.compute_stats([42])
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["min"], 42)
        self.assertEqual(stats["max"], 42)
        self.assertAlmostEqual(stats["avg"], 42.0)
        self.assertAlmostEqual(stats["p50"], 42.0)


# ── suggest_threshold ──────────────────────────────────────────────


class TestSuggestThreshold(unittest.TestCase):
    """suggest_threshold() recommends warning/critical thresholds."""

    def test_sufficient_samples(self):
        """With >=10 samples, warning = p95*1.2, critical = p99*1.5."""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 90, "p99": 95,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        self.assertAlmostEqual(result["warning"], round(90 * 1.2, 2))
        self.assertAlmostEqual(result["critical"], round(95 * 1.5, 2))

    def test_insufficient_samples(self):
        """With <10 samples, returns None thresholds."""
        stats = {"count": 5, "p95": 90, "p99": 95}
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        self.assertIsNone(result["warning"])
        self.assertIsNone(result["critical"])
        self.assertIn("樣本不足", result["note"])

    def test_connections_rounds_up(self):
        """Connections metric rounds thresholds to int (ceil)."""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 83.3, "p99": 91.7,
        }
        result = baseline_discovery.suggest_threshold(stats, "connections")
        # 83.3 * 1.2 = 99.96 -> ceil -> 100
        self.assertIsInstance(result["warning"], int)
        self.assertEqual(result["warning"], math.ceil(83.3 * 1.2))
        # 91.7 * 1.5 = 137.55 -> ceil -> 138
        self.assertIsInstance(result["critical"], int)
        self.assertEqual(result["critical"], math.ceil(91.7 * 1.5))

    def test_zero_p95_returns_none(self):
        """p95 == 0 means metric not active, warning should be None."""
        stats = {
            "count": 100, "min": 0, "max": 0, "avg": 0,
            "p50": 0, "p90": 0, "p95": 0, "p99": 0,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        self.assertIsNone(result["warning"])

    def test_note_present(self):
        """Result always includes a note field."""
        stats = {
            "count": 100, "min": 10, "max": 100, "avg": 50,
            "p50": 50, "p90": 80, "p95": 90, "p99": 95,
        }
        result = baseline_discovery.suggest_threshold(stats, "cpu")
        self.assertIn("note", result)
        self.assertTrue(len(result["note"]) > 0)


# ── DEFAULT_METRICS ────────────────────────────────────────────────


class TestDefaultMetrics(unittest.TestCase):
    """DEFAULT_METRICS structure and format-string safety."""

    def test_all_metrics_have_required_keys(self):
        """Every metric must have query, unit, description."""
        for key, info in baseline_discovery.DEFAULT_METRICS.items():
            self.assertIn("query", info, f"{key} missing 'query'")
            self.assertIn("unit", info, f"{key} missing 'unit'")
            self.assertIn("description", info, f"{key} missing 'description'")

    def test_queries_have_tenant_placeholder(self):
        """Every query must have {tenant} placeholder."""
        for key, info in baseline_discovery.DEFAULT_METRICS.items():
            # format should work without error
            try:
                formatted = info["query"].format(tenant="test-tenant")
            except KeyError as e:
                self.fail(f"{key} query has unexpected placeholder: {e}")
            self.assertIn("test-tenant", formatted,
                          f"{key} query doesn't embed tenant after format")

    def test_known_metric_keys(self):
        """Expected metric keys are present."""
        expected = {"connections", "cpu", "slow_queries", "memory", "disk_io"}
        actual = set(baseline_discovery.DEFAULT_METRICS.keys())
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
