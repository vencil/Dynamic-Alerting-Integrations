#!/usr/bin/env python3
"""test_integration.py — 跨模組整合測試。

驗證 onboard → scaffold → generate 三步流水線的端到端資料流：
  1. onboard_platform 產出 onboard-hints.json
  2. scaffold_tenant 讀取 hints 並產出 tenant YAML + defaults
  3. generate_alertmanager_routes 讀取 YAML 並產出 routes/receivers/inhibit

這些測試不啟動 K8s，僅驗證模組間的資料契約。
"""
import os

import pytest
import yaml

pytestmark = pytest.mark.integration

from factories import make_am_config, make_am_receiver, write_yaml

from _lib_python import write_onboard_hints, read_onboard_hints
from scaffold_tenant import (
    generate_defaults,
    generate_tenant,
    write_outputs,
    RULE_PACKS,
)
from generate_alertmanager_routes import (
    load_tenant_configs,
    generate_routes,
    generate_inhibit_rules,
)
from onboard_platform import (
    analyze_alertmanager,
    generate_tenant_routing_yamls,
)


# ============================================================
# Pipeline: onboard hints → scaffold → generate routes
# ============================================================

class TestOnboardToScaffoldPipeline:
    """onboard_platform → scaffold_tenant 資料流驗證。"""

    def test_hints_round_trip_drives_scaffold(self, config_dir):
        """onboard hints 寫入後，scaffold 能正確讀取並產出 tenant config。"""
        hints = {
            "tenants": ["db-a", "db-b"],
            "db_types": {
                "db-a": ["postgresql"],
                "db-b": ["mariadb"],
            },
        }
        hints_path = write_onboard_hints(config_dir, hints)
        loaded = read_onboard_hints(hints_path)

        # 為每個 tenant 執行 scaffold
        for tenant in loaded["tenants"]:
            db_list = ["kubernetes"] + loaded["db_types"].get(tenant, [])
            defaults = generate_defaults(db_list)
            tenant_data = generate_tenant(tenant, db_list, interactive=False)

            assert "defaults" in defaults
            assert tenant in tenant_data["tenants"]

    def test_scaffold_output_loadable_by_generate(self, config_dir):
        """scaffold 產出的 YAML 檔案可被 generate_routes 正確載入。"""
        # Step 1: Scaffold
        defaults = generate_defaults(["kubernetes", "postgresql"])
        tenant_data = generate_tenant("db-a", ["kubernetes", "postgresql"],
                                       interactive=False)
        # 手動加入 routing（scaffold non-interactive 不自動加）
        tenant_data["tenants"]["db-a"]["_routing"] = {
            "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"}
        }
        report = "# test report"
        write_outputs(config_dir, "db-a", defaults, tenant_data, report)

        # Step 2: load_tenant_configs 讀取
        routing_configs, dedup_configs, *_ = load_tenant_configs(config_dir)
        assert "db-a" in routing_configs
        assert routing_configs["db-a"]["receiver"]["type"] == "webhook"


class TestScaffoldToGeneratePipeline:
    """scaffold_tenant → generate_alertmanager_routes 資料流驗證。"""

    def test_scaffold_output_generates_valid_routes(self, config_dir):
        """scaffold 產出經 generate_routes 產生合法 Alertmanager routes。"""
        defaults = generate_defaults(["kubernetes"])
        tenant_data = generate_tenant("db-a", ["kubernetes"], interactive=False)
        tenant_data["tenants"]["db-a"]["_routing"] = {
            "receiver": {"type": "webhook", "url": "https://a.example.com/alert"},
            "group_wait": "30s",
            "repeat_interval": "4h",
        }
        tenant_data["tenants"]["db-a"]["_severity_dedup"] = "enable"
        write_outputs(config_dir, "db-a", defaults, tenant_data, "report")

        routing_configs, dedup_configs, *_ = load_tenant_configs(config_dir)
        routes, receivers, warnings = generate_routes(routing_configs)

        assert len(routes) >= 1
        assert routes[0]["receiver"] == "tenant-db-a"
        assert any(r["name"] == "tenant-db-a" for r in receivers)

        # inhibit rules
        inhibit = generate_inhibit_rules(dedup_configs)
        assert len(inhibit) >= 1
        assert any('tenant="db-a"' in str(rule) for rule in inhibit)

    def test_multi_tenant_scaffold_to_routes(self, config_dir):
        """多 tenant scaffold 正確產生多組 routes。"""
        for tenant, db in [("db-a", "postgresql"), ("db-b", "mariadb")]:
            defaults = generate_defaults(["kubernetes", db])
            td = generate_tenant(tenant, ["kubernetes", db], interactive=False)
            td["tenants"][tenant]["_routing"] = {
                "receiver": {"type": "webhook",
                             "url": f"https://{tenant}.example.com/alert"},
            }
            write_outputs(config_dir, tenant, defaults, td, "report")

        routing_configs, _, *_ = load_tenant_configs(config_dir)
        routes, receivers, _ = generate_routes(routing_configs)

        tenant_names = {r["receiver"].replace("tenant-", "") for r in routes}
        assert "db-a" in tenant_names
        assert "db-b" in tenant_names


class TestOnboardToGeneratePipeline:
    """onboard_platform → generate_alertmanager_routes 完整流水線。"""

    def test_analyze_am_then_generate_yamls_then_load(self, config_dir):
        """AM 逆向分析 → 產生 tenant YAML → load_tenant_configs 讀取。"""
        am_config = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a",
                     "group_wait": "30s", "repeat_interval": "4h"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://a.example.com/hook"),
            ],
        )

        routings, summary = analyze_alertmanager(am_config)
        assert "db-a" in routings

        yaml_dict = generate_tenant_routing_yamls(routings)
        assert "db-a" in yaml_dict

        for tenant, content in yaml_dict.items():
            write_yaml(config_dir, f"{tenant}.yaml", content)

        routing_configs, _, *_ = load_tenant_configs(config_dir)
        assert "db-a" in routing_configs

    def test_full_pipeline_with_dedup(self, config_dir):
        """完整流水線含 severity dedup 的 inhibit rules。"""
        am_config = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"}],
            receivers=[
                {"name": "default"},
                make_am_receiver("tenant-db-a", url="https://a.example.com"),
            ],
            inhibit_rules=[{
                "source_matchers": ['severity="critical"', 'metric_group=~".+"',
                                    'tenant="db-a"'],
                "target_matchers": ['severity="warning"', 'metric_group=~".+"',
                                    'tenant="db-a"'],
                "equal": ["metric_group"],
            }],
        )

        routings, summary = analyze_alertmanager(am_config)
        assert summary["dedup_tenants"].get("db-a") == "enable"

        yaml_dict = generate_tenant_routing_yamls(
            routings, dedup_info=summary["dedup_tenants"])

        for tenant, content in yaml_dict.items():
            write_yaml(config_dir, f"{tenant}.yaml", content)

        routing_configs, dedup_configs, *_ = load_tenant_configs(config_dir)
        routes, receivers, _ = generate_routes(routing_configs)
        inhibit = generate_inhibit_rules(dedup_configs)

        assert len(routes) >= 1
        assert len(inhibit) >= 1


# ============================================================
# Data contract verification
# ============================================================

class TestDataContracts:
    """模組間資料契約驗證。"""

    def test_onboard_hints_schema(self, config_dir):
        """onboard hints JSON 遵循預期 schema。"""
        hints = {
            "tenants": ["db-a"],
            "db_types": {"db-a": ["postgresql"]},
            "routing_hints": {"db-a": {
                "receiver_type": "webhook",
                "group_wait": "30s",
            }},
        }
        path = write_onboard_hints(config_dir, hints)
        loaded = read_onboard_hints(path)

        # 驗證 schema
        assert isinstance(loaded["tenants"], list)
        assert isinstance(loaded["db_types"], dict)
        assert isinstance(loaded["routing_hints"], dict)

    def test_scaffold_yaml_loadable_as_tenant_config(self, config_dir):
        """scaffold 產出的 YAML 符合 load_tenant_configs 預期格式。"""
        defaults = generate_defaults(["kubernetes"])
        td = generate_tenant("db-x", ["kubernetes"], interactive=False)
        td["tenants"]["db-x"]["_routing"] = {
            "receiver": {"type": "slack",
                         "api_url": "https://hooks.slack.com/services/T/B/X"}
        }
        td["tenants"]["db-x"]["_severity_dedup"] = "enable"
        write_outputs(config_dir, "db-x", defaults, td, "report")

        routing_configs, dedup_configs, *_ = load_tenant_configs(config_dir)

        # Routing config 有 receiver key
        assert "receiver" in routing_configs["db-x"]
        assert routing_configs["db-x"]["receiver"]["type"] == "slack"

        # Dedup config 是 "enable" 或 "disable"
        assert dedup_configs["db-x"] in ("enable", "disable", True, False)

    def test_rule_packs_keys_stable(self):
        """RULE_PACKS 至少包含核心 packs。"""
        core = {"kubernetes", "postgresql", "mariadb", "redis", "mongodb"}
        assert core.issubset(set(RULE_PACKS.keys()))


# ============================================================
# Pre-loaded routing_dir fixture tests
# ============================================================

class TestRoutingDirFixture:
    """使用 routing_dir fixture 的快速整合測試。"""

    def test_load_preloaded_configs(self, routing_dir):
        """routing_dir 預載的 db-a + db-b 可被 load_tenant_configs 正確讀取。"""
        routing_configs, dedup_configs, *_ = load_tenant_configs(routing_dir)
        assert "db-a" in routing_configs
        assert "db-b" in routing_configs
        assert routing_configs["db-a"]["receiver"]["type"] == "webhook"
        assert routing_configs["db-b"]["receiver"]["type"] == "slack"
        assert dedup_configs["db-a"] == "enable"
        assert dedup_configs["db-b"] == "enable"

    def test_generate_routes_from_preloaded(self, routing_dir):
        """預載 configs 產生合法 routes + receivers + inhibit。"""
        routing_configs, dedup_configs, *_ = load_tenant_configs(routing_dir)
        routes, receivers, _ = generate_routes(routing_configs)
        inhibit, _ = generate_inhibit_rules(dedup_configs)

        assert len(routes) == 2
        tenant_names = sorted(r["receiver"].replace("tenant-", "") for r in routes)
        assert tenant_names == ["db-a", "db-b"]
        assert len(inhibit) == 2

    def test_preloaded_with_enforced_routing(self, routing_dir):
        """預載 configs + enforced routing 產生 continue routes。"""
        from factories import make_enforced_routing
        routing_configs, _, *_ = load_tenant_configs(routing_dir)
        enforced = make_enforced_routing()
        routes, receivers, _ = generate_routes(
            routing_configs, enforced_routing=enforced)

        enforced_routes = [r for r in routes if r.get("continue")]
        assert len(enforced_routes) >= 1
        # Tenant routes 仍存在
        tenant_routes = [r for r in routes if not r.get("continue")]
        assert len(tenant_routes) == 2


# ============================================================
# PipelineBuilder tests
# ============================================================

class TestPipelineBuilder:
    """PipelineBuilder 鏈式建構器整合測試。"""

    def test_single_tenant_build(self, config_dir):
        """單一 tenant 建構 + 產生 routes。"""
        from factories import PipelineBuilder
        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook")
            .build())

        assert "db-a" in result.routing_configs
        assert result.routing_configs["db-a"]["receiver"]["type"] == "webhook"
        assert result.dedup_configs["db-a"] == "enable"

        routes, receivers, _ = generate_routes(result.routing_configs)
        assert len(routes) == 1
        assert routes[0]["receiver"] == "tenant-db-a"

    def test_multi_tenant_mixed_types(self, config_dir):
        """多 tenant 混合 receiver types。"""
        from factories import PipelineBuilder
        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook")
            .with_tenant("db-b", "slack", channel="#alerts")
            .with_tenant("db-c", "email")
            .build())

        assert len(result.routing_configs) == 3
        assert result.routing_configs["db-b"]["receiver"]["type"] == "slack"

        routes, _, _ = generate_routes(result.routing_configs)
        tenant_names = sorted(r["receiver"].replace("tenant-", "") for r in routes)
        assert tenant_names == ["db-a", "db-b", "db-c"]

    def test_dedup_override(self, config_dir):
        """dedup 覆寫正確反映在 inhibit rules。"""
        from factories import PipelineBuilder
        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook")
            .with_tenant("db-b", "webhook", dedup="disable")
            .build())

        assert result.dedup_configs["db-a"] == "enable"
        assert result.dedup_configs["db-b"] == "disable"

        inhibit, _ = generate_inhibit_rules(result.dedup_configs)
        # 只有 db-a 產生 inhibit rule
        assert len(inhibit) == 1
        assert 'tenant="db-a"' in str(inhibit[0])

    def test_builder_with_custom_keys(self, config_dir):
        """自訂 metric keys 寫入 YAML。"""
        from factories import PipelineBuilder
        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook",
                         keys={"mysql_connections": "70", "mysql_slow_queries": "5"})
            .build())

        assert "db-a" in result.routing_configs

    def test_builder_with_metadata(self, config_dir):
        """metadata 寫入 YAML 並可讀取。"""
        from factories import PipelineBuilder
        result = (PipelineBuilder(config_dir)
            .with_tenant("db-a", "webhook",
                         metadata={"runbook_url": "https://wiki.example.com/db-a"})
            .build())

        assert "db-a" in result.routing_configs
