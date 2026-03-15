#!/usr/bin/env python3
"""test_snapshot.py — 輸出格式穩定性快照測試。

驗證 generate_routes() / generate_inhibit_rules() 產出的資料結構
不會因重構而意外改變：
  1. 單一 tenant route 結構快照
  2. 多 tenant route 排序穩定性
  3. Inhibit rule 結構快照
  4. Enforced routing 結構快照
  5. Per-rule override 結構快照
  6. render_output() YAML 格式穩定性
"""

import json
import os

import pytest
import yaml

pytestmark = pytest.mark.snapshot

from factories import make_receiver, make_routing_config, make_override, make_enforced_routing

from generate_alertmanager_routes import (
    generate_routes,
    generate_inhibit_rules,
    render_output,
)

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


# ── Helpers ───────────────────────────────────────────────────


def _load_snapshot(name):
    """從 snapshots/ 目錄載入 JSON 快照。"""
    path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_snapshot(name, data):
    """將資料寫入 snapshots/ 目錄作為 JSON 快照。"""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
    os.chmod(path, 0o600)


def _snapshot_exists(name):
    """檢查快照檔案是否存在。"""
    return os.path.isfile(os.path.join(SNAPSHOT_DIR, f"{name}.json"))


def _format_diff(actual, expected):
    """產生結構化 diff 報告。優先使用 deepdiff，fallback 到 JSON 截斷比對。"""
    try:
        from deepdiff import DeepDiff
        diff = DeepDiff(expected, actual, ignore_order=False, verbose_level=2)
        if diff:
            return diff.to_json(indent=2)[:800]
    except ImportError:
        pass
    return (
        f"  actual:   {json.dumps(actual, indent=2, sort_keys=True)[:400]}\n"
        f"  expected: {json.dumps(expected, indent=2, sort_keys=True)[:400]}"
    )


def _assert_snapshot(name, actual):
    """比較實際輸出與快照。首次執行自動建立快照。

    設定 UPDATE_SNAPSHOTS=1 環境變數可強制更新快照。
    使用 deepdiff（如已安裝）產生結構化 diff 報告。
    """
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not _snapshot_exists(name):
        _save_snapshot(name, actual)
        return  # 首次建立或強制更新，不比較

    expected = _load_snapshot(name)
    assert actual == expected, (
        f"快照 '{name}' 不符。\n"
        f"若為預期變更，執行: UPDATE_SNAPSHOTS=1 pytest tests/test_snapshot.py\n"
        f"差異:\n{_format_diff(actual, expected)}"
    )


# ── Single Tenant Route Snapshot ──────────────────────────────


class TestSingleTenantRouteSnapshot:
    """單一 tenant 產出的 route/receiver 結構快照。"""

    def test_webhook_route_structure(self):
        """Webhook tenant route 結構快照。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
                "group_wait": "30s",
                "repeat_interval": "4h",
            }
        }
        routes, receivers, warnings = generate_routes(routing_configs)

        snapshot = {
            "routes": routes,
            "receivers": receivers,
        }
        _assert_snapshot("single_tenant_webhook_route", snapshot)

    def test_slack_route_structure(self):
        """Slack tenant route 結構快照。"""
        routing_configs = {
            "db-b": {
                "receiver": {
                    "type": "slack",
                    "api_url": "https://hooks.slack.com/services/T/B/X",
                    "channel": "#db-alerts",
                },
            }
        }
        routes, receivers, warnings = generate_routes(routing_configs)

        snapshot = {
            "routes": routes,
            "receivers": receivers,
        }
        _assert_snapshot("single_tenant_slack_route", snapshot)

    def test_email_route_structure(self):
        """Email tenant route 結構快照。"""
        routing_configs = {
            "db-c": {
                "receiver": {
                    "type": "email",
                    "to": "dba@example.com",
                    "smarthost": "smtp.example.com:587",
                },
            }
        }
        routes, receivers, warnings = generate_routes(routing_configs)

        snapshot = {
            "routes": routes,
            "receivers": receivers,
        }
        _assert_snapshot("single_tenant_email_route", snapshot)


# ── Multi Tenant Ordering Snapshot ────────────────────────────


class TestMultiTenantOrderingSnapshot:
    """多 tenant 排序穩定性快照。"""

    def test_alphabetical_ordering(self):
        """多 tenant routes 依字母排序。"""
        routing_configs = {
            "db-c": make_routing_config("db-c"),
            "db-a": make_routing_config("db-a"),
            "db-b": make_routing_config("db-b"),
        }
        routes, receivers, _ = generate_routes(routing_configs)

        tenant_order = [r["receiver"].replace("tenant-", "") for r in routes]
        _assert_snapshot("multi_tenant_ordering", {
            "tenant_order": tenant_order,
            "routes_count": len(routes),
            "receivers_count": len(receivers),
        })


# ── Inhibit Rule Snapshot ─────────────────────────────────────


class TestInhibitRuleSnapshot:
    """Inhibit rule 結構快照。"""

    def test_single_tenant_inhibit(self):
        """單一 tenant inhibit rule 結構。"""
        dedup_configs = {"db-a": "enable"}
        rules, warnings = generate_inhibit_rules(dedup_configs)

        _assert_snapshot("single_tenant_inhibit", {
            "rules": rules,
            "warnings": warnings,
        })

    def test_multi_tenant_inhibit_with_disable(self):
        """多 tenant（含 disable）inhibit rules。"""
        dedup_configs = {
            "db-a": "enable",
            "db-b": "disable",
            "db-c": "enable",
        }
        rules, warnings = generate_inhibit_rules(dedup_configs)

        _assert_snapshot("multi_tenant_inhibit_mixed", {
            "rules": rules,
            "warning_count": len(warnings),
            "enabled_tenants": [r["source_matchers"][2] for r in rules],
        })


# ── Enforced Routing Snapshot ─────────────────────────────────


class TestEnforcedRoutingSnapshot:
    """Platform enforced routing 結構快照。"""

    def test_enforced_webhook_structure(self):
        """Enforced webhook route 結構。"""
        routing_configs = {
            "db-a": make_routing_config("db-a"),
        }
        enforced = make_enforced_routing()
        routes, receivers, _ = generate_routes(
            routing_configs, enforced_routing=enforced)

        # 取出 enforced routes（continue: true）
        enforced_routes = [r for r in routes if r.get("continue")]
        _assert_snapshot("enforced_webhook_route", {
            "enforced_routes": enforced_routes,
            "total_routes": len(routes),
            "total_receivers": len(receivers),
        })

    def test_enforced_per_tenant_placeholder(self):
        """Enforced per-tenant {{tenant}} 佔位符展開。"""
        routing_configs = {
            "db-a": make_routing_config("db-a"),
            "db-b": make_routing_config("db-b"),
        }
        enforced = make_enforced_routing(per_tenant=True)
        routes, receivers, _ = generate_routes(
            routing_configs, enforced_routing=enforced)

        enforced_routes = [r for r in routes if r.get("continue")]
        enforced_receiver_names = [r["receiver"] for r in enforced_routes]
        _assert_snapshot("enforced_per_tenant_placeholder", {
            "enforced_receiver_names": enforced_receiver_names,
            "enforced_routes_count": len(enforced_routes),
        })


# ── Render Output YAML Snapshot ───────────────────────────────


class TestRenderOutputSnapshot:
    """render_output() YAML 格式穩定性。"""

    def test_complete_output_structure(self):
        """完整 render_output 結構（routes + receivers + inhibit）。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
                "group_wait": "30s",
            }
        }
        dedup_configs = {"db-a": "enable"}

        routes, receivers, _ = generate_routes(routing_configs)
        inhibit, _ = generate_inhibit_rules(dedup_configs)
        output = render_output(routes, receivers, inhibit)

        # 驗證 YAML 可解析
        parsed = yaml.safe_load(output)
        assert parsed is not None

        # 快照驗證結構 keys
        _assert_snapshot("render_output_structure", {
            "top_level_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else [],
            "yaml_length": len(output),
            "routes_count": len(routes),
            "receivers_count": len(receivers),
            "inhibit_count": len(inhibit),
        })


# ── Route Key Completeness ────────────────────────────────────


class TestRouteKeyCompleteness:
    """Route dict 必備 key 穩定性。"""

    def test_route_required_keys(self):
        """每個 route dict 包含必備 keys。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://example.com/hook"},
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            }
        }
        routes, _, _ = generate_routes(routing_configs)
        assert len(routes) >= 1

        route = routes[0]
        required_keys = {"receiver", "matchers"}
        assert required_keys.issubset(set(route.keys())), \
            f"Route 缺少必備 keys: {required_keys - set(route.keys())}"

    def test_receiver_required_keys(self):
        """每個 receiver dict 包含必備 keys。"""
        routing_configs = {
            "db-a": make_routing_config("db-a"),
        }
        _, receivers, _ = generate_routes(routing_configs)
        assert len(receivers) >= 1

        receiver = receivers[0]
        assert "name" in receiver, "Receiver 缺少 'name' key"


# ── Negative / Edge Case Snapshots ────────────────────────────


class TestEmptyInputSnapshot:
    """空輸入的輸出結構快照。"""

    def test_empty_routing_configs(self):
        """空 routing_configs 產出空 routes。"""
        routes, receivers, warnings = generate_routes({})
        _assert_snapshot("empty_routing_configs", {
            "routes": routes,
            "receivers": receivers,
            "warnings": warnings,
        })

    def test_empty_dedup_configs(self):
        """空 dedup_configs 產出空 inhibit rules。"""
        rules, warnings = generate_inhibit_rules({})
        _assert_snapshot("empty_dedup_configs", {
            "rules": rules,
            "warnings": warnings,
        })

    def test_render_output_empty(self):
        """空 routes + receivers 的 render_output。"""
        output = render_output([], [])
        _assert_snapshot("render_output_empty", {
            "output_length": len(output),
            "is_empty_or_whitespace": output.strip() == "" or output.strip() == "{}",
        })


class TestAllDisabledDedupSnapshot:
    """全部 disable 時的 inhibit rules 快照。"""

    def test_all_tenants_disabled(self):
        """所有 tenant disable dedup → 0 rules + N warnings。"""
        dedup_configs = {
            "db-a": "disable",
            "db-b": "disable",
        }
        rules, warnings = generate_inhibit_rules(dedup_configs)
        _assert_snapshot("all_disabled_dedup", {
            "rules_count": len(rules),
            "warning_count": len(warnings),
            "all_info": all("INFO" in w for w in warnings),
        })


class TestTimingEdgeCaseSnapshot:
    """Timing 參數邊界值的 route 結構快照。"""

    def test_all_timing_fields(self):
        """完整 timing 參數的 route 結構。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
                "group_wait": "5s",
                "group_interval": "5s",
                "repeat_interval": "1m",
            }
        }
        routes, _, _ = generate_routes(routing_configs)
        assert len(routes) >= 1
        route = routes[0]
        _assert_snapshot("timing_edge_case_route", {
            "has_group_wait": "group_wait" in route,
            "has_group_interval": "group_interval" in route,
            "has_repeat_interval": "repeat_interval" in route,
            "route_keys": sorted(route.keys()),
        })


# ── Scaffold Tenant Output Snapshots ────────────────────────


class TestScaffoldOutputSnapshot:
    """scaffold_tenant.py 核心輸出結構快照。"""

    def test_generate_defaults_structure(self):
        """generate_defaults() 產出結構穩定性。"""
        from scaffold_tenant import generate_defaults, RULE_PACKS
        result = generate_defaults(RULE_PACKS)
        # result 可能是 dict 或 YAML 字串
        if isinstance(result, str):
            parsed = yaml.safe_load(result)
        else:
            parsed = result
        _assert_snapshot("scaffold_generate_defaults", {
            "top_level_keys": sorted(parsed.keys()),
            "defaults_key_count": len(parsed.get("defaults", {})),
            "has_defaults": "defaults" in parsed,
        })

    def test_generate_tenant_structure(self):
        """generate_tenant() 產出結構穩定性。"""
        from scaffold_tenant import generate_tenant
        result = generate_tenant("db-snapshot", ["mysql"])
        # result 可能是 dict 或 YAML 字串
        if isinstance(result, str):
            parsed = yaml.safe_load(result)
        else:
            parsed = result
        tenant_data = parsed.get("tenants", {}).get("db-snapshot", {})
        _assert_snapshot("scaffold_generate_tenant", {
            "top_level_keys": sorted(parsed.keys()),
            "has_tenants": "tenants" in parsed,
            "tenant_key_count": len(tenant_data),
            "has_metric_keys": len(tenant_data) > 0,
        })
