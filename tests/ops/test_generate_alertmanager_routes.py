"""Unit tests for generate_alertmanager_routes.py.

涵蓋核心函式：
- build_receiver_config: receiver 結構驗證與 AM config 產生
- validate_receiver_domains: domain allowlist SSRF 防護
- generate_inhibit_rules: severity dedup inhibit rule 產生
- generate_routes: 完整 route tree 產生（含 enforced + tenant + overrides）
- merge_routing_with_defaults: routing defaults 三態繼承
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
    load_tenant_tree,
    _extract_host,
    load_policy,
    PolicyInputError,
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

    @pytest.mark.parametrize("rtype", ["WEBHOOK", "Webhook", " webhook", "webhook "])
    def test_type_is_matched_exactly(self, rtype):
        """type 欄位不做大小寫或前後空白正規化，與 schema `const` 及 Go guard 一致（#2180）。"""
        receiver = {"type": rtype, "url": "https://example.com"}
        config, warnings = build_receiver_config(receiver, "db-a")
        assert config is None
        assert any("unknown receiver type" in w for w in warnings)

    @pytest.mark.parametrize("receiver", [
        {"type": "webhook", "url": "https://h.example/x\n"},
        {"type": "email", "to": ["a@example.com"], "smarthost": "smtp:25\n",
         "from": "alerts@example.com"},
    ], ids=["url", "smarthost"])
    def test_format_rejects_final_newline(self, receiver):
        """值結尾帶換行時產生器必須擋下（#2180）：Python `re.search` 的 `$` 也會在結尾換行前成立，
        所以格式檢查只靠 `re.fullmatch`；這一列不經 schema，單獨守住產生器那一方。"""
        config, warnings = build_receiver_config(receiver, "t1")
        assert config is None
        assert any("is not in the format" in w for w in warnings)

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

    def test_pagerduty_missing_both_keys(self):
        """PagerDuty service_key 與 routing_key 都沒給 → WARN 並略過（#2137）。"""
        cfg, warnings = build_receiver_config({"type": "pagerduty"}, "t")
        assert cfg is None
        assert any("requires exactly one of 'service_key' or 'routing_key', none is set"
                   in w for w in warnings)

    def test_pagerduty_routing_key_only_is_valid(self):
        """只給 routing_key（Events API v2）是合法的，會進 AM config（#2137）。"""
        cfg, warnings = build_receiver_config({"type": "pagerduty", "routing_key": "r"}, "t")
        assert warnings == []
        assert cfg == {"pagerduty_configs": [{"routing_key": "r"}]}

    def test_pagerduty_both_keys_rejected(self):
        """兩個都給 → WARN 並略過；訊息說明 AM 會走 v1 並忽略 routing_key（#2137）。"""
        cfg, warnings = build_receiver_config(
            {"type": "pagerduty", "service_key": "k", "routing_key": "r"}, "t")
        assert cfg is None
        assert any("not both" in w and "v1" in w and "skipping" in w for w in warnings)

    def test_pagerduty_empty_key_counts_as_unset(self):
        """空字串視同沒給，與必填欄位的判斷一致。"""
        cfg, warnings = build_receiver_config(
            {"type": "pagerduty", "service_key": "", "routing_key": "r"}, "t")
        assert warnings == []
        assert cfg["pagerduty_configs"][0]["routing_key"] == "r"


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
        routes, receivers, warnings = generate_routes(cfg, allowed_domains=["*.example.com"], tenants=())
        assert len(routes) == 0
        assert any("not in allowed_domains" in w for w in warnings)

    def test_allowed_domain_generates_route(self):
        """允許的網域產生路由。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://hook.example.com/x"}}}
        routes, receivers, _ = generate_routes(cfg, allowed_domains=["*.example.com"], tenants=())
        assert len(routes) == 1

    def test_no_policy_no_filtering(self):
        """No allowed_domains → backward compatible, all pass。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://any.com/x"}}}
        routes, _, _ = generate_routes(cfg, allowed_domains=None, tenants=())
        assert len(routes) == 1

    def test_load_policy_from_file(self, config_dir):
        """從檔案載入 policy 的 allowed_domains。"""
        path = os.path.join(config_dir, "policy.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("allowed_domains:\n  - '*.example.com'\n  - hooks.slack.com\n")
        os.chmod(path, 0o600)
        domains = load_policy(path)
        assert domains == ["*.example.com", "hooks.slack.com"]

    def test_load_policy_missing_file_raises(self):
        """供了 --policy 但檔案不存在 → 拋，不可回空列表。

        ⛔ 這條原本斷言 `== []`（#1556）。空 allowlist 與「沒有要求限制」
        在型別上同形，於是打錯路徑＝SSRF 網域檢查靜默關閉且 rc=0。
        姊妹條在 tests/ops/test_domain_policy.py。
        """
        with pytest.raises(PolicyInputError):
            load_policy("/nonexistent/policy.yaml")

    def test_load_policy_no_key(self, config_dir):
        """Policy 檔案缺少 allowed_domains 鍵 → caller error（#1649）。

        原本斷言 `== []`，也就是把「只寫 lint 規則的檔」讀成不限制。
        """
        path = os.path.join(config_dir, "policy.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("denied_functions:\n  - holt_winters\n")
        os.chmod(path, 0o600)
        with pytest.raises(PolicyInputError):
            load_policy(path)


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
        # #1982: a platform file only supplies defaults for a tenant a
        # tenant file declares; it cannot create one.
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
        routing = load_tenant_tree(config_dir).routing_configs
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
        routing = load_tenant_tree(config_dir).routing_configs
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
        routing = load_tenant_tree(config_dir).routing_configs
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
        # #1982: a platform file only supplies defaults for a tenant a
        # tenant file declares; it cannot create one.
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
        routing = load_tenant_tree(config_dir).routing_configs
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
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
        assert "db-c" not in routing  # routing disabled
        assert dedup["db-c"] == "enable"  # dedup still tracked

    def test_no_defaults_no_routing(self, config_dir):
        """無 _routing_defaults 也無 _routing → 不產出路由。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
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
        routing = load_tenant_tree(config_dir).routing_configs
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
        # #1982: a platform file only supplies defaults for a tenant a
        # tenant file declares; it cannot create one.
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
        routing = load_tenant_tree(config_dir).routing_configs
        assert routing["db-a"]["receiver"]["to"] == ["db-a-team@example.com"]


# ============================================================
# #1231 deprecated key aliases（Python ↔ Go alias boundary 對稱）
# ============================================================

class TestDeprecationNoticePin:
    """措辭 pin（仿 TestPolicyErrorPrefixPin 的鎖法）：NOTICE 行不得含
    `WARN`／`skipping`（丟棄設定項的措辭；阻擋判定自 #2489 起依型別
    `SkippedEntryWarning`），也不得以 _policy_errors 的 `ERROR:` 前綴開頭。"""


    def test_alias_table_mirrors_go_side(self):
        """alias 表雙點名內容 pin：commit 2 改接 registry 前，Python↔Go
        兩份手 pin 表必須同內容（Go 側在 pkg/config/aliases.go）。"""
        from _grar_validate import DEPRECATED_KEY_ALIASES
        assert DEPRECATED_KEY_ALIASES == {
            "mysql_cpu": "mysql_threads_running"}


class TestGenerateRoutes:
    """generate_routes() 整合測試。"""

    def test_single_tenant_webhook(self):
        """單一 tenant 產生一組 route + receiver。"""
        configs = {"db-a": {**make_routing_config(), "group_wait": "30s"}}
        routes, receivers, warnings = generate_routes(configs, tenants=())
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
        routes, receivers, warnings = generate_routes(configs, tenants=())
        assert len(routes) == 2
        assert routes[0]["receiver"] == "tenant-db-a"
        assert routes[1]["receiver"] == "tenant-db-b"

    def test_missing_receiver_skipped(self):
        """缺少 receiver 的 tenant 被跳過並產生警告。"""
        configs = {"db-a": {"group_wait": "30s"}}
        routes, receivers, warnings = generate_routes(configs, tenants=())
        assert len(routes) == 0
        assert any("missing required 'receiver'" in w for w in warnings)

    def test_enforced_routing_single(self):
        """單一 enforced route 插入在 tenant route 之前。"""
        configs = {"db-a": make_routing_config(url="https://tenant.example.com")}
        enforced = make_enforced_routing()
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced, tenants=())
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
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced, tenants=())
        assert len(routes) == 4  # 2 enforced + 2 tenant
        enforced_routes = [r for r in routes if r.get("continue")]
        assert len(enforced_routes) == 2

    def test_enforced_missing_receiver_warns(self):
        """Enforced routing 缺少 receiver 產生警告但不影響 tenant routes。"""
        configs = {"db-a": make_routing_config()}
        enforced = {"match": ['severity="critical"']}  # no receiver
        routes, receivers, warnings = generate_routes(configs, enforced_routing=enforced, tenants=())
        assert any("missing 'receiver'" in w for w in warnings)
        assert len(routes) == 1

    def test_domain_allowlist_blocks_tenant(self):
        """Domain allowlist 阻擋 tenant receiver。"""
        configs = {"db-a": make_routing_config(url="https://evil.com/hook")}
        routes, receivers, warnings = generate_routes(
            configs, allowed_domains=["*.example.com"], tenants=())
        assert len(routes) == 0
        assert any("not in allowed_domains" in w for w in warnings)

    def test_routing_overrides(self):
        """Per-rule routing overrides 是 tenant 主 route 的子路由（#2252）。"""
        configs = {
            "db-a": {
                **make_routing_config(url="https://default.example.com"),
                "overrides": [{
                    "alertname": "HighCPU",
                    "receiver": make_receiver("slack", api_url="https://hooks.slack.com/cpu"),
                }],
            }
        }
        routes, receivers, warnings = generate_routes(configs, tenants=())
        assert len(routes) == 1
        assert routes[0]["receiver"] == "tenant-db-a"
        assert routes[0]["routes"][0]["receiver"] == "tenant-db-a-override-0"

    def test_timing_params_applied(self):
        """Timing parameters 正確套用到 route。"""
        configs = {
            "db-a": {
                **make_routing_config(),
                "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            }
        }
        routes, _, _ = generate_routes(configs, tenants=())
        assert routes[0]["group_wait"] == "30s"
        assert routes[0]["group_interval"] == "5m"
        assert routes[0]["repeat_interval"] == "4h"

    def test_group_by_passed_through(self):
        """group_by 傳遞到路由。"""
        cfg = {"db-a": {"receiver": make_receiver(), "group_by": ["alertname", "severity"]}}
        routes, _, _ = generate_routes(cfg, tenants=())
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
        assert sub[0]["matchers"] == ['metric_group="cpu_metrics"']

    def test_alertname_override_matchers(self):
        """alertname override 只產生自己的 alertname matcher；tenant matcher
        只在父節點（tenant 主 route，#2252）。"""
        routing = {"receiver": make_receiver(), "overrides": [
            make_override("alertname", "DiskFull")
        ]}
        sub, recv, warns = expand_routing_overrides("db-a", routing)
        assert len(sub) == 1
        assert sub[0]["matchers"] == ['alertname="DiskFull"']

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
        routes, receivers, warnings = _build_enforced_routes(None, {}, tenants=())
        assert routes == []
        assert receivers == []
        assert warnings == []

    def test_empty_dict_returns_empty(self):
        """空 dict enforced routing 回傳空結果。"""
        routes, receivers, warnings = _build_enforced_routes({}, {}, tenants=())
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
        enforced = load_tenant_tree(config_dir).enforced_routing
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
        enforced = load_tenant_tree(config_dir).enforced_routing
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
        enforced = load_tenant_tree(config_dir).enforced_routing
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
            tenant_cfg, enforced_routing=enforced, tenants=())
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
        routes, _, _ = generate_routes({}, enforced_routing=enforced, tenants=())
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
        routes, _, warnings = generate_routes({}, enforced_routing=enforced, tenants=())
        assert routes[0]["group_wait"] == "5s"
        assert routes[0]["repeat_interval"] == "72h"
        assert len(warnings) >= 2

    def test_enforced_missing_receiver_skipped(self):
        """Enforced without receiver → warning, no route."""
        enforced = {"enabled": True, "match": ['severity="critical"']}
        routes, _, warnings = generate_routes({}, enforced_routing=enforced, tenants=())
        assert len(routes) == 0
        assert any("missing 'receiver'" in w for w in warnings)

    def test_enforced_domain_policy_blocks(self):
        """Enforced receiver blocked by domain policy → warning, no route."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://evil.com/alerts"},
        }
        routes, _, warnings = generate_routes(
            {}, enforced_routing=enforced, allowed_domains=["*.example.com"], tenants=())
        assert len(routes) == 0
        assert any("blocked by domain policy" in w for w in warnings)

    def test_enforced_none_no_effect(self):
        """enforced_routing=None → no platform route (backward compat)."""
        tenant_cfg = {
            "db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com/alerts"}},
        }
        routes, receivers, _ = generate_routes(tenant_cfg, enforced_routing=None, tenants=())
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
        # #1982: a platform file only supplies defaults for a tenant a
        # tenant file declares; it cannot create one.
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a: {}\n")
        write_yaml(config_dir, "db-b.yaml", "tenants:\n  db-b: {}\n")
        tree = load_tenant_tree(config_dir)
        routing, enforced = tree.routing_configs, tree.enforced_routing
        assert enforced is not None
        routes, receivers, _ = generate_routes(
            routing, enforced_routing=enforced, tenants=())
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
# #2252: override 是 tenant 主路由的子路由（巢狀）
# ============================================================
class TestOverrideRoutesNestUnderTenantRoute:
    """override 子路由掛在 tenant 主路由的 ``routes`` 底下。

    Alertmanager 的子路由從父節點的 RouteOpts 起算（dispatch/route.go
    ``newRoute``），所以 override 沒寫的 timing / group_by 繼承 tenant 主路由；
    若與主路由同層（#2252 之前），繼承的是 root。租戶範圍只由父節點的
    ``tenant="<id>"`` matcher 決定：子路由不重複它，父節點一定帶它。
    """

    _T = "demo-nest"
    _MAIN = {"type": "webhook", "url": "https://hooks.example.com/main"}

    @classmethod
    def _cfg(cls, *overrides, **main):
        cfg = {"receiver": cls._MAIN, **main}
        if overrides:
            cfg["overrides"] = list(overrides)
        return {cls._T: cfg}

    @staticmethod
    def _ov(**kw):
        kw.setdefault("receiver", {"type": "webhook",
                                   "url": "https://hooks.example.com/ov"})
        return kw

    def test_overrides_are_children_of_the_tenant_route(self):
        routes, receivers, warnings = generate_routes(self._cfg(
            self._ov(alertname="DiskFull"),
            self._ov(metric_group="cpu_metrics")), tenants=())
        assert warnings == []
        assert len(routes) == 1, routes
        parent = routes[0]
        assert parent["matchers"] == [f'tenant="{self._T}"']
        assert parent["receiver"] == f"tenant-{self._T}"
        assert parent["routes"] == [
            {"matchers": ['alertname="DiskFull"'],
             "receiver": f"tenant-{self._T}-override-0"},
            {"matchers": ['metric_group="cpu_metrics"'],
             "receiver": f"tenant-{self._T}-override-1"},
        ]
        assert sorted(r["name"] for r in receivers) == [
            f"tenant-{self._T}", f"tenant-{self._T}-override-0",
            f"tenant-{self._T}-override-1"]

    def test_no_top_level_route_targets_an_override_receiver(self):
        """反證同層：頂層不得出現任何 override receiver 的 route。"""
        routes, _, _ = generate_routes({
            **self._cfg(self._ov(alertname="A")),
            "demo-other": {"receiver": self._MAIN,
                           "overrides": [self._ov(metric_group="g")]},
        }, tenants=())
        assert [r["receiver"] for r in routes] == [
            "tenant-demo-nest", "tenant-demo-other"]
        assert all("-override-" not in r["receiver"] for r in routes)

    def test_child_leaves_undeclared_timing_to_the_tenant_route(self):
        """tenant repeat_interval=30m、override 沒寫 timing → 子路由不帶
        timing / group_by 鍵（由 AM 從 tenant 主路由繼承 30m），父節點帶 30m。"""
        routes, _, _ = generate_routes(self._cfg(
            self._ov(alertname="A"),
            repeat_interval="30m", group_wait="45s", group_by=["alertname"]), tenants=())
        parent = routes[0]
        assert parent["repeat_interval"] == "30m"
        child = parent["routes"][0]
        assert set(child) == {"matchers", "receiver"}, child

    def test_child_keeps_its_own_declared_timing(self):
        routes, _, _ = generate_routes(self._cfg(
            self._ov(alertname="A", repeat_interval="10m",
                     group_by=["alertname", "instance"]),
            repeat_interval="30m"), tenants=())
        child = routes[0]["routes"][0]
        assert child["repeat_interval"] == "10m"
        assert child["group_by"] == ["alertname", "instance"]
        assert "tenant" not in "".join(child["matchers"])

    def test_override_order_is_preserved_first_match_wins(self):
        """AM 在子路由間取第一個命中者；list 順序必須照 overrides 原順序。"""
        routes, _, _ = generate_routes(self._cfg(
            self._ov(alertname="Dup"), self._ov(alertname="Dup")), tenants=())
        assert [c["receiver"] for c in routes[0]["routes"]] == [
            f"tenant-{self._T}-override-0", f"tenant-{self._T}-override-1"]

    def test_tenant_without_overrides_has_no_routes_key(self):
        """沒有 override 的 tenant 維持原形狀（不產生空的 routes）。"""
        routes, _, _ = generate_routes(self._cfg(repeat_interval="30m"), tenants=())
        assert "routes" not in routes[0]

    def test_all_overrides_skipped_leaves_no_routes_key(self):
        routes, _, warnings = generate_routes(self._cfg(
            {"alertname": "NoReceiver"}), tenants=())
        assert any("missing 'receiver'" in w for w in warnings)
        assert "routes" not in routes[0]

    def test_enforced_routes_stay_top_level_ahead_of_tenant_routes(self):
        """_routing_enforced（continue: true）位置與語意不受巢狀影響。"""
        enforced = {"receiver": {"type": "webhook",
                                 "url": "https://noc.example.com/x"},
                    "match": ['severity="critical"']}
        routes, _, _ = generate_routes(
            self._cfg(self._ov(alertname="A")), enforced_routing=enforced, tenants=())
        assert [r["receiver"] for r in routes] == [
            "platform-enforced", f"tenant-{self._T}"]
        assert routes[0]["continue"] is True
        assert "continue" not in routes[1]
        assert "continue" not in routes[1]["routes"][0]

    def test_per_tenant_enforced_route_is_not_nested(self):
        enforced = {"receiver": {"type": "webhook",
                                 "url": "https://noc.example.com/{{tenant}}"}}
        routes, _, _ = generate_routes(
            self._cfg(self._ov(alertname="A")), enforced_routing=enforced, tenants=())
        assert [r["receiver"] for r in routes] == [
            f"platform-enforced-{self._T}", f"tenant-{self._T}"]
        assert "routes" not in routes[0]

    def test_assembled_configmap_keeps_the_nesting(self):
        """組完整 ConfigMap 後巢狀仍在，custom 子樹的 tenant child 帶著同一批
        子路由（#2342）。"""
        from generate_alertmanager_routes import (
            assemble_configmap, load_base_config)
        routes, receivers, _ = generate_routes(self._cfg(
            self._ov(alertname="A")), tenants=())
        cm = yaml.safe_load(assemble_configmap(
            load_base_config(None), routes, receivers, []))
        am = yaml.safe_load(cm["data"]["alertmanager.yml"])
        top = am["route"]["routes"]
        tenant_routes = [r for r in top
                         if r.get("receiver") == f"tenant-{self._T}"]
        assert len(tenant_routes) == 1
        assert tenant_routes[0]["routes"][0]["receiver"] == \
            f"tenant-{self._T}-override-0"
        custom = [r for r in top if 'component="custom"' in r["matchers"]][0]
        assert custom["routes"] == [{"matchers": [f'tenant="{self._T}"'],
                                     "receiver": f"tenant-{self._T}",
                                     "routes": tenant_routes[0]["routes"]}]
        assert all("-override-" not in r.get("receiver", "") for r in top)


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

    def test_metadata_is_not_read(self, config_dir):
        """#3043：產生器不再讀 `_metadata`（讀了也沒有任何輸出用到）。"""
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", metadata={
                       "runbook_url": "https://wiki.example.com/{{tenant}}",
                       "owner": "dba-team",
                   }))
        parsed = _parse_config_files(config_dir)
        assert "metadata_configs" not in parsed
        assert "db-a" in parsed["all_tenants"]

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
# load_tenant_tree (整合測試，使用臨時目錄)
# ============================================================
class TestLoadTenantConfigs:
    """load_tenant_tree() 整合測試。"""

    def test_basic_tenant_routing(self, config_dir):
        """基本 tenant routing 正確載入。"""
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing))
        routing_configs = load_tenant_tree(config_dir).routing_configs
        assert "db-a" in routing_configs

    def test_reserved_file_skipped(self, config_dir):
        """以 _ 開頭的檔案包含 _routing_defaults 仍被讀取。"""
        write_yaml(config_dir, "_defaults.yaml",
                   "_routing_defaults:\n  group_wait: '30s'\n")
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing))
        routing_configs = load_tenant_tree(config_dir).routing_configs
        assert "db-a" in routing_configs

    def test_empty_dir(self, config_dir):
        """空目錄回傳空結果。"""
        routing_configs = load_tenant_tree(config_dir).routing_configs
        assert routing_configs == {}

    def test_severity_dedup(self, config_dir):
        """_severity_dedup 正確載入。"""
        routing = {"receiver": make_receiver()}
        write_yaml(config_dir, "db-a.yaml",
                   make_tenant_yaml("db-a", routing=routing, severity_dedup="disable"))
        dedup = load_tenant_tree(config_dir).dedup_configs
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
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
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
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
        assert len(routing) == 0
        assert dedup["db-b"] == "enable"

    def test_multiple_files(self, config_dir):
        """載入多個配置檔案。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://a.example.com'\n")
        write_yaml(config_dir, "db-b.yaml", "tenants:\n  db-b:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://b.example.com'\n")
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
        assert len(routing) == 2
        assert len(dedup) == 2

    def test_skips_dotfiles(self, config_dir):
        """跳過隱藏檔案（以點開頭）。"""
        write_yaml(config_dir, ".hidden.yaml", "tenants:\n  hidden:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'x'\n")
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
        assert len(routing) == 0
        assert len(dedup) == 0

    def test_dedup_disable_explicit(self, config_dir):
        """顯式禁用 severity dedup。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    _severity_dedup: 'disable'\n")
        dedup = load_tenant_tree(config_dir).dedup_configs
        assert dedup["db-a"] == "disable"

    def test_dedup_mixed_tenants(self, config_dir):
        """enable (default) + disable → dedup dict 正確區分。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        write_yaml(config_dir, "db-b.yaml", "tenants:\n  db-b:\n    _severity_dedup: 'disable'\n")
        dedup = load_tenant_tree(config_dir).dedup_configs
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
        dedup = load_tenant_tree(config_dir).dedup_configs
        for t in ("t1", "t2", "t3"):
            assert dedup[t] == "disable", f"{t}"

    def test_dedup_only_no_routing_produces_inhibit(self, config_dir):
        """無 _routing 的 tenant 仍可產出 inhibit rule（E2E 驗證）。"""
        write_yaml(config_dir, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
        tree = load_tenant_tree(config_dir)
        routing, dedup = tree.routing_configs, tree.dedup_configs
        assert len(routing) == 0
        rules, _ = generate_inhibit_rules(dedup)
        assert len(rules) == 1

    def test_old_string_receiver_rejected(self):
        """v1.2.0 舊格式 (receiver: URL string) 在 generate_routes 應被跳過。"""
        cfg = {"db-a": {"receiver": "https://example.com", "group_wait": "30s"}}
        routes, _, warnings = generate_routes(cfg, tenants=())
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
            routing = load_tenant_tree(d).routing_configs
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
            routing = load_tenant_tree(d).routing_configs
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
            routing = load_tenant_tree(d).routing_configs
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
            routing = load_tenant_tree(d).routing_configs
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
            warnings = load_tenant_tree(d).schema_warnings
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
# #2243: domain policy 也套用在 _routing.overrides 產生的子路由
# ============================================================
class TestDomainPolicyOverrideSubroutes:
    """每條會產出 AM receiver 的 override 子路由，受與主路由相同的約束。

    #2252：產生器把 override 放成 tenant 主路由的子路由，子路由沒宣告的
    timing / group_by 在 AM 裡繼承 tenant 主路由的值，所以檢查用「子路由自己
    宣告的值，沒宣告就用主路由的值」——也就是它實際生效的值。
    """

    _PD = {"type": "pagerduty", "service_key": "k"}
    _SLACK = {"type": "slack", "api_url": "https://hooks.slack.com/x"}

    @classmethod
    def _routing(cls, *overrides, **main):
        rc = {"receiver": cls._PD}
        rc.update(main)
        if overrides:
            rc["overrides"] = list(overrides)
        return {"tenant-fin": rc}

    @staticmethod
    def _policy(**constraints):
        return {"finance": {"tenants": ["tenant-fin"],
                            "constraints": constraints}}

    def test_forbidden_override_receiver_strict_errors_and_names_it(self):
        routing = self._routing({"alertname": "MysqlDown",
                                 "receiver": self._SLACK})
        policy = self._policy(forbidden_receiver_types=["slack"])
        msgs = check_domain_policies(routing, policy, strict=True)
        assert msgs == [
            "  ERROR: domain_policy 'finance', tenant 'tenant-fin' "
            "override[0] (alertname=MysqlDown): receiver type 'slack' is "
            "forbidden — fix: domain forbids ['slack']; switch override[0]'s "
            "receiver.type to a compliant type or amend the domain policy"]
        # 非 strict：同一違規以 WARN 呈現，不阻擋
        lenient = check_domain_policies(routing, policy)
        assert len(lenient) == 1
        assert lenient[0].lstrip().startswith("WARN:")
        assert "override[0] (alertname=MysqlDown)" in lenient[0]

    def test_override_receiver_not_in_allowed_types(self):
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD},
            {"metric_group": "disk", "receiver": self._SLACK})
        policy = self._policy(allowed_receiver_types=["pagerduty", "email"])
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 1, msgs
        assert "override[1] (metric_group=disk)" in msgs[0]
        assert "not in allowed types ['email', 'pagerduty']" in msgs[0]

    def test_override_own_repeat_interval_above_max(self):
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD, "repeat_interval": "4h"},
            repeat_interval="30m")
        policy = self._policy(max_repeat_interval="1h")
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 1, msgs
        assert "override[0] (alertname=A): repeat_interval '4h' exceeds " \
               "max '1h'" in msgs[0]
        assert "lower override[0]'s repeat_interval" in msgs[0]
        assert len(check_domain_policies(routing, policy)) == 1

    def test_override_own_group_wait_below_min(self):
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD, "group_wait": "5s"},
            group_wait="30s")
        policy = self._policy(min_group_wait="30s")
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 1, msgs
        assert "override[0] (alertname=A): group_wait '5s' below minimum" \
            in msgs[0]

    def test_override_without_timing_is_backfilled_from_main(self):
        """沒帶 timing 的 override 用主路由的值檢查（#2252：AM 繼承的是
        tenant 主路由）。主路由違規 → 主路由與該 override 各報一條，override
        那條標明值是繼承來的、修法指向主路由或 override 自己的值。"""
        routing = self._routing({"alertname": "A", "receiver": self._PD},
                                repeat_interval="24h", group_wait="5s")
        policy = self._policy(max_repeat_interval="1h", min_group_wait="30s")
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 4, msgs
        sub = [m for m in msgs if "override[0]" in m]
        assert sub == [
            "  ERROR: domain_policy 'finance', tenant 'tenant-fin' "
            "override[0] (alertname=A): repeat_interval '24h' (inherited "
            "from the tenant's main route) exceeds max '1h' — fix: lower the "
            "tenant's (which override[0] inherits) or override[0]'s own "
            "repeat_interval to '1h' or less, or raise the policy's "
            "max_repeat_interval",
            "  ERROR: domain_policy 'finance', tenant 'tenant-fin' "
            "override[0] (alertname=A): group_wait '5s' (inherited from the "
            "tenant's main route) below minimum '30s' — fix: raise the "
            "tenant's (which override[0] inherits) or override[0]'s own "
            "group_wait to '30s' or more, or lower the policy's "
            "min_group_wait",
        ]
        # 非 strict：同樣兩條子路由 WARN（無修法提示）
        lenient = check_domain_policies(routing, policy)
        assert len(lenient) == 4, lenient
        assert sum("override[0]" in m and "(inherited from the tenant's "
                   "main route)" in m for m in lenient) == 2

    def test_override_own_timing_is_not_replaced_by_main(self):
        """override 自己宣告的值優先，不被主路由的值蓋過（反方向）。"""
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD, "repeat_interval": "30m",
             "group_wait": "1m"},
            repeat_interval="24h", group_wait="5s")
        policy = self._policy(max_repeat_interval="1h", min_group_wait="30s")
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 2, msgs
        assert not any("override" in m for m in msgs), msgs

    def test_main_without_value_leaves_override_unconstrained(self):
        """主路由與 override 都沒寫 → 兩者都繼承 root，沒有可檢查的值。"""
        routing = self._routing({"alertname": "A", "receiver": self._PD})
        policy = self._policy(max_repeat_interval="1h", min_group_wait="30s")
        assert check_domain_policies(routing, policy, strict=True) == []

    def test_override_group_by_follows_main_route_semantics(self):
        """enforce_group_by：override 宣告缺 label → 點名 override；沒宣告 →
        繼承主路由的 group_by（#2252），主路由合規則 override 也合規。"""
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD,
             "group_by": ["alertname"]},
            {"alertname": "B", "receiver": self._PD},
            group_by=["tenant", "alertname"])
        policy = self._policy(enforce_group_by=["tenant", "alertname"])
        msgs = check_domain_policies(routing, policy, strict=True)
        assert msgs == [
            "  ERROR: domain_policy 'finance', tenant 'tenant-fin' "
            "override[0] (alertname=A): group_by missing required labels: "
            "['tenant'] — fix: add ['tenant'] to override[0]'s group_by "
            "(policy requires ['alertname', 'tenant'])"]

    def test_override_inherits_noncompliant_main_group_by(self):
        """主路由 group_by 缺 label → 沒宣告 group_by 的 override 也缺，並標明
        繼承來源。"""
        routing = self._routing({"metric_group": "disk",
                                 "receiver": self._PD},
                                group_by=["alertname"])
        policy = self._policy(enforce_group_by=["tenant", "alertname"])
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 2, msgs
        assert ("override[0] (metric_group=disk): group_by (inherited from "
                "the tenant's main route) missing required labels: "
                "['tenant']") in msgs[1]
        assert "add ['tenant'] to the tenant's (which override[0] " \
               "inherits) or override[0]'s own group_by" in msgs[1]

    def test_malformed_own_group_by_is_not_papered_over(self):
        """override 寫了非 list 的 group_by（產生器會丟掉它）→ strict 仍點名
        它自己，不以主路由的合規值掩蓋。"""
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD, "group_by": "tenant"},
            group_by=["tenant", "alertname"])
        policy = self._policy(enforce_group_by=["tenant"])
        msgs = check_domain_policies(routing, policy, strict=True)
        assert len(msgs) == 1, msgs
        assert "override[0] (alertname=A): group_by must be a list" in msgs[0]

    def test_compliant_overrides_produce_no_messages(self):
        routing = self._routing(
            {"alertname": "A", "receiver": self._PD, "repeat_interval": "30m",
             "group_wait": "30s", "group_by": ["tenant", "alertname"]},
            repeat_interval="1h", group_wait="30s",
            group_by=["tenant", "alertname"])
        policy = self._policy(
            allowed_receiver_types=["pagerduty"],
            forbidden_receiver_types=["slack"],
            enforce_group_by=["tenant", "alertname"],
            max_repeat_interval="1h", min_group_wait="30s")
        assert check_domain_policies(routing, policy, strict=True) == []
        assert check_domain_policies(routing, policy) == []

    def test_overrides_the_generator_skips_are_not_checked(self):
        """產生器不產出的 override（缺 receiver、matcher 不合法、主 receiver
        缺席）不是子路由，不被點名。"""
        policy = self._policy(forbidden_receiver_types=["slack"],
                              max_repeat_interval="1h")
        skipped = [
            {"alertname": "A", "repeat_interval": "9h"},          # 無 receiver
            {"alertname": "A", "metric_group": "g",
             "receiver": self._SLACK},                           # 兩個 matcher
            {"receiver": self._SLACK},                           # 無 matcher
            "not-a-mapping",
        ]
        routing = self._routing(*skipped)
        assert check_domain_policies(routing, policy, strict=True) == []
        no_main = {"tenant-fin": {"overrides": [
            {"alertname": "A", "receiver": self._SLACK}]}}
        assert check_domain_policies(no_main, policy, strict=True) == []

    def test_main_route_messages_unchanged_without_overrides(self):
        """無 overrides 時主路由訊息逐字不變（回歸釘）。"""
        routing = {"tenant-fin": {"receiver": self._SLACK}}
        policy = self._policy(forbidden_receiver_types=["slack"])
        assert check_domain_policies(routing, policy, strict=True) == [
            "  ERROR: domain_policy 'finance', tenant 'tenant-fin': receiver "
            "type 'slack' is forbidden — fix: domain forbids ['slack']; "
            "switch the tenant's receiver.type to a compliant type or amend "
            "the domain policy"]
        assert check_domain_policies(routing, policy) == [
            "  WARN: domain_policy 'finance', tenant 'tenant-fin': receiver "
            "type 'slack' is forbidden"]

    def test_cli_strict_blocks_override_violation(self):
        """CLI：override 違規在 --strict 下 exit 1 並點名該 override；
        非 strict 仍 exit 0。"""
        import subprocess
        script = TestStrictValidateCLI._SCRIPT
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_domain_policy.yaml", {"domain_policies": self._policy(
                forbidden_receiver_types=["slack"])})
            _wy(d, "tenant-fin.yaml", {"tenants": {"tenant-fin": {
                "_routing": {
                    "receiver": self._PD,
                    "overrides": [{"alertname": "MysqlDown",
                                   "receiver": self._SLACK}],
                }}}})

            def run(*flags):
                return subprocess.run(
                    [sys.executable, script, "--config-dir", d, "--validate",
                     *flags],
                    capture_output=True, text=True, encoding="utf-8",
                    timeout=60)

            strict = run("--strict")
            assert strict.returncode == 1, strict.stdout + strict.stderr
            assert "override[0] (alertname=MysqlDown)" in strict.stderr
            lenient = run()
            assert lenient.returncode == 0, lenient.stdout + lenient.stderr
            assert "override[0] (alertname=MysqlDown)" in lenient.stderr


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
        # #1650: this runner used to add `--dry-run`; every caller passes
        # `--validate`, which never reads it, and the tool now exits 2 on
        # the pair.
        return subprocess.run(
            [sys.executable, self._SCRIPT, "--config-dir", config_dir,
             *extra_flags],
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
