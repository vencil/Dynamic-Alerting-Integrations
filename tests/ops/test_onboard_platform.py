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
        """基本預設值產生。"""
        candidates = [
            {"status": "perfect", "metric_key": "cpu", "severity": "warning", "threshold_value": "80"},
            {"status": "perfect", "metric_key": "cpu", "severity": "critical", "threshold_value": "95"},
        ]
        defaults = generate_defaults_from_candidates(candidates)
        assert "defaults" in defaults
        assert defaults["defaults"]["cpu"] == "80"
        assert defaults["defaults"]["cpu_critical"] == "95"

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
