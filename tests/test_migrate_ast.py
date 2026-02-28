#!/usr/bin/env python3
"""test_migrate_ast.py — AST Migration Engine 測試套件 (Phase 10)。

驗證 promql-parser AST 引擎的核心功能:
  1. Metric name 精準提取 (vs regex blacklist)
  2. Prefix injection (custom_ 前綴)
  3. Tenant label injection (tenant=~".+")
  4. 巢狀/複雜 PromQL 正確性 (and/or/unless, offset, subquery)
  5. Graceful degradation (AST 失敗降級為 regex)
  6. Roundtrip validation (改寫後的 PromQL 仍可 parse)
  7. End-to-end process_rule 整合

用法:
  python3 -m pytest tests/test_migrate_ast.py -v
  python3 tests/test_migrate_ast.py  # 直接執行
"""

import os
import sys
import unittest

# Add scripts/tools to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import migrate_rule  # noqa: E402

# Check if AST is available
HAS_AST = migrate_rule.HAS_AST


class TestASTMetricExtraction(unittest.TestCase):
    """測試 AST 精準 metric name 提取。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_simple_metric(self):
        result = migrate_rule.extract_metrics_ast("mysql_up > 0")
        self.assertEqual(result, ["mysql_up"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_metric_with_labels(self):
        result = migrate_rule.extract_metrics_ast(
            'mysql_up{job="mysql", instance=~"10.0.*:3306"} == 0'
        )
        self.assertEqual(result, ["mysql_up"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_rate_wrapped(self):
        """rate() 包裹的 metric — regex 容易誤取 rate 為 metric。"""
        result = migrate_rule.extract_metrics_ast(
            "rate(http_requests_total[5m]) > 100"
        )
        self.assertEqual(result, ["http_requests_total"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_nested_functions(self):
        """多層巢狀函式。"""
        result = migrate_rule.extract_metrics_ast(
            "sum by (user) (rate(mysql_global_status_queries[5m] offset 1h))"
        )
        self.assertEqual(result, ["mysql_global_status_queries"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_compound_and(self):
        """and 複合表達式 — 多個 metric。"""
        result = migrate_rule.extract_metrics_ast(
            "(mysql_global_status_threads_connected > 100) "
            "and (mysql_global_status_threads_running > 50)"
        )
        self.assertEqual(result, [
            "mysql_global_status_threads_connected",
            "mysql_global_status_threads_running",
        ])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_compound_or_unless(self):
        """or/unless 複合表達式。"""
        result = migrate_rule.extract_metrics_ast(
            "metric_a > 1 or metric_b > 2 unless metric_c > 3"
        )
        self.assertIn("metric_a", result)
        self.assertIn("metric_b", result)
        self.assertIn("metric_c", result)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_histogram_quantile(self):
        """histogram_quantile + le label。"""
        result = migrate_rule.extract_metrics_ast(
            'histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))'
        )
        self.assertEqual(result, ["http_request_duration_seconds_bucket"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_binary_arithmetic(self):
        """算術運算中的 metric。"""
        result = migrate_rule.extract_metrics_ast(
            "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes > 1e9"
        )
        self.assertIn("node_memory_MemTotal_bytes", result)
        self.assertIn("node_memory_MemAvailable_bytes", result)

    def test_fallback_on_invalid(self):
        """無效 PromQL 回傳空 list (降級為 regex)。"""
        result = migrate_rule.extract_metrics_ast("this is not PromQL {{{}}")
        self.assertEqual(result, [])

    def test_extract_all_metrics_uses_ast(self):
        """extract_all_metrics() 優先使用 AST。"""
        result = migrate_rule.extract_all_metrics(
            "rate(my_counter_total[5m]) > 10"
        )
        self.assertIn("my_counter_total", result)
        # 不應包含 "rate" (regex 版本可能包含)
        self.assertNotIn("rate", result)


class TestASTLabelMatchers(unittest.TestCase):
    """測試 AST 維度標籤提取。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_dimension_labels(self):
        result = migrate_rule.extract_label_matchers_ast(
            'redis_connected_clients{db="0", role="master"} > 100'
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["metric"], "redis_connected_clients")
        self.assertIn("db", result[0]["labels"])
        self.assertIn("role", result[0]["labels"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_skip_infrastructure_labels(self):
        """跳過 job/instance/namespace 等基礎設施 label。"""
        result = migrate_rule.extract_label_matchers_ast(
            'mysql_up{job="mysql", instance="10.0.0.1:3306", queue="tasks"} == 0'
        )
        # Should only have "queue", not "job" or "instance"
        if result:
            for r in result:
                self.assertNotIn("job", r["labels"])
                self.assertNotIn("instance", r["labels"])


class TestSemanticBreakDetection(unittest.TestCase):
    """測試語義中斷函式偵測。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_absent_detected(self):
        self.assertTrue(migrate_rule.detect_semantic_break_ast(
            'absent(mysql_up{job="mysql"})'
        ))

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_predict_linear_detected(self):
        self.assertTrue(migrate_rule.detect_semantic_break_ast(
            'predict_linear(node_filesystem_free_bytes[1h], 4*3600) < 0'
        ))

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_normal_not_detected(self):
        self.assertFalse(migrate_rule.detect_semantic_break_ast(
            'rate(http_requests_total[5m]) > 100'
        ))


class TestPrefixRewrite(unittest.TestCase):
    """測試 prefix injection (AST-Informed String Surgery)。"""

    def test_simple_prefix(self):
        result = migrate_rule.rewrite_expr_prefix(
            "mysql_connections > 100",
            {"mysql_connections": "custom_mysql_connections"}
        )
        self.assertEqual(result, "custom_mysql_connections > 100")

    def test_prefix_with_labels(self):
        result = migrate_rule.rewrite_expr_prefix(
            'mysql_connections{job="mysql"} > 100',
            {"mysql_connections": "custom_mysql_connections"}
        )
        self.assertIn('custom_mysql_connections{job="mysql"}', result)

    def test_prefix_does_not_affect_substring(self):
        """prefix 不應影響包含目標名稱為子字串的其他 metric。"""
        result = migrate_rule.rewrite_expr_prefix(
            "mysql_connections_total > 100",
            {"mysql_connections": "custom_mysql_connections"}
        )
        # mysql_connections_total 不應被改
        self.assertIn("mysql_connections_total", result)

    def test_prefix_compound_expr(self):
        """複合表達式中多個 metric 分別 prefix。"""
        result = migrate_rule.rewrite_expr_prefix(
            "(metric_a > 10) and (metric_b > 20)",
            {"metric_a": "custom_metric_a", "metric_b": "custom_metric_b"}
        )
        self.assertIn("custom_metric_a", result)
        self.assertIn("custom_metric_b", result)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_prefix_validates_reparse(self):
        """改寫後的表達式必須可以 reparse。"""
        result = migrate_rule.rewrite_expr_prefix(
            'rate(http_requests_total{method="GET"}[5m]) > 100',
            {"http_requests_total": "custom_http_requests_total"}
        )
        import promql_parser
        ast = promql_parser.parse(result)
        self.assertIsNotNone(ast)


class TestTenantLabelInjection(unittest.TestCase):
    """測試 tenant label 注入。"""

    def test_inject_into_existing_labels(self):
        result = migrate_rule.rewrite_expr_tenant_label(
            'mysql_up{job="mysql"} > 0',
            ["mysql_up"]
        )
        self.assertIn('tenant=~".+"', result)
        self.assertIn('job="mysql"', result)

    def test_inject_bare_metric(self):
        result = migrate_rule.rewrite_expr_tenant_label(
            "mysql_up > 0",
            ["mysql_up"]
        )
        self.assertIn('{tenant=~".+"}', result)

    def test_inject_multiple_metrics(self):
        result = migrate_rule.rewrite_expr_tenant_label(
            "(metric_a > 10) and (metric_b > 20)",
            ["metric_a", "metric_b"]
        )
        # Both metrics should have tenant label
        self.assertEqual(result.count('tenant=~".+"'), 2)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_inject_validates_reparse(self):
        """注入 tenant label 後仍可 parse。"""
        result = migrate_rule.rewrite_expr_tenant_label(
            'mysql_up{job="mysql"} > 0',
            ["mysql_up"]
        )
        import promql_parser
        ast = promql_parser.parse(result)
        self.assertIsNotNone(ast)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_inject_complex_nested(self):
        """巢狀函式中的 tenant label 注入。"""
        result = migrate_rule.rewrite_expr_tenant_label(
            "sum by (user) (rate(mysql_queries[5m] offset 1h))",
            ["mysql_queries"]
        )
        self.assertIn('tenant=~".+"', result)
        import promql_parser
        ast = promql_parser.parse(result)
        self.assertIsNotNone(ast)


class TestRegexKillerCases(unittest.TestCase):
    """Gemini 指定的「Regex Killer」測試案例。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_compound_and_binary(self):
        """Case 1: 複合 and 運算。"""
        expr = ("(mysql_global_status_threads_connected > 100) "
                "and (mysql_global_status_threads_running > 50)")
        metrics = migrate_rule.extract_metrics_ast(expr)
        self.assertEqual(len(metrics), 2)
        self.assertIn("mysql_global_status_threads_connected", metrics)
        self.assertIn("mysql_global_status_threads_running", metrics)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_complex_label_regex(self):
        """Case 2: 複雜 label matcher (regex)。"""
        expr = 'mysql_up{job="mysql", instance=~"10.0.*:3306"} == 0'
        metrics = migrate_rule.extract_metrics_ast(expr)
        self.assertEqual(metrics, ["mysql_up"])

        # Prefix + tenant injection roundtrip
        rewritten = migrate_rule.rewrite_expr_prefix(expr, {"mysql_up": "custom_mysql_up"})
        rewritten = migrate_rule.rewrite_expr_tenant_label(rewritten, ["custom_mysql_up"])
        import promql_parser
        ast = promql_parser.parse(rewritten)
        self.assertIsNotNone(ast)

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_aggregation_offset_time_window(self):
        """Case 3: 聚合 + 時間窗 + offset。"""
        expr = "sum by (user) (rate(mysql_global_status_queries[5m] offset 1h))"
        metrics = migrate_rule.extract_metrics_ast(expr)
        self.assertEqual(metrics, ["mysql_global_status_queries"])

        # Tenant injection preserves structure
        rewritten = migrate_rule.rewrite_expr_tenant_label(expr, metrics)
        self.assertIn('tenant=~".+"', rewritten)
        import promql_parser
        ast = promql_parser.parse(rewritten)
        # Verify prettify roundtrip
        pretty = ast.prettify()
        self.assertIn("offset", pretty)
        self.assertIn("5m", pretty)


class TestWalkVectorSelectors(unittest.TestCase):
    """測試 AST 走訪器覆蓋所有節點類型。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_unary_expr(self):
        """UnaryExpr: -metric_a"""
        import promql_parser
        ast = promql_parser.parse("-my_metric")
        vs_list = list(migrate_rule._walk_vector_selectors(ast))
        self.assertEqual(len(vs_list), 1)
        self.assertEqual(vs_list[0].name, "my_metric")

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_paren_expr(self):
        """ParenExpr: (metric_a > 0)"""
        import promql_parser
        ast = promql_parser.parse("(my_metric > 0)")
        vs_list = list(migrate_rule._walk_vector_selectors(ast))
        self.assertEqual(len(vs_list), 1)
        self.assertEqual(vs_list[0].name, "my_metric")

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_matrix_selector(self):
        """MatrixSelector: metric[5m]"""
        import promql_parser
        ast = promql_parser.parse("rate(my_counter[5m])")
        vs_list = list(migrate_rule._walk_vector_selectors(ast))
        self.assertEqual(len(vs_list), 1)
        self.assertEqual(vs_list[0].name, "my_counter")


class TestEndToEnd(unittest.TestCase):
    """端到端測試: process_rule 整合。"""

    def test_simple_rule_with_ast(self):
        """簡單規則: AST 引擎應產出正確的三件套。"""
        rule = {
            "alert": "TestHighConnections",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "perfect")
        self.assertEqual(result.triage_action, "auto")
        self.assertIn("custom_mysql_connections", result.tenant_config)
        self.assertEqual(len(result.recording_rules), 2)
        self.assertEqual(len(result.alert_rules), 1)

        # Recording rule LHS should contain tenant label if AST is available
        rec_expr = result.recording_rules[0]["expr"]
        if HAS_AST:
            self.assertIn("tenant", rec_expr)

    def test_complex_rule_with_ast(self):
        """複雜規則: rate() 包裹。"""
        rule = {
            "alert": "TestHighQPS",
            "expr": "rate(mysql_global_status_queries[5m]) > 1000",
            "labels": {"severity": "critical"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "complex")
        # base_key should be the actual metric, not "rate"
        self.assertIn("custom_mysql_global_status_queries_critical",
                       result.tenant_config)

    def test_no_ast_fallback(self):
        """use_ast=False 時應使用舊版 regex 引擎。"""
        rule = {
            "alert": "TestSimple",
            "expr": "my_metric > 50",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=False
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "perfect")
        self.assertIn("custom_my_metric", result.tenant_config)

    def test_golden_standard_skip(self):
        """字典命中黃金標準時: 不加 prefix，建議 use_golden。"""
        dictionary = {
            "mysql_global_status_threads_connected": {
                "maps_to": "mysql_connections",
                "golden_rule": "MariaDBHighConnections",
                "rule_pack": "mariadb",
                "note": "use scaffold_tenant.py",
            }
        }
        rule = {
            "alert": "TestConnections",
            "expr": "mysql_global_status_threads_connected > 200",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary=dictionary, use_ast=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.triage_action, "use_golden")

    def test_semantic_break_returns_none(self):
        """語義中斷函式 (absent) 應回傳 unparseable。"""
        rule = {
            "alert": "TestAbsent",
            "expr": 'absent(mysql_up{job="mysql"}) == 1',
            "labels": {"severity": "critical"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=True
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "unparseable")


class TestGracefulDegradation(unittest.TestCase):
    """測試降級行為。"""

    def test_extract_metrics_ast_without_lib(self):
        """當 HAS_AST=False 時，extract_metrics_ast 回傳空。"""
        orig = migrate_rule.HAS_AST
        try:
            migrate_rule.HAS_AST = False
            result = migrate_rule.extract_metrics_ast("mysql_up > 0")
            self.assertEqual(result, [])
        finally:
            migrate_rule.HAS_AST = orig

    def test_extract_all_metrics_fallback(self):
        """extract_all_metrics 降級為 regex 仍能提取 metric。"""
        orig = migrate_rule.HAS_AST
        try:
            migrate_rule.HAS_AST = False
            result = migrate_rule.extract_all_metrics("mysql_up > 0")
            self.assertIn("mysql_up", result)
        finally:
            migrate_rule.HAS_AST = orig


if __name__ == "__main__":
    print(f"AST Engine available: {HAS_AST}")
    if HAS_AST:
        print("promql-parser loaded successfully")
    unittest.main()
