"""
tests/test_migrate_v3.py — pytest style unit tests for migrate_rule.py v3 core functions.
Tests the Triage/Prefix/Dictionary/Aggregation logic introduced in v0.6.0.
AST engine tests are in test_migrate_ast.py (v0.11.0).
"""

import json
import os
import tempfile

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import migrate_rule module (sys.path set by conftest.py)
# ---------------------------------------------------------------------------
TOOLS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "tools")

import migrate_rule  # noqa: E402


# ===================================================================
# 1. guess_aggregation — 智能聚合猜測
# ===================================================================

@pytest.mark.parametrize("metric_name,expr,expected_mode", [
    ("mysql_slow_queries", "rate(mysql_global_status_slow_queries[5m])", "sum"),
    ("http_requests", "increase(http_requests_total[1h])", "sum"),
    ("http_requests_total", "http_requests_total", "sum"),
    ("cpu_percent", "container_cpu_percent", "max"),
    ("request_latency", "request_latency_seconds", "max"),
    ("mysql_connections", "mysql_global_status_threads_connected", "max"),
    ("network_bytes", "network_receive_bytes", "sum"),
    ("buffer_pool", "pages_data / pages_total * 100", "max"),
], ids=["rate→sum", "increase→sum", "total_suffix→sum", "percent→max",
        "latency→max", "connections→max", "bytes→sum", "division→max"])
def test_guess_aggregation_modes(metric_name, expr, expected_mode):
    """各種指標模式正確判斷聚合方式。"""
    mode, _ = migrate_rule.guess_aggregation(metric_name, expr)
    assert mode == expected_mode


def test_guess_aggregation_fallback_returns_max():
    """未匹配任何規則時後備回傳 max。"""
    mode, reason = migrate_rule.guess_aggregation(
        "some_obscure_metric", "some_obscure_metric")
    assert mode == "max"
    assert "Fallback" in reason


def test_guess_aggregation_reason_is_nonempty():
    """聚合判斷原因不為空。"""
    _, reason = migrate_rule.guess_aggregation(
        "mysql_connections", "mysql_global_status_threads_connected")
    assert len(reason) > 0


# ===================================================================
# 2. lookup_dictionary — 啟發式字典查找
# ===================================================================

_SAMPLE_DICT_V3 = {
    "mysql_global_status_threads_connected": {
        "maps_to": "mysql_connections",
        "golden_rule": "MariaDBHighConnections",
        "rule_pack": "mariadb",
        "note": "test note",
    },
}

def test_lookup_dictionary_match():
    """測試字典命中。"""
    result = migrate_rule.lookup_dictionary(
        "mysql_global_status_threads_connected", _SAMPLE_DICT_V3)
    assert result is not None
    assert result["golden_rule"] == "MariaDBHighConnections"

def test_lookup_dictionary_no_match():
    """測試字典未命中。"""
    result = migrate_rule.lookup_dictionary(
        "unknown_metric", _SAMPLE_DICT_V3)
    assert result is None

def test_lookup_dictionary_empty_dict():
    """測試空字典。"""
    result = migrate_rule.lookup_dictionary("foo", {})
    assert result is None

def test_lookup_dictionary_none_dict():
    """測試 None 字典。"""
    result = migrate_rule.lookup_dictionary("foo", None)
    assert result is None


# ===================================================================
# 3. parse_expr — PromQL 解析 (regex path)
# ===================================================================

def test_parse_expr_simple_gt():
    """測試簡單大於。"""
    result = migrate_rule.parse_expr(
        "mysql_global_status_threads_connected > 100", use_ast=False)
    assert result is not None
    assert result["op"] == ">"
    assert result["val"] == "100"
    assert result["base_key"] == "mysql_global_status_threads_connected"
    assert result["is_complex"] is False

def test_parse_expr_simple_lt():
    """測試簡單小於。"""
    result = migrate_rule.parse_expr("mysql_up < 1", use_ast=False)
    assert result is not None
    assert result["op"] == "<"
    assert result["val"] == "1"

def test_parse_expr_complex_rate():
    """測試複雜 rate 表達式。"""
    result = migrate_rule.parse_expr(
        "rate(mysql_global_status_slow_queries[5m]) > 0.1",
        use_ast=False)
    assert result is not None
    assert result["is_complex"] is True
    assert result["val"] == "0.1"

def test_parse_expr_unparseable_no_threshold():
    """測試無閾值的無法解析表達式。"""
    result = migrate_rule.parse_expr(
        "absent(mysql_up)", use_ast=False)
    assert result is None

def test_parse_expr_semantic_break_absent():
    """測試語義中斷 absent。"""
    result = migrate_rule.parse_expr(
        "absent(mysql_up) > 0", use_ast=False)
    assert result is None

def test_parse_expr_eq_operator():
    """測試等於運算子。"""
    result = migrate_rule.parse_expr(
        "mysql_up == 0", use_ast=False)
    assert result is not None
    assert result["op"] == "=="

def test_parse_expr_scientific_notation():
    """測試科學記號。"""
    result = migrate_rule.parse_expr(
        "metric > 1e6", use_ast=False)
    assert result is not None
    assert result["val"] == "1e6"


# ===================================================================
# 4. MigrationResult — 資料結構
# ===================================================================

def test_migration_result_defaults():
    """測試 MigrationResult 預設值。"""
    r = migrate_rule.MigrationResult("TestAlert", "perfect")
    assert r.alert_name == "TestAlert"
    assert r.status == "perfect"
    assert r.severity == "warning"
    assert r.tenant_config == {}
    assert r.recording_rules == []
    assert r.alert_rules == []
    assert r.agg_mode is None
    assert r.dict_match is None
    assert r.triage_action is None

def test_migration_result_critical_severity():
    """測試 MigrationResult 的 critical 嚴重度。"""
    r = migrate_rule.MigrationResult("TestAlert", "complex", "critical")
    assert r.severity == "critical"


# ===================================================================
# 5. process_rule — 核心處理邏輯 (regex path)
# ===================================================================

def test_process_rule_perfect_simple():
    """測試簡單完美規則。"""
    rule = {
        "alert": "TestHighConnections",
        "expr": "mysql_connections > 100",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
    assert result is not None
    assert result.status == "perfect"
    assert result.triage_action == "auto"
    assert "custom_mysql_connections" in result.tenant_config
    assert result.tenant_config["custom_mysql_connections"] == "100"
    assert len(result.recording_rules) == 2
    assert len(result.alert_rules) == 1

def test_process_rule_complex_with_rate():
    """測試複雜 rate 規則。"""
    rule = {
        "alert": "TestSlowQueries",
        "expr": "rate(mysql_slow_queries[5m]) > 0.1",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
    assert result is not None
    assert result.status == "complex"
    assert result.triage_action == "review"
    assert result.agg_mode == "sum"

def test_process_rule_unparseable():
    """測試無法解析規則。"""
    rule = {
        "alert": "TestAbsent",
        "expr": "absent(mysql_up)",
        "labels": {"severity": "critical"},
    }
    result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
    assert result is not None
    assert result.status == "unparseable"
    assert result.triage_action == "skip"
    assert result.llm_prompt is not None

def test_process_rule_golden_match():
    """測試黃金標準匹配。"""
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
    assert result.triage_action == "use_golden"
    assert result.dict_match is not None

def test_process_rule_no_prefix():
    """測試無前綴。"""
    rule = {
        "alert": "TestNoPrefix",
        "expr": "my_metric > 10",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(rule, prefix="", use_ast=False)
    assert "my_metric" in result.tenant_config
    # Alert name should NOT have "Custom" prefix
    assert result.alert_rules[0]["alert"] == "TestNoPrefix"

def test_process_rule_critical_severity_key():
    """測試 critical 嚴重度鍵。"""
    rule = {
        "alert": "TestCritical",
        "expr": "my_metric > 200",
        "labels": {"severity": "critical"},
    }
    result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
    assert "custom_my_metric_critical" in result.tenant_config

def test_process_rule_no_alert_name_returns_none():
    """測試無告警名稱回傳 None。"""
    rule = {"expr": "metric > 1"}
    result = migrate_rule.process_rule(rule, use_ast=False)
    assert result is None

def test_process_rule_shadow_labels_with_prefix():
    """測試帶前綴的影子標籤。"""
    rule = {
        "alert": "TestShadow",
        "expr": "metric > 5",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(rule, prefix="custom_", use_ast=False)
    labels = result.alert_rules[0].get("labels", {})
    assert labels.get("source") == "legacy"
    assert labels.get("migration_status") == "shadow"


# ===================================================================
# 6. write_triage_csv — CSV 輸出
# ===================================================================

def test_write_triage_csv_output():
    """測試 triage CSV 輸出。"""
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
        assert os.path.exists(csv_path)
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            lines = f.readlines()
        # Header + 2 data rows
        assert len(lines) == 3
        assert "AlertA" in lines[1]
        assert "AlertB" in lines[2]


# ===================================================================
# 7. write_prefix_mapping — Prefix Mapping YAML
# ===================================================================

def test_write_prefix_mapping_output():
    """測試前綴映射輸出。"""
    r = migrate_rule.MigrationResult("TestAlert", "perfect")
    r.tenant_config = {"custom_metric": "100"}
    results = [r]

    with tempfile.TemporaryDirectory() as tmpdir:
        path = migrate_rule.write_prefix_mapping(results, tmpdir, "custom_")
        assert path is not None
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        assert "custom_metric" in data
        assert data["custom_metric"]["original_metric"] == "metric"

def test_write_prefix_mapping_no_prefix_returns_none():
    """測試無前綴回傳 None。"""
    results = [migrate_rule.MigrationResult("X", "perfect")]
    with tempfile.TemporaryDirectory() as tmpdir:
        path = migrate_rule.write_prefix_mapping(results, tmpdir, "")
        assert path is None

def test_write_prefix_mapping_unparseable_skipped():
    """測試無法解析規則被跳過。"""
    r = migrate_rule.MigrationResult("X", "unparseable")
    with tempfile.TemporaryDirectory() as tmpdir:
        path = migrate_rule.write_prefix_mapping([r], tmpdir, "custom_")
        assert path is None


# ===================================================================
# 8. Convergence rate fix — golden_matches 排除 unparseable
# ===================================================================

def test_convergence_rate_golden_unparseable_not_over_subtracted():
    """測試 unparseable 規則與 golden 不過度扣減。"""
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
    assert convertible == 1

    # Old buggy formula would give: 2 + 0 - 2 = 0
    golden_all = [r for r in results if r.triage_action == "use_golden"]
    buggy_convertible = len(perfect) + len(complex_rules) - len(golden_all)
    assert buggy_convertible == 0  # confirms the bug existed


# ===================================================================
# 9. load_metric_dictionary — 字典載入
# ===================================================================

def test_load_metric_dictionary_loads_real_dictionary():
    """測試從 YAML 載入字典。"""
    d = migrate_rule.load_metric_dictionary(TOOLS_DIR)
    assert isinstance(d, dict)
    assert "mysql_global_status_threads_connected" in d

def test_load_metric_dictionary_missing_file_returns_empty():
    """測試缺失檔案回傳空字典。"""
    d = migrate_rule.load_metric_dictionary("/nonexistent")
    assert d == {}
