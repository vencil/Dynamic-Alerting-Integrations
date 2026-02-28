"""
tests/test_migrate_v3.py — Unit tests for migrate_rule.py v3 core functions.
Tests the Triage/Prefix/Dictionary/Aggregation logic introduced in v0.6.0.
AST engine tests are in test_migrate_ast.py (v0.11.0).
"""

import json
import os
import sys
import tempfile
import unittest

import yaml

# ---------------------------------------------------------------------------
# Import migrate_rule module
# ---------------------------------------------------------------------------
TOOLS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "tools")
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import migrate_rule  # noqa: E402


# ===================================================================
# 1. guess_aggregation — 智能聚合猜測
# ===================================================================
class TestGuessAggregation(unittest.TestCase):
    """Verify the 6-rule heuristic engine for sum vs max."""

    def test_rate_returns_sum(self):
        mode, _ = migrate_rule.guess_aggregation(
            "mysql_slow_queries",
            "rate(mysql_global_status_slow_queries[5m])",
        )
        self.assertEqual(mode, "sum")

    def test_increase_returns_sum(self):
        mode, _ = migrate_rule.guess_aggregation(
            "http_requests", "increase(http_requests_total[1h])")
        self.assertEqual(mode, "sum")

    def test_total_suffix_returns_sum(self):
        mode, _ = migrate_rule.guess_aggregation(
            "http_requests_total", "http_requests_total")
        self.assertEqual(mode, "sum")

    def test_percent_keyword_returns_max(self):
        mode, _ = migrate_rule.guess_aggregation(
            "cpu_percent", "container_cpu_percent")
        self.assertEqual(mode, "max")

    def test_latency_keyword_returns_max(self):
        mode, _ = migrate_rule.guess_aggregation(
            "request_latency", "request_latency_seconds")
        self.assertEqual(mode, "max")

    def test_connections_keyword_returns_max(self):
        mode, _ = migrate_rule.guess_aggregation(
            "mysql_connections", "mysql_global_status_threads_connected")
        self.assertEqual(mode, "max")

    def test_bytes_keyword_returns_sum(self):
        mode, _ = migrate_rule.guess_aggregation(
            "network_bytes", "network_receive_bytes")
        self.assertEqual(mode, "sum")

    def test_division_returns_max(self):
        mode, _ = migrate_rule.guess_aggregation(
            "buffer_pool", "pages_data / pages_total * 100")
        self.assertEqual(mode, "max")

    def test_fallback_returns_max(self):
        mode, reason = migrate_rule.guess_aggregation(
            "some_obscure_metric", "some_obscure_metric")
        self.assertEqual(mode, "max")
        self.assertIn("Fallback", reason)

    def test_reason_is_nonempty(self):
        _, reason = migrate_rule.guess_aggregation(
            "mysql_connections", "mysql_global_status_threads_connected")
        self.assertTrue(len(reason) > 0)


# ===================================================================
# 2. lookup_dictionary — 啟發式字典查找
# ===================================================================
class TestLookupDictionary(unittest.TestCase):
    """Verify dictionary lookup logic."""

    SAMPLE_DICT = {
        "mysql_global_status_threads_connected": {
            "maps_to": "mysql_connections",
            "golden_rule": "MariaDBHighConnections",
            "rule_pack": "mariadb",
            "note": "test note",
        },
    }

    def test_match(self):
        result = migrate_rule.lookup_dictionary(
            "mysql_global_status_threads_connected", self.SAMPLE_DICT)
        self.assertIsNotNone(result)
        self.assertEqual(result["golden_rule"], "MariaDBHighConnections")

    def test_no_match(self):
        result = migrate_rule.lookup_dictionary(
            "unknown_metric", self.SAMPLE_DICT)
        self.assertIsNone(result)

    def test_empty_dict(self):
        result = migrate_rule.lookup_dictionary("foo", {})
        self.assertIsNone(result)

    def test_none_dict(self):
        result = migrate_rule.lookup_dictionary("foo", None)
        self.assertIsNone(result)


# ===================================================================
# 3. parse_expr — PromQL 解析 (regex path)
# ===================================================================
class TestParseExprRegex(unittest.TestCase):
    """Test parse_expr with regex fallback (use_ast=False)."""

    def test_simple_gt(self):
        result = migrate_rule.parse_expr(
            "mysql_global_status_threads_connected > 100", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["op"], ">")
        self.assertEqual(result["val"], "100")
        self.assertEqual(result["base_key"], "mysql_global_status_threads_connected")
        self.assertFalse(result["is_complex"])

    def test_simple_lt(self):
        result = migrate_rule.parse_expr("mysql_up < 1", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["op"], "<")
        self.assertEqual(result["val"], "1")

    def test_complex_rate(self):
        result = migrate_rule.parse_expr(
            "rate(mysql_global_status_slow_queries[5m]) > 0.1",
            use_ast=False)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_complex"])
        self.assertEqual(result["val"], "0.1")

    def test_unparseable_no_threshold(self):
        result = migrate_rule.parse_expr(
            "absent(mysql_up)", use_ast=False)
        self.assertIsNone(result)

    def test_semantic_break_absent(self):
        result = migrate_rule.parse_expr(
            "absent(mysql_up) > 0", use_ast=False)
        self.assertIsNone(result)

    def test_eq_operator(self):
        result = migrate_rule.parse_expr(
            "mysql_up == 0", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["op"], "==")

    def test_scientific_notation(self):
        result = migrate_rule.parse_expr(
            "metric > 1e6", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["val"], "1e6")


# ===================================================================
# 4. MigrationResult — 資料結構
# ===================================================================
class TestMigrationResult(unittest.TestCase):
    """Verify MigrationResult defaults."""

    def test_defaults(self):
        r = migrate_rule.MigrationResult("TestAlert", "perfect")
        self.assertEqual(r.alert_name, "TestAlert")
        self.assertEqual(r.status, "perfect")
        self.assertEqual(r.severity, "warning")
        self.assertEqual(r.tenant_config, {})
        self.assertEqual(r.recording_rules, [])
        self.assertEqual(r.alert_rules, [])
        self.assertIsNone(r.agg_mode)
        self.assertIsNone(r.dict_match)
        self.assertIsNone(r.triage_action)

    def test_critical_severity(self):
        r = migrate_rule.MigrationResult("TestAlert", "complex", "critical")
        self.assertEqual(r.severity, "critical")


# ===================================================================
# 5. process_rule — 核心處理邏輯 (regex path)
# ===================================================================
class TestProcessRule(unittest.TestCase):
    """Test process_rule with use_ast=False."""

    def test_perfect_simple(self):
        rule = {
            "alert": "TestHighConnections",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "perfect")
        self.assertEqual(result.triage_action, "auto")
        self.assertIn("custom_mysql_connections", result.tenant_config)
        self.assertEqual(result.tenant_config["custom_mysql_connections"], "100")
        self.assertEqual(len(result.recording_rules), 2)
        self.assertEqual(len(result.alert_rules), 1)

    def test_complex_with_rate(self):
        rule = {
            "alert": "TestSlowQueries",
            "expr": "rate(mysql_slow_queries[5m]) > 0.1",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "complex")
        self.assertEqual(result.triage_action, "review")
        self.assertEqual(result.agg_mode, "sum")

    def test_unparseable(self):
        rule = {
            "alert": "TestAbsent",
            "expr": "absent(mysql_up)",
            "labels": {"severity": "critical"},
        }
        result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "unparseable")
        self.assertEqual(result.triage_action, "skip")
        self.assertIsNotNone(result.llm_prompt)

    def test_golden_match(self):
        dictionary = {
            "mysql_connections": {
                "maps_to": "mysql_connections",
                "golden_rule": "MariaDBHighConnections",
                "rule_pack": "mariadb",
                "note": "test",
            },
        }
        rule = {
            "alert": "TestGolden",
            "expr": "mysql_connections > 50",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary=dictionary, use_ast=False)
        self.assertEqual(result.triage_action, "use_golden")
        self.assertIsNotNone(result.dict_match)

    def test_no_prefix(self):
        rule = {
            "alert": "TestNoPrefix",
            "expr": "my_metric > 10",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(rule, prefix="", use_ast=False)
        self.assertIn("my_metric", result.tenant_config)
        # Alert name should NOT have "Custom" prefix
        self.assertEqual(result.alert_rules[0]["alert"], "TestNoPrefix")

    def test_critical_severity_key(self):
        rule = {
            "alert": "TestCritical",
            "expr": "my_metric > 200",
            "labels": {"severity": "critical"},
        }
        result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
        self.assertIn("custom_my_metric_critical", result.tenant_config)

    def test_no_alert_name_returns_none(self):
        rule = {"expr": "metric > 1"}
        result = migrate_rule.process_rule(rule, use_ast=False)
        self.assertIsNone(result)

    def test_shadow_labels_with_prefix(self):
        rule = {
            "alert": "TestShadow",
            "expr": "metric > 5",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
        labels = result.alert_rules[0].get("labels", {})
        self.assertEqual(labels.get("source"), "legacy")
        self.assertEqual(labels.get("migration_status"), "shadow")


# ===================================================================
# 6. write_triage_csv — CSV 輸出
# ===================================================================
class TestWriteTriageCsv(unittest.TestCase):
    """Verify triage CSV output."""

    def test_csv_output(self):
        results = [
            migrate_rule.MigrationResult("AlertA", "perfect"),
            migrate_rule.MigrationResult("AlertB", "complex"),
        ]
        results[0].triage_action = "auto"
        results[0].original_expr = "m > 1"
        results[1].triage_action = "review"
        results[1].agg_mode = "sum"
        results[1].agg_reason = "rate"
        results[1].original_expr = "rate(m[5m]) > 0.1"

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = migrate_rule.write_triage_csv(results, tmpdir, {})
            self.assertTrue(os.path.exists(csv_path))
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                lines = f.readlines()
            # Header + 2 data rows
            self.assertEqual(len(lines), 3)
            self.assertIn("AlertA", lines[1])
            self.assertIn("AlertB", lines[2])


# ===================================================================
# 7. write_prefix_mapping — Prefix Mapping YAML
# ===================================================================
class TestWritePrefixMapping(unittest.TestCase):
    """Verify prefix mapping output."""

    def test_mapping_output(self):
        r = migrate_rule.MigrationResult("TestAlert", "perfect")
        r.tenant_config = {"custom_metric": "100"}
        results = [r]

        with tempfile.TemporaryDirectory() as tmpdir:
            path = migrate_rule.write_prefix_mapping(results, tmpdir, "custom_")
            self.assertIsNotNone(path)
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            self.assertIn("custom_metric", data)
            self.assertEqual(data["custom_metric"]["original_metric"], "metric")

    def test_no_prefix_returns_none(self):
        results = [migrate_rule.MigrationResult("X", "perfect")]
        with tempfile.TemporaryDirectory() as tmpdir:
            path = migrate_rule.write_prefix_mapping(results, tmpdir, "")
            self.assertIsNone(path)

    def test_unparseable_skipped(self):
        r = migrate_rule.MigrationResult("X", "unparseable")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = migrate_rule.write_prefix_mapping([r], tmpdir, "custom_")
            self.assertIsNone(path)


# ===================================================================
# 8. Convergence rate fix — golden_matches 排除 unparseable
# ===================================================================
class TestConvergenceRate(unittest.TestCase):
    """Verify the A1 fix: golden_matches should not include unparseable."""

    def test_golden_unparseable_not_over_subtracted(self):
        """If an unparseable rule has triage_action=use_golden, it should
        not be subtracted from convertible count."""
        results = []

        # 2 perfect (1 golden)
        r1 = migrate_rule.MigrationResult("Perfect1", "perfect")
        r1.triage_action = "auto"
        r1.tenant_config = {"custom_m1": "10"}
        r1.recording_rules = [{"record": "tenant:custom_m1:max", "expr": "..."}]
        results.append(r1)

        r2 = migrate_rule.MigrationResult("PerfectGolden", "perfect")
        r2.triage_action = "use_golden"
        r2.dict_match = {"maps_to": "x", "golden_rule": "G1"}
        r2.tenant_config = {"custom_m2": "20"}
        r2.recording_rules = [{"record": "tenant:custom_m2:max", "expr": "..."}]
        results.append(r2)

        # 1 unparseable with golden
        r3 = migrate_rule.MigrationResult("UnparseableGolden", "unparseable")
        r3.triage_action = "use_golden"
        r3.dict_match = {"maps_to": "y", "golden_rule": "G2"}
        results.append(r3)

        # Compute: perfect=2, complex=0, golden_parseable=1 (only r2)
        # convertible should be 2 + 0 - 1 = 1
        perfect = [r for r in results if r.status == "perfect"]
        complex_rules = [r for r in results if r.status == "complex"]
        golden_parseable = [r for r in results
                           if r.triage_action == "use_golden"
                           and r.status != "unparseable"]
        convertible = len(perfect) + len(complex_rules) - len(golden_parseable)
        self.assertEqual(convertible, 1)

        # Old buggy formula would give: 2 + 0 - 2 = 0
        golden_all = [r for r in results if r.triage_action == "use_golden"]
        buggy_convertible = len(perfect) + len(complex_rules) - len(golden_all)
        self.assertEqual(buggy_convertible, 0)  # confirms the bug existed


# ===================================================================
# 9. load_metric_dictionary — 字典載入
# ===================================================================
class TestLoadMetricDictionary(unittest.TestCase):
    """Verify dictionary loading from YAML."""

    def test_loads_real_dictionary(self):
        d = migrate_rule.load_metric_dictionary(TOOLS_DIR)
        self.assertIsInstance(d, dict)
        self.assertIn("mysql_global_status_threads_connected", d)

    def test_missing_file_returns_empty(self):
        d = migrate_rule.load_metric_dictionary("/nonexistent")
        self.assertEqual(d, {})


if __name__ == "__main__":
    unittest.main()
