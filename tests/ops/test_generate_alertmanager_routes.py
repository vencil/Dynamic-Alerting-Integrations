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

    def test_defaults_keys_excluded(self):
        """defaults 中定義的 key 不在 tenant 驗證範圍。"""
        keys = {"custom_metric"}
        defaults_keys = {"custom_metric"}
        warnings = validate_tenant_keys("db-a", keys, defaults_keys)
        assert warnings == []


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
