"""
tests/ops/test_migrate_v3.py — migrate_rule.py 的字典與聚合猜測 helper（onboard-platform
也讀）。migrate-rule 本身（#4226）的測試在 test_migrate_rule.py。
"""

import os

import pytest

# ---------------------------------------------------------------------------
# Import migrate_rule module (sys.path set by conftest.py)
# ---------------------------------------------------------------------------
TOOLS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, 'scripts', "tools")

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
        "mysql_global_status_threads_connected > 100")
    assert result is not None
    assert result["op"] == ">"
    assert result["val"] == "100"
    assert result["base_key"] == "mysql_global_status_threads_connected"
    assert result["is_complex"] is False

def test_parse_expr_simple_lt():
    """測試簡單小於。"""
    result = migrate_rule.parse_expr("mysql_up < 1")
    assert result is not None
    assert result["op"] == "<"
    assert result["val"] == "1"

def test_parse_expr_complex_rate():
    """測試複雜 rate 表達式。"""
    result = migrate_rule.parse_expr(
        "rate(mysql_global_status_slow_queries[5m]) > 0.1")
    assert result is not None
    assert result["is_complex"] is True
    assert result["val"] == "0.1"

def test_parse_expr_unparseable_no_threshold():
    """測試無閾值的無法解析表達式。"""
    result = migrate_rule.parse_expr(
        "absent(mysql_up)")
    assert result is None

def test_parse_expr_semantic_break_absent():
    """測試語義中斷 absent。"""
    result = migrate_rule.parse_expr(
        "absent(mysql_up) > 0")
    assert result is None

def test_parse_expr_eq_operator():
    """測試等於運算子。"""
    result = migrate_rule.parse_expr(
        "mysql_up == 0")
    assert result is not None
    assert result["op"] == "=="

def test_parse_expr_scientific_notation():
    """測試科學記號。"""
    result = migrate_rule.parse_expr(
        "metric > 1e6")
    assert result is not None
    assert result["val"] == "1e6"


# ===================================================================
# 4. MigrationResult — 資料結構
# ===================================================================


# ===================================================================
# 5. process_rule — 核心處理邏輯 (regex path)
# ===================================================================


# ===================================================================
# 6. write_triage_csv — CSV 輸出
# ===================================================================


# ===================================================================
# 7. write_prefix_mapping — Prefix Mapping YAML
# ===================================================================


# ===================================================================
# 8. Convergence rate fix — golden_matches 排除 unparseable
# ===================================================================


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
