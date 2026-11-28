#!/usr/bin/env python3
"""test_migrate_ast.py — 遷移引擎讀 PromQL 的方式（#4215：Prometheus 的語法樹）。

驗證（onboard-platform 也讀這些 helper：parse_expr、guess_aggregation、字典）:
  1. Metric name 從語法樹讀（`_lib_consumer_read.promql_metric_names`）
  2. 巢狀/複雜 PromQL 正確性 (and/or/unless, offset, subquery)
  3. 拒收的運算式點名 raise

migrate-rule 本身（#4226）的測試在 test_migrate_rule.py。

用法:
  python3 -m pytest tests/ops/test_migrate_ast.py -v
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import _lib_consumer_read  # noqa: E402
import migrate_rule  # noqa: E402


def _names(expr):
    """The metrics *expr* reads, off its parse tree."""
    return _lib_consumer_read.promql_metric_names([expr], raise_refused=True)[0]


def _breaks(expr):
    """Whether *expr* calls a semantic-break function (SEMANTIC_BREAK_FUNCS)."""
    return bool(migrate_rule._calls(migrate_rule._tree_of(expr)) & migrate_rule.SEMANTIC_BREAK_FUNCS)


def test_ast_simple_metric():
    """測試簡單 metric 名稱。"""
    result = _names("mysql_up > 0")
    assert result == ["mysql_up"]

def test_ast_metric_with_labels():
    """測試帶標籤的 metric。"""
    result = _names(
        'mysql_up{job="mysql", instance=~"10.0.*:3306"} == 0'
    )
    assert result == ["mysql_up"]

def test_ast_rate_wrapped():
    """測試 rate() 包裹的 metric（regex 容易誤取）。"""
    result = _names(
        "rate(http_requests_total[5m]) > 100"
    )
    assert result == ["http_requests_total"]

def test_ast_nested_functions():
    """測試多層巢狀函式。"""
    result = _names(
        "sum by (user) (rate(mysql_global_status_queries[5m] offset 1h))"
    )
    assert result == ["mysql_global_status_queries"]

def test_ast_compound_and():
    """測試 and 複合表達式（多個 metric）。"""
    result = _names(
        "(mysql_global_status_threads_connected > 100) "
        "and (mysql_global_status_threads_running > 50)"
    )
    assert result == [
        "mysql_global_status_threads_connected",
        "mysql_global_status_threads_running",
    ]

def test_ast_compound_or_unless():
    """測試 or/unless 複合表達式。"""
    result = _names(
        "metric_a > 1 or metric_b > 2 unless metric_c > 3"
    )
    assert "metric_a" in result
    assert "metric_b" in result
    assert "metric_c" in result

def test_ast_histogram_quantile():
    """測試 histogram_quantile 加 le 標籤。"""
    result = _names(
        'histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))'
    )
    assert result == ["http_request_duration_seconds_bucket"]

def test_ast_binary_arithmetic():
    """測試算術運算中的 metric。"""
    result = _names(
        "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes > 1e9"
    )
    assert "node_memory_MemTotal_bytes" in result
    assert "node_memory_MemAvailable_bytes" in result

def test_ast_dimension_labels():
    """測試維度標籤提取。"""
    result = migrate_rule.extract_label_matchers(
        'redis_connected_clients{db="0", role="master"} > 100'
    )
    assert len(result) == 1
    assert result[0]["metric"] == "redis_connected_clients"
    assert "db" in result[0]["labels"]
    assert "role" in result[0]["labels"]

def test_ast_skip_infrastructure_labels():
    """測試跳過基礎設施標籤（job/instance/namespace）。"""
    result = migrate_rule.extract_label_matchers(
        'mysql_up{job="mysql", instance="10.0.0.1:3306", queue="tasks"} == 0'
    )
    # Should only have "queue", not "job" or "instance"
    if result:
        for r in result:
            assert "job" not in r["labels"]
            assert "instance" not in r["labels"]


def test_ast_absent_detected():
    """測試 absent 函數偵測。"""
    assert _breaks(
        'absent(mysql_up{job="mysql"})'
    ) is True

def test_ast_predict_linear_detected():
    """測試 predict_linear 函數偵測。"""
    assert _breaks(
        'predict_linear(node_filesystem_free_bytes[1h], 4*3600) < 0'
    ) is True

def test_ast_normal_not_detected():
    """測試正常表達式不偵測為語義中斷。"""
    assert _breaks(
        'rate(http_requests_total[5m]) > 100'
    ) is False


def test_killer_compound_and_binary():
    """測試複合 and 運算（Case 1）。"""
    expr = ("(mysql_global_status_threads_connected > 100) "
            "and (mysql_global_status_threads_running > 50)")
    metrics = _names(expr)
    assert len(metrics) == 2
    assert "mysql_global_status_threads_connected" in metrics
    assert "mysql_global_status_threads_running" in metrics


def test_walk_unary_expr():
    """測試一元表達式走訪：-metric_a。"""
    selectors = migrate_rule._selectors(migrate_rule._tree_of("-my_metric"))
    assert [migrate_rule._selector_metric(n) for n in selectors] == ["my_metric"]

def test_walk_paren_expr():
    """測試括號表達式走訪：(metric_a > 0)。"""
    selectors = migrate_rule._selectors(migrate_rule._tree_of("(my_metric > 0)"))
    assert [migrate_rule._selector_metric(n) for n in selectors] == ["my_metric"]

def test_walk_matrix_selector():
    """測試矩陣選擇器走訪：metric[5m]。"""
    selectors = migrate_rule._selectors(migrate_rule._tree_of("rate(my_counter[5m])"))
    assert [migrate_rule._selector_metric(n) for n in selectors] == ["my_counter"]


def test_parse_all_metrics_simple():
    """測試簡單表達式的所有 metrics。"""
    parsed = migrate_rule.parse_expr("mysql_connections > 100")
    assert parsed is not None
    assert parsed["all_metrics"] == ["mysql_connections"]

def test_parse_all_metrics_compound():
    """測試複合表達式的所有 metrics。"""
    parsed = migrate_rule.parse_expr(
        "(metric_a - metric_b) / metric_c > 0.5"
    )
    assert parsed is not None
    assert "metric_a" in parsed["all_metrics"]
    assert "metric_b" in parsed["all_metrics"]
    assert "metric_c" in parsed["all_metrics"]


def test_nested_absent_in_rate():
    """測試巢狀 Call 中的 absent：absent(rate(x[5m]))。"""
    # absent 接受 vector，rate 接受 matrix — 這不是合法 PromQL，
    # 但 _walk_calls 應能遍歷巢狀 Call 找到 absent
    # 使用合法的表達式：absent(rate(x[5m]))
    assert _breaks(
        'absent(rate(http_requests_total[5m]))'
    ) is True

def test_deeply_nested_predict_linear():
    """測試深層巢狀：sum(predict_linear(x[1h], 3600))。"""
    assert _breaks(
        'sum(predict_linear(node_filesystem_free_bytes[1h], 3600))'
    ) is True

def test_no_break_in_nested_safe_funcs():
    """測試巢狀安全函數無中斷：sum(rate(x[5m]))。"""
    assert _breaks(
        'sum(rate(http_requests_total[5m]))'
    ) is False


def test_an_expression_the_parser_refuses_raises_naming_it():
    """#4215: Prometheus' parser decides; a refusal names the expression."""
    with pytest.raises(_lib_consumer_read.InputRefused, match="this is not PromQL"):
        _names("this is not PromQL {{{}}")


def test_every_semantic_break_function_is_one_prometheus_knows():
    """The policy list names only functions the deployed Prometheus parses
    (`da-read promql-names`): an unknown one is refused at the rule loader
    already, so listing it would only be a stale entry (holt_winters)."""
    names = sorted(migrate_rule.SEMANTIC_BREAK_FUNCS)
    known = _lib_consumer_read.promql_names(names)
    assert all(known), [n for n, k in zip(names, known) if not k]

def test_dict_load_from_nonexistent_dir():
    """測試不存在的目錄回傳空 dict。"""
    result = migrate_rule.load_metric_dictionary("/nonexistent/path")
    assert result == {}

def test_dict_load_from_scripts_tools():
    """測試從 scripts/tools 載入（若檔案存在）。"""
    script_dir = os.path.join(REPO_ROOT, "scripts", "tools")
    dict_path = os.path.join(script_dir, "metric-dictionary.yaml")
    result = migrate_rule.load_metric_dictionary(script_dir)
    if os.path.exists(dict_path):
        assert isinstance(result, dict)
        assert len(result) > 0
    else:
        assert result == {}

def test_dict_lookup_dictionary_none():
    """測試空字典查找回傳 None。"""
    result = migrate_rule.lookup_dictionary("any_metric", None)
    assert result is None

def test_dict_lookup_dictionary_miss():
    """測試字典中不存在的 metric 回傳 None。"""
    result = migrate_rule.lookup_dictionary("missing", {"other": {}})
    assert result is None

def test_dict_lookup_dictionary_hit():
    """測試字典中存在的 metric 回傳對應 entry。"""
    entry = {"maps_to": "mysql_connections", "golden_rule": "X"}
    result = migrate_rule.lookup_dictionary("mysql_up", {"mysql_up": entry})
    assert result == entry


# --- #4215: what the text reading got wrong, read off the parse tree ---------

def test_a_multi_line_alert_is_split():
    """The regex split did not cross lines: every multi-line legacy alert was
    `unparseable`."""
    parsed = migrate_rule.parse_expr("sum by (job) (\n  rate(x_total[5m])\n) > 3\n")
    assert parsed is not None
    assert (parsed["op"], parsed["val"], parsed["base_key"]) == (">", "3", "x_total")


def test_a_label_value_does_not_make_a_selector_complex():
    """`-` or `[` inside a label value made the text reading call it complex."""
    parsed = migrate_rule.parse_expr('x{job="a-b", re=~"[0-9]+"} > 1')
    assert parsed["is_complex"] is False


def test_guess_aggregation_reads_calls_and_division_off_the_tree():
    # a `rate(` and a `/` inside label values are text, not a call or division
    assert migrate_rule.guess_aggregation("x_gauge", 'x_gauge{q="rate(a)/b"}')[0] == "max"
    assert migrate_rule.guess_aggregation("x_gauge", "rate(x_gauge[5m])")[0] == "sum"
    assert migrate_rule.guess_aggregation("x_gauge", "x_gauge / y")[1].startswith("包含除法")


@pytest.mark.parametrize("threshold", ["0x1F", "5m", "Inf", "NaN", "1_000"])
def test_a_threshold_the_threshold_file_cannot_hold_is_unparseable(threshold):
    """#4215 review: PromQL accepts these numbers, the threshold file and
    da-guard do not — writing failed late and the whole run produced nothing."""
    assert migrate_rule.parse_expr(f"x > {threshold}") is None


@pytest.mark.parametrize("threshold", ["1", "0.5", "1e3", "-1", "+1"])
def test_a_plain_threshold_is_kept_as_written(threshold):
    assert migrate_rule.parse_expr(f"x > {threshold}")["val"] == threshold


# --- #4298 review ------------------------------------------------------------


@pytest.mark.parametrize("threshold", ["012", "-012", "00"])
def test_an_octal_looking_threshold_is_unparseable(threshold):
    """Prometheus reads `012` as octal (10); float() would write 12."""
    assert migrate_rule.parse_expr(f"x > {threshold}") is None


@pytest.mark.parametrize("expr", [
    '{__name__="legacy metric"} > 1',   # no bare selector spells it
    '{service="api"} > 1',              # no name at all: no shared unknown key
])
def test_a_rule_whose_key_cannot_be_derived_is_unparseable(expr):
    assert migrate_rule.parse_expr(expr) is None


def test_a_tree_too_deep_to_walk_is_refused_and_named_not_a_crash():
    deep = "(" * 1200 + "x" + ")" * 1200
    trees = _lib_consumer_read.promql_shape_each([deep, "y > 2"])
    assert isinstance(trees[0], _lib_consumer_read.InputRefused)
    assert "too deeply" in str(trees[0])
    assert trees[1]["kind"] == "binary"
