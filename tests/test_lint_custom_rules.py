#!/usr/bin/env python3
"""test_lint_custom_rules.py — CI Deny-list Linter 測試套件 (Phase 10)。

驗證 lint_custom_rules.py 的核心功能:
  1. Denied function 偵測
  2. Denied pattern 偵測 (含 whitespace 變體)
  3. Required label 檢查
  4. Range vector duration 超限
  5. ConfigMap wrapper 解析
  6. Policy 載入與合併
  7. Duration parsing
  8. Expiry / owner label 檢查

用法:
  python3 -m pytest tests/test_lint_custom_rules.py -v
"""

import os
import sys
import tempfile
import unittest

import yaml

# Add scripts/tools to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import lint_custom_rules  # noqa: E402


class TestParseDuration(unittest.TestCase):
    """測試 Prometheus duration 解析。"""

    def test_seconds(self):
        self.assertEqual(lint_custom_rules.parse_duration_seconds("30s"), 30)

    def test_minutes(self):
        self.assertEqual(lint_custom_rules.parse_duration_seconds("5m"), 300)

    def test_hours(self):
        self.assertEqual(lint_custom_rules.parse_duration_seconds("1h"), 3600)

    def test_days(self):
        self.assertEqual(lint_custom_rules.parse_duration_seconds("2d"), 172800)

    def test_integer_passthrough(self):
        self.assertEqual(lint_custom_rules.parse_duration_seconds(60), 60)

    def test_invalid_returns_none(self):
        self.assertIsNone(lint_custom_rules.parse_duration_seconds("abc"))

    def test_empty_returns_none(self):
        self.assertIsNone(lint_custom_rules.parse_duration_seconds(""))


class TestLintExprDeniedFunctions(unittest.TestCase):
    """測試 denied function 偵測。"""

    def setUp(self):
        self.policy = lint_custom_rules.DEFAULT_POLICY.copy()

    def test_holt_winters_detected(self):
        results = lint_custom_rules.lint_expr(
            "holt_winters(my_metric[1h], 0.3, 0.7)", self.policy, "test.yaml", "TestRule"
        )
        errors = [r for r in results if "holt_winters" in r.message]
        self.assertEqual(len(errors), 1)

    def test_predict_linear_detected(self):
        results = lint_custom_rules.lint_expr(
            "predict_linear(disk_free[1h], 3600) < 0", self.policy, "test.yaml", "TestRule"
        )
        errors = [r for r in results if "predict_linear" in r.message]
        self.assertEqual(len(errors), 1)

    def test_safe_function_passes(self):
        results = lint_custom_rules.lint_expr(
            "rate(http_requests_total[5m]) > 100", self.policy, "test.yaml", "TestRule"
        )
        func_errors = [r for r in results if "denied function" in r.message]
        self.assertEqual(len(func_errors), 0)

    def test_function_name_not_substring(self):
        """不應誤判包含 denied function name 的 metric name。"""
        results = lint_custom_rules.lint_expr(
            "my_predict_linear_metric > 100", self.policy, "test.yaml", "TestRule"
        )
        func_errors = [r for r in results if "predict_linear" in r.message]
        self.assertEqual(len(func_errors), 0)


class TestLintExprDeniedPatterns(unittest.TestCase):
    """測試 denied pattern 偵測 (含 whitespace 變體)。"""

    def setUp(self):
        self.policy = lint_custom_rules.DEFAULT_POLICY.copy()

    def test_wildcard_regex_detected(self):
        results = lint_custom_rules.lint_expr(
            'my_metric{job=~".*"} > 0', self.policy, "test.yaml", "TestRule"
        )
        pat_errors = [r for r in results if "denied pattern" in r.message]
        self.assertGreaterEqual(len(pat_errors), 1)

    def test_wildcard_regex_with_space_detected(self):
        """Whitespace 變體: =~ ".*" (空格)。"""
        results = lint_custom_rules.lint_expr(
            'my_metric{job=~ ".*"} > 0', self.policy, "test.yaml", "TestRule"
        )
        pat_errors = [r for r in results if "denied pattern" in r.message]
        self.assertGreaterEqual(len(pat_errors), 1)

    def test_without_tenant_detected(self):
        results = lint_custom_rules.lint_expr(
            "sum without(tenant) (my_metric)", self.policy, "test.yaml", "TestRule"
        )
        pat_errors = [r for r in results if "without(tenant)" in r.message]
        self.assertGreaterEqual(len(pat_errors), 1)

    def test_without_tenant_space_detected(self):
        """Whitespace 變體: without (tenant)。"""
        results = lint_custom_rules.lint_expr(
            "sum without (tenant) (my_metric)", self.policy, "test.yaml", "TestRule"
        )
        pat_errors = [r for r in results if "without(tenant)" in r.message]
        self.assertGreaterEqual(len(pat_errors), 1)

    def test_safe_pattern_passes(self):
        results = lint_custom_rules.lint_expr(
            'my_metric{job="mysql"} > 0', self.policy, "test.yaml", "TestRule"
        )
        pat_errors = [r for r in results if "denied pattern" in r.message]
        self.assertEqual(len(pat_errors), 0)


class TestLintExprRangeDuration(unittest.TestCase):
    """測試 range vector duration 超限。"""

    def setUp(self):
        self.policy = lint_custom_rules.DEFAULT_POLICY.copy()

    def test_exceeds_max_range(self):
        results = lint_custom_rules.lint_expr(
            "rate(my_metric[7d])", self.policy, "test.yaml", "TestRule"
        )
        range_errors = [r for r in results if "range vector" in r.message]
        self.assertEqual(len(range_errors), 1)

    def test_within_max_range(self):
        results = lint_custom_rules.lint_expr(
            "rate(my_metric[30m])", self.policy, "test.yaml", "TestRule"
        )
        range_errors = [r for r in results if "range vector" in r.message]
        self.assertEqual(len(range_errors), 0)

    def test_exact_max_range_passes(self):
        results = lint_custom_rules.lint_expr(
            "rate(my_metric[1h])", self.policy, "test.yaml", "TestRule"
        )
        range_errors = [r for r in results if "range vector" in r.message]
        self.assertEqual(len(range_errors), 0)


class TestLintLabels(unittest.TestCase):
    """測試 required label 檢查。"""

    def setUp(self):
        self.policy = lint_custom_rules.DEFAULT_POLICY.copy()

    def test_missing_tenant_label(self):
        results = lint_custom_rules.lint_labels(
            {"severity": "warning"}, self.policy, "test.yaml", "TestRule", is_recording=False
        )
        self.assertEqual(len(results), 1)
        self.assertIn("tenant", results[0].message)

    def test_has_tenant_label(self):
        results = lint_custom_rules.lint_labels(
            {"severity": "warning", "tenant": "db-a"}, self.policy, "test.yaml", "TestRule",
            is_recording=False
        )
        self.assertEqual(len(results), 0)

    def test_recording_rule_skips_label_check(self):
        results = lint_custom_rules.lint_labels(
            {}, self.policy, "test.yaml", "TestRule", is_recording=True
        )
        self.assertEqual(len(results), 0)


class TestExpiryOwnerLabels(unittest.TestCase):
    """測試 Tier 3 governance label 檢查。"""

    def test_missing_expiry_warns(self):
        results = lint_custom_rules.check_expiry_label(
            {"owner": "team-a"}, "test.yaml", "TestRule"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].severity, "WARN")

    def test_has_expiry_passes(self):
        results = lint_custom_rules.check_expiry_label(
            {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
        )
        self.assertEqual(len(results), 0)

    def test_missing_owner_warns(self):
        results = lint_custom_rules.check_owner_label(
            {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].severity, "WARN")

    def test_has_owner_passes(self):
        results = lint_custom_rules.check_owner_label(
            {"owner": "team-a"}, "test.yaml", "TestRule"
        )
        self.assertEqual(len(results), 0)


class TestLintFile(unittest.TestCase):
    """測試完整檔案 lint (含 ConfigMap wrapper)。"""

    def test_direct_rule_format(self):
        """直接 Prometheus rule group 格式。"""
        content = {
            "groups": [{
                "name": "test_group",
                "rules": [{
                    "alert": "TestAlert",
                    "expr": "my_metric > 100",
                    "labels": {"severity": "warning", "tenant": "db-a",
                               "owner": "team-a", "expiry": "2026-12-31"},
                }]
            }]
        }
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False, encoding='utf-8'
        ) as f:
            yaml.safe_dump(content, f)
            fpath = f.name
        try:
            results, count = lint_custom_rules.lint_file(
                fpath, lint_custom_rules.DEFAULT_POLICY
            )
            self.assertEqual(count, 1)
            # Should not have errors (clean rule)
            errors = [r for r in results if r.severity == "ERROR"]
            self.assertEqual(len(errors), 0)
        finally:
            os.unlink(fpath)

    def test_configmap_wrapper(self):
        """ConfigMap data wrapper 格式。"""
        inner_yaml = yaml.safe_dump({
            "groups": [{
                "name": "wrapped_group",
                "rules": [{
                    "alert": "WrappedAlert",
                    "expr": 'holt_winters(my_metric[1h], 0.3, 0.7) > 100',
                    "labels": {"severity": "critical"},
                }]
            }]
        })
        content = {"data": {"rules.yaml": inner_yaml}}
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False, encoding='utf-8'
        ) as f:
            yaml.safe_dump(content, f)
            fpath = f.name
        try:
            results, count = lint_custom_rules.lint_file(
                fpath, lint_custom_rules.DEFAULT_POLICY
            )
            self.assertEqual(count, 1)
            errors = [r for r in results if r.severity == "ERROR"]
            # Should detect: denied function + missing tenant label
            self.assertGreaterEqual(len(errors), 2)
        finally:
            os.unlink(fpath)

    def test_empty_file(self):
        """空檔案不應出錯。"""
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False, encoding='utf-8'
        ) as f:
            f.write("")
            fpath = f.name
        try:
            results, count = lint_custom_rules.lint_file(
                fpath, lint_custom_rules.DEFAULT_POLICY
            )
            self.assertEqual(count, 0)
            self.assertEqual(len(results), 0)
        finally:
            os.unlink(fpath)

    def test_nonexistent_file(self):
        """不存在的檔案應回傳 ERROR。"""
        results, count = lint_custom_rules.lint_file(
            "/nonexistent/path.yaml", lint_custom_rules.DEFAULT_POLICY
        )
        self.assertEqual(count, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].severity, "ERROR")


class TestLoadPolicy(unittest.TestCase):
    """測試 policy 載入與合併。"""

    def test_no_policy_uses_defaults(self):
        policy = lint_custom_rules.load_policy(None)
        self.assertEqual(policy, lint_custom_rules.DEFAULT_POLICY)

    def test_custom_policy_overrides(self):
        custom = {"max_range_duration": "2h", "denied_functions": ["my_func"]}
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.yaml', delete=False, encoding='utf-8'
        ) as f:
            yaml.safe_dump(custom, f)
            fpath = f.name
        try:
            policy = lint_custom_rules.load_policy(fpath)
            self.assertEqual(policy["max_range_duration"], "2h")
            self.assertEqual(policy["denied_functions"], ["my_func"])
            # Unspecified keys should retain defaults
            self.assertEqual(policy["required_labels"], ["tenant"])
        finally:
            os.unlink(fpath)

    def test_invalid_policy_falls_back(self):
        policy = lint_custom_rules.load_policy("/nonexistent/policy.yaml")
        self.assertEqual(policy, lint_custom_rules.DEFAULT_POLICY)


class TestGroupInterval(unittest.TestCase):
    """測試 group evaluation_interval 檢查。"""

    def test_exceeds_max_interval(self):
        policy = {"max_evaluation_interval": "60s"}
        results = lint_custom_rules.lint_group_interval(
            "120s", policy, "test.yaml", "my_group"
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].severity, "WARN")

    def test_within_max_interval(self):
        policy = {"max_evaluation_interval": "60s"}
        results = lint_custom_rules.lint_group_interval(
            "30s", policy, "test.yaml", "my_group"
        )
        self.assertEqual(len(results), 0)


class TestCollectFiles(unittest.TestCase):
    """測試檔案收集。"""

    def test_collect_yaml_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create test files
            for name in ["a.yaml", "b.yml", "c.txt", "d.yaml"]:
                with open(os.path.join(tmpdir, name), 'w') as f:
                    f.write("test")
            files = lint_custom_rules.collect_files([tmpdir])
            self.assertEqual(len(files), 3)  # a.yaml, b.yml, d.yaml
            self.assertTrue(all(f.endswith((".yaml", ".yml")) for f in files))

    def test_single_file(self):
        with tempfile.NamedTemporaryFile(
            suffix='.yaml', delete=False
        ) as f:
            fpath = f.name
        try:
            files = lint_custom_rules.collect_files([fpath])
            self.assertEqual(len(files), 1)
        finally:
            os.unlink(fpath)


class TestLintResultStr(unittest.TestCase):
    """測試 LintResult 字串輸出。"""

    def test_error_format(self):
        r = lint_custom_rules.LintResult("test.yaml", "MyRule", None, "ERROR", "bad thing")
        self.assertEqual(str(r), "ERROR: test.yaml [MyRule] - bad thing")

    def test_with_line_hint(self):
        r = lint_custom_rules.LintResult("test.yaml", "MyRule", 42, "WARN", "warning")
        self.assertEqual(str(r), "WARN: test.yaml:42 [MyRule] - warning")

    def test_no_rule_name(self):
        r = lint_custom_rules.LintResult("test.yaml", None, None, "ERROR", "msg")
        self.assertEqual(str(r), "ERROR: test.yaml - msg")


if __name__ == "__main__":
    unittest.main()
