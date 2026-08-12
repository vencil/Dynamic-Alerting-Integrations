#!/usr/bin/env python3
"""Tests for onboard_platform.py — Reverse analysis engine for Dynamic Alerting onboarding.

pytest style：使用 plain assert + conftest fixtures。
"""
import csv
import os
import tempfile

import pytest
import yaml

from factories import make_am_config, make_am_receiver, write_yaml
from onboard_platform import (
    parse_alertmanager_config,
    flatten_route_tree,
    reverse_map_receiver,
    analyze_alertmanager,
    generate_tenant_routing_yamls,
    _extract_tenant_from_matchers,
    _check_timing_guardrails,
    classify_rule,
    _clean_alert_expr,
    _enrich_parsed_result,
    extract_threshold_candidates,
    analyze_rule_files,
    generate_defaults_from_candidates,
    _render_critical_suggestion,
    write_migration_csv,
    parse_scrape_configs,
    analyze_relabel_configs,
    analyze_scrape_configs,
    scan_rule_files,
    write_outputs,
    DEFAULT_TENANT_LABEL,
)


# ============================================================
# Phase 1: Alertmanager Reverse Analysis
# ============================================================

class TestParseAlertmanagerConfig:
    """parse_alertmanager_config() 各種輸入格式。"""

    def test_raw_alertmanager_yaml(self, config_dir):
        """Alertmanager 原始 YAML 正確解析。"""
        path = write_yaml(config_dir, "am.yaml", yaml.dump({
            "route": {"receiver": "default", "group_by": ["alertname"]},
            "receivers": [{"name": "default"}],
        }))
        result = parse_alertmanager_config(path)
        assert result is not None
        assert "route" in result
        assert "receivers" in result

    def test_configmap_wrapped(self, config_dir):
        """ConfigMap with data.alertmanager.yml string."""
        inner = yaml.dump({
            "route": {"receiver": "default"},
            "receivers": [{"name": "default"}],
        })
        path = write_yaml(config_dir, "cm.yaml", yaml.dump({
            "data": {"alertmanager.yml": inner},
        }))
        result = parse_alertmanager_config(path)
        assert result is not None
        assert "route" in result

    def test_invalid_file(self, config_dir):
        """無效 YAML 檔案回傳 None。"""
        path = write_yaml(config_dir, "bad.yaml", "just a string\n")
        result = parse_alertmanager_config(path)
        assert result is None

    def test_nonexistent_file(self):
        """不存在的檔案回傳 None。"""
        assert parse_alertmanager_config("/nonexistent/path.yaml") is None

    def test_empty_file(self, config_dir):
        """空檔案回傳 None。"""
        path = write_yaml(config_dir, "empty.yaml", "")
        assert parse_alertmanager_config(path) is None


class TestExtractTenantFromMatchers:
    """從 Alertmanager matchers 提取租戶標籤。"""

    def test_exact_match(self):
        """完全相符的租戶 matcher 正確提取。"""
        assert _extract_tenant_from_matchers(['tenant="db-a"'], "tenant") == "db-a"

    def test_regex_exact(self):
        """Regex matcher that is actually exact (no regex chars)."""
        assert _extract_tenant_from_matchers(['tenant=~"db-b"'], "tenant") == "db-b"

    def test_true_regex_skipped(self):
        """True regex patterns should be skipped."""
        assert _extract_tenant_from_matchers(['tenant=~"db-.*"'], "tenant") is None

    def test_custom_label(self):
        """自訂標籤名稱的租戶提取。"""
        assert _extract_tenant_from_matchers(['instance="prod-1"'], "instance") == "prod-1"

    def test_no_tenant_matcher(self):
        """找不到租戶 matcher 回傳 None。"""
        assert _extract_tenant_from_matchers(['severity="critical"'], "tenant") is None

    def test_empty_matchers(self):
        """空或無效 matchers 回傳 None。"""
        assert _extract_tenant_from_matchers([], "tenant") is None
        assert _extract_tenant_from_matchers(None, "tenant") is None


class TestFlattenRouteTree:
    """展平 Alertmanager 路由樹結構。"""

    def test_simple_flat(self):
        """簡單路由樹展平成平面列表。"""
        route = {
            "receiver": "default",
            "routes": [
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
                {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
            ],
        }
        flat = flatten_route_tree(route)
        tenants = sorted(r["tenant"] for r in flat if r["tenant"])
        assert tenants == ["db-a", "db-b"]

    def test_nested_routes(self):
        """巢狀路由結構正確展平。"""
        route = {
            "receiver": "default",
            "routes": [{
                "matchers": ['tenant="db-a"'],
                "receiver": "tenant-db-a",
                "routes": [{
                    "matchers": ['severity="critical"'],
                    "receiver": "tenant-db-a-critical",
                }],
            }],
        }
        flat = flatten_route_tree(route)
        assert len(flat) >= 2

    def test_continue_flag(self):
        """continue 旗標正確保留。"""
        route = {"receiver": "platform", "continue": True, "routes": []}
        flat = flatten_route_tree(route)
        assert flat[0]["continue_flag"] is True

    def test_legacy_match_format(self):
        """舊版 match 格式相容。"""
        route = {
            "receiver": "default",
            "routes": [{"match": {"tenant": "db-a"}, "receiver": "tenant-db-a"}],
        }
        flat = flatten_route_tree(route)
        tenants = [r["tenant"] for r in flat if r["tenant"]]
        assert "db-a" in tenants

    def test_empty_route(self):
        """空路由回傳空列表。"""
        assert flatten_route_tree({}) == []

    def test_custom_tenant_label(self):
        """自訂租戶標籤名稱的展平。"""
        route = {
            "receiver": "default",
            "routes": [{"matchers": ['cluster="prod"'], "receiver": "cluster-prod"}],
        }
        flat = flatten_route_tree(route, tenant_label="cluster")
        tenants = [r["tenant"] for r in flat if r["tenant"]]
        assert "prod" in tenants


class TestReverseMapReceiver:
    """逆向映射 Alertmanager receiver 設定。"""

    @pytest.mark.parametrize("name,rtype,config_key,config,expected_type,extra_check", [
        ("tenant-db-a", "webhook", "webhook_configs",
         [{"url": "https://hook.example.com"}],
         "webhook", ("url", "https://hook.example.com")),
        ("team-slack", "slack", "slack_configs",
         [{"api_url": "https://hooks.slack.com/services/T/B/X", "channel": "#alerts"}],
         "slack", ("channel", "#alerts")),
        ("dba-email", "email", "email_configs",
         [{"to": "dba@example.com", "smarthost": "smtp.example.com:587"}],
         "email", ("to", "dba@example.com")),
        ("oncall-pd", "pagerduty", "pagerduty_configs",
         [{"service_key": "abc123"}],
         "pagerduty", None),
        ("noc-teams", "teams", "msteams_configs",
         [{"webhook_url": "https://outlook.office.com/webhook/test"}],
         "teams", None),
    ], ids=["webhook", "slack", "email", "pagerduty", "teams"])
    def test_receiver_type(self, name, rtype, config_key, config, expected_type, extra_check):
        """各 receiver 類型正確逆向映射。"""
        receivers = [{"name": name, config_key: config}]
        result = reverse_map_receiver(receivers, name)
        assert result is not None
        assert result["type"] == expected_type
        if extra_check:
            key, val = extra_check
            assert result[key] == val

    def test_missing_receiver(self):
        """找不到的 receiver 回傳 None。"""
        assert reverse_map_receiver([{"name": "other"}], "missing") is None

    def test_empty_receivers(self):
        """空或無效 receivers 回傳 None。"""
        assert reverse_map_receiver([], "any") is None
        assert reverse_map_receiver(None, "any") is None


# ── Alertmanager integration ─────────────────────────────────

class TestAnalyzeAlertmanager:
    """分析 Alertmanager 配置並提取租戶路由資訊。"""

    def test_basic_tenant_extraction(self):
        """基本租戶路由提取與分析。"""
        am = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a",
                     "group_wait": "30s", "repeat_interval": "4h"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://hook.example.com"),
            ],
        )
        routings, summary = analyze_alertmanager(am)
        assert "db-a" in routings
        assert routings["db-a"]["receiver"]["type"] == "webhook"
        assert summary["tenant_routes"] == 1

    def test_platform_enforced_route_skipped(self):
        """平台強制路由被正確跳過。"""
        am = make_am_config(
            routes=[
                {"receiver": "platform-noc", "continue": True},
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
            ],
            receivers=[
                {"name": "default"},
                make_am_receiver("platform-noc", url="https://noc.example.com"),
                make_am_receiver("tenant-db-a", url="https://a.example.com"),
            ],
        )
        routings, summary = analyze_alertmanager(am)
        assert "db-a" in routings
        assert len(summary["skipped_routes"]) >= 1

    def test_timing_guardrail_warnings(self):
        """時序機制警告正確偵測。"""
        am = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a",
                     "group_wait": "1s"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://a.example.com"),
            ],
        )
        _, summary = analyze_alertmanager(am)
        assert any("below" in w.lower() for w in summary["warnings"])

    def test_severity_dedup_detection(self):
        """嚴重度 dedup 機制正確偵測。"""
        am = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://a.example.com"),
            ],
            inhibit_rules=[{
                "source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="db-a"'],
                "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="db-a"'],
                "equal": ["metric_group"],
            }],
        )
        _, summary = analyze_alertmanager(am)
        assert summary["dedup_tenants"].get("db-a") == "enable"

    def test_custom_tenant_label(self):
        """自訂租戶標籤名稱的分析。"""
        am = make_am_config(
            routes=[{"matchers": ['cluster="prod-1"'], "receiver": "cluster-prod-1"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("cluster-prod-1", url="https://p.example.com"),
            ],
        )
        routings, _ = analyze_alertmanager(am, tenant_label="cluster")
        assert "prod-1" in routings

    def test_multiple_tenants(self):
        """多個租戶同時分析。"""
        am = make_am_config(
            routes=[
                {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
                {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
            ],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://a.example.com"),
                make_am_receiver("tenant-db-b", "slack", url="https://slack.example.com"),
            ],
        )
        routings, summary = analyze_alertmanager(am)
        assert summary["tenant_routes"] == 2
        assert routings["db-a"]["receiver"]["type"] == "webhook"
        assert routings["db-b"]["receiver"]["type"] == "slack"


class TestCheckTimingGuardrails:
    """檢查時序機制的有效性。"""

    def test_valid_timing(self):
        """有效時序值無警告。"""
        val, warn = _check_timing_guardrails("30s", "group_wait")
        assert val == "30s"
        assert warn is None

    def test_below_minimum(self):
        """低於最小值的警告。"""
        val, warn = _check_timing_guardrails("1s", "group_wait")
        assert warn is not None
        assert "below" in warn

    def test_above_maximum(self):
        """超過最大值的警告。"""
        val, warn = _check_timing_guardrails("100h", "repeat_interval")
        assert warn is not None
        assert "above" in warn

    def test_none_value(self):
        """None 值無警告。"""
        val, warn = _check_timing_guardrails(None, "group_wait")
        assert val is None
        assert warn is None


class TestGenerateTenantRoutingYamls:
    """產生租戶路由 YAML 設定。"""

    def test_basic_generation(self):
        """基本路由 YAML 產生。"""
        routings = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
                "group_wait": "30s",
            },
        }
        result = generate_tenant_routing_yamls(routings)
        assert "db-a" in result
        parsed = yaml.safe_load(result["db-a"])
        assert parsed["tenants"]["db-a"]["_routing"]["receiver"]["type"] == "webhook"

    def test_with_dedup_info(self):
        """包含 dedup 資訊的路由 YAML 產生。"""
        routings = {"db-a": {"receiver": {"type": "webhook", "url": "https://a.com"}}}
        result = generate_tenant_routing_yamls(routings, dedup_info={"db-a": "enable"})
        parsed = yaml.safe_load(result["db-a"])
        assert parsed["tenants"]["db-a"]["_severity_dedup"] == "enable"


# ============================================================
# Phase 2: Rule File Analysis
# ============================================================

class TestClassifyRule:
    """分類告警規則類型。"""

    def test_recording_rule(self):
        """錄製規則正確分類。"""
        assert classify_rule({"record": "foo:bar:max"}) == "recording"

    def test_alert_rule(self):
        """告警規則正確分類。"""
        assert classify_rule({"alert": "HighCPU"}) == "alert"

    def test_unknown_rule(self):
        """未知規則類型正確分類。"""
        assert classify_rule({"comment": "test"}) == "unknown"


class TestCleanAlertExpr:
    """_clean_alert_expr() 運算式清理測試。"""

    def test_multiline_merge(self):
        """多行運算式合併為單行。"""
        assert _clean_alert_expr("cpu_usage\n  > 80") == "cpu_usage > 80"

    def test_remove_unless_maintenance(self):
        """移除 unless on() maintenance 子句。"""
        expr = 'metric > 50 unless on(tenant) (user_state_filter{flag="maintenance"} == 1)'
        assert _clean_alert_expr(expr) == "metric > 50"

    def test_unwrap_balanced_parens(self):
        """移除最外層平衡括號。"""
        assert _clean_alert_expr("(cpu > 80)") == "cpu > 80"

    def test_keep_unbalanced_parens(self):
        """不平衡括號不移除。"""
        expr = "(cpu > 80) or (mem > 90)"
        assert _clean_alert_expr(expr) == expr

    def test_empty_expr(self):
        """空運算式回傳空字串。"""
        assert _clean_alert_expr("") == ""
        assert _clean_alert_expr("   ") == ""

    def test_nested_balanced_parens(self):
        """巢狀平衡括號正確處理。"""
        assert _clean_alert_expr("(max(cpu{job='test'}) > 80)") == "max(cpu{job='test'}) > 80"


class TestExtractThresholdCandidates:
    """提取閾值候選值。"""

    def test_simple_threshold(self):
        """簡單閾值運算式提取。"""
        rule = {
            "alert": "HighConnections",
            "expr": "mysql_connections > 100",
            "labels": {"severity": "warning"},
        }
        result = extract_threshold_candidates(rule)
        if result["status"] != "unparseable":
            assert result["threshold_value"] == "100"
            assert result["operator"] == ">"
            assert result["severity"] == "warning"

    def test_unparseable_expr(self):
        """無法解析的運算式標記為 unparseable。"""
        rule = {
            "alert": "ComplexAlert",
            "expr": "absent(up{job='test'})",
            "labels": {"severity": "critical"},
        }
        assert extract_threshold_candidates(rule)["status"] == "unparseable"

    def test_severity_extraction(self):
        """嚴重度標籤正確提取。"""
        rule = {"alert": "CriticalCPU", "expr": "cpu_usage > 95",
                "labels": {"severity": "critical"}}
        assert extract_threshold_candidates(rule)["severity"] == "critical"

    def test_metric_group_extraction(self):
        """度量群組標籤正確提取。"""
        rule = {"alert": "TestAlert", "expr": "metric_a > 50",
                "labels": {"severity": "warning", "metric_group": "metric_a"}}
        assert extract_threshold_candidates(rule)["metric_group"] == "metric_a"


class TestAnalyzeRuleFiles:
    """分析告警規則檔案。"""

    def test_basic_rule_file(self, config_dir):
        """基本規則檔案分析。"""
        content = yaml.dump({
            "groups": [{
                "name": "test-alerts",
                "rules": [
                    {"alert": "HighCPU", "expr": "cpu > 80", "labels": {"severity": "warning"}},
                    {"record": "tenant:cpu:max", "expr": "max by(tenant) (cpu)"},
                ],
            }],
        })
        path = write_yaml(config_dir, "rules.yaml", content)
        candidates, recording, summary = analyze_rule_files([path])
        assert summary["alert_rules"] == 1
        assert summary["recording_rules"] == 1
        assert summary["total_groups"] == 1

    def test_empty_file(self, config_dir):
        """空規則檔案分析。"""
        path = write_yaml(config_dir, "empty.yaml", "")
        _, _, summary = analyze_rule_files([path])
        assert summary["total_rules"] == 0

    def test_nonexistent_file(self):
        """不存在的規則檔案報告錯誤。"""
        _, _, summary = analyze_rule_files(["/nonexistent/rules.yaml"])
        assert len(summary["errors"]) > 0

    def test_configmap_wrapped_rules(self, config_dir):
        """Rule files wrapped in ConfigMap format."""
        inner_dict = {
            "groups": [{
                "name": "test",
                "rules": [{"alert": "Test", "expr": "metric > 1",
                            "labels": {"severity": "warning"}}],
            }],
        }
        cm = {"data": {"rules.yaml": yaml.dump(inner_dict)}}
        path = write_yaml(config_dir, "cm.yaml", yaml.dump(cm, default_flow_style=False))
        candidates, _, summary = analyze_rule_files([path])
        assert summary["alert_rules"] == 1


class TestGenerateDefaultsFromCandidates:
    """從候選值產生預設值。"""

    def test_basic_defaults(self):
        """基本預設值產生 —— warning 進 defaults、critical 進另一層（#1218）。

        ⛔ 這支測試原本斷言 `defaults["defaults"]["cpu_critical"] == "95"`，
        也就是把缺陷本身釘成契約。`resolveCriticalRows` 只迭代租戶覆寫，所以
        `defaults:` 裡的 `<base>_critical` 產不出 critical 閾值——它會落到 base
        resolver，發出一條 `severity="warning"` 的 series，而 `_critical` 這串字會落進
        component/metric label 裡、不會變成 severity（`parseMetricKey` 切第一個底線：
        本例 `cpu_critical` → `{component="cpu", metric="critical"}`；無底線的 key →
        `component="default"`）。⛔ **不是** `metric="<整個 key>"`——後者正是 #731 的形狀，
        `app/rulepack_contract_test.go` 就是為了擋它而存在。
        沒有記錄規則 join 它，對應的 `tenant:alert_threshold:` 恆空、`*Critical` 恆不開火。
        兩個方向實測於 pkg/config/critical_tier_placement_test.go。而這支工具產出的
        檔案，標頭明寫「Review and merge into conf.d/_defaults.yaml」——照做的客戶
        會在合併當下失去整個 critical 分級，而那正是他們跑 onboard 的理由。
        """
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        assert "defaults" in defaults
        assert defaults["defaults"]["cpu"] == "80"
        assert "cpu_critical" not in defaults["defaults"]
        assert defaults["critical_overrides"]["cpu_critical"] == "95"

    def test_critical_tier_is_rendered_as_a_commented_tenant_block(self):
        """產出檔裡 critical 層必須是註解、且指向租戶檔。

        parse 後的 `defaults:` 不得含它（否則照標頭指示合併就會複製缺陷），但值
        必須留在檔案裡——這支工具的全部價值就是把客戶既有的 critical 閾值撈出來。
        """
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
        ]
        suggestion = generate_defaults_from_candidates(candidates)
        body = yaml.dump({"defaults": suggestion["defaults"]}) + \
            _render_critical_suggestion(suggestion["critical_overrides"],
                                        suggestion["defaults"])

        parsed = yaml.safe_load(body)
        assert parsed["defaults"] == {"cpu": "80"}
        assert not [k for k in parsed["defaults"] if k.endswith("_critical")]
        assert 'cpu_critical: "95"' in body
        assert "do NOT put these under `defaults:`" in body
        # 每一行 critical 建議都必須是註解——少一個 `#` 就等於把它併回 defaults:
        for line in body.splitlines():
            if "cpu_critical" in line:
                assert line.lstrip().startswith("#"), line

    # 兩個段落標題。取自渲染器本身而非再抄一次字面值：抄一次就是第二份契約，
    # 而這整個 PR 的主題就是手抄副本會各自漂移。
    _READY_HEADER = "# READY —"
    _BLOCKED_HEADER = "# ⛔ BLOCKED —"

    def test_the_two_section_headers_are_what_the_renderer_emits(self):
        """上面兩個常數是本檔的比對錨點，所以先證明它們真的出現在渲染輸出裡——
        否則後面每一條 `in body` / `not in body` 都會變成恆真或恆假。"""
        both = _render_critical_suggestion({"a_b_critical": "1", "c_d_critical": "2"},
                                           {"a_b": "1"})
        assert self._READY_HEADER in both, both
        assert self._BLOCKED_HEADER in both, both

    def test_dangling_critical_is_separated_from_the_copyable_ones(self):
        """⛔ 建議行分兩組，判準是 base 在不在**同一份** `defaults:`（#1218 盲審）。

        單一組的版本對每一條都給同一句指示「Copy each line into the `tenants:`
        block」，而唯一的 caveat 講錯了後果——「takes effect while `<base>` has a
        value」讀起來是「那一列不生效」，實際是 `ValidateTenantKeys` 把懸空的
        `<base>_critical` 放進 **blocking** `Errors`、tenant-api 拒收**整份**租戶檔
        （#1227；`gitops/writer.go:599` 的 `keyErrs`）。只有 critical severity 規則
        的客戶（只 page 不 warn，很常見）會 100% 命中。
        """
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
            # base 從未以 warning severity 出現 → defaults 裡不會有 disk_usage
            {"status": "perfect", "metric_key": "disk_usage", "severity": "critical", "threshold_value": "95"},
        ]
        suggestion = generate_defaults_from_candidates(candidates)
        assert suggestion["defaults"] == {"cpu": "80"}
        assert set(suggestion["critical_overrides"]) == {
            "cpu_critical", "disk_usage_critical"}

        body = _render_critical_suggestion(
            suggestion["critical_overrides"], suggestion["defaults"])
        # 段落標題，不是「出現過這個字」——BLOCKED 段的說明文字本身就提到 READY
        # 清單（「move the line up to the READY list」），用鬆散比對會自己騙自己。
        assert body.count(self._READY_HEADER) == 1 and body.count(self._BLOCKED_HEADER) == 1
        ready, blocked = body.split(self._BLOCKED_HEADER, 1)

        # 有 base 的那條在 READY 段；沒有 base 的在 BLOCKED 段——不是交換
        assert "cpu_critical" in ready and "cpu_critical" not in blocked
        assert "disk_usage_critical" in blocked and "disk_usage_critical" not in ready
        # BLOCKED 段必須講對後果（整份被拒），不是「不生效」
        assert "WHOLE tenant file" in blocked
        # 數字仍然留著——這支工具的全部價值就是把客戶既有的閾值撈出來
        assert 'disk_usage_critical: "95"' in body
        # 兩組都必須整行是註解
        for line in body.splitlines():
            if "_critical:" in line:
                assert line.lstrip().startswith("#"), line

    def test_ready_states_which_defaults_it_was_judged_against(self):
        """⛔ READY 是拿**這份建議自己的** `defaults:` 算的，不是客戶部署中的
        `conf.d/_defaults.yaml`——這支工具從未讀過那個檔。而同一份產出的標頭寫
        「Review and merge into conf.d/_defaults.yaml」，review 這個動詞本身就在
        邀請選擇性合併：只要客戶決定不併某個 `<base>`，對應的 READY 行就變成
        BLOCKED 情境（整份租戶檔被拒），而檔案裡沒有任何字提醒他（round-2 盲審）。
        """
        # ⛔ fixture 必須同時產出 READY 與 BLOCKED 兩段，否則 `split(BLOCKED)[0]`
        # 就等於整份 body，這條斷言會退化成「檔案裡某處有這些字」——盲審實測前一版
        # 的 fixture 全部落在 READY、`split(...)[0] == body`，分段是裝飾性的。
        suggestion = generate_defaults_from_candidates([
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
            {"status": "perfect", "metric_key": "disk_usage", "severity": "critical", "threshold_value": "95"},
        ])
        body = _render_critical_suggestion(
            suggestion["critical_overrides"], suggestion["defaults"])
        assert self._BLOCKED_HEADER in body, body
        ready = body.split(self._BLOCKED_HEADER)[0]
        assert ready != body, "split 沒切到東西 ⇒ 下面的斷言退化成整檔搜尋"

        # 條件必須明講是「上面那個 defaults: 區塊」而且未查過部署中的檔，
        # 而且要在 READY 段**之內**——下界是 READY 標題，不是檔案開頭。
        # ⛔ 只驗「在 BLOCKED 之前」擋不住「被搬進共用前言」：前言也在 BLOCKED
        # 之前。變異實測：把條件搬到前言，只驗上界的版本照樣綠。
        lo = body.index(self._READY_HEADER)
        hi = body.index(self._BLOCKED_HEADER)
        for phrase in ("ABOVE", "never looked at your",
                       "once you have actually merged"):
            assert phrase in ready, (phrase, body)
            assert lo < body.index(phrase) < hi, (phrase, body)

        # ⛔ 而且必須**指名目的地檔案**。這一段的全部價值就是「別放進
        # _defaults.yaml」，但在 round-6 盲審之前，這份產出檔唯一出現過的檔名
        # 就是 conf.d/_defaults.yaml（它自己的檔頭指令），也就是這些 key 放進去
        # 會失效的那一個。變異實測：把這行拿掉，當時沒有任何測試轉紅。
        assert "conf.d/<tenant>.yaml" in ready, body
        assert "NOT" in ready and "conf.d/_defaults.yaml" in ready, body

    def test_blocked_remedy_states_its_fleet_wide_cost(self):
        """⛔ BLOCKED 的補救（把 `<base>` 加進 `defaults:`）不是免費的：實測一個
        `defaults:` key 在 3 租戶機隊上會讓**每一個**租戶多一條 warning 列，而同一
        個產品的 `_defaults.yaml` 標頭正好反過來警告不要這樣做（"Do NOT move one
        into `defaults:` to 'fix' the silence — that arms a platform-chosen
        number for every tenant"）。兩份產出檔給相反的建議是最糟的形狀。
        """
        suggestion = generate_defaults_from_candidates([
            {"status": "perfect", "metric_key": "disk_usage",
             "severity": "critical", "threshold_value": "95"},
        ])
        blocked = _render_critical_suggestion(
            suggestion["critical_overrides"], suggestion.get("defaults") or {})
        assert "EVERY" in blocked and "tenant" in blocked, blocked
        # 並且要說明沒有租戶範圍的替代路徑（base 寫成租戶 override 不算數）
        assert "resolveCriticalRows" in blocked, blocked
        assert "tenant override does not enable" in blocked, blocked

    def test_a_key_without_the_suffix_is_dropped_rather_than_mis_sliced(self):
        """⛔ 這支守衛（`onboard_platform.py` 的 `endswith(CRITICAL_SUFFIX)`）是為了
        修「類別」而加的——但加完**沒有任何斷言在守它**（round-4 盲審：刪掉那兩行，
        全檔照樣綠，因為六個呼叫點餵的都是 `*_critical`）。

        處置為何是丟棄而非保留：這一層收到的非 `_critical` key **不是客戶的數字**
        （客戶的閾值在 `defaults` 那一半，由 `:644` 依 severity 分流），而是產生器
        自己壞掉才會出現的東西。同檔「不要靜默丟棄客戶數字」的原則講的是**值**，
        不是畸形的鍵名——把它切掉九個字元再拿去比對 `defaults`，才會產生一個
        與這個 key 無關的判斷。
        """
        body = _render_critical_suggestion(
            {"cpu_critical": "95", "not_a_critical_key": "1"}, {"cpu": "80"})
        assert 'cpu_critical: "95"' in body
        assert "not_a_critical_key" not in body, body
        # …且不得把它誤切成 `not_a_criti` 之類的東西拿去比對
        assert "not_a_criti" not in body, body

    def test_all_blocked_when_the_estate_is_critical_only(self):
        """只 page 不 warn 的 estate：每一條建議都是懸空的，檔案不得出現 READY 段。"""
        suggestion = generate_defaults_from_candidates([
            {"status": "perfect", "metric_key": "disk_usage",
             "severity": "critical", "threshold_value": "95"},
        ])
        assert "defaults" not in suggestion
        body = _render_critical_suggestion(
            suggestion["critical_overrides"], suggestion.get("defaults") or {})
        assert self._BLOCKED_HEADER in body
        assert self._READY_HEADER not in body

    def test_critical_only_estate_never_writes_an_empty_defaults_mapping(self):
        """⛔ 空的 `defaults:` 必須**省略**，不能 dump 成 `defaults: {}`（#1218 盲審）。

        寫檔的守衛從 `if defaults:` 放寬成 `if suggestion:`，所以只有 critical
        severity 的 estate 現在會走到寫檔。而這個檔的標頭寫的是「Review and merge
        into conf.d/_defaults.yaml」——把一個字面的 `defaults: {}` 併到已經有內容的
        `_defaults.yaml` 上，等於清空平台整個 base tier；同一個編輯在「鍵不存在」時
        則無害。

        走真的 `_write_phase2_outputs`，不是重寫一次它的組字串邏輯。
        """
        from pathlib import Path

        import onboard_platform as op

        candidates = [
            {"status": "perfect", "metric_key": "disk_usage",
             "severity": "critical", "threshold_value": "95"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            report = {"files_written": [], "phases": {}}
            op._write_phase2_outputs(tmpdir, (candidates, [], {}), report)

            written = [p for p in report["files_written"]
                       if p.endswith("_defaults-suggestion.yaml")]
            assert written, report["files_written"]
            text = Path(written[0]).read_text(encoding="utf-8")

        parsed = yaml.safe_load(text)
        # 沒有 warning 層可建議時，`defaults:` 這個鍵根本不該存在
        assert parsed is None or "defaults" not in parsed, text
        assert "defaults: {}" not in text, text
        # …但撈回來的數字仍然在檔案裡，那是跑 onboard 的唯一理由
        assert 'disk_usage_critical: "95"' in text, text

    def test_no_critical_block_when_there_is_no_critical_tier(self):
        """空的一層不出標題——避免指向一個沒有內容的區段。"""
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
        ]
        suggestion = generate_defaults_from_candidates(candidates)
        assert "critical_overrides" not in suggestion
        assert _render_critical_suggestion(suggestion.get("critical_overrides", {})) == ""

    def test_the_customer_facing_text_never_spells_the_731_shape(self):
        """⛔ 這一支守的是「客戶會讀到的字」，不是 gate 的字。

        `check_threshold_reachability` 那邊已有一支守衛在防 #731 的 metric-label
        拼法，但它只看 gate 自己吐的訊息。這個檔案的文字有兩個出口，兩個都直接進
        客戶的視野：`_render_critical_suggestion` 寫進客戶 `_defaults.yaml` 的註解，
        以及本模組解釋 `_critical` 為何不能放 `defaults:` 的說明文字。

        ⛔ 實測（第五輪變異）：把這兩處改回 `metric="<整個 key>"`，`tests/ops/` 全綠
        ——修了措辭卻沒配斷言，這個 repo 第四次踩。`parseMetricKey` 切**第一個**底線，
        所以 `<base>_critical` 的 `_critical` 落在 component/metric label 裡而不會變成
        severity；把它寫成 `metric="<整個 key>"` 是在指一個不存在的 series。
        """
        import pathlib
        import re as _re

        import onboard_platform as _mod

        rendered = _render_critical_suggestion({"pg_connections_critical": "500"})
        assert rendered, "沒有輸出就沒有守到東西"

        source = pathlib.Path(_mod.__file__).read_text(encoding="utf-8")

        # ⛔ 兩個出口的規則不同，因為它們的讀者不同。第一版守衛在這裡犯過錯：
        # 用 `metric="[^"]*_critical[^"]*"` 去抓，只釘得住「label 裡含 _critical」
        # 這一種拼法，而變異寫成 `metric="<whole key>"` 就整個穿過去。釘拼法只會
        # 得到一份含那幾種拼法的 denylist。

        # 出口一：寫進客戶 `_defaults.yaml` 的註解。客戶檔不是教材，裡面的
        # `metric="…"` 一定是具體 label 值，出現佔位符就是把「切分」這件事糊掉。
        placeholder = _re.search(r'metric="[^"]*<[^"]*"', rendered)
        assert not placeholder, (
            f"寫進客戶檔的註解出現佔位符 {placeholder.group(0)!r}；"
            "客戶讀到的必須是具體 label 值"
        )

        # 而且註解給的對應必須自洽：`<key> -> {component="A", metric="B"}` 裡
        # `A_B` 必須就是 `<key>`。⛔ 這不是在 Python 重實作 parseMetricKey
        # （`rulepack_contract_test.go` 明文警告那會變成新的 echo chamber），
        # 是驗這個示例自己算得通——`metric="<whole key>"` 這類寫法算不通。
        example = _re.search(
            r'(\w+) -> \{component="([^"]*)", metric="([^"]*)"\}', rendered)
        assert example, f"註解不再給具體對應，讀者無從驗證：{rendered}"
        key, comp, met = example.groups()
        assert f"{comp}_{met}" == key, (
            f"註解裡的示例算不通：{key} 被說成 component={comp!r} metric={met!r}，"
            f"但 {comp}_{met} != {key}"
        )

        # 出口二：模組自己的說明文字。這裡**允許**寫出錯誤拼法，前提是明講它是
        # 錯的——樹上就有一句「Spelling it `metric="<whole key>"` names a series
        # that does not exist」。規則因此是「反例必須被標記為反例」，而不是
        # 「不准出現」。
        for i, line in enumerate(source.splitlines()):
            if not _re.search(r'metric="[^"]*<', line):
                continue
            window = " ".join(source.splitlines()[max(0, i - 1): i + 2])
            # ⛔ Explicit negations ONLY. The first version also accepted `⛔`
            # and `never`, which appear 8 and 7 times in this module — including
            # inside `_render_critical_suggestion`'s own docstring — so a future
            # placeholder passed as soon as it landed within one line of any
            # unrelated prose carrying either. The docstring claimed the rule was
            # "反例必須被標記為反例"; the marker set did not require a statement
            # that the spelling is WRONG. (CodeRabbit, PR #1410)
            # ⚠️ Honest limit: "is this marked as a counterexample" is a semantic
            # question, so this stays a denylist of negation phrasings. The hard,
            # derivable rule is the one above — the RENDERED output (what the
            # customer reads) may not contain a placeholder at all, and its
            # example must be arithmetically self-consistent.
            assert _re.search(r"does not exist|not a series|no such series", window), (
                f"第 {i + 1} 行寫出了 metric label 的佔位符拼法卻沒有標記為反例："
                f"{line.strip()!r} — 讀者會把它當成實際行為"
            )

        # 正面：註解必須點出「切第一個底線」，否則讀者無從判斷
        assert "first" in rendered.lower() or "第一個" in rendered, rendered

    def test_most_common_value(self):
        """最常見值作為預設值。"""
        candidates = [
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "100"},
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "100"},
            {"status": "perfect", "metric_key": "conn", "severity": "warning", "threshold_value": "200"},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        assert defaults["defaults"]["conn"] == "100"

    def test_skip_unparseable(self):
        """跳過無法解析的候選值。"""
        candidates = [
            {"status": "unparseable", "metric_key": None, "severity": "warning", "threshold_value": None},
        ]
        assert generate_defaults_from_candidates(candidates) == {}


class TestWriteMigrationCsv:
    """產生遷移計畫 CSV 檔案。"""

    def test_csv_output(self, config_dir):
        """CSV 輸出正確產生。"""
        candidates = [{
            "alert_name": "HighCPU", "file": "rules.yaml", "group": "test",
            "status": "perfect", "severity": "warning", "metric_key": "cpu",
            "threshold_value": "80", "operator": ">", "aggregation": "max",
            "agg_reason": "test", "metric_group": None, "dict_match": None,
        }]
        path = os.path.join(config_dir, "plan.csv")
        write_migration_csv(candidates, path)
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]["alert_name"] == "HighCPU"


# ============================================================
# Phase 3: Scrape Config Analysis
# ============================================================

class TestParseScrapeConfigs:
    """解析 Prometheus 抓取設定。"""

    def test_raw_prometheus_yaml(self, config_dir):
        """Prometheus 原始 YAML 正確解析。"""
        path = write_yaml(config_dir, "prom.yaml", yaml.dump({
            "scrape_configs": [
                {"job_name": "node", "static_configs": [{"targets": ["localhost:9100"]}]},
            ],
        }))
        result = parse_scrape_configs(path)
        assert len(result) == 1
        assert result[0]["job_name"] == "node"

    def test_configmap_wrapped(self, config_dir):
        """ConfigMap 包裝的抓取設定正確解析。"""
        inner = yaml.dump({"scrape_configs": [{"job_name": "kubelet"}]})
        path = write_yaml(config_dir, "cm.yaml", yaml.dump({
            "data": {"prometheus.yml": inner},
        }))
        result = parse_scrape_configs(path)
        assert len(result) == 1

    def test_empty_file(self, config_dir):
        """空抓取設定檔案回傳空列表。"""
        path = write_yaml(config_dir, "empty.yaml", "")
        assert parse_scrape_configs(path) == []


class TestAnalyzeRelabelConfigs:
    """分析標籤重標記設定。"""

    def test_namespace_mapping(self):
        """命名空間租戶映射偵測。"""
        sc = {
            "job_name": "pod-monitor",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
            ],
        }
        result = analyze_relabel_configs(sc)
        assert result["has_tenant_mapping"] is True
        assert result["mapping_type"] == "namespace"

    def test_service_label_mapping(self):
        """服務標籤租戶映射偵測。"""
        sc = {
            "job_name": "svc-monitor",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_service_label_tenant"],
                 "target_label": "tenant"},
            ],
        }
        result = analyze_relabel_configs(sc)
        assert result["has_tenant_mapping"] is True
        assert result["mapping_type"] == "service_label"

    def test_no_tenant_mapping(self):
        """無租戶映射時提供建議。"""
        sc = {
            "job_name": "basic",
            "relabel_configs": [
                {"source_labels": ["__address__"], "target_label": "__param_target"},
            ],
        }
        result = analyze_relabel_configs(sc)
        assert result["has_tenant_mapping"] is False
        assert len(result["suggestions"]) > 0

    def test_custom_tenant_label(self):
        """自訂租戶標籤名稱的映射偵測。"""
        sc = {
            "job_name": "custom",
            "relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "cluster"},
            ],
        }
        assert analyze_relabel_configs(sc, tenant_label="cluster")["has_tenant_mapping"] is True

    def test_metric_relabel_configs(self):
        """度量重標記設定中的租戶映射偵測。"""
        sc = {
            "job_name": "exporter",
            "metric_relabel_configs": [
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
            ],
        }
        assert analyze_relabel_configs(sc)["has_tenant_mapping"] is True


class TestAnalyzeScrapeConfigs:
    """分析所有抓取設定。"""

    def test_mixed_jobs(self):
        """混合有無租戶映射的工作分析。"""
        scrape_configs = [
            {"job_name": "with-tenant",
             "relabel_configs": [
                 {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "tenant"},
             ]},
            {"job_name": "without-tenant", "relabel_configs": []},
        ]
        _, summary = analyze_scrape_configs(scrape_configs)
        assert summary["total_jobs"] == 2
        assert summary["with_tenant_mapping"] == 1
        assert summary["without_tenant_mapping"] == 1


# ============================================================
# Integration: Output Generation
# ============================================================

class TestScanRuleFiles:
    """掃描規則檔案。"""

    def test_glob_pattern(self, config_dir):
        """Glob 模式掃描規則檔案。"""
        write_yaml(config_dir, "a.yaml", "groups: []")
        write_yaml(config_dir, "b.yml", "groups: []")
        write_yaml(config_dir, "c.txt", "not yaml")
        assert len(scan_rule_files(os.path.join(config_dir, "*.yaml"))) == 1
        assert len(scan_rule_files(os.path.join(config_dir, "*.yml"))) == 1


class TestWriteOutputs:
    """產生輸出檔案與報告。"""

    def test_dry_run(self):
        """乾運行模式不產生實際檔案。"""
        report = write_outputs(
            "/tmp/test",
            phase1_results=({"db-a": {"receiver": {"type": "webhook"}}},
                            {"tenant_routes": 1, "total_routes": 1,
                             "skipped_routes": [], "dedup_tenants": {}, "warnings": []}),
            dry_run=True,
        )
        assert "phase1" in report["phases"]
        assert len(report["files_written"]) == 0

    def test_phase1_file_output(self, config_dir):
        """Phase 1 輸出檔案正確產生。"""
        report = write_outputs(
            config_dir,
            phase1_results=(
                {"db-a": {"receiver": {"type": "webhook", "url": "https://a.com"}}},
                {"tenant_routes": 1, "total_routes": 1, "skipped_routes": [],
                 "dedup_tenants": {}, "warnings": []},
            ),
        )
        assert len(report["files_written"]) > 0
        assert os.path.exists(os.path.join(config_dir, "phase1-routing", "db-a.yaml"))

    def test_phase2_file_output(self, config_dir):
        """Phase 2 輸出檔案正確產生。"""
        candidates = [
            {"alert_name": "Test", "file": "r.yaml", "group": "g",
             "status": "perfect", "severity": "warning", "metric_key": "cpu",
             "threshold_value": "80", "operator": ">", "aggregation": "max",
             "agg_reason": "test", "metric_group": None, "dict_match": None},
        ]
        report = write_outputs(
            config_dir,
            phase2_results=(candidates, [],
                            {"files_scanned": 1, "total_groups": 1, "total_rules": 1,
                             "alert_rules": 1, "recording_rules": 0, "parseable": 1,
                             "unparseable": 0, "errors": []}),
        )
        assert os.path.exists(os.path.join(config_dir, "phase2-rules", "migration-plan.csv"))

    def test_no_results_empty_report(self):
        """無結果時產生空白報告。"""
        report = write_outputs("/tmp/test", dry_run=True)
        assert report["phases"] == {}
