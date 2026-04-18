#!/usr/bin/env python3
"""test_lint_custom_rules.py — CI Deny-list Linter pytest 測試套件 (Phase 10)。

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
import tempfile

import pytest
import yaml

# Add scripts/tools to path

import lint_custom_rules  # noqa: E402


# ── Shared Fixture ────────────────────────────────────────────────

@pytest.fixture
def policy():
    """提供預設 lint 政策。"""
    return lint_custom_rules.DEFAULT_POLICY.copy()


# ── 1. Duration parsing ──────────────────────────────────────────

def test_duration_seconds():
    """測試秒數解析。"""
    assert lint_custom_rules.parse_duration_seconds("30s") == 30

def test_duration_minutes():
    """測試分鐘解析。"""
    assert lint_custom_rules.parse_duration_seconds("5m") == 300

def test_duration_hours():
    """測試小時解析。"""
    assert lint_custom_rules.parse_duration_seconds("1h") == 3600

def test_duration_days():
    """測試天數解析。"""
    assert lint_custom_rules.parse_duration_seconds("2d") == 172800

def test_duration_integer_passthrough():
    """測試整數直通。"""
    assert lint_custom_rules.parse_duration_seconds(60) == 60

def test_duration_invalid_returns_none():
    """測試無效字串回傳 None。"""
    assert lint_custom_rules.parse_duration_seconds("abc") is None

def test_duration_empty_returns_none():
    """測試空字串回傳 None。"""
    assert lint_custom_rules.parse_duration_seconds("") is None


def test_lint_holt_winters_detected(policy):
    """測試 holt_winters 函數偵測。"""
    results = lint_custom_rules.lint_expr(
        "holt_winters(my_metric[1h], 0.3, 0.7)", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "holt_winters" in r.message]
    assert len(errors) == 1

def test_lint_predict_linear_detected(policy):
    """測試 predict_linear 函數偵測。"""
    results = lint_custom_rules.lint_expr(
        "predict_linear(disk_free[1h], 3600) < 0", policy, "test.yaml", "TestRule"
    )
    errors = [r for r in results if "predict_linear" in r.message]
    assert len(errors) == 1

def test_lint_safe_function_passes(policy):
    """測試安全函數通過檢查。"""
    results = lint_custom_rules.lint_expr(
        "rate(http_requests_total[5m]) > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "denied function" in r.message]
    assert len(func_errors) == 0

def test_lint_function_name_not_substring(policy):
    """測試 metric 名稱中包含函數名稱不被誤判。"""
    results = lint_custom_rules.lint_expr(
        "my_predict_linear_metric > 100", policy, "test.yaml", "TestRule"
    )
    func_errors = [r for r in results if "predict_linear" in r.message]
    assert len(func_errors) == 0


def test_lint_wildcard_regex_detected(policy):
    """測試萬用字元正規表達式偵測。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job=~".*"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) >= 1

def test_lint_wildcard_regex_with_space_detected(policy):
    """測試空格變體: =~ ".*"。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job=~ ".*"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) >= 1

def test_lint_without_tenant_detected(policy):
    """測試 without(tenant) 偵測。"""
    results = lint_custom_rules.lint_expr(
        "sum without(tenant) (my_metric)", policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "without(tenant)" in r.message]
    assert len(pat_errors) >= 1

def test_lint_without_tenant_space_detected(policy):
    """測試空格變體: without (tenant)。"""
    results = lint_custom_rules.lint_expr(
        "sum without (tenant) (my_metric)", policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "without(tenant)" in r.message]
    assert len(pat_errors) >= 1

def test_lint_safe_pattern_passes(policy):
    """測試安全 pattern 通過檢查。"""
    results = lint_custom_rules.lint_expr(
        'my_metric{job="mysql"} > 0', policy, "test.yaml", "TestRule"
    )
    pat_errors = [r for r in results if "denied pattern" in r.message]
    assert len(pat_errors) == 0


def test_lint_exceeds_max_range(policy):
    """測試超出最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[7d])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 1

def test_lint_within_max_range(policy):
    """測試在最大範圍內。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[30m])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 0

def test_lint_exact_max_range_passes(policy):
    """測試精確最大範圍。"""
    results = lint_custom_rules.lint_expr(
        "rate(my_metric[1h])", policy, "test.yaml", "TestRule"
    )
    range_errors = [r for r in results if "range vector" in r.message]
    assert len(range_errors) == 0


def test_lint_missing_tenant_label(policy):
    """測試缺少租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning"}, policy, "test.yaml", "TestRule", is_recording=False
    )
    assert len(results) == 1
    assert "tenant" in results[0].message

def test_lint_has_tenant_label(policy):
    """測試有租戶標籤。"""
    results = lint_custom_rules.lint_labels(
        {"severity": "warning", "tenant": "db-a"}, policy, "test.yaml", "TestRule",
        is_recording=False
    )
    assert len(results) == 0

def test_lint_recording_rule_skips_label_check(policy):
    """測試記錄規則跳過標籤檢查。"""
    results = lint_custom_rules.lint_labels(
        {}, policy, "test.yaml", "TestRule", is_recording=True
    )
    assert len(results) == 0


def test_expiry_missing_expiry_warns():
    """測試缺少到期日期警告。"""
    results = lint_custom_rules.check_expiry_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_expiry_has_expiry_passes():
    """測試有到期日期通過檢查。"""
    results = lint_custom_rules.check_expiry_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0

def test_owner_missing_owner_warns():
    """測試缺少擁有者警告。"""
    results = lint_custom_rules.check_owner_label(
        {"expiry": "2026-06-30"}, "test.yaml", "TestRule"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_owner_has_owner_passes():
    """測試有擁有者通過檢查。"""
    results = lint_custom_rules.check_owner_label(
        {"owner": "team-a"}, "test.yaml", "TestRule"
    )
    assert len(results) == 0


def test_lint_file_direct_rule_format():
    """測試直接 Prometheus rule group 格式。"""
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
        assert count == 1
        # Should not have errors (clean rule)
        errors = [r for r in results if r.severity == "ERROR"]
        assert len(errors) == 0
    finally:
        os.unlink(fpath)

def test_lint_file_configmap_wrapper():
    """測試 ConfigMap data wrapper 格式。"""
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
        assert count == 1
        errors = [r for r in results if r.severity == "ERROR"]
        # Should detect: denied function + missing tenant label
        assert len(errors) >= 2
    finally:
        os.unlink(fpath)

def test_lint_file_empty_file():
    """測試空檔案不應出錯。"""
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        f.write("")
        fpath = f.name
    try:
        results, count = lint_custom_rules.lint_file(
            fpath, lint_custom_rules.DEFAULT_POLICY
        )
        assert count == 0
        assert len(results) == 0
    finally:
        os.unlink(fpath)

def test_lint_file_nonexistent_file():
    """測試不存在的檔案應回傳 ERROR。"""
    results, count = lint_custom_rules.lint_file(
        "/nonexistent/path.yaml", lint_custom_rules.DEFAULT_POLICY
    )
    assert count == 0
    assert len(results) == 1
    assert results[0].severity == "ERROR"


def test_load_policy_no_policy_uses_defaults():
    """測試無政策時使用預設值。"""
    policy = lint_custom_rules.load_policy(None)
    assert policy == lint_custom_rules.DEFAULT_POLICY

def test_load_policy_custom_policy_overrides():
    """測試自訂政策覆寫。"""
    custom = {"max_range_duration": "2h", "denied_functions": ["my_func"]}
    with tempfile.NamedTemporaryFile(
        mode='w', suffix='.yaml', delete=False, encoding='utf-8'
    ) as f:
        yaml.safe_dump(custom, f)
        fpath = f.name
    try:
        policy = lint_custom_rules.load_policy(fpath)
        assert policy["max_range_duration"] == "2h"
        assert policy["denied_functions"] == ["my_func"]
        # Unspecified keys should retain defaults
        assert policy["required_labels"] == ["tenant"]
    finally:
        os.unlink(fpath)

def test_load_policy_invalid_policy_falls_back():
    """測試無效政策回退到預設值。"""
    policy = lint_custom_rules.load_policy("/nonexistent/policy.yaml")
    assert policy == lint_custom_rules.DEFAULT_POLICY


def test_group_interval_exceeds_max():
    """測試超出最大求值時間間隔。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "120s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 1
    assert results[0].severity == "WARN"

def test_group_interval_within_max():
    """測試在最大求值時間間隔內。"""
    policy = {"max_evaluation_interval": "60s"}
    results = lint_custom_rules.lint_group_interval(
        "30s", policy, "test.yaml", "my_group"
    )
    assert len(results) == 0


def test_collect_yaml_files():
    """測試收集 YAML 檔案。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        for name in ["a.yaml", "b.yml", "c.txt", "d.yaml"]:
            with open(os.path.join(tmpdir, name), 'w') as f:
                f.write("test")
        files = lint_custom_rules.collect_files([tmpdir])
        assert len(files) == 3  # a.yaml, b.yml, d.yaml
        assert all(f.endswith((".yaml", ".yml")) for f in files)

def test_collect_single_file():
    """測試收集單一檔案。"""
    with tempfile.NamedTemporaryFile(
        suffix='.yaml', delete=False
    ) as f:
        fpath = f.name
    try:
        files = lint_custom_rules.collect_files([fpath])
        assert len(files) == 1
    finally:
        os.unlink(fpath)


def test_lint_result_error_format():
    """測試 LintResult 錯誤格式。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", None, "ERROR", "bad thing")
    assert str(r) == "ERROR: test.yaml [MyRule] - bad thing"

def test_lint_result_with_line_hint():
    """測試 LintResult 包含行提示。"""
    r = lint_custom_rules.LintResult("test.yaml", "MyRule", 42, "WARN", "warning")
    assert str(r) == "WARN: test.yaml:42 [MyRule] - warning"

def test_lint_result_no_rule_name():
    """測試 LintResult 無規則名稱。"""
    r = lint_custom_rules.LintResult("test.yaml", None, None, "ERROR", "msg")
    assert str(r) == "ERROR: test.yaml - msg"
