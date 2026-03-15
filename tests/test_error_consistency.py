#!/usr/bin/env python3
"""test_error_consistency.py — 錯誤訊息格式一致性測試。

驗證各模組產出的 warning/error 訊息遵循統一格式：
  - warnings list 項目: ``"  WARN: {context}: {message}"``
  - INFO 項目: ``"  INFO: {context}: {message}"``
  - stderr ERROR: ``"ERROR: {message}"``

也驗證特定 error path 的訊息完整性。
"""

import re

import pytest

from factories import make_receiver, make_routing_config, make_am_config, make_am_receiver

import generate_alertmanager_routes as gar

generate_routes = gar.generate_routes
generate_inhibit_rules = gar.generate_inhibit_rules
# 內部函式用 getattr 安全存取
_build_enforced_routes = getattr(gar, "_build_enforced_routes", None)

from onboard_platform import (
    analyze_alertmanager,
    _check_timing_guardrails,
)

# ── Warning format patterns ──────────────────────────────────

# 標準格式：以 "  WARN: " 或 "  INFO: " 開頭
_WARN_RE = re.compile(r"^\s{2}(WARN|INFO): .+: .+$")


# ── generate_routes warnings ─────────────────────────────────


class TestGenerateRoutesWarningFormat:
    """generate_routes 產出的 warnings 格式一致性。"""

    def test_unknown_receiver_type_warning_format(self):
        """未知 receiver type 產生格式正確的 warning。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "unknown_type", "url": "https://x.com"},
            }
        }
        _, _, warnings = generate_routes(routing_configs)
        assert len(warnings) >= 1
        for w in warnings:
            assert _WARN_RE.match(w), f"格式不符: {w!r}"

    def test_missing_receiver_warning_format(self):
        """缺少 receiver 產生格式正確的 warning。"""
        routing_configs = {
            "db-a": {"group_wait": "30s"},  # 缺 receiver
        }
        _, _, warnings = generate_routes(routing_configs)
        assert len(warnings) >= 1
        for w in warnings:
            assert _WARN_RE.match(w), f"格式不符: {w!r}"

    def test_invalid_receiver_object_warning_format(self):
        """receiver 非 dict 產生格式正確的 warning。"""
        routing_configs = {
            "db-a": {"receiver": "not-a-dict"},
        }
        _, _, warnings = generate_routes(routing_configs)
        assert len(warnings) >= 1
        for w in warnings:
            assert _WARN_RE.match(w), f"格式不符: {w!r}"

    def test_missing_receiver_type_warning_format(self):
        """receiver 缺 type 產生格式正確的 warning。"""
        routing_configs = {
            "db-a": {"receiver": {"url": "https://x.com"}},
        }
        _, _, warnings = generate_routes(routing_configs)
        assert len(warnings) >= 1
        for w in warnings:
            assert _WARN_RE.match(w), f"格式不符: {w!r}"

    def test_invalid_overrides_list_warning_format(self):
        """overrides 非 list 產生格式正確的 warning。"""
        routing_configs = {
            "db-a": {
                "receiver": make_receiver("webhook"),
                "overrides": "not-a-list",
            }
        }
        _, _, warnings = generate_routes(routing_configs)
        override_warns = [w for w in warnings if "overrides" in w.lower()]
        assert len(override_warns) >= 1
        for w in override_warns:
            assert _WARN_RE.match(w), f"格式不符: {w!r}"


# ── generate_inhibit_rules warnings ──────────────────────────


class TestInhibitWarningFormat:
    """generate_inhibit_rules 的 INFO 訊息格式一致性。"""

    def test_disabled_dedup_info_format(self):
        """disable dedup 的 INFO 訊息格式正確。"""
        rules, warnings = generate_inhibit_rules({"db-a": "disable"})
        assert len(warnings) == 1
        assert _WARN_RE.match(warnings[0]), f"格式不符: {warnings[0]!r}"
        assert "INFO" in warnings[0]

    @pytest.mark.parametrize("tenant", ["db-a", "prod-cluster-1", "test_env"])
    def test_tenant_name_in_warning(self, tenant):
        """INFO 訊息包含 tenant 名稱。"""
        _, warnings = generate_inhibit_rules({tenant: "disable"})
        assert any(tenant in w for w in warnings), \
            f"Tenant '{tenant}' 未出現在 warnings 中"


# ── Timing guardrail warnings ────────────────────────────────


class TestTimingWarningFormat:
    """_check_timing_guardrails 的 warning 訊息格式。"""

    @pytest.mark.parametrize("value,field,expected_keyword", [
        ("1s", "group_wait", "below"),
        ("100h", "repeat_interval", "above"),
        ("0s", "group_interval", "below"),
    ])
    def test_boundary_warning_contains_keyword(self, value, field, expected_keyword):
        """邊界值 warning 包含正確的 keyword (below/above)。"""
        _, warn = _check_timing_guardrails(value, field)
        assert warn is not None
        assert expected_keyword in warn.lower()


# ── Warning deduplication ────────────────────────────────────


class TestWarningDeduplication:
    """同一 tenant 不重複產生相同 warning。"""

    def test_no_duplicate_warnings(self):
        """多 tenant 各自產生獨立的 warnings，無重複。"""
        routing_configs = {
            "db-a": {"receiver": "not-a-dict"},
            "db-b": {"receiver": "also-not-dict"},
        }
        _, _, warnings = generate_routes(routing_configs)
        # 每個 warning 應含不同 tenant
        assert len(warnings) == len(set(warnings)), \
            f"有重複 warnings: {warnings}"


# ── Enforced routing warnings ────────────────────────────────


class TestEnforcedRoutingWarningFormat:
    """_build_enforced_routes 的 warning 格式。"""

    def test_missing_receiver_in_enforced(self):
        """enforced routing 缺 receiver 產生 WARN。"""
        enforced = {"no_receiver": True}  # 缺少 "receiver" key
        routing_configs = {"db-a": make_routing_config("db-a")}
        _, _, warnings = _build_enforced_routes(enforced, routing_configs, None)
        assert len(warnings) >= 1
        for w in warnings:
            assert "WARN" in w
