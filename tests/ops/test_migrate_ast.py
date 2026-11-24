#!/usr/bin/env python3
"""test_migrate_ast.py — 遷移引擎讀 PromQL 的方式（#4215：Prometheus 的語法樹）。

驗證:
  1. Metric name 從語法樹讀（`_lib_consumer_read.promql_metric_names`）
  2. Prefix injection (custom_ 前綴)
  3. Tenant label 依 selector 的 byte 位置注入（tenant=~".+"）
  4. 巢狀/複雜 PromQL 正確性 (and/or/unless, offset, subquery)
  5. 改寫後的 PromQL 仍被 Prometheus 的 parser 接受；拒收的運算式點名 raise
  6. End-to-end process_rule 整合

用法:
  python3 -m pytest tests/ops/test_migrate_ast.py -v
"""

import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

import _lib_consumer_read  # noqa: E402
import migrate_rule  # noqa: E402


def _names(expr):
    """The metrics *expr* reads, off its parse tree."""
    return _lib_consumer_read.promql_metric_names([expr], raise_refused=True)[0]


def _breaks(expr):
    """Whether *expr* calls a semantic-break function (SEMANTIC_BREAK_FUNCS)."""
    return bool(migrate_rule._calls(migrate_rule._tree_of(expr)) & migrate_rule.SEMANTIC_BREAK_FUNCS)


def _rewrite(expr, metrics):
    """*expr* with every selector reading *metrics* given `tenant=~".+"`."""
    return migrate_rule.rewrite_expr_tenant_label(expr, migrate_rule._tree_of(expr), set(metrics))


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


def test_tenant_inject_into_existing_labels():
    """測試在現有標籤中注入租戶。"""
    result = _rewrite(
        'mysql_up{job="mysql"} > 0',
        ["mysql_up"]
    )
    assert 'tenant=~".+"' in result
    assert 'job="mysql"' in result

def test_tenant_inject_bare_metric():
    """測試在裸露 metric 中注入租戶。"""
    result = _rewrite(
        "mysql_up > 0",
        ["mysql_up"]
    )
    assert '{tenant=~".+"}' in result

def test_tenant_inject_multiple_metrics():
    """測試多個 metric 的租戶注入。"""
    result = _rewrite(
        "(metric_a > 10) and (metric_b > 20)",
        ["metric_a", "metric_b"]
    )
    # Both metrics should have tenant label
    assert result.count('tenant=~".+"') == 2

def test_tenant_inject_validates_reparse():
    """測試注入租戶標籤後仍可 parse。"""
    result = _rewrite(
        'mysql_up{job="mysql"} > 0',
        ["mysql_up"]
    )
    ast = migrate_rule._tree_of(result)
    assert ast is not None

def test_tenant_inject_complex_nested():
    """測試巢狀函式中的租戶標籤注入。"""
    result = _rewrite(
        "sum by (user) (rate(mysql_queries[5m] offset 1h))",
        ["mysql_queries"]
    )
    assert 'tenant=~".+"' in result
    ast = migrate_rule._tree_of(result)
    assert ast is not None


def test_killer_compound_and_binary():
    """測試複合 and 運算（Case 1）。"""
    expr = ("(mysql_global_status_threads_connected > 100) "
            "and (mysql_global_status_threads_running > 50)")
    metrics = _names(expr)
    assert len(metrics) == 2
    assert "mysql_global_status_threads_connected" in metrics
    assert "mysql_global_status_threads_running" in metrics

def test_killer_complex_label_regex():
    """測試複雜 label matcher（Case 2）。"""
    expr = 'mysql_up{job="mysql", instance=~"10.0.*:3306"} == 0'
    metrics = _names(expr)
    assert metrics == ["mysql_up"]

    # Tenant injection roundtrip
    rewritten = _rewrite(expr, ["mysql_up"])
    ast = migrate_rule._tree_of(rewritten)
    assert ast is not None

def test_killer_aggregation_offset_time_window():
    """測試聚合 + 時間窗 + offset（Case 3）。"""
    expr = "sum by (user) (rate(mysql_global_status_queries[5m] offset 1h))"
    metrics = _names(expr)
    assert metrics == ["mysql_global_status_queries"]

    # Tenant injection preserves structure
    rewritten = _rewrite(expr, metrics)
    assert 'tenant=~".+"' in rewritten
    migrate_rule._tree_of(rewritten)  # Prometheus accepts it
    assert "[5m] offset 1h" in rewritten


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

def test_e2e_simple_rule_with_ast():
    """測試簡單規則：AST 引擎應產出正確的三件套。"""
    rule = {
        "alert": "TestHighConnections",
        "expr": "mysql_connections > 100",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )
    assert result is not None
    assert result.status == "perfect"
    assert result.triage_action == "auto"
    assert "custom_mysql_connections" in result.tenant_config
    assert len(result.recording_rules) == 2
    assert len(result.alert_rules) == 1

    # Recording rule LHS carries the tenant label
    rec_expr = result.recording_rules[0]["expr"]
    assert "tenant" in rec_expr

def test_e2e_complex_rule_with_ast():
    """測試複雜規則：rate() 包裹。"""
    rule = {
        "alert": "TestHighQPS",
        "expr": "rate(mysql_global_status_queries[5m]) > 1000",
        "labels": {"severity": "critical"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )
    assert result is not None
    assert result.status == "complex"
    # base_key should be the actual metric, not "rate"
    assert "custom_mysql_global_status_queries_critical" in result.tenant_config

def test_e2e_golden_standard_skip():
    """測試字典命中黃金標準：不加 prefix，建議 use_golden。"""
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
        rule, prefix="custom_", dictionary=dictionary
    )
    assert result is not None
    assert result.triage_action == "use_golden"

def test_e2e_semantic_break_returns_none():
    """測試語義中斷函式 (absent) 應回傳 unparseable。"""
    rule = {
        "alert": "TestAbsent",
        "expr": 'absent(mysql_up{job="mysql"}) == 1',
        "labels": {"severity": "critical"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )
    assert result is not None
    assert result.status == "unparseable"


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

def test_write_outputs_ast_path():
    """測試 AST 引擎產出：recording rule LHS 應包含租戶標籤。"""
    import tempfile

    rule = {
        "alert": "TestASTOutput",
        "expr": "mysql_connections > 100",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )
    assert result is not None

    with tempfile.TemporaryDirectory() as tmpdir:
        counts = migrate_rule.write_outputs(
            [result], tmpdir, prefix="custom_", dictionary={}, tenant="t1"
        )
        assert len(counts) == 4

        # Verify recording rules contain tenant label
        rr_path = os.path.join(tmpdir, migrate_rule.ARTIFACT_DIR, "platform-recording-rules.yaml")
        with open(rr_path, 'r', encoding='utf-8') as f:
            content = yaml.safe_load(f)
        rules = content["groups"][0]["rules"]
        # First rule should be the aggregation recording rule
        agg_rule = rules[0]
        assert "tenant" in agg_rule["expr"]
        assert "custom_mysql_connections" in agg_rule["record"]


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


def test_tenant_edge_same_metric_multiple_occurrences():
    """測試同一 metric 多次出現：每處都應注入租戶。"""
    result = _rewrite(
        'my_metric{a="1"} / my_metric{b="2"}',
        ["my_metric"]
    )
    # Both occurrences should have tenant label
    assert result.count('tenant=~".+"') == 2

def test_tenant_edge_same_metric_bare_and_labeled():
    """同一 metric 同時帶/不帶標籤：兩處都注入（#4215：依 selector 的位置插入；
    regex 時代只注入帶標籤的那處，裸露的那處讀到所有租戶）。"""
    result = _rewrite('my_metric > on() group_left my_metric{a="1"}', ["my_metric"])
    assert result == 'my_metric{tenant=~".+"} > on() group_left my_metric{tenant=~".+",a="1"}'


@pytest.mark.parametrize("expr,want", [
    # a string literal spelling the metric is not a selector
    ('label_replace(up, "a", "up", "", "")', 'label_replace(up{tenant=~".+"}, "a", "up", "", "")'),
    # a name given as a `__name__` matcher
    ('{__name__="x"} > 1', '{tenant=~".+",__name__="x"} > 1'),
    # whitespace between name and `{`, and an empty `{}`
    ('x {a="1"} > 2', 'x {tenant=~".+",a="1"} > 2'),
    ('x{} > 1', 'x{tenant=~".+"} > 1'),
    # range, offset and a multibyte label value before the selector
    ('x{db="日本"} + rate(y[5m] offset 1h) > 1',
     'x{tenant=~".+",db="日本"} + rate(y{tenant=~".+"}[5m] offset 1h) > 1'),
    # a comment inside the expression
    ('x # c\n  > 0', 'x{tenant=~".+"} # c\n  > 0'),
    # a comment between the name and its `{`, and inside an empty `{}`
    ('x # note\n{a="b"} > 1', 'x # note\n{tenant=~".+",a="b"} > 1'),
    ('x{ # c\n} > 1', 'x{tenant=~".+" # c\n} > 1'),
])
def test_tenant_inject_by_position(expr, want):
    """#4215: the tenant matcher goes in at each selector's byte offset."""
    metrics = _names(expr)
    assert _rewrite(expr, metrics) == want


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


def test_write_outputs_basic():
    """測試基本遷移產出：驗證所有檔案都被建立且 YAML 可解析。"""
    import tempfile

    rule = {
        "alert": "TestHighConn",
        "expr": "mysql_connections > 100",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )
    assert result is not None

    with tempfile.TemporaryDirectory() as tmpdir:
        counts = migrate_rule.write_outputs(
            [result], tmpdir, prefix="custom_", dictionary={}, tenant="t1"
        )
        assert len(counts) == 4  # (perfect, complex, unparseable, golden)

        # Verify files exist
        expected_files = [
            f"{migrate_rule.CONFD_DIR}/t1.yaml",
            f"{migrate_rule.CONFD_DIR}/_defaults.yaml",
            f"{migrate_rule.ARTIFACT_DIR}/platform-recording-rules.yaml",
            f"{migrate_rule.ARTIFACT_DIR}/platform-alert-rules.yaml",
            "migration-report.txt",
            "triage-report.csv",
            f"{migrate_rule.ARTIFACT_DIR}/prefix-mapping.yaml",
        ]
        for fname in expected_files:
            fpath = os.path.join(tmpdir, fname)
            assert os.path.exists(fpath), f"Missing: {fname}"

        # Verify recording rules YAML is parseable
        with open(os.path.join(tmpdir, migrate_rule.ARTIFACT_DIR, "platform-recording-rules.yaml"),
                   'r', encoding='utf-8') as f:
            content = yaml.safe_load(f)
            assert "groups" in content
            rules = content["groups"][0]["rules"]
            assert len(rules) > 0

def test_write_triage_csv_structure():
    """測試 Triage CSV：驗證欄位數量和標頭。"""
    import tempfile
    import csv as csv_mod

    rule = {
        "alert": "TestSimple",
        "expr": "my_metric > 50",
        "labels": {"severity": "warning"},
    }
    result = migrate_rule.process_rule(
        rule, prefix="custom_", dictionary={}
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = migrate_rule.write_triage_csv([result], tmpdir, {})
        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv_mod.reader(f)
            header = next(reader)
            assert len(header) == 14  # 14 columns
            row = next(reader)
            assert row[0] == "TestSimple"  # Alert Name


def _make_suppression_pair(prefix="custom_", base_metric="mysql_connections",
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
        warn_rule, prefix=prefix, dictionary={}
    )
    crit_r = migrate_rule.process_rule(
        crit_rule, prefix=prefix, dictionary={}
    )
    return [warn_r, crit_r]

def test_suppress_basic_pairing():
    """測試基本配對：warning + critical 都應有 metric_group label。"""
    results = _make_suppression_pair()
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 1

    # v1.2.0: 不再修改 PromQL，只加 metric_group label
    warn_labels = results[0].alert_rules[0]["labels"]
    crit_labels = results[1].alert_rules[0]["labels"]
    assert warn_labels["metric_group"] == "custom_mysql_connections"
    assert crit_labels["metric_group"] == "custom_mysql_connections"
    # PromQL 不應被修改（仍只有 maintenance unless）
    warn_expr = results[0].alert_rules[0]["expr"]
    assert warn_expr.count("unless on(tenant)") == 1

def test_suppress_critical_not_modified():
    """測試 critical 的 expr 不應被修改。"""
    results = _make_suppression_pair()
    crit_expr_before = results[1].alert_rules[0]["expr"]
    migrate_rule.apply_auto_suppression(results)
    crit_expr_after = results[1].alert_rules[0]["expr"]
    assert crit_expr_before == crit_expr_after

def test_suppress_no_pairing_warning_only():
    """測試只有 warning 沒有 critical 時不配對。"""
    warn_rule = {
        "alert": "TestWarnOnly",
        "expr": "my_metric > 50",
        "labels": {"severity": "warning"},
    }
    results = [migrate_rule.process_rule(
        warn_rule, prefix="custom_", dictionary={}
    )]
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 0
    # 仍然只有一個 unless
    assert results[0].alert_rules[0]["expr"].count("unless on(tenant)") == 1

def test_suppress_no_pairing_critical_only():
    """測試只有 critical 沒有 warning 時不配對。"""
    crit_rule = {
        "alert": "TestCritOnly",
        "expr": "my_metric > 100",
        "labels": {"severity": "critical"},
    }
    results = [migrate_rule.process_rule(
        crit_rule, prefix="custom_", dictionary={}
    )]
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 0

def test_suppress_no_pairing_different_metrics():
    """測試不同 metric 的 warning/critical 時不配對。"""
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
            warn_rule, prefix="custom_", dictionary={}),
        migrate_rule.process_rule(
            crit_rule, prefix="custom_", dictionary={}),
    ]
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 0

def test_suppress_multiple_pairs():
    """測試多組配對：各自獨立配對，各有 metric_group label。"""
    results = (
        _make_suppression_pair(base_metric="metric_a", warn_val="10", crit_val="20")
        + _make_suppression_pair(base_metric="metric_b", warn_val="30", crit_val="60")
    )
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 2

    # v1.2.0: 所有 warning+critical 都應有 metric_group label
    groups_seen = set()
    for r in results:
        mg = r.alert_rules[0]["labels"].get("metric_group")
        assert mg is not None, f"{r.alert_name} missing metric_group"
        groups_seen.add(mg)
    assert groups_seen == {"custom_metric_a", "custom_metric_b"}

def test_suppress_operator_preserved():
    """測試 < 運算子：metric_group label 與運算子無關，仍應正確配對。"""
    results = _make_suppression_pair(op="<", warn_val="100", crit_val="50")
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 1
    # v1.2.0: 只驗證 metric_group label 被加上
    warn_labels = results[0].alert_rules[0]["labels"]
    assert warn_labels["metric_group"] == "custom_mysql_connections"
    # PromQL 仍只有原始的 1 個 unless (maintenance)
    warn_expr = results[0].alert_rules[0]["expr"]
    assert warn_expr.count("unless on(tenant)") == 1

def test_suppress_notes_added():
    """測試配對後 warning result 應有 Severity Dedup 備註。"""
    results = _make_suppression_pair()
    migrate_rule.apply_auto_suppression(results)
    notes = results[0].notes
    assert any("Severity Dedup" in n for n in notes)

def test_suppress_unparseable_skipped():
    """測試 unparseable 規則不參與配對。"""
    results = _make_suppression_pair()
    results[1].status = "unparseable"  # 標記 critical 為 unparseable
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 0

def test_suppress_golden_skipped():
    """測試 use_golden 規則不參與配對。"""
    results = _make_suppression_pair()
    results[1].triage_action = "use_golden"
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 0

def test_suppress_write_outputs_with_suppression():
    """測試端到端：write_outputs 產出的 alert YAML 包含 metric_group label。"""
    import tempfile

    results = _make_suppression_pair()
    migrate_rule.apply_auto_suppression(results)

    with tempfile.TemporaryDirectory() as tmpdir:
        migrate_rule.write_outputs(results, tmpdir, prefix="custom_",
                                    dictionary={}, tenant="t1")
        alert_path = os.path.join(tmpdir, migrate_rule.ARTIFACT_DIR, "platform-alert-rules.yaml")
        with open(alert_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # v1.2.0: alert YAML 應包含 metric_group label
        assert 'metric_group: "custom_mysql_connections"' in content   # #3206: always quoted
        # unless 數量: warning(1 maintenance) + critical(1 maintenance) = 2
        assert content.count("unless on(tenant)") == 2

def test_suppress_dry_run_with_suppression():
    """測試 dry-run path：print_dry_run 不應因 suppressed expr 而崩潰。"""
    import io
    import contextlib

    results = _make_suppression_pair()
    migrate_rule.apply_auto_suppression(results)

    # Capture stdout — 確認 print_dry_run 能正常輸出
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        migrate_rule.print_dry_run(results)

    output = buf.getvalue()
    # 應包含兩條規則的摘要
    assert "TestHighConn" in output
    assert "TestHighConnCritical" in output

def test_suppress_no_prefix_pairing():
    """測試 prefix="" 時也能正確配對，metric_group label 仍被加上。"""
    results = _make_suppression_pair(prefix="")
    n = migrate_rule.apply_auto_suppression(results)
    assert n == 1
    # v1.2.0: 驗證 metric_group label（不再檢查 PromQL 修改）
    warn_labels = results[0].alert_rules[0]["labels"]
    assert warn_labels["metric_group"] == "mysql_connections"


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


def test_a_bare_selector_pins_nothing():
    """`x{db="a"} + x`: the bare `x` reads every db, so db is not pinned (the
    text reading only looked at the `{...}` it found)."""
    assert migrate_rule.static_label_values('x{db="a"} + x') == {}
    assert migrate_rule.static_label_values('x{db="a"} + x{db="a"}') == {"db": "a"}


@pytest.mark.parametrize("threshold", ["0x1F", "5m", "Inf", "NaN", "1_000"])
def test_a_threshold_the_threshold_file_cannot_hold_is_unparseable(threshold):
    """#4215 review: PromQL accepts these numbers, the threshold file and
    da-guard do not — writing failed late and the whole run produced nothing."""
    assert migrate_rule.parse_expr(f"x > {threshold}") is None


@pytest.mark.parametrize("threshold", ["1", "0.5", "1e3", "-1", "+1"])
def test_a_plain_threshold_is_kept_as_written(threshold):
    assert migrate_rule.parse_expr(f"x > {threshold}")["val"] == threshold


def test_a_selector_with_no_fixed_name_is_flagged_for_review():
    """No `__name__=` to pin: no tenant matcher can go in, so the recording rule
    reads every tenant — the result says so instead of shipping it silently."""
    rule = {"alert": "A", "expr": 'x / {job="a"} > 1', "labels": {"severity": "warning"}}
    result = migrate_rule.process_rule(rule, prefix="custom_", dictionary={})
    assert result.triage_action == "review"
    assert any('{job="a"}' in n for n in result.notes)
