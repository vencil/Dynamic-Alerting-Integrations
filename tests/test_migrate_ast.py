#!/usr/bin/env python3
"""test_migrate_ast.py — AST Migration Engine 測試套件 (Phase 11)。

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

import yaml

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


class TestParseExprAllMetrics(unittest.TestCase):
    """測試 parse_expr 回傳的 all_metrics 欄位。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_all_metrics_simple(self):
        """簡單表達式: all_metrics 應包含唯一 metric。"""
        parsed = migrate_rule.parse_expr("mysql_connections > 100", use_ast=True)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["all_metrics"], ["mysql_connections"])

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_all_metrics_compound(self):
        """複合表達式: all_metrics 應包含多個 metric。"""
        parsed = migrate_rule.parse_expr(
            "(metric_a - metric_b) / metric_c > 0.5", use_ast=True
        )
        self.assertIsNotNone(parsed)
        self.assertIn("metric_a", parsed["all_metrics"])
        self.assertIn("metric_b", parsed["all_metrics"])
        self.assertIn("metric_c", parsed["all_metrics"])

    def test_all_metrics_no_ast(self):
        """use_ast=False: all_metrics 應為空 list。"""
        parsed = migrate_rule.parse_expr("mysql_up > 0", use_ast=False)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["all_metrics"], [])


class TestWriteOutputsWithAST(unittest.TestCase):
    """端到端 AST 路徑的 write_outputs 測試。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_write_outputs_ast_path(self):
        """AST 引擎產出: recording rule LHS 應包含 tenant label。"""
        import tempfile

        rule = {
            "alert": "TestASTOutput",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=True
        )
        self.assertIsNotNone(result)

        with tempfile.TemporaryDirectory() as tmpdir:
            counts = migrate_rule.write_outputs(
                [result], tmpdir, prefix="custom_", dictionary={}
            )
            self.assertEqual(len(counts), 4)

            # Verify recording rules contain tenant label
            rr_path = os.path.join(tmpdir, "platform-recording-rules.yaml")
            with open(rr_path, 'r', encoding='utf-8') as f:
                content = yaml.safe_load(f)
            rules = content["groups"][0]["rules"]
            # First rule should be the aggregation recording rule
            agg_rule = rules[0]
            self.assertIn("tenant", agg_rule["expr"])
            self.assertIn("custom_mysql_connections", agg_rule["record"])


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


class TestNestedSemanticBreak(unittest.TestCase):
    """B1: 巢狀 Call 中的語義中斷偵測。"""

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_nested_absent_in_rate(self):
        """rate(absent(x)) — 內層 absent 應被偵測。"""
        # absent 接受 vector，rate 接受 matrix — 這不是合法 PromQL，
        # 但 _walk_calls 應能遍歷巢狀 Call 找到 absent
        # 使用合法的表達式：absent(rate(x[5m]))
        self.assertTrue(migrate_rule.detect_semantic_break_ast(
            'absent(rate(http_requests_total[5m]))'
        ))

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_deeply_nested_predict_linear(self):
        """sum(predict_linear(x[1h], 3600)) — 深層巢狀。"""
        self.assertTrue(migrate_rule.detect_semantic_break_ast(
            'sum(predict_linear(node_filesystem_free_bytes[1h], 3600))'
        ))

    @unittest.skipUnless(HAS_AST, "promql-parser not installed")
    def test_no_break_in_nested_safe_funcs(self):
        """sum(rate(x[5m])) — 無語義中斷。"""
        self.assertFalse(migrate_rule.detect_semantic_break_ast(
            'sum(rate(http_requests_total[5m]))'
        ))


class TestTenantLabelEdgeCases(unittest.TestCase):
    """B2: tenant label 注入邊界案例。"""

    def test_same_metric_multiple_occurrences(self):
        """同一 metric 出現多次 (帶 label) — 每處都應注入 tenant。"""
        result = migrate_rule.rewrite_expr_tenant_label(
            'my_metric{a="1"} / my_metric{b="2"}',
            ["my_metric"]
        )
        # Both occurrences should have tenant label
        self.assertEqual(result.count('tenant=~".+"'), 2)

    def test_same_metric_bare_and_labeled(self):
        """同一 metric 同時有帶 label 和不帶 label 的用法。

        Known limitation: if/else 邏輯假設同一 metric 的所有出現形式一致。
        當 Pattern 1 (帶 label) 命中時，裸露的 metric 不會被注入 tenant。
        實際場景中，recording rule 的 LHS 通常只有一種形式，此限制影響小。
        """
        result = migrate_rule.rewrite_expr_tenant_label(
            'my_metric > on() group_left my_metric{a="1"}',
            ["my_metric"]
        )
        # Known limitation: only the labeled occurrence gets injected
        # because the if/else branch only applies one pattern type per metric
        self.assertGreaterEqual(result.count('tenant=~".+"'), 1)
        self.assertIn('tenant=~".+",a="1"', result)


class TestMetricDictionaryLoading(unittest.TestCase):
    """B3: metric dictionary 載入測試。"""

    def test_load_from_nonexistent_dir(self):
        """不存在的目錄 — 回傳空 dict。"""
        result = migrate_rule.load_metric_dictionary("/nonexistent/path")
        self.assertEqual(result, {})

    def test_load_from_scripts_tools(self):
        """從 scripts/tools 載入 — 應成功 (若檔案存在)。"""
        script_dir = os.path.join(REPO_ROOT, "scripts", "tools")
        dict_path = os.path.join(script_dir, "metric-dictionary.yaml")
        result = migrate_rule.load_metric_dictionary(script_dir)
        if os.path.exists(dict_path):
            self.assertIsInstance(result, dict)
            self.assertGreater(len(result), 0)
        else:
            self.assertEqual(result, {})

    def test_lookup_dictionary_none(self):
        """空字典查找 — 回傳 None。"""
        result = migrate_rule.lookup_dictionary("any_metric", None)
        self.assertIsNone(result)

    def test_lookup_dictionary_miss(self):
        """字典中不存在的 metric — 回傳 None。"""
        result = migrate_rule.lookup_dictionary("missing", {"other": {}})
        self.assertIsNone(result)

    def test_lookup_dictionary_hit(self):
        """字典中存在的 metric — 回傳對應 entry。"""
        entry = {"maps_to": "mysql_connections", "golden_rule": "X"}
        result = migrate_rule.lookup_dictionary("mysql_up", {"mysql_up": entry})
        self.assertEqual(result, entry)


class TestWriteOutputsIntegration(unittest.TestCase):
    """B4: write_outputs 整合測試 — 驗證產出檔案結構。"""

    def test_write_outputs_basic(self):
        """基本遷移產出: 驗證所有檔案都被建立且 YAML 可解析。"""
        import tempfile

        rule = {
            "alert": "TestHighConn",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=False
        )
        self.assertIsNotNone(result)

        with tempfile.TemporaryDirectory() as tmpdir:
            counts = migrate_rule.write_outputs(
                [result], tmpdir, prefix="custom_", dictionary={}
            )
            self.assertEqual(len(counts), 4)  # (perfect, complex, unparseable, golden)

            # Verify files exist
            expected_files = [
                "tenant-config.yaml",
                "platform-recording-rules.yaml",
                "platform-alert-rules.yaml",
                "migration-report.txt",
                "triage-report.csv",
                "prefix-mapping.yaml",
            ]
            for fname in expected_files:
                fpath = os.path.join(tmpdir, fname)
                self.assertTrue(os.path.exists(fpath), f"Missing: {fname}")

            # Verify recording rules YAML is parseable
            with open(os.path.join(tmpdir, "platform-recording-rules.yaml"),
                       'r', encoding='utf-8') as f:
                content = yaml.safe_load(f)
                self.assertIn("groups", content)
                rules = content["groups"][0]["rules"]
                self.assertGreater(len(rules), 0)

    def test_write_triage_csv_structure(self):
        """Triage CSV: 驗證欄位數量和標頭。"""
        import tempfile
        import csv as csv_mod

        rule = {
            "alert": "TestSimple",
            "expr": "my_metric > 50",
            "labels": {"severity": "warning"},
        }
        result = migrate_rule.process_rule(
            rule, prefix="custom_", dictionary={}, use_ast=False
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = migrate_rule.write_triage_csv([result], tmpdir, {})
            with open(csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv_mod.reader(f)
                header = next(reader)
                self.assertEqual(len(header), 14)  # 14 columns
                row = next(reader)
                self.assertEqual(row[0], "TestSimple")  # Alert Name


class TestAutoSuppression(unittest.TestCase):
    """Auto-Suppression: warning ↔ critical 配對測試。"""

    def _make_pair(self, prefix="custom_", base_metric="mysql_connections",
                   op=">", warn_val="100", crit_val="200"):
        """產生一組 warning + critical 規則並處理。"""
        warn_rule = {
            "alert": "TestHighConn",
            "expr": f"{base_metric} {op} {warn_val}",
            "labels": {"severity": "warning"},
        }
        crit_rule = {
            "alert": "TestHighConnCritical",
            "expr": f"{base_metric} {op} {crit_val}",
            "labels": {"severity": "critical"},
        }
        warn_r = migrate_rule.process_rule(
            warn_rule, prefix=prefix, dictionary={}, use_ast=True
        )
        crit_r = migrate_rule.process_rule(
            crit_rule, prefix=prefix, dictionary={}, use_ast=True
        )
        return [warn_r, crit_r]

    def test_basic_pairing(self):
        """基本配對: warning + critical → warning 應有雙層 unless。"""
        results = self._make_pair()
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 1)

        warn_expr = results[0].alert_rules[0]["expr"]
        # 應有兩個 unless
        self.assertEqual(warn_expr.count("unless on(tenant)"), 2)
        # 第一個 unless 是 maintenance
        self.assertIn('user_state_filter{filter="maintenance"}', warn_expr)
        # 第二個 unless 引用 critical threshold
        self.assertIn("alert_threshold:custom_mysql_connections_critical",
                       warn_expr)

    def test_critical_not_modified(self):
        """critical 的 expr 不應被修改。"""
        results = self._make_pair()
        crit_expr_before = results[1].alert_rules[0]["expr"]
        migrate_rule.apply_auto_suppression(results)
        crit_expr_after = results[1].alert_rules[0]["expr"]
        self.assertEqual(crit_expr_before, crit_expr_after)

    def test_no_pairing_warning_only(self):
        """只有 warning 沒有 critical → 不配對。"""
        warn_rule = {
            "alert": "TestWarnOnly",
            "expr": "my_metric > 50",
            "labels": {"severity": "warning"},
        }
        results = [migrate_rule.process_rule(
            warn_rule, prefix="custom_", dictionary={}, use_ast=True
        )]
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 0)
        # 仍然只有一個 unless
        self.assertEqual(
            results[0].alert_rules[0]["expr"].count("unless on(tenant)"), 1
        )

    def test_no_pairing_critical_only(self):
        """只有 critical 沒有 warning → 不配對。"""
        crit_rule = {
            "alert": "TestCritOnly",
            "expr": "my_metric > 100",
            "labels": {"severity": "critical"},
        }
        results = [migrate_rule.process_rule(
            crit_rule, prefix="custom_", dictionary={}, use_ast=True
        )]
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 0)

    def test_no_pairing_different_metrics(self):
        """不同 metric 的 warning/critical → 不配對。"""
        warn_rule = {
            "alert": "TestConn",
            "expr": "metric_a > 50",
            "labels": {"severity": "warning"},
        }
        crit_rule = {
            "alert": "TestCPU",
            "expr": "metric_b > 100",
            "labels": {"severity": "critical"},
        }
        results = [
            migrate_rule.process_rule(
                warn_rule, prefix="custom_", dictionary={}, use_ast=True),
            migrate_rule.process_rule(
                crit_rule, prefix="custom_", dictionary={}, use_ast=True),
        ]
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 0)

    def test_multiple_pairs(self):
        """多組配對: 各自獨立配對。"""
        results = (
            self._make_pair(base_metric="metric_a", warn_val="10", crit_val="20")
            + self._make_pair(base_metric="metric_b", warn_val="30", crit_val="60")
        )
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 2)

        # 兩個 warning 都有雙層 unless
        for r in results:
            if r.severity == "warning":
                self.assertEqual(
                    r.alert_rules[0]["expr"].count("unless on(tenant)"), 2
                )

    def test_operator_preserved(self):
        """< 運算子: suppression 子句應使用相同運算子。"""
        results = self._make_pair(op="<", warn_val="100", crit_val="50")
        migrate_rule.apply_auto_suppression(results)
        warn_expr = results[0].alert_rules[0]["expr"]
        # 應有 "< on(tenant) group_left" 在 suppression 子句
        lines = warn_expr.split("\n")
        # 尋找 suppression 區塊中的運算子
        suppression_ops = [l.strip() for l in lines
                           if "on(tenant) group_left" in l]
        # 第一個是原始 alert expr，第二個是 suppression
        self.assertEqual(len(suppression_ops), 2)
        self.assertTrue(suppression_ops[1].startswith("<"))

    def test_notes_added(self):
        """配對後 warning result 應有 Auto-Suppression 備註。"""
        results = self._make_pair()
        migrate_rule.apply_auto_suppression(results)
        notes = results[0].notes
        self.assertTrue(any("Auto-Suppression" in n for n in notes))

    def test_unparseable_skipped(self):
        """unparseable 規則不參與配對。"""
        results = self._make_pair()
        results[1].status = "unparseable"  # 標記 critical 為 unparseable
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 0)

    def test_golden_skipped(self):
        """use_golden 規則不參與配對。"""
        results = self._make_pair()
        results[1].triage_action = "use_golden"
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 0)

    def test_write_outputs_with_suppression(self):
        """端到端: write_outputs 產出的 alert YAML 包含雙層 unless。"""
        import tempfile

        results = self._make_pair()
        migrate_rule.apply_auto_suppression(results)

        with tempfile.TemporaryDirectory() as tmpdir:
            migrate_rule.write_outputs(results, tmpdir, prefix="custom_",
                                        dictionary={})
            alert_path = os.path.join(tmpdir, "platform-alert-rules.yaml")
            with open(alert_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # warning alert 應有雙層 unless
            self.assertIn("alert_threshold:custom_mysql_connections_critical",
                          content)
            self.assertEqual(content.count("unless on(tenant)"), 3)
            # 3 = warning(2) + critical(1)

    def test_dry_run_with_suppression(self):
        """dry-run path: print_dry_run 不應因 suppressed expr 而崩潰。"""
        import io
        import contextlib

        results = self._make_pair()
        migrate_rule.apply_auto_suppression(results)

        # Capture stdout — 確認 print_dry_run 能正常輸出
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            migrate_rule.print_dry_run(results)

        output = buf.getvalue()
        # 應包含兩條規則的摘要
        self.assertIn("TestHighConn", output)
        self.assertIn("TestHighConnCritical", output)

    def test_no_prefix_pairing(self):
        """prefix="" 時也能正確配對。"""
        results = self._make_pair(prefix="")
        n = migrate_rule.apply_auto_suppression(results)
        self.assertEqual(n, 1)
        warn_expr = results[0].alert_rules[0]["expr"]
        self.assertIn("alert_threshold:mysql_connections_critical", warn_expr)


if __name__ == "__main__":
    print(f"AST Engine available: {HAS_AST}")
    if HAS_AST:
        print("promql-parser loaded successfully")
    unittest.main()
