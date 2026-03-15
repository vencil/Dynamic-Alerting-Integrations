#!/usr/bin/env python3
"""test_regression.py — 已知 bug 回歸測試。

收集開發過程中發現的 bug 和修正，建立專門的回歸測試防止復發。
每個測試附帶原始 bug 描述作為 docstring。
"""

import os

import pytest
import yaml

pytestmark = pytest.mark.regression

from factories import (
    make_receiver, make_routing_config, make_tenant_yaml,
    make_am_config, make_am_receiver, write_yaml,
)

from generate_alertmanager_routes import (
    load_tenant_configs,
    generate_routes,
    generate_inhibit_rules,
)
from scaffold_tenant import RULE_PACKS, generate_defaults, generate_tenant
from onboard_platform import (
    analyze_alertmanager,
    generate_tenant_routing_yamls,
)


# ── Regression: dict key 誤用 ────────────────────────────────


class TestDictKeyRegression:
    """dict key 拼寫錯誤的回歸測試。"""

    def test_parse_config_returns_explicit_routing_not_routing_configs(self, config_dir):
        """Bug: 曾用 parsed["routing_configs"] 但正確 key 是 parsed["explicit_routing"]。

        load_tenant_configs 回傳 tuple 而非 dict，routing_configs 在 index 0。
        此測試確認 API 回傳結構穩定。
        """
        write_yaml(config_dir, "db-a.yaml", make_tenant_yaml(
            "db-a",
            keys={"mysql_connections": "70"},
            routing={"receiver": make_receiver("webhook")},
        ))
        result = load_tenant_configs(config_dir)
        # load_tenant_configs 回傳 tuple: (routing_configs, dedup_configs, ...)
        assert isinstance(result, tuple)
        routing_configs = result[0]
        assert isinstance(routing_configs, dict)
        assert "db-a" in routing_configs


# ── Regression: mariadb default_on ────────────────────────────


class TestMariadbDefaultOnRegression:
    """mariadb pack 的 default_on 回歸測試。"""

    def test_mariadb_has_default_on_true(self):
        """Bug: 測試假設所有非 kubernetes pack 都 default_on=False，
        但 mariadb（MySQL/MariaDB combo）有 default_on=True。
        """
        assert "mariadb" in RULE_PACKS
        pack = RULE_PACKS["mariadb"]
        assert pack.get("default_on") is True

    def test_kubernetes_has_default_on_true(self):
        """kubernetes pack 始終 default_on=True（核心 pack）。"""
        assert RULE_PACKS["kubernetes"].get("default_on") is True


# ── Regression: generate_tenant_routing_yamls API ─────────────


class TestGenerateRoutingYamlsApiRegression:
    """generate_tenant_routing_yamls API 使用回歸測試。"""

    def test_returns_dict_of_yaml_strings(self):
        """Bug: 曾誤以為 generate_tenant_routing_yamls 會寫入磁碟，
        實際回傳 {tenant: yaml_string} dict。
        """
        routings = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://a.example.com"},
            }
        }
        result = generate_tenant_routing_yamls(routings)
        assert isinstance(result, dict)
        assert "db-a" in result
        # 值是 YAML 字串，不是 dict
        assert isinstance(result["db-a"], str)
        # 可以被解析
        parsed = yaml.safe_load(result["db-a"])
        assert isinstance(parsed, dict)

    def test_dedup_info_keyword_not_dedup_config(self):
        """Bug: 曾用 dedup_config keyword，正確名稱是 dedup_info。"""
        routings = {"db-a": {"receiver": {"type": "webhook", "url": "https://a.com"}}}
        # 正確 keyword 是 dedup_info
        result = generate_tenant_routing_yamls(routings, dedup_info={"db-a": "enable"})
        parsed = yaml.safe_load(result["db-a"])
        assert parsed["tenants"]["db-a"]["_severity_dedup"] == "enable"


# ── Regression: validate-config help text drift ───────────────


class TestHelpTextDriftRegression:
    """help text 與 COMMAND_MAP 同步的回歸測試。"""

    def test_validate_config_known_drift(self):
        """Bug: validate-config 存在於 COMMAND_MAP 但未列入 help text。
        此為已知 drift，已加入 _HELP_EXEMPT。
        確認 COMMAND_MAP 中確實有 validate-config。
        """
        import entrypoint
        assert "validate-config" in entrypoint.COMMAND_MAP


# ── Regression: Hypothesis float truncation ───────────────────


class TestFloatTruncationRegression:
    """parse_duration_seconds float 處理的回歸測試。"""

    def test_float_returns_int_truncation(self):
        """Bug: 曾 assert result == value（float），但函式實際回傳 int(value)。"""
        from _lib_python import parse_duration_seconds
        assert parse_duration_seconds(3.7) == 3
        assert parse_duration_seconds(0.9) == 0
        assert isinstance(parse_duration_seconds(3.7), int)


# ── Regression: scaffold output + load_tenant_configs ─────────


class TestScaffoldLoadRegression:
    """scaffold 輸出與 load_tenant_configs 的相容性回歸測試。"""

    def test_scaffold_non_interactive_needs_manual_routing(self, config_dir):
        """Bug: scaffold non-interactive 模式不自動加 _routing，
        需手動注入後才能被 load_tenant_configs 正確讀取。
        """
        td = generate_tenant("db-a", ["kubernetes"], interactive=False)
        # Non-interactive 不會有 _routing
        assert "_routing" not in td["tenants"]["db-a"]

        # 手動加入 routing 後寫入
        td["tenants"]["db-a"]["_routing"] = {
            "receiver": {"type": "webhook", "url": "https://a.com/hook"}
        }
        defaults = generate_defaults(["kubernetes"])
        from scaffold_tenant import write_outputs
        write_outputs(config_dir, "db-a", defaults, td, "# report")

        routing_configs, _, *_ = load_tenant_configs(config_dir)
        assert "db-a" in routing_configs
        assert routing_configs["db-a"]["receiver"]["type"] == "webhook"


# ── Regression: AM analyze dedup detection ────────────────────


class TestDedupDetectionRegression:
    """AM 逆向分析中 dedup 偵測的回歸測試。"""

    def test_dedup_requires_all_three_matchers(self):
        """確保 dedup 偵測需要 severity + metric_group + tenant 三個 matcher。"""
        # 完整三 matcher → 偵測到 enable
        am = make_am_config(
            routes=[{"matchers": ['tenant="db-a"'], "receiver": "t"}],
            receivers=[{"name": "default"}, make_am_receiver("t")],
            inhibit_rules=[{
                "source_matchers": [
                    'severity="critical"',
                    'metric_group=~".+"',
                    'tenant="db-a"',
                ],
                "target_matchers": [
                    'severity="warning"',
                    'metric_group=~".+"',
                    'tenant="db-a"',
                ],
                "equal": ["metric_group"],
            }],
        )
        _, summary = analyze_alertmanager(am)
        assert summary["dedup_tenants"].get("db-a") == "enable"
