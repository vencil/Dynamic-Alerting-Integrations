"""Unit tests for generate_alertmanager_routes.py.

涵蓋核心函式：
- build_receiver_config: receiver 結構驗證與 AM config 產生
- validate_receiver_domains: domain allowlist SSRF 防護
- generate_inhibit_rules: severity dedup inhibit rule 產生
- generate_routes: 完整 route tree 產生（含 enforced + tenant + overrides）
- merge_routing_with_defaults: routing defaults 三態繼承
- validate_tenant_keys: tenant config reserved key 驗證
- _substitute_tenant / _contains_tenant_placeholder: {{tenant}} 佔位符處理
- _apply_timing_params: timing guardrails 驗證與夾限
"""
import os
import sys
import tempfile

import pytest
import yaml

from factories import (
    make_receiver, make_routing_config, make_tenant_yaml,
    make_enforced_routing, make_override, write_yaml,
)

from generate_alertmanager_routes import (  # noqa: E402
    build_receiver_config,
    validate_receiver_domains,
    generate_inhibit_rules,
    generate_routes,
    merge_routing_with_defaults,
    validate_tenant_keys,
    _substitute_tenant,
    _contains_tenant_placeholder,
    _apply_timing_params,
    _build_enforced_routes,
    _build_tenant_routes,
    _parse_config_files,
    _merge_tenant_routing,
    _validate_profile_refs,
    check_domain_policies,
    expand_routing_overrides,
    load_tenant_configs,
    _extract_host,
    load_policy,
    PLATFORM_DEFAULTS,
)


# ============================================================
# build_receiver_config
# ============================================================
class TestBuildReceiverConfig:
    """build_receiver_config() 測試。"""

    # ── Parametrized positive tests for all receiver types ──
    @pytest.mark.parametrize("rtype,am_config_key,check_field,check_value", [
        ("webhook", "webhook_configs", "url", "https://hooks.example.com/alert"),
        ("slack", "slack_configs", "api_url", "https://hooks.slack.com/services/T/B/X"),
        ("email", "email_configs", "to", "admin@example.com"),
        ("teams", "msteams_configs", None, None),
        ("pagerduty", "pagerduty_configs", None, None),
    ], ids=["webhook", "slack", "email", "teams", "pagerduty"])
    def test_receiver_basic(self, rtype, am_config_key, check_field, check_value):
        """各 receiver type 產生正確的 AM config key 與欄位值。"""
        kwargs = {"channel": "#alerts"} if rtype == "slack" else {}
        config, warnings = build_receiver_config(make_receiver(rtype, **kwargs), "db-a")
        assert config is not None
        assert am_config_key in config
        assert warnings == []
        if check_field:
            assert config[am_config_key][0][check_field] == check_value

    def test_slack_channel_included(self):
        """Slack receiver 包含 optional channel。"""
        config, _ = build_receiver_config(
            make_receiver("slack", channel="#alerts"), "db-a")
        assert config["slack_configs"][0]["channel"] == "#alerts"

    def test_missing_type(self):
        """缺少 type 欄位回傳 None + 警告。"""
        receiver = {"url": "https://hooks.example.com"}
        config, warnings = build_receiver_config(receiver, "db-a")
        assert config is None
        assert any("missing required 'receiver.type'" in w for w in warnings)

    def test_unknown_type(self):
        """不支援的 type 回傳 None + 警告。"""
        receiver = {"type": "discord", "url": "https://discord.com/webhook"}
        config, warnings = build_receiver_config(receiver, "db-a")
        assert config is None
        assert any("unknown receiver type" in w for w in warnings)

    def test_missing_required_field(self):
        """缺少必要欄位回傳 None + 警告。"""
        receiver = {"type": "webhook"}  # missing 'url'
        config, warnings = build_receiver_config(receiver, "db-a")
        assert config is None
        assert any("requires 'url'" in w for w in warnings)

    def test_not_a_dict(self):
        """receiver 非 dict 回傳 None + 警告。"""
        config, warnings = build_receiver_config("http://bad", "db-a")
        assert config is None
        assert any("must be an object" in w for w in warnings)

    def test_type_case_insensitive(self):
        """type 欄位不區分大小寫。"""
        receiver = {"type": "WEBHOOK", "url": "https://example.com"}
        config, warnings = build_receiver_config(receiver, "db-a")
        assert config is not None
        assert "webhook_configs" in config

    def test_optional_fields_included(self):
        """有提供的 optional 欄位會被包含在 AM config 中。"""
        receiver = {
            "type": "webhook",
            "url": "https://example.com",
            "send_resolved": True,
        }
        config, _ = build_receiver_config(receiver, "db-a")
        assert config["webhook_configs"][0]["send_resolved"] is True

    def test_rocketchat_basic(self):
        """Rocket.Chat receiver 基本結構正確。"""
        cfg, warnings = build_receiver_config(
            {"type": "rocketchat", "url": "https://chat.example.com/hooks/x/y"}, "t")
        assert warnings == []
        assert "webhook_configs" in cfg
        assert cfg["webhook_configs"][0]["url"] == "https://chat.example.com/hooks/x/y"

    def test_rocketchat_metadata_not_in_am(self):
        """Rocket.Chat metadata (channel/username) 不傳給 AM config。"""
        cfg, _ = build_receiver_config(
            {"type": "rocketchat", "url": "https://chat.example.com/hooks/x/y",
             "channel": "#alerts", "username": "PrometheusBot"}, "t")
        entry = cfg["webhook_configs"][0]
        assert "channel" not in entry
        assert "username" not in entry

    def test_pagerduty_with_optional(self):
        """PagerDuty receiver 含選擇性欄位。"""
        cfg, _ = build_receiver_config(
            {"type": "pagerduty", "service_key": "abc", "severity": "critical",
             "client": "Dynamic Alerting"}, "t")
        entry = cfg["pagerduty_configs"][0]
        assert entry["severity"] == "critical"
        assert entry["client"] == "Dynamic Alerting"

    def test_pagerduty_missing_service_key(self):
        """PagerDuty 缺少必填 service_key 欄位。"""
        cfg, warnings = build_receiver_config({"type": "pagerduty"}, "t")
        assert cfg is None
        assert any("requires 'service_key'" in w for w in warnings)


# ============================================================
# validate_receiver_domains
# ============================================================
class TestValidateReceiverDomains:
    """validate_receiver_domains() SSRF 防護測試。"""

    def test_allowed_domain_passes(self):
        """允許的 domain 不產生警告。"""
        receiver = {"type": "webhook", "url": "https://hooks.example.com/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_blocked_domain_warns(self):
        """不在允許清單的 domain 產生警告。"""
        receiver = {"type": "webhook", "url": "https://evil.attacker.com/steal"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert any("not in allowed_domains" in w for w in warnings)

    def test_empty_allowlist_passes_all(self):
        """空的 allowlist 不做任何限制。"""
        receiver = {"type": "webhook", "url": "https://anywhere.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", [])
        assert warnings == []

    def test_email_smarthost_checked(self):
        """Email 的 smarthost（host:port 格式）也會被檢查。"""
        receiver = {"type": "email", "smarthost": "smtp.evil.com:587"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert any("not in allowed_domains" in w for w in warnings)

    def test_pagerduty_no_url_fields(self):
        """PagerDuty 無 URL 欄位，不做 domain 檢查。"""
        receiver = {"type": "pagerduty", "service_key": "abc"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_exact_domain_match(self):
        """精確網域符合 allowlist。"""
        receiver = {"type": "slack", "api_url": "https://hooks.slack.com/services/x"}
        warnings = validate_receiver_domains(receiver, "t", ["hooks.slack.com"])
        assert warnings == []

    def test_wildcard_domain_match(self):
        """萬用字元網域符合 allowlist。"""
        receiver = {"type": "teams", "webhook_url": "https://outlook.office.com/webhook/x"}
        warnings = validate_receiver_domains(receiver, "t", ["*.office.com"])
        assert warnings == []

    def test_email_smarthost_validated(self):
        """Email smarthost 驗證通過。"""
        receiver = {"type": "email", "to": ["a@b.com"], "smarthost": "smtp.example.com:587"}
        warnings = validate_receiver_domains(receiver, "t", ["*.example.com"])
        assert warnings == []

    def test_email_smarthost_blocked(self):
        """Email smarthost 被 allowlist 阻止。"""
        receiver = {"type": "email", "to": ["a@b.com"], "smarthost": "rogue.evil.com:25"}
        warnings = validate_receiver_domains(receiver, "t", ["*.example.com"])
        assert len(warnings) == 1
        assert "not in allowed_domains" in warnings[0]

    def test_none_allowlist_skips_check(self):
        """Allowlist 為 None 時略過網域檢查。"""
        warnings = validate_receiver_domains(
            {"type": "webhook", "url": "https://x.com"}, "t", None)
        assert warnings == []

    def test_blocked_domain_skips_route(self):
        """Domain not in allowlist → route not generated。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://evil.com/x"}}}
        routes, receivers, warnings = generate_routes(cfg, allowed_domains=["*.example.com"])
        assert len(routes) == 0
        assert any("not in allowed_domains" in w for w in warnings)

    def test_allowed_domain_generates_route(self):
        """允許的網域產生路由。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://hook.example.com/x"}}}
        routes, receivers, _ = generate_routes(cfg, allowed_domains=["*.example.com"])
        assert len(routes) == 1

    def test_no_policy_no_filtering(self):
        """No allowed_domains → backward compatible, all pass。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://any.com/x"}}}
        routes, _, _ = generate_routes(cfg, allowed_domains=None)
        assert len(routes) == 1

    def test_load_policy_from_file(self, config_dir):
        """從檔案載入 policy 的 allowed_domains。"""
        path = os.path.join(config_dir, "policy.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("allowed_domains:\n  - '*.example.com'\n  - hooks.slack.com\n")
        os.chmod(path, 0o600)
        domains = load_policy(path)
        assert domains == ["*.example.com", "hooks.slack.com"]

    def test_load_policy_missing_file(self):
        """Policy 檔案不存在時返回空列表。"""
        assert load_policy("/nonexistent/policy.yaml") == []

    def test_load_policy_no_key(self, config_dir):
        """Policy 檔案缺少 allowed_domains 鍵。"""
        path = os.path.join(config_dir, "policy.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("denied_functions:\n  - holt_winters\n")
        os.chmod(path, 0o600)
        assert load_policy(path) == []


class TestExtractHost:
    """_extract_host() URL 解析測試。"""

    @pytest.mark.parametrize("url,expected", [
        ("https://hooks.example.com/path", "hooks.example.com"),
        ("smtp.example.com:587", "smtp.example.com"),
        (None, None),
        ("", None),
    ], ids=["https-url", "host-port", "none", "empty"])
    def test_extract_host(self, url, expected):
        """從 URL 提取主機名稱。"""
        assert _extract_host(url) == expected


# ============================================================
# generate_inhibit_rules
# ============================================================
class TestGenerateInhibitRules:
    """generate_inhibit_rules() severity dedup 測試。"""

    def test_disabled_skips_rule(self):
        """disable 跳過 inhibit rule 並產生 INFO 訊息。"""
        dedup = {"db-a": "disable"}
        rules, warnings = generate_inhibit_rules(dedup)
        assert len(rules) == 0
        assert any("disabled" in w for w in warnings)

    def test_empty_config(self):
        """空 config 產生空結果。"""
        rules, warnings = generate_inhibit_rules({})
        assert rules == []
        assert warnings == []

    def test_enabled_tenant_rule_structure(self):
        """Enabled tenant → 完整 inhibit rule 結構 + equal 不含 tenant。"""
        rules, _ = generate_inhibit_rules({"db-a": "enable"})
        assert len(rules) == 1
        rule = rules[0]
        # source: critical + metric_group + tenant
        assert 'severity="critical"' in rule["source_matchers"]
        assert 'metric_group=~".+"' in rule["source_matchers"]
        assert 'tenant="db-a"' in rule["source_matchers"]
        # target: warning + metric_group + tenant
        assert 'severity="warning"' in rule["target_matchers"]
        assert 'metric_group=~".+"' in rule["target_matchers"]
        assert 'tenant="db-a"' in rule["target_matchers"]
        # equal 只有 metric_group（tenant 已在 matchers 中）
        assert rule["equal"] == ["metric_group"]
        assert "tenant" not in rule["equal"]

    def test_mixed_tenants(self):
        """混合多個租戶啟用和禁用狀態。"""
        rules, _ = generate_inhibit_rules({"db-a": "enable", "db-b": "disable", "db-c": "enable"})
        assert len(rules) == 2
        tenants = {r["source_matchers"][2] for r in rules}
        assert tenants == {'tenant="db-a"', 'tenant="db-c"'}

    def test_sorted_by_tenant(self):
        """Inhibit 規則依租戶排序。"""
        rules, _ = generate_inhibit_rules({"db-c": "enable", "db-a": "enable", "db-b": "enable"})
        tenants = [r["source_matchers"][2] for r in rules]
        assert tenants == ['tenant="db-a"', 'tenant="db-b"', 'tenant="db-c"']

    def test_all_disabled(self):
        """所有租戶都禁用時無 inhibit 規則。"""
        rules, warnings = generate_inhibit_rules({"db-a": "disable", "db-b": "disable"})
        assert rules == []
        assert len(warnings) == 2


class TestTenantPlaceholder:
    """{{tenant}} 佔位符處理測試。"""

    def test_substitute_string(self):
        """字串中替換 {{tenant}} 佔位符。"""
        assert _substitute_tenant("channel-{{tenant}}", "db-a") == "channel-db-a"

    def test_substitute_nested_dict(self):
        """嵌套字典中替換 {{tenant}}。"""
        obj = {"channel": "#{{tenant}}-alerts", "nested": {"url": "https://{{tenant}}.example.com"}}
        result = _substitute_tenant(obj, "db-a")
        assert result["channel"] == "#db-a-alerts"
        assert result["nested"]["url"] == "https://db-a.example.com"

    def test_substitute_list(self):
        """列表中替換 {{tenant}}。"""
        obj = ["{{tenant}}-1", "static", "{{tenant}}-2"]
        result = _substitute_tenant(obj, "db-a")
        assert result == ["db-a-1", "static", "db-a-2"]

    def test_contains_placeholder_true(self):
        """偵測到 {{tenant}} 佔位符存在。"""
        assert _contains_tenant_placeholder({"url": "https://{{tenant}}.example.com"}) is True

    def test_contains_placeholder_false(self):
        """未偵測到 {{tenant}} 佔位符。"""
        assert _contains_tenant_placeholder({"url": "https://static.example.com"}) is False

    def test_contains_placeholder_in_list(self):
        """列表中偵測 {{tenant}} 佔位符。"""
        assert _contains_tenant_placeholder(["{{tenant}}"]) is True

    def test_non_string_value(self):
        """非字串值不應觸發佔位符偵測。"""
        assert _contains_tenant_placeholder({"count": 42}) is False


# ============================================================
# _apply_timing_params
# ============================================================
class TestApplyTimingParams:
    """Timing guardrails 測試。"""

    def test_valid_params(self):
        """合法參數直接通過。"""
        source = {"group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h"}
        timing, warnings = _apply_timing_params(source, "db-a")
        assert timing["group_wait"] == "30s"
        assert timing["group_interval"] == "5m"
        assert timing["repeat_interval"] == "4h"
        assert warnings == []

    def test_below_minimum_clamped(self):
        """低於下限會被夾限並產生警告。"""
        source = {"group_wait": "1s"}
        timing, warnings = _apply_timing_params(source, "db-a")
        assert timing["group_wait"] == "5s"
        assert any("below minimum" in w for w in warnings)

    def test_above_maximum_clamped(self):
        """高於上限會被夾限並產生警告。"""
        source = {"repeat_interval": "100d"}
        timing, warnings = _apply_timing_params(source, "db-a")
        assert any("above maximum" in w for w in warnings)

    def test_missing_params_not_added(self):
        """未提供的參數不會出現在結果中。"""
        timing, warnings = _apply_timing_params({}, "db-a")
        assert timing == {}
        assert warnings == []


# ============================================================
# merge_routing_with_defaults
# ============================================================
class TestMergeRoutingWithDefaults:
    """Routing defaults 三態繼承測試。"""

    def test_tenant_overrides_default(self):
        """Tenant 值覆蓋 default。"""
        defaults = {"group_wait": "30s", "receiver": {"type": "webhook", "url": "https://default.com"}}
        tenant = {"group_wait": "10s"}
        merged = merge_routing_with_defaults(defaults, tenant, "db-a")
        assert merged["group_wait"] == "10s"
        # receiver 從 default 繼承
        assert merged["receiver"]["url"] == "https://default.com"

    def test_tenant_placeholder_expanded(self):
        """{{tenant}} 在 merge 時展開。"""
        defaults = {"receiver": {"type": "slack", "api_url": "https://hooks.slack.com/{{tenant}}"}}
        merged = merge_routing_with_defaults(defaults, {}, "db-a")
        assert "db-a" in merged["receiver"]["api_url"]

    def test_empty_tenant_inherits_all(self):
        """空 tenant routing 完全繼承 defaults。"""
        defaults = {"group_wait": "30s", "group_interval": "5m"}
        merged = merge_routing_with_defaults(defaults, {}, "db-a")
        assert merged["group_wait"] == "30s"
        assert merged["group_interval"] == "5m"


# ============================================================
# validate_tenant_keys
# ============================================================

    def test_tenant_inherits_defaults(self, config_dir):
        """無 _routing 的 tenant 繼承 _routing_defaults。"""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp:587"
  group_wait: "30s"
tenants:
  db-a:
    mysql_connections: "70"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert "db-a" in routing
        assert routing["db-a"]["receiver"]["type"] == "email"
        assert routing["db-a"]["group_wait"] == "30s"

    def test_tenant_overrides_receiver(self, config_dir):
        """有 _routing 的 tenant 覆寫 receiver，timing 從 defaults。"""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["default@example.com"]
    smarthost: "smtp:587"
  group_wait: "30s"
  repeat_interval: "4h"
""")
        write_yaml(config_dir, "db-b.yaml", """
tenants:
  db-b:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/x"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert routing["db-b"]["receiver"]["type"] == "slack"
        assert routing["db-b"]["group_wait"] == "30s"
        assert routing["db-b"]["repeat_interval"] == "4h"

    def test_tenant_disables_routing(self, config_dir):
        """_routing: "disable" → 不產出路由。"""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp:587"
""")
        write_yaml(config_dir, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert "db-c" not in routing

    def test_tenant_template_substitution(self, config_dir):
        """{{tenant}} 在 receiver fields 被替換為 tenant name。"""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "rocketchat"
    url: "https://chat.example.com/hooks/x/y"
    channel: "#alerts-{{tenant}}"
  group_wait: "30s"
tenants:
  db-a:
    mysql_connections: "70"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        # channel is in the receiver dict (metadata, not AM config)
        assert routing["db-a"]["receiver"]["channel"] == "#alerts-db-a"

    def test_disabled_routing_still_tracks_dedup(self, config_dir):
        """_routing: disable の tenant でも dedup は追跡される。"""
        write_yaml(config_dir, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
    _severity_dedup: "enable"
""")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert "db-c" not in routing  # routing disabled
        assert dedup["db-c"] == "enable"  # dedup still tracked

    def test_no_defaults_no_routing(self, config_dir):
        """無 _routing_defaults 也無 _routing → 不產出路由。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert len(routing) == 0
        assert dedup["db-a"] == "enable"

    def test_defaults_boundary_warning(self, config_dir):
        """_routing_defaults 在 tenant 檔案中會被忽略並警告。"""
        write_yaml(config_dir, "db-a.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["bad@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        # db-a should NOT have routing from its own _routing_defaults
        assert len(routing) == 0

    def test_template_in_email_to(self, config_dir):
        """{{tenant}} 在 email to list 被替換。"""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["{{tenant}}-team@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
        routing, _, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert routing["db-a"]["receiver"]["to"] == ["db-a-team@example.com"]
class TestValidateTenantKeys:
    """Tenant config reserved key 驗證測試。"""

    def test_valid_keys_no_warnings(self):
        """存在於 defaults 中的 key 不產生警告。"""
        keys = {"connection_count", "latency_ms"}
        defaults_keys = {"connection_count", "latency_ms"}
        warnings = validate_tenant_keys("db-a", keys, defaults_keys)
        assert warnings == []

    def test_reserved_key_accepted(self):
        """合法的 reserved key（如 _silent_mode）不產生警告。"""
        keys = {"_silent_mode", "connection_count"}
        warnings = validate_tenant_keys("db-a", keys, set())
        assert not any("_silent_mode" in w for w in warnings)

    def test_unknown_underscore_key_warns(self):
        """未知的 _ 開頭 key 產生警告。"""
        keys = {"_unknown_flag"}
        warnings = validate_tenant_keys("db-a", keys, set())
        assert any("_unknown_flag" in w for w in warnings)

    def test_dangling_critical_warns_python_side_of_the_go_parity(self):
        """`<metric>_critical` 的 base 不在 defaults → 警告（Python↔Go 對稱）。

        Go 的 `ValidateTenantKeys` 過去對 `_critical` 是無條件 `continue`，
        於是最危險的那一層（懸空 critical 產不出任何 row，不像 base key 還會
        回落平台預設）反而是唯一沒有作者可見訊號的。Python 這側一直都會警告；
        本測試把這半釘住，避免將來只有一邊被改回去。
        """
        warnings = validate_tenant_keys(
            "db-a", {"oracle_wait_time_rate_critical"}, {"mysql_connections"})
        assert any("oracle_wait_time_rate_critical" in w for w in warnings)

    def test_critical_with_base_default_is_silent(self):
        """base 在 defaults 時不得警告——否則「讓它變紅」會被誤當成修好。"""
        warnings = validate_tenant_keys(
            "db-a", {"mysql_connections_critical"}, {"mysql_connections"})
        assert warnings == []

    def test_defaults_keys_excluded(self):
        """defaults 中定義的 key 不在 tenant 驗證範圍。"""
        keys = {"custom_metric"}
        defaults_keys = {"custom_metric"}
        warnings = validate_tenant_keys("db-a", keys, defaults_keys)
        assert warnings == []

    def test_custom_alerts_reserved_no_warning(self):
        """_custom_alerts（v2.9.0 ADR-024 能力 B, #741）為合法 reserved key，
        不可被當成 typo（Python VALID_RESERVED_KEYS ↔ Go validReservedKeys 同步）。"""
        warnings = validate_tenant_keys("db-b", {"_custom_alerts"}, set())
        assert warnings == []

    def test_custom_alerts_alongside_other_reserved(self):
        """_custom_alerts 與其他 reserved key / prefix 並存時皆不報警。"""
        keys = {"_custom_alerts", "_silent_mode", "_severity_dedup", "_routing"}
        warnings = validate_tenant_keys("db-b", keys, set())
        assert warnings == []


# ============================================================
# #1189 / TRK-337 optional_overrides：平台認得但不給值的第三格
# （Python ↔ Go membership 對稱；Go 側見
#  components/threshold-exporter/app/pkg/config/optional_overrides_membership_test.go）
# ============================================================
class TestOptionalOverridesMembership:
    """宣告但無值的 key：租戶設得進去，平台不主張數值。

    重點不在「多接受一組 key」，而在**哪幾格刻意不 union**——那幾格
    Go 也拒絕，兩側必須逐格一致，否則 CI 說可以、寫入閘門說不行
    （或反之），那正是 #1189 的病本身。
    """

    _DEFAULTS = {"mysql_connections"}

    def test_declared_flat_key_accepted(self):
        """宣告過的平鍵可被租戶設定——本次改動的核心。"""
        warnings = validate_tenant_keys(
            "db-a", {"oracle_wait_time_rate"}, self._DEFAULTS,
            {"oracle_wait_time_rate"})
        assert warnings == []

    def test_declared_list_under_retired_spelling_still_matches(self):
        """⭐ 清單本身要先正規化。

        比對用的是**正規化後**的租戶 key，所以清單裡若留著舊拼法
        （mysql_cpu），就永遠配不上 mysql_threads_running：平台宣告了一個
        永遠啟用不了的 key，而且沒有任何訊號——正是本次要消滅的靜默漂移
        在新機制上復發。用現存的活別名，不自造假別名。
        """
        warnings = validate_tenant_keys(
            "db-a", {"mysql_threads_running"}, self._DEFAULTS, {"mysql_cpu"})
        assert warnings == []

    def test_tenant_uses_retired_spelling_of_declared_key(self):
        """租戶用舊拼法設定宣告 key → 只有 NOTICE，不阻擋。"""
        warnings = validate_tenant_keys(
            "db-a", {"mysql_cpu"}, self._DEFAULTS, {"mysql_threads_running"})
        assert all(w.strip().startswith("NOTICE:") for w in warnings), warnings
        assert any("mysql_threads_running" in w for w in warnings)

    def test_declared_dimensional_base_accepted(self):
        """dimensional 這一格落地即生效：resolveDimensionalRows 從不查
        defaults，租戶寫下去就會 emit，接受它沒有承諾任何 resolver 給不出
        的東西。"""
        warnings = validate_tenant_keys(
            "db-a", {'oracle_wait_time_rate{db="ORCL"}'}, self._DEFAULTS,
            {"oracle_wait_time_rate"})
        assert warnings == []

    def test_critical_twin_of_declared_base_still_warns(self):
        """⛔ 刻意不 union 的那一格（Python ↔ Go 一致）。

        Go 的 resolveCriticalRows 認的是 defaults[base]，base 只被宣告時
        直接丟棄該 row。這裡若放行，等於 CI 祝福了一個 exporter 會默默扔掉
        的 key——寫入成功、什麼都沒 emit、訊號只在 exporter 的 log 裡。
        """
        warnings = validate_tenant_keys(
            "db-a", {"oracle_wait_time_rate_critical"}, self._DEFAULTS,
            {"oracle_wait_time_rate"})
        assert any("oracle_wait_time_rate_critical" in w for w in warnings)

    def test_critical_key_named_on_the_list_is_still_refused(self):
        """⛔ Py↔Go 對稱：清單裡直接列 `_critical` key 仍要警告。

        Go 的 cascade 先走 `_critical` 分支、兩條路都 continue，所以那種 key
        根本到不了 membership 放寬，且 resolveCriticalRows 在 base 無值時會丟
        掉整個 row。這裡若放行，CI 綠而 tenant-api 寫入閘門拒絕——正是
        #1189 的分歧，而且是 registry 裡的多數形狀（25 個中 16 個是
        `_critical`）。
        """
        warnings = validate_tenant_keys(
            "db-a", {"jvm_memory_critical"}, self._DEFAULTS,
            {"jvm_memory_critical"})
        assert any("jvm_memory_critical" in w for w in warnings), warnings

    def test_critical_key_on_the_list_with_valued_base_is_silent(self):
        """對照組：base 有值時本來就該通過，別把上一條修成一律報警。"""
        warnings = validate_tenant_keys(
            "db-a", {"mysql_connections_critical"}, {"mysql_connections"},
            {"mysql_connections_critical"})
        assert warnings == []

    def test_declared_list_is_not_a_blanket_amnesty(self):
        """宣告清單是成員集合，不是大赦：前綴相似與無關 key 照樣警告。"""
        for key in ("oracle_wait_time_rate_extra", "postgres_nonsense"):
            warnings = validate_tenant_keys(
                "db-a", {key}, self._DEFAULTS, {"oracle_wait_time_rate"})
            assert any("not in defaults" in w for w in warnings), (key, warnings)
            assert any(key in w for w in warnings), (key, warnings)

    def test_absent_argument_preserves_rejection(self):
        """省略 / None / 空集合三者行為一致，且維持既有拒絕。"""
        results = [
            validate_tenant_keys("db-a", {"oracle_wait_time_rate"}, self._DEFAULTS),
            validate_tenant_keys("db-a", {"oracle_wait_time_rate"}, self._DEFAULTS, None),
            validate_tenant_keys("db-a", {"oracle_wait_time_rate"}, self._DEFAULTS, set()),
        ]
        assert all(r == results[0] for r in results), results
        assert any("not in defaults" in w for w in results[0])

    def test_optional_overrides_is_threaded_from_the_platform_file(self, config_dir):
        """⭐ 有參數 ≠ 有人傳。

        membership 放寬只在 load_tenant_configs 真的把解析出來的清單交給
        validator 時才存在；預設值 None 讓「解析了但沒接上」可以無聲通過，
        所以端到端釘住這條線。
        """
        write_yaml(config_dir, "_defaults.yaml", """
defaults:
  mysql_connections: 70
optional_overrides:
  - oracle_wait_time_rate
tenants:
  db-a:
    oracle_wait_time_rate: "5"
""")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert schema_warnings == []

    def test_optional_overrides_in_a_tenant_file_is_ignored(self, config_dir):
        """⛔ 安全邊界，鏡射 Go 的 applyBoundaryRules。

        若租戶能在自己的檔案裡列 key，就等於自我授權繞過這道檢查本身
        （tenant-api 不是唯一寫入者，GitOps 可以直推）。清單只從 `_` 前綴
        的平台檔認得。
        """
        write_yaml(config_dir, "_defaults.yaml", "defaults:\n  mysql_connections: 70\n")
        write_yaml(config_dir, "db-a.yaml", """
optional_overrides:
  - anything_i_want
tenants:
  db-a:
    anything_i_want: "5"
""")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert any("anything_i_want" in w and "not in defaults" in w
                   for w in schema_warnings), schema_warnings


class TestMembershipParityMatrix:
    """Python 半邊的 Go↔Python membership parity pin（#1189 / TRK-337）。

    兩側是同一條規則的獨立手抄、被不同 gate 消費，所以**誰也不斷言誰**：
    兩邊各自對 `tests/shared/optional_overrides_membership_matrix.json`
    斷言，讓一致性變成**傳遞性**的而非用文字宣稱的。Go 半邊在
    `components/threshold-exporter/app/pkg/config/optional_overrides_parity_test.go`。

    真正的價值在複合形狀：兩邊 cascade 的分支順序不同（Go 先驗
    `_critical` / dimensional 再驗 membership，Python 相反），所以任何
    「既在清單上又是複合形狀」的 key 都是一側可以無聲遮蔽另一側的地方。
    這張表第一次畫出來時就抓到三個這樣的分歧。
    """

    @staticmethod
    def _matrix():
        import json
        from pathlib import Path
        p = (Path(__file__).resolve().parents[2]
             / "tests" / "shared" / "optional_overrides_membership_matrix.json")
        return json.loads(p.read_text(encoding="utf-8"))

    def test_matrix_is_not_truncated(self):
        """空集合靜默：矩陣縮到零仍會永遠綠，正是本 repo 一再抓到的形狀。"""
        cases = self._matrix()["cases"]
        assert len(cases) >= 20, f"matrix looks truncated: {len(cases)}"
        assert {c["verdict"] for c in cases} <= {"accept", "refuse"}

    def test_python_side_matches_the_matrix(self):
        m = self._matrix()
        defaults = set(m["defaults"])
        failures = []
        for c in m["cases"]:
            warnings = validate_tenant_keys(
                "t-1", {c["key"]}, set(defaults), set(c["declared"]))
            # accept ＝ 沒有阻擋性訊息；NOTICE 兩側皆為告知性，不算拒絕
            accepted = not any("WARN:" in w for w in warnings)
            want = c["verdict"] == "accept"
            if accepted != want:
                failures.append(
                    f"{c['name']}: key={c['key']!r} declared={c['declared']!r} "
                    f"want={c['verdict']} got={'accept' if accepted else 'refuse'} "
                    f"why={c['why']} warnings={warnings}")
        assert not failures, "\n".join(failures)


# ============================================================
# #1231 deprecated key aliases（Python ↔ Go alias boundary 對稱）
# ============================================================
class TestDeprecatedKeyAliases:
    """#1231：舊拼寫（mysql_cpu）在 2-release 過渡窗內以 canonical key
    通過驗證，發非阻擋 NOTICE 取代 unknown-key 警告。fixture 覆蓋兩態：
    「舊名 defaults」（現態，platform rename 在 commit 2）與
    「新名 defaults＋舊 key override」（rename 後的出貨態）。"""

    _OLD_DEFAULTS = {"mysql_cpu", "container_cpu"}
    _NEW_DEFAULTS = {"mysql_threads_running", "container_cpu"}

    def _notices(self, warnings):
        return [w for w in warnings if w.lstrip().startswith("NOTICE:")]

    def test_old_key_with_new_defaults_notice_not_unknown(self):
        """出貨態：舊 override 對新名 defaults → 只有 NOTICE，無 unknown-key。"""
        warnings = validate_tenant_keys("t", {"mysql_cpu"}, self._NEW_DEFAULTS)
        assert len(warnings) == 1, warnings
        n = warnings[0]
        assert n.lstrip().startswith("NOTICE:")
        assert "mysql_cpu" in n and "mysql_threads_running" in n
        assert "not in defaults" not in n

    def test_old_key_with_old_defaults_still_notices(self):
        """現態：defaults 仍是舊名——alias 表已生效，照樣提示改名。"""
        warnings = validate_tenant_keys("t", {"mysql_cpu"}, self._OLD_DEFAULTS)
        assert self._notices(warnings), warnings
        assert not [w for w in warnings if "WARN" in w], warnings

    def test_old_critical_not_dangling_after_alias(self):
        """舊拼寫 _critical 的 base 經 alias 後在 defaults → 不落 dangling。"""
        for defaults in (self._OLD_DEFAULTS, self._NEW_DEFAULTS):
            warnings = validate_tenant_keys(
                "t", {"mysql_cpu_critical"}, defaults)
            assert not [w for w in warnings if "WARN" in w], (defaults, warnings)
            assert self._notices(warnings), (defaults, warnings)

    def test_dimensional_old_base_canonicalizes(self):
        """dimensional 舊 base canonicalize 後照走 version-label 驗證。"""
        warnings = validate_tenant_keys(
            "t", {'mysql_cpu{version="v2"}'}, self._NEW_DEFAULTS)
        assert self._notices(warnings), warnings
        # mysql_threads_running 非 pilot metric → version-label 警告照發，
        # 且訊息以 canonical 拼寫呈現（aliased key 的訊息一律點名新 key）。
        pilot = [w for w in warnings if "non-pilot" in w]
        assert pilot and "mysql_threads_running" in pilot[0], warnings

    def test_prefix_typo_still_unknown(self):
        """⛔ 禁 prefix-match：mysql_cpu_util 仍是 unknown-key，無 NOTICE。"""
        warnings = validate_tenant_keys(
            "t", {"mysql_cpu_util"}, self._NEW_DEFAULTS)
        assert not self._notices(warnings), warnings
        assert any("mysql_cpu_util" in w and "not in defaults" in w
                   for w in warnings), warnings

    def test_both_spellings_conflict_reports_ignored(self):
        """兩拼寫並存 → canonical 勝出，舊 key 回報 ignored（同 Go dedup）。"""
        warnings = validate_tenant_keys(
            "t", {"mysql_cpu", "mysql_threads_running"}, self._NEW_DEFAULTS)
        assert len(warnings) == 1, warnings
        assert "ignored" in warnings[0] and "mysql_cpu" in warnings[0]
        assert not [w for w in warnings if "WARN" in w], warnings


class TestDeprecationNoticePin:
    """措辭 pin（仿 TestPolicyErrorPrefixPin 的鎖法）：NOTICE 行不得踩到
    兩份 fatal 判定——generate_alertmanager_routes._validate_mode:136 的
    `"WARN" in w and "skipping" in w` 與 validate_config.py:220 的手抄副本，
    以及 _policy_errors 的 `ERROR:` 前綴。兩份判定都在此直接執行驗證。"""

    def _all_notices(self):
        out = []
        out += validate_tenant_keys("t", {"mysql_cpu"},
                                    {"mysql_threads_running"})
        out += validate_tenant_keys("t", {"mysql_cpu_critical"},
                                    {"mysql_threads_running"})
        out += validate_tenant_keys(
            "t", {"mysql_cpu", "mysql_threads_running"},
            {"mysql_threads_running"})
        assert out, "fixture 應產出至少一則 NOTICE"
        return out

    def test_notice_never_matches_legacy_fatal_predicate(self):
        """直接執行 --validate 的 legacy fatal 述詞：必須全空。"""
        notices = self._all_notices()
        fatal = [w for w in notices if "WARN" in w and "skipping" in w]
        assert fatal == []
        # 更嚴：連個別子字串都不許出現，杜絕未來措辭 drift 慢慢靠近述詞。
        assert not [w for w in notices if "skipping" in w]
        assert not [w for w in notices if "WARN" in w]

    def test_notice_never_matches_policy_error_prefix(self):
        """_policy_errors 的 blocking 前綴判定：必須全空。"""
        from _grar_validate import POLICY_ERROR_PREFIX
        notices = self._all_notices()
        assert not [w for w in notices
                    if w.lstrip().startswith(POLICY_ERROR_PREFIX)]

    def test_alias_table_mirrors_go_side(self):
        """alias 表雙點名內容 pin：commit 2 改接 registry 前，Python↔Go
        兩份手 pin 表必須同內容（Go 側在 pkg/config/aliases.go）。"""
        from _grar_validate import DEPRECATED_KEY_ALIASES
        assert DEPRECATED_KEY_ALIASES == {
            "mysql_cpu": "mysql_threads_running"}


class TestValidateVersionLabel:
    """ADR-024 OQ-6 dimensional `version` label guard (Python side of the
    雙語 da-guard; mirrors Go config.validateVersionLabel)."""

    _DEFAULTS = {"container_cpu", "container_memory", "redis_memory"}

    def _warn(self, key):
        return "\n".join(validate_tenant_keys("t", {key}, self._DEFAULTS))

    def test_valid_pilot_version_no_warning(self):
        assert validate_tenant_keys("t", {'container_cpu{version="v2"}'}, self._DEFAULTS) == []
        assert validate_tenant_keys("t", {'container_memory{version="v2-rc1"}'}, self._DEFAULTS) == []

    def test_empty_version_warns(self):
        assert "empty version" in self._warn('container_cpu{version=""}')

    def test_reserved_default_warns(self):
        assert "reserved" in self._warn('container_cpu{version="default"}')

    def test_bad_charset_warns(self):
        assert "violates" in self._warn('container_cpu{version="V2.0"}')

    def test_non_pilot_metric_warns(self):
        assert "non-pilot" in self._warn('redis_memory{version="v2"}')

    def test_regex_matcher_warns(self):
        assert "regex version matcher" in self._warn('container_cpu{version=~"v.*"}')

    def test_non_version_dimensional_unaffected(self):
        assert validate_tenant_keys("t", {'container_cpu{env="prod"}'}, self._DEFAULTS) == []

    def test_version_substring_not_mismatched(self):
        # `app_version` must NOT be treated as the `version` label; a non-pilot
        # base would otherwise spuriously emit a "non-pilot" warning.
        assert validate_tenant_keys("t", {'redis_memory{app_version="v2"}'}, self._DEFAULTS) == []


# ============================================================
# generate_routes (整合測試)
# ============================================================

    def test_typo_reserved_key(self, config_dir):
        """_silence_mode (typo) → warning."""
        write_yaml(config_dir, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    _silence_mode: "warning"
""")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert len(schema_warnings) == 1
        assert "unknown reserved key" in schema_warnings[0]
        assert "_silence_mode" in schema_warnings[0]

    def test_unknown_metric_key(self, config_dir):
        """Key not in defaults → warning."""
        write_yaml(config_dir, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    postgres_connections: "60"
""")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert len(schema_warnings) == 1
        assert "not in defaults" in schema_warnings[0]
        assert "postgres_connections" in schema_warnings[0]

    def test_critical_suffix_valid(self, config_dir):
        """mysql_connections_critical → valid if mysql_connections in defaults."""
        write_yaml(config_dir, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    mysql_connections_critical: "90"
""")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert schema_warnings == []

    def test_state_prefix_valid(self, config_dir):
        """_state_* 前綴鍵有效。"""
        write_yaml(config_dir, "_defaults.yaml", "tenants:\n  db-a:\n    _state_maintenance: 'enable'\n")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert schema_warnings == []

    def test_no_defaults_all_metric_keys_warn(self, config_dir):
        """No defaults section → any non-reserved tenant key warns."""
        write_yaml(config_dir, "t.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        _, _, schema_warnings, _er, _mc = load_tenant_configs(config_dir)
        assert len(schema_warnings) == 1
        assert "not in defaults" in schema_warnings[0]

    def test_validate_tenant_keys_function(self):
        """Direct unit test of validate_tenant_keys()."""
        defaults_keys = {"mysql_connections", "redis_memory"}
        keys = {"mysql_connections", "_silent_mode", "_routing", "_state_maintenance",
                "redis_memory_critical", "_silence_mode", "unknown_metric"}
        warnings = validate_tenant_keys("t1", keys, defaults_keys)
        assert len(warnings) == 2
        typo_warnings = [w for w in warnings if "typo" in w]
        metric_warnings = [w for w in warnings if "not in defaults" in w]
        assert len(typo_warnings) == 1
        assert "_silence_mode" in typo_warnings[0]
        assert len(metric_warnings) == 1
        assert "unknown_metric" in metric_warnings[0]
class TestGenerateRoutes:
    """generate_routes() 整合測試。"""

    def test_single_tenant_webhook(self):
        """單一 tenant 產生一組 route + receiver。"""
        configs = {"db-a": {**make_routing_config(), "group_wait": "30s"}}
        routes, receivers, warnings = generate_routes(configs)
        assert len(routes) == 1
        assert routes[0]["receiver"] == "tenant-db-a"
        assert routes[0]["matchers"] == ['tenant="db-a"']
        assert len(receivers) == 1
        assert receivers[0]["name"] == "tenant-db-a"
        assert "webhook_configs" in receivers[0]

    def test_multiple_tenants_sorted(self):
        """多個 tenant 按字母排序。"""
        configs = {
            "db-b": make_routing_config(url="https://b.example.com"),
            "db-a": make_routing_config(url="https://a.example.com"),
        }
        routes, receivers, warnings = generate_routes(configs)
        assert len(routes) == 2
        assert routes[0]["receiver"] == "tenant-db-a"
        assert routes[1]["receiver"] == "tenant-db-b"

    def test_missing_receiver_skipped(self):
        """缺少 receiver 的 tenant 被跳過並產生警告。"""
        configs = {"db-a": {"group_wait": "30s"}}
        routes, receivers, warnings = generate_routes(configs)
        assert len(routes) == 0
        assert any("missing required 'receiver'" in w for w in warnings)

    def test_enforced_routing_single(self):
        """單一 enforced route 插入在 tenant route 之前。"""
        configs = {"db-a": make_routing_config(url="https://tenant.example.com")}
        enforced = make_enforced_routing()
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced)
        assert len(routes) == 2
        assert routes[0]["receiver"] == "platform-enforced"
        assert routes[0].get("continue") is True
        assert routes[1]["receiver"] == "tenant-db-a"

    def test_enforced_routing_per_tenant(self):
        """含 {{tenant}} 的 enforced routing 為每個 tenant 展開。"""
        configs = {
            "db-a": make_routing_config(url="https://a.example.com"),
            "db-b": make_routing_config(url="https://b.example.com"),
        }
        enforced = make_enforced_routing("slack", per_tenant=True)
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced)
        assert len(routes) == 4  # 2 enforced + 2 tenant
        enforced_routes = [r for r in routes if r.get("continue")]
        assert len(enforced_routes) == 2

    def test_enforced_missing_receiver_warns(self):
        """Enforced routing 缺少 receiver 產生警告但不影響 tenant routes。"""
        configs = {"db-a": make_routing_config()}
        enforced = {"match": ['severity="critical"']}  # no receiver
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced)
        assert any("missing 'receiver'" in w for w in warnings)
        assert len(routes) == 1

    def test_domain_allowlist_blocks_tenant(self):
        """Domain allowlist 阻擋 tenant receiver。"""
        configs = {"db-a": make_routing_config(url="https://evil.com/hook")}
        routes, receivers, warnings = generate_routes(
            configs, allowed_domains=["*.example.com"])
        assert len(routes) == 0
        assert any("not in allowed_domains" in w for w in warnings)

    def test_routing_overrides(self):
        """Per-rule routing overrides 插入在 tenant 主 route 之前。"""
        configs = {
            "db-a": {
                **make_routing_config(url="https://default.example.com"),
                "overrides": [{
                    "alertname": "HighCPU",
                    "receiver": make_receiver("slack", api_url="https://hooks.slack.com/cpu"),
                }],
            }
        }
        routes, receivers, warnings = generate_routes(configs)
        assert len(routes) == 2
        assert "db-a-override-0" in routes[0]["receiver"]

    def test_timing_params_applied(self):
        """Timing parameters 正確套用到 route。"""
        configs = {
            "db-a": {
                **make_routing_config(),
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            }
        }
        routes, _, _ = generate_routes(configs)
        assert routes[0]["group_wait"] == "30s"
        assert routes[0]["group_interval"] == "5m"
        assert routes[0]["repeat_interval"] == "4h"

    def test_group_by_passed_through(self):
        """group_by 傳遞到路由。"""
        cfg = {"db-a": {"receiver": make_receiver(), "group_by": ["alertname", "severity"]}}
        routes, _, _ = generate_routes(cfg)
        assert routes[0]["group_by"] == ["alertname", "severity"]



class TestExpandRoutingOverrides:
    """expand_routing_overrides() 邊界與錯誤處理測試。"""

    def test_no_overrides_returns_empty(self):
        """routing_config 無 overrides key 回傳空結果。"""
        routing = {"receiver": make_receiver()}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert recv == []
        assert warns == []

    def test_empty_overrides_list(self):
        """overrides=[] 回傳空結果。"""
        routing = {"receiver": make_receiver(), "overrides": []}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []

    def test_overrides_not_list_warns(self):
        """overrides 非 list 產生 WARN。"""
        routing = {"receiver": make_receiver(), "overrides": "bad"}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert any("must be a list" in w for w in warns)

    def test_override_not_dict_warns(self):
        """override entry 非 dict 產生 WARN。"""
        routing = {"receiver": make_receiver(), "overrides": ["not-a-dict"]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert any("must be a dict" in w for w in warns)

    def test_missing_alertname_and_metric_group(self):
        """override 缺 alertname 和 metric_group 產生 WARN。"""
        routing = {"receiver": make_receiver(), "overrides": [
            {"receiver": make_receiver()}  # 刻意缺少 match key
        ]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert any("alertname" in w and "metric_group" in w for w in warns)

    def test_both_alertname_and_metric_group(self):
        """override 同時有 alertname 和 metric_group 產生 WARN。"""
        bad = make_override()
        bad["metric_group"] = "cpu"  # 加上第二個 match key
        routing = {"receiver": make_receiver(), "overrides": [bad]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert any("both" in w for w in warns)

    def test_missing_receiver_warns(self):
        """override 缺 receiver 產生 WARN。"""
        routing = {"receiver": make_receiver(), "overrides": [
            {"alertname": "HighCPU"}  # 刻意缺少 receiver
        ]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert sub == []
        assert any("missing 'receiver'" in w for w in warns)

    def test_metric_group_override(self):
        """metric_group override 產生正確 matcher。"""
        routing = {"receiver": make_receiver(), "overrides": [
            make_override("metric_group", "cpu_metrics")
        ]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert len(sub) == 1
        assert any('metric_group="cpu_metrics"' in m for m in sub[0]["matchers"])

    def test_alertname_override_matchers(self):
        """alertname override 產生 tenant + alertname matcher。"""
        routing = {"receiver": make_receiver(), "overrides": [
            make_override("alertname", "DiskFull")
        ]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert len(sub) == 1
        matchers = sub[0]["matchers"]
        assert any('tenant="db-a"' in m for m in matchers)
        assert any('alertname="DiskFull"' in m for m in matchers)

    def test_group_by_applied(self):
        """override 含 group_by 正確套用。"""
        routing = {"receiver": make_receiver(), "overrides": [
            make_override(group_by=["alertname", "instance"])
        ]}
        sub, _, _ = expand_routing_overrides("db-a", routing)
        assert sub[0]["group_by"] == ["alertname", "instance"]

    def test_multiple_overrides(self):
        """多個 override 產生多個 sub-route。"""
        routing = {"receiver": make_receiver(), "overrides": [
            make_override("alertname", "A"),
            make_override("alertname", "B"),
        ]}
        sub, recv, _ = expand_routing_overrides("db-a", routing)
        assert len(sub) == 2
        assert len(recv) == 2
        assert "override-0" in recv[0]["name"]
        assert "override-1" in recv[1]["name"]

    def test_domain_allowlist_blocks(self):
        """domain allowlist 不匹配時 override 被拒絕。"""
        routing = {"receiver": make_receiver(), "overrides": [
            {"alertname": "HighCPU",
             "receiver": {"type": "webhook", "url": "https://evil.com/hook"}}
        ]}
        sub, recv, warns = expand_routing_overrides(
            "db-a", routing, allowed_domains=["*.example.com"])
        assert sub == []
        assert any("not in allowed_domains" in w for w in warns)


class TestBuildEnforcedRoutes:
    """_build_enforced_routes() 單元測試。"""

    def test_none_enforced_returns_empty(self):
        """None enforced routing 回傳空結果。"""
        routes, receivers, warnings = _build_enforced_routes(None, {})
        assert routes == []
        assert receivers == []
        assert warnings == []

    def test_empty_dict_returns_empty(self):
        """空 dict enforced routing 回傳空結果。"""
        routes, receivers, warnings = _build_enforced_routes({}, {})
        assert routes == []

    def test_enforced_route_loaded_from_defaults(self, config_dir):
        """_routing_enforced in _ prefixed file → loaded."""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_enforced:
  enabled: true
  receiver:
    type: "webhook"
    url: "https://noc.example.com/alerts"
  match:
    - 'severity="critical"'
tenants:
  db-a:
    mysql_connections: "70"
""")
        _, _, _, enforced, _mc = load_tenant_configs(config_dir)
        assert enforced is not None
        assert enforced["enabled"]
        assert enforced["receiver"]["url"] == "https://noc.example.com/alerts"

    def test_enforced_disabled_returns_none(self, config_dir):
        """_routing_enforced with enabled: false → None."""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_enforced:
  enabled: false
  receiver:
    type: "webhook"
    url: "https://noc.example.com/alerts"
tenants:
  db-a:
    mysql_connections: "70"
""")
        _, _, _, enforced, _mc = load_tenant_configs(config_dir)
        assert enforced is None

    def test_enforced_ignored_in_tenant_file(self, config_dir):
        """_routing_enforced in non-_ file → ignored with warning."""
        write_yaml(config_dir, "db-a.yaml", """
_routing_enforced:
  enabled: true
  receiver:
    type: "webhook"
    url: "https://evil.com/alerts"
tenants:
  db-a:
    mysql_connections: "70"
""")
        _, _, _, enforced, _mc = load_tenant_configs(config_dir)
        assert enforced is None

    def test_enforced_route_first_with_continue(self):
        """Enforced route appears BEFORE tenant routes with continue: true."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
            "match": ['severity="critical"'],
        }
        tenant_cfg = {
            "db-a": {"receiver": {"type": "webhook", "url": "https://tenant.example.com/alerts"}},
        }
        routes, receivers, warnings = generate_routes(
            tenant_cfg, enforced_routing=enforced)
        assert len(routes) == 2
        # First route is platform enforced
        assert routes[0]["receiver"] == "platform-enforced"
        assert routes[0]["continue"]
        assert routes[0]["matchers"] == ['severity="critical"']
        # Second route is tenant
        assert routes[1]["receiver"] == "tenant-db-a"
        # Two receivers
        assert len(receivers) == 2
        assert receivers[0]["name"] == "platform-enforced"
        assert receivers[1]["name"] == "tenant-db-a"

    def test_enforced_route_no_matchers(self):
        """Enforced route without match → catches all alerts (NOC sees everything)."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
        }
        routes, _, _ = generate_routes({}, enforced_routing=enforced)
        assert len(routes) == 1
        assert routes[0]["receiver"] == "platform-enforced"
        assert routes[0]["continue"]
        assert "matchers" not in routes[0]

    def test_enforced_with_timing_guardrails(self):
        """Enforced route timing params are clamped by guardrails."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
            "group_wait": "1s",       # below min → clamped to 5s
            "repeat_interval": "100h",  # above max → clamped to 72h
        }
        routes, _, warnings = generate_routes({}, enforced_routing=enforced)
        assert routes[0]["group_wait"] == "5s"
        assert routes[0]["repeat_interval"] == "72h"
        assert len(warnings) >= 2

    def test_enforced_missing_receiver_skipped(self):
        """Enforced without receiver → warning, no route."""
        enforced = {"enabled": True, "match": ['severity="critical"']}
        routes, _, warnings = generate_routes({}, enforced_routing=enforced)
        assert len(routes) == 0
        assert any("missing 'receiver'" in w for w in warnings)

    def test_enforced_domain_policy_blocks(self):
        """Enforced receiver blocked by domain policy → warning, no route."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://evil.com/alerts"},
        }
        routes, _, warnings = generate_routes(
            {}, enforced_routing=enforced, allowed_domains=["*.example.com"])
        assert len(routes) == 0
        assert any("blocked by domain policy" in w for w in warnings)

    def test_enforced_none_no_effect(self):
        """enforced_routing=None → no platform route (backward compat)."""
        tenant_cfg = {
            "db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com/alerts"}},
        }
        routes, receivers, _ = generate_routes(tenant_cfg, enforced_routing=None)
        assert len(routes) == 1
        assert routes[0]["receiver"] == "tenant-db-a"

    def test_enforced_e2e_from_config_dir(self, config_dir):
        """End-to-end: load from config dir → generate routes with enforced first."""
        write_yaml(config_dir, "_defaults.yaml", """
_routing_enforced:
  enabled: true
  receiver:
    type: "webhook"
    url: "https://noc.example.com/alerts"
  match:
    - 'severity="critical"'
  group_wait: "15s"
_routing_defaults:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp.example.com:587"
    from: "alerts@example.com"
  group_wait: "30s"
tenants:
  db-a:
    mysql_connections: "70"
  db-b:
    mysql_connections: "80"
""")
        routing, dedup, _sw, enforced, _mc = load_tenant_configs(config_dir)
        assert enforced is not None
        routes, receivers, _ = generate_routes(
            routing, enforced_routing=enforced)
        # First route = platform enforced, then 2 tenant routes
        assert len(routes) == 3
        assert routes[0]["receiver"] == "platform-enforced"
        assert routes[0]["continue"]
        assert routes[0]["group_wait"] == "15s"
        # Tenant routes sorted
        assert routes[1]["receiver"] == "tenant-db-a"
        assert routes[2]["receiver"] == "tenant-db-b"


class TestBuildTenantRoutes:
    """_build_tenant_routes() 單元測試。"""

    def test_empty_configs(self):
        """空 routing configs 回傳空結果。"""
        routes, receivers, warnings = _build_tenant_routes({})
        assert routes == []

    def test_single_tenant(self):
        """單一 tenant 正確產生 route + receiver。"""
        configs = {"db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com"}}}
        routes, receivers, warnings = _build_tenant_routes(configs)
        assert len(routes) == 1
        assert routes[0]["receiver"] == "tenant-db-a"


# ============================================================
# _parse_config_files (單元測試)
# ============================================================
class TestParseConfigFiles:
    """_parse_config_files() YAML 解析單元測試。"""

    def test_basic_tenant(self, config_dir):
        """基本 tenant 解析：all_tenants + tenant_keys 正確填充。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", keys={"mysql_connections": "70"}))
        parsed = _parse_config_files(config_dir)
        assert "db-a" in parsed["all_tenants"]
        assert "mysql_connections" in parsed["tenant_keys"]["db-a"]

    def test_routing_defaults_from_underscore_file(self, config_dir):
        """_routing_defaults 僅從 _ 開頭檔案讀取。"""
        write_yaml(config_dir, "_defaults.yaml",
                   "_routing_defaults:\n  group_wait: '30s'\n")
        parsed = _parse_config_files(config_dir)
        assert parsed["routing_defaults"]["group_wait"] == "30s"

    def test_routing_defaults_ignored_from_tenant_file(self, config_dir):
        """_routing_defaults 在 tenant 檔案中被忽略（產生警告）。"""
        write_yaml(config_dir, "db-a.yaml",
                   "_routing_defaults:\n  group_wait: '10s'\ntenants:\n  db-a:\n    key: '1'\n")
        parsed = _parse_config_files(config_dir)
        assert parsed["routing_defaults"] == {}

    def test_enforced_routing_enabled(self, config_dir):
        """_routing_enforced 啟用時正確載入。"""
        content = (
            "_routing_enforced:\n"
            "  enabled: true\n"
            "  receiver:\n"
            "    type: webhook\n"
            "    url: https://noc.example.com/alert\n"
        )
        write_yaml(config_dir, "_defaults.yaml", content)
        parsed = _parse_config_files(config_dir)
        assert parsed["enforced_routing"] is not None
        assert parsed["enforced_routing"]["receiver"]["type"] == "webhook"

    def test_enforced_routing_disabled(self, config_dir):
        """_routing_enforced 未啟用時為 None。"""
        content = (
            "_routing_enforced:\n"
            "  enabled: false\n"
            "  receiver:\n"
            "    type: webhook\n"
            "    url: https://noc.example.com/alert\n"
        )
        write_yaml(config_dir, "_defaults.yaml", content)
        parsed = _parse_config_files(config_dir)
        assert parsed["enforced_routing"] is None

    def test_severity_dedup_default_enable(self, config_dir):
        """未指定 _severity_dedup 時預設 enable。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", keys={"key": "1"}))
        parsed = _parse_config_files(config_dir)
        assert parsed["dedup_configs"].get("db-a") == "enable"

    def test_severity_dedup_explicit_disable(self, config_dir):
        """明確 disable _severity_dedup。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", keys={"key": "1"}, severity_dedup="disable"))
        parsed = _parse_config_files(config_dir)
        assert parsed["dedup_configs"].get("db-a") == "disable"

    def test_routing_disable_string(self, config_dir):
        """_routing 為 'disable' 字串時加入 disabled_tenants。"""
        content = "tenants:\n  db-a:\n    _routing: disable\n    key: '1'\n"
        write_yaml(config_dir, "db-a.yaml", content)
        parsed = _parse_config_files(config_dir)
        assert "db-a" in parsed["disabled_tenants"]
        assert "db-a" not in parsed["explicit_routing"]

    def test_defaults_keys_collected(self, config_dir):
        """defaults 區塊的 key 被收集到 defaults_keys。"""
        content = "defaults:\n  container_cpu: 80\n  container_memory: 85\n"
        write_yaml(config_dir, "_defaults.yaml", content)
        parsed = _parse_config_files(config_dir)
        assert "container_cpu" in parsed["defaults_keys"]
        assert "container_memory" in parsed["defaults_keys"]

    def test_metadata_extraction(self, config_dir):
        """_metadata 正確擷取並展開 {{tenant}}。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", metadata={
                       "runbook_url": "https://wiki.example.com/{{tenant}}",
                       "owner": "dba-team",
                   }))
        parsed = _parse_config_files(config_dir)
        assert "db-a" in parsed["metadata_configs"]
        assert "db-a" in parsed["metadata_configs"]["db-a"]["runbook_url"]

    def test_metadata_without_placeholder(self, config_dir):
        """_metadata 無 {{tenant}} 佔位符時原值保留。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", metadata={
                       "owner": "dba-team",
                       "tier": "production",
                   }))
        parsed = _parse_config_files(config_dir)
        meta = parsed["metadata_configs"]["db-a"]
        assert meta["owner"] == "dba-team"
        assert meta["tier"] == "production"

    def test_metadata_with_keys_and_routing(self, config_dir):
        """_metadata 與 keys、routing 共存時全部正確解析。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a",
                                    keys={"mysql_connections": "70"},
                                    routing={"receiver": make_receiver()},
                                    metadata={"owner": "dba-team"}))
        parsed = _parse_config_files(config_dir)
        assert "db-a" in parsed["all_tenants"]
        assert "mysql_connections" in parsed["tenant_keys"].get("db-a", {})
        assert "db-a" in parsed["explicit_routing"]
        assert parsed["metadata_configs"]["db-a"]["owner"] == "dba-team"

    def test_empty_file_skipped(self, config_dir):
        """空 YAML 檔案被安全跳過。"""
        write_yaml(config_dir, "empty.yaml", "")
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", keys={"key": "1"}))
        parsed = _parse_config_files(config_dir)
        assert "db-a" in parsed["all_tenants"]

    def test_hidden_files_skipped(self, config_dir):
        """以 . 開頭的檔案被跳過。"""
        write_yaml(config_dir, ".hidden.yaml",
                   make_tenant_yaml("hidden", keys={"key": "1"}))
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", keys={"key": "1"}))
        parsed = _parse_config_files(config_dir)
        assert "hidden" not in parsed["all_tenants"]
        assert "db-a" in parsed["all_tenants"]


# ============================================================
# _merge_tenant_routing (單元測試)
# ============================================================
class TestMergeTenantRouting:
    """_merge_tenant_routing() routing defaults 合併測試。"""

    def test_explicit_routing_used(self):
        """有明確 routing 的 tenant 使用自身設定。"""
        parsed = {
            "all_tenants": ["db-a"],
            "disabled_tenants": set(),
            "explicit_routing": {
                "db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com"}},
            },
        }
        result = _merge_tenant_routing(parsed, {})
        assert "db-a" in result
        assert result["db-a"]["receiver"]["url"] == "https://a.example.com"

    def test_defaults_applied_when_no_explicit(self):
        """無明確 routing 但有 defaults 時，繼承 defaults。"""
        parsed = {
            "all_tenants": ["db-a"],
            "disabled_tenants": set(),
            "explicit_routing": {},
        }
        defaults = {"receiver": {"type": "webhook", "url": "https://default.example.com"}}
        result = _merge_tenant_routing(parsed, defaults)
        assert "db-a" in result
        assert result["db-a"]["receiver"]["url"] == "https://default.example.com"

    def test_disabled_tenant_skipped(self):
        """disabled tenant 不出現在結果中。"""
        parsed = {
            "all_tenants": ["db-a", "db-b"],
            "disabled_tenants": {"db-a"},
            "explicit_routing": {
                "db-b": {"receiver": {"type": "webhook", "url": "https://b.example.com"}},
            },
        }
        result = _merge_tenant_routing(parsed, {})
        assert "db-a" not in result
        assert "db-b" in result

    def test_no_defaults_no_routing_empty(self):
        """無 defaults 且無明確 routing 的 tenant 不產生結果。"""
        parsed = {
            "all_tenants": ["db-a"],
            "disabled_tenants": set(),
            "explicit_routing": {},
        }
        result = _merge_tenant_routing(parsed, {})
        assert result == {}

    def test_duplicate_tenants_deduped(self):
        """重複 tenant 名稱只出現一次。"""
        parsed = {
            "all_tenants": ["db-a", "db-a", "db-a"],
            "disabled_tenants": set(),
            "explicit_routing": {
                "db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com"}},
            },
        }
        result = _merge_tenant_routing(parsed, {})
        assert len(result) == 1


# ============================================================
# load_tenant_configs (整合測試，使用臨時目錄)
# ============================================================
class TestLoadTenantConfigs:
    """load_tenant_configs() 整合測試。"""

    def test_basic_tenant_routing(self, config_dir):
        """基本 tenant routing 正確載入。"""
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing))
        routing_configs, dedup, warnings, enforced, metadata = load_tenant_configs(config_dir)
        assert "db-a" in routing_configs

    def test_reserved_file_skipped(self, config_dir):
        """以 _ 開頭的檔案包含 _routing_defaults 仍被讀取。"""
        write_yaml(config_dir, "_defaults.yaml",
                   "_routing_defaults:\n  group_wait: '30s'\n")
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing))
        routing_configs, dedup, warnings, enforced, metadata = load_tenant_configs(config_dir)
        assert "db-a" in routing_configs

    def test_empty_dir(self, config_dir):
        """空目錄回傳空結果。"""
        routing_configs, dedup, warnings, enforced, metadata = load_tenant_configs(config_dir)
        assert routing_configs == {}

    def test_severity_dedup(self, config_dir):
        """_severity_dedup 正確載入。"""
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing, severity_dedup="disable"))
        routing_configs, dedup, warnings, enforced, metadata = load_tenant_configs(config_dir)
        assert dedup.get("db-a") == "disable"

    def test_routing_and_default_dedup(self, config_dir):
        """有 _routing → routing dict 有值; 無 _severity_dedup → default enable。"""
        write_yaml(config_dir, "db-a.yaml", """
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_wait: "30s"
""")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert routing["db-a"]["receiver"]["type"] == "webhook"
        assert routing["db-a"]["receiver"]["url"] == "https://webhook.example.com/alerts"
        assert dedup["db-a"] == "enable"

    def test_no_routing_still_tracks_dedup(self, config_dir):
        """無 _routing 的 tenant 仍出現在 dedup dict 中。"""
        write_yaml(config_dir, "db-b.yaml", """
tenants:
  db-b:
    mysql_connections: "80"
""")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert len(routing) == 0
        assert dedup["db-b"] == "enable"

    def test_multiple_files(self, config_dir):
        """載入多個配置檔案。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://a.example.com'\n")
        write_yaml(config_dir, "db-b.yaml", "tenants:\n  db-b:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://b.example.com'\n")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert len(routing) == 2
        assert len(dedup) == 2

    def test_skips_dotfiles(self, config_dir):
        """跳過隱藏檔案（以點開頭）。"""
        write_yaml(config_dir, ".hidden.yaml", "tenants:\n  hidden:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'x'\n")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert len(routing) == 0
        assert len(dedup) == 0

    def test_dedup_disable_explicit(self, config_dir):
        """顯式禁用 severity dedup。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    _severity_dedup: 'disable'\n")
        _, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert dedup["db-a"] == "disable"

    def test_dedup_mixed_tenants(self, config_dir):
        """enable (default) + disable → dedup dict 正確區分。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        write_yaml(config_dir, "db-b.yaml", "tenants:\n  db-b:\n    _severity_dedup: 'disable'\n")
        _, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert dedup["db-a"] == "enable"
        assert dedup["db-b"] == "disable"

    def test_dedup_disable_synonyms(self, config_dir):
        """disabled / off / false 全視同 disable（與 Go 行為一致）。"""
        write_yaml(config_dir, "t.yaml", """
tenants:
  t1:
    _severity_dedup: "disabled"
  t2:
    _severity_dedup: "off"
  t3:
    _severity_dedup: "false"
""")
        _, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        for t in ("t1", "t2", "t3"):
            assert dedup[t] == "disable", f"{t}"

    def test_dedup_only_no_routing_produces_inhibit(self, config_dir):
        """無 _routing 的 tenant 仍可產出 inhibit rule（E2E 驗證）。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        routing, dedup, _sw, _er, _mc = load_tenant_configs(config_dir)
        assert len(routing) == 0
        rules, _ = generate_inhibit_rules(dedup)
        assert len(rules) == 1

    def test_old_string_receiver_rejected(self):
        """v1.2.0 舊格式 (receiver: URL string) 在 generate_routes 應被跳過。"""
        cfg = {"db-a": {"receiver": "https://example.com", "group_wait": "30s"}}
        routes, _, warnings = generate_routes(cfg)
        assert len(routes) == 0
        assert any("must be an object" in w for w in warnings)


def _wy(tmpdir, filename, data):
    """write_yaml wrapper that accepts dict and auto-dumps to YAML string."""
    write_yaml(tmpdir, filename, yaml.dump(data, default_flow_style=False))


# ============================================================
# ADR-007: Routing Profiles (v2.1.0)
# ============================================================
class TestRoutingProfiles:
    """Tests for _routing_profiles.yaml parsing and profile merge."""

    def test_profile_parsed_from_correct_file(self):
        """routing_profiles parsed only from _routing_profiles.yaml."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-sre": {
                        "receiver": make_receiver("webhook"),
                        "group_wait": "30s",
                    }
                }
            })
            _wy(d,"_defaults.yaml", {
                "defaults": {"cpu": 90}
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"_routing_profile": "team-sre", "cpu": "80"}}
            })
            parsed = _parse_config_files(d)
            assert "team-sre" in parsed["routing_profiles"]
            assert parsed["tenant_profile_refs"]["db-a"] == "team-sre"

    def test_profile_ignored_in_non_reserved_file(self):
        """routing_profiles in a tenant file should be warned and ignored."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"db-a.yaml", {
                "routing_profiles": {"fake": {}},
                "tenants": {"db-a": {"cpu": "80"}}
            })
            parsed = _parse_config_files(d)
            assert len(parsed["routing_profiles"]) == 0

    def test_profile_merge_into_routing(self):
        """Profile config should be merged between defaults and tenant _routing."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_defaults.yaml", {
                "defaults": {"cpu": 90},
                "_routing_defaults": {
                    "group_wait": "10s",
                    "group_interval": "1m",
                },
            })
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-dba": {
                        "receiver": make_receiver("webhook"),
                        "group_wait": "30s",
                        "repeat_interval": "4h",
                    }
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {
                    "_routing_profile": "team-dba",
                    "_routing": {"repeat_interval": "2h"},  # tenant override
                }}
            })
            routing, _, _, _, _ = load_tenant_configs(d)
            rc = routing["db-a"]
            # group_wait: profile overrides defaults
            assert rc["group_wait"] == "30s"
            # group_interval: from defaults (profile didn't set it)
            assert rc["group_interval"] == "1m"
            # repeat_interval: tenant overrides profile
            assert rc["repeat_interval"] == "2h"

    def test_profile_without_tenant_override(self):
        """Tenant with only profile ref and no explicit _routing."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-sre": {
                        "receiver": make_receiver("slack"),
                        "group_wait": "45s",
                    }
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"_routing_profile": "team-sre"}}
            })
            routing, _, _, _, _ = load_tenant_configs(d)
            assert "db-a" in routing
            assert routing["db-a"]["group_wait"] == "45s"

    def test_unknown_profile_ref_warns(self):
        """Reference to nonexistent profile should produce warning."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"_routing_profile": "nonexistent"}}
            })
            parsed = _parse_config_files(d)
            warnings = _validate_profile_refs(parsed)
            assert any("nonexistent" in w for w in warnings)

    def test_multiple_tenants_share_profile(self):
        """Multiple tenants referencing same profile get same base config."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-shared": {
                        "receiver": make_receiver("webhook"),
                        "group_wait": "20s",
                    }
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"_routing_profile": "team-shared"}}
            })
            _wy(d,"db-b.yaml", {
                "tenants": {"db-b": {"_routing_profile": "team-shared"}}
            })
            routing, _, _, _, _ = load_tenant_configs(d)
            assert routing["db-a"]["group_wait"] == "20s"
            assert routing["db-b"]["group_wait"] == "20s"

    def test_tenant_without_profile_unaffected(self):
        """Tenants not using profiles should behave exactly as before."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_defaults.yaml", {
                "_routing_defaults": {
                    "receiver": make_receiver("webhook"),
                    "group_wait": "15s",
                }
            })
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-other": {"group_wait": "99s"}
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"cpu": "80"}}
            })
            routing, _, _, _, _ = load_tenant_configs(d)
            # Should use defaults, not profile
            assert routing["db-a"]["group_wait"] == "15s"


# ============================================================
# ADR-007: Domain Policies (v2.1.0)
# ============================================================
class TestDomainPolicies:
    """Tests for _domain_policy.yaml parsing and validation."""

    def test_policy_parsed_from_correct_file(self):
        """domain_policies parsed only from _domain_policy.yaml."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_domain_policy.yaml", {
                "domain_policies": {
                    "finance": {
                        "tenants": ["db-a"],
                        "constraints": {
                            "forbidden_receiver_types": ["slack"],
                        }
                    }
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"cpu": "80"}}
            })
            parsed = _parse_config_files(d)
            assert "finance" in parsed["domain_policies"]

    def test_policy_ignored_in_wrong_file(self):
        """domain_policies in a tenant file should be warned and ignored."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"db-a.yaml", {
                "domain_policies": {"fake": {}},
                "tenants": {"db-a": {"cpu": "80"}}
            })
            parsed = _parse_config_files(d)
            assert len(parsed["domain_policies"]) == 0

    def test_forbidden_receiver_type(self):
        """Domain policy should flag forbidden receiver types."""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"},
                "group_wait": "30s",
            }
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {
                    "forbidden_receiver_types": ["slack", "webhook"],
                }
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert any("slack" in m and "forbidden" in m for m in msgs)

    def test_allowed_receiver_type_violation(self):
        """Receiver type not in allowed list should be flagged."""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "slack"},
            }
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {
                    "allowed_receiver_types": ["pagerduty", "email"],
                }
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert any("not in allowed types" in m for m in msgs)

    def test_allowed_receiver_type_passes(self):
        """Compliant receiver type should not produce warnings."""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "pagerduty"},
            }
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {
                    "allowed_receiver_types": ["pagerduty", "email"],
                }
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert len(msgs) == 0

    def test_max_repeat_interval_exceeded(self):
        """repeat_interval exceeding max should be flagged."""
        routing_configs = {
            "db-a": {"repeat_interval": "12h"},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {"max_repeat_interval": "1h"},
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert any("repeat_interval" in m and "exceeds" in m for m in msgs)

    def test_max_repeat_interval_within_limit(self):
        """repeat_interval within limit should not be flagged."""
        routing_configs = {
            "db-a": {"repeat_interval": "30m"},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {"max_repeat_interval": "1h"},
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert len(msgs) == 0

    def test_min_group_wait_violation(self):
        """group_wait below minimum should be flagged."""
        routing_configs = {
            "db-a": {"group_wait": "5s"},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {"min_group_wait": "30s"},
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert any("group_wait" in m and "below minimum" in m for m in msgs)

    def test_enforce_group_by_missing_labels(self):
        """Missing required group_by labels should be flagged."""
        routing_configs = {
            "db-a": {"group_by": ["alertname"]},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {
                    "enforce_group_by": ["alertname", "tenant", "severity"],
                },
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert any("group_by" in m and "missing" in m for m in msgs)

    def test_enforce_group_by_all_present(self):
        """All required group_by labels present should not be flagged."""
        routing_configs = {
            "db-a": {"group_by": ["alertname", "tenant", "severity", "extra"]},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {
                    "enforce_group_by": ["alertname", "tenant", "severity"],
                },
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert len(msgs) == 0

    def test_strict_mode_produces_errors(self):
        """Strict mode should produce ERROR instead of WARN."""
        routing_configs = {
            "db-a": {"receiver": {"type": "slack"}},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {"forbidden_receiver_types": ["slack"]},
            }
        }
        msgs = check_domain_policies(routing_configs, policies, strict=True)
        assert any(m.strip().startswith("ERROR") for m in msgs)

    def test_tenant_not_in_policy_skipped(self):
        """Tenants not listed in policy should not be checked."""
        routing_configs = {
            "db-a": {"receiver": {"type": "slack"}},
            "db-b": {"receiver": {"type": "slack"}},
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],  # only db-a
                "constraints": {"forbidden_receiver_types": ["slack"]},
            }
        }
        msgs = check_domain_policies(routing_configs, policies)
        # Only db-a should be flagged
        assert sum(1 for m in msgs if "db-a" in m) >= 1
        assert sum(1 for m in msgs if "db-b" in m) == 0

    def test_multiple_policies_multiple_violations(self):
        """Multiple policies can flag the same tenant independently."""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "slack"},
                "repeat_interval": "24h",
            },
        }
        policies = {
            "finance": {
                "tenants": ["db-a"],
                "constraints": {"forbidden_receiver_types": ["slack"]},
            },
            "sla-gold": {
                "tenants": ["db-a"],
                "constraints": {"max_repeat_interval": "1h"},
            },
        }
        msgs = check_domain_policies(routing_configs, policies)
        assert len(msgs) >= 2

    def test_end_to_end_profile_plus_policy(self):
        """Integration: profile provides routing, policy validates it."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d,"_routing_profiles.yaml", {
                "routing_profiles": {
                    "team-sre": {
                        "receiver": make_receiver("slack"),
                        "group_wait": "30s",
                        "repeat_interval": "4h",
                    }
                }
            })
            _wy(d,"_domain_policy.yaml", {
                "domain_policies": {
                    "finance": {
                        "tenants": ["db-a"],
                        "constraints": {
                            "forbidden_receiver_types": ["slack"],
                            "max_repeat_interval": "1h",
                        }
                    }
                }
            })
            _wy(d,"db-a.yaml", {
                "tenants": {"db-a": {"_routing_profile": "team-sre"}}
            })
            _, _, warnings, _, _ = load_tenant_configs(d)
            # Should have warnings about slack forbidden + repeat_interval
            slack_warns = [w for w in warnings if "slack" in w and "forbidden" in w]
            repeat_warns = [w for w in warnings if "repeat_interval" in w]
            assert len(slack_warns) >= 1
            assert len(repeat_warns) >= 1


# ============================================================
# ADR-007 --strict: violation message contract (unit level)
# ============================================================
class TestDomainPolicyStrictMessages:
    """strict=True 訊息契約：含實際值 vs 域限制 + 修法提示；非 strict 訊息不變。"""

    _ROUTING = {
        "tenant-fin": {
            "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"},
            "group_by": ["alertname"],
            "group_wait": "5s",
            "repeat_interval": "24h",
        }
    }
    _POLICIES = {
        "finance": {
            "tenants": ["tenant-fin"],
            "constraints": {
                "forbidden_receiver_types": ["slack"],
                "allowed_receiver_types": ["email", "pagerduty"],
                "enforce_group_by": ["tenant", "alertname"],
                "max_repeat_interval": "1h",
                "min_group_wait": "30s",
            },
        }
    }

    def test_strict_messages_carry_fix_hint(self):
        """strict 下每條違規都帶修法提示。"""
        msgs = check_domain_policies(self._ROUTING, self._POLICIES, strict=True)
        assert len(msgs) == 5
        assert all(m.lstrip().startswith("ERROR:") for m in msgs)
        assert all("fix:" in m for m in msgs)
        # 每條都含 tenant + constraint 上下文
        assert all("tenant-fin" in m and "finance" in m for m in msgs)

    def test_strict_messages_show_actual_vs_limit(self):
        """strict 訊息含實際值 vs 域限制。"""
        msgs = check_domain_policies(self._ROUTING, self._POLICIES, strict=True)
        joined = "\n".join(msgs)
        # repeat_interval: 實際 24h vs 上限 1h
        assert "'24h' exceeds max '1h'" in joined
        # group_wait: 實際 5s vs 下限 30s
        assert "'5s' below minimum '30s'" in joined
        # group_by: 缺 tenant，並提示 policy 要求集合
        assert "['tenant']" in joined
        # receiver: 實際 slack vs forbidden/allowed 集合
        assert "'slack' is forbidden" in joined
        assert "['email', 'pagerduty']" in joined

    def test_nonstrict_messages_unchanged(self):
        """非 strict 訊息維持既有格式（無 fix 提示、WARN 前綴）——向後相容。"""
        msgs = check_domain_policies(self._ROUTING, self._POLICIES)
        assert len(msgs) == 5
        assert all(m.lstrip().startswith("WARN:") for m in msgs)
        assert all("fix:" not in m for m in msgs)


# ============================================================
# ADR-007 --strict: CLI-level exit-code contract
# ============================================================
class TestStrictValidateCLI:
    """generate_alertmanager_routes.py --validate [--strict] CLI 契約。

    strict 下 domain-policy 違規 exit 1（EXIT_VIOLATION）、合規 exit 0；
    非 strict 行為完全不變（違規仍 WARN + exit 0）。
    """

    _SCRIPT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "scripts", "tools", "ops", "generate_alertmanager_routes.py")

    @staticmethod
    def _write_confd(d, constraints, routing_defaults=None):
        """建一個含 domain policy 的最小 conf.d。"""
        defaults = routing_defaults or {
            "receiver": {"type": "slack",
                         "api_url": "https://hooks.slack.com/services/T/B/X"},
            "group_by": ["alertname"],
            "group_wait": "5s",
            "repeat_interval": "24h",
        }
        _wy(d, "_defaults.yaml", {
            "defaults": {"cpu": 80},
            "_routing_defaults": defaults,
        })
        _wy(d, "_domain_policy.yaml", {
            "domain_policies": {
                "finance": {
                    "tenants": ["tenant-fin"],
                    "constraints": constraints,
                }
            }
        })
        _wy(d, "tenant-fin.yaml", {"tenants": {"tenant-fin": {"cpu": "90"}}})

    def _run(self, config_dir, *extra_flags):
        import subprocess
        return subprocess.run(
            [sys.executable, self._SCRIPT, "--config-dir", config_dir,
             "--dry-run", *extra_flags],
            capture_output=True, text=True, encoding="utf-8", timeout=60)

    def test_nonstrict_validate_exit0_on_violation(self):
        """向後相容：非 strict 下違規只 WARN，--validate 仍 exit 0。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_confd(d, {"forbidden_receiver_types": ["slack"]})
            result = self._run(d, "--validate")
            assert result.returncode == 0, result.stderr
            assert "WARN" in result.stderr
            assert "ERROR" not in result.stderr
            assert "OK: all configs valid" in result.stdout

    def test_strict_validate_exit1_on_violation(self):
        """strict 下違規變 ERROR 且 --validate exit 1。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_confd(d, {"forbidden_receiver_types": ["slack"]})
            result = self._run(d, "--validate", "--strict")
            assert result.returncode == 1, result.stdout + result.stderr
            assert "ERROR" in result.stderr
            assert "fix:" in result.stderr
            assert "FAIL" in result.stderr

    def test_strict_validate_exit0_when_compliant(self):
        """strict 下全 constraint 合規 exit 0。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_confd(
                d,
                {
                    "allowed_receiver_types": ["email"],
                    "forbidden_receiver_types": ["slack", "webhook"],
                    "enforce_group_by": ["tenant", "alertname"],
                    "max_repeat_interval": "1h",
                    "min_group_wait": "30s",
                },
                routing_defaults={
                    "receiver": {"type": "email", "to": ["oncall@example.com"],
                                 "from": "alerting@example.com",
                                 "smarthost": "smtp.example.com:587"},
                    "group_by": ["tenant", "alertname"],
                    "group_wait": "30s",
                    "repeat_interval": "30m",
                })
            result = self._run(d, "--validate", "--strict")
            assert result.returncode == 0, result.stdout + result.stderr
            assert "OK: all configs valid" in result.stdout

    def test_strict_render_mode_aborts_on_violation(self):
        """strict 不限 --validate：render 模式下違規也報錯終止（ADR-007）。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_confd(d, {"forbidden_receiver_types": ["slack"]})
            result = self._run(d, "--strict")
            assert result.returncode == 1, result.stdout + result.stderr
            assert "FAIL" in result.stderr

    @pytest.mark.parametrize("constraints,expect", [
        ({"forbidden_receiver_types": ["slack"]}, "is forbidden"),
        ({"allowed_receiver_types": ["email", "pagerduty"]},
         "not in allowed types"),
        ({"enforce_group_by": ["tenant", "alertname"]},
         "group_by missing required labels"),
        ({"max_repeat_interval": "1h"}, "exceeds max"),
        ({"min_group_wait": "30s"}, "below minimum"),
    ], ids=["forbidden-type", "allowed-type", "enforce-group-by",
            "max-repeat-interval", "min-group-wait"])
    def test_strict_each_constraint_blocks(self, constraints, expect):
        """三類 constraint（receiver types / group_by / 時序×2）各自可擋。"""
        with tempfile.TemporaryDirectory() as d:
            self._write_confd(d, constraints)
            result = self._run(d, "--validate", "--strict")
            assert result.returncode == 1, result.stdout + result.stderr
            assert expect in result.stderr
