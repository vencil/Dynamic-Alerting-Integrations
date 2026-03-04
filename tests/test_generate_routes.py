#!/usr/bin/env python3
"""test_generate_routes.py — generate_alertmanager_routes.py 測試套件。

驗證核心功能:
  1. Duration 解析與格式化 (parse_duration_seconds / format_duration)
  2. Guardrail clamp 邏輯 + 常數一致性
  3. load_tenant_configs() — routing + severity_dedup 讀取
  4. generate_routes() — route + receiver 結構
  5. generate_inhibit_rules() — per-tenant severity dedup inhibit rules
  6. render_output() — YAML 片段組合
  7. SAST: open() 必須帶 encoding="utf-8"

用法:
  python3 -m pytest tests/test_generate_routes.py -v
"""

import inspect
import os
import re
import stat
import sys
import tempfile
import unittest

# Add scripts/tools to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import generate_alertmanager_routes as gen  # noqa: E402


# ── Shared helper ──────────────────────────────────────────────────

def _write_yaml(tmpdir, filename, content):
    """Write a YAML file into tmpdir with secure permissions."""
    path = os.path.join(tmpdir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


# ── 1. Duration parsing + formatting ──────────────────────────────

class TestDuration(unittest.TestCase):
    """parse_duration_seconds() + format_duration() 測試。"""

    # — parse —

    def test_parse_seconds(self):
        self.assertEqual(gen.parse_duration_seconds("30s"), 30)

    def test_parse_minutes(self):
        self.assertEqual(gen.parse_duration_seconds("5m"), 300)

    def test_parse_hours(self):
        self.assertEqual(gen.parse_duration_seconds("4h"), 14400)

    def test_parse_days(self):
        self.assertEqual(gen.parse_duration_seconds("1d"), 86400)

    def test_parse_invalid(self):
        """Invalid / edge-case inputs all return None."""
        for bad in ("abc", "", None, "5x", "s"):
            self.assertIsNone(gen.parse_duration_seconds(bad), msg=f"input={bad!r}")

    # — format —

    def test_format_seconds(self):
        self.assertEqual(gen.format_duration(30), "30s")

    def test_format_minutes(self):
        self.assertEqual(gen.format_duration(300), "5m")

    def test_format_hours(self):
        self.assertEqual(gen.format_duration(3600), "1h")

    def test_format_no_day_suffix(self):
        """Prometheus 不支援 'd' 格式，大時數仍用 'h'。"""
        self.assertEqual(gen.format_duration(86400), "24h")
        self.assertEqual(gen.format_duration(259200), "72h")


# ── 2. Guardrails ─────────────────────────────────────────────────

class TestValidateAndClamp(unittest.TestCase):
    """validate_and_clamp() guardrail 測試 + 常數一致性。"""

    def test_within_bounds_unchanged(self):
        val, warnings = gen.validate_and_clamp("group_wait", "30s", "t")
        self.assertEqual(val, "30s")
        self.assertEqual(warnings, [])

    def test_below_minimum_clamped(self):
        val, warnings = gen.validate_and_clamp("group_wait", "1s", "t")
        self.assertEqual(val, "5s")
        self.assertIn("below minimum", warnings[0])

    def test_above_maximum_clamped(self):
        val, warnings = gen.validate_and_clamp("repeat_interval", "100h", "t")
        self.assertEqual(val, "72h")
        self.assertIn("above maximum", warnings[0])

    def test_invalid_value_uses_platform_default(self):
        val, warnings = gen.validate_and_clamp("group_wait", "abc", "t")
        self.assertEqual(val, gen.PLATFORM_DEFAULTS["group_wait"])
        self.assertIn("invalid", warnings[0])

    def test_unknown_param_passes_through(self):
        val, warnings = gen.validate_and_clamp("unknown_param", "xyz", "t")
        self.assertEqual(val, "xyz")
        self.assertEqual(warnings, [])

    def test_all_guardrail_bounds(self):
        """各 param 上下界全部驗證。"""
        cases = [
            ("group_wait",      "2s",  "5s"),   # below min
            ("group_wait",      "10m", "5m"),    # above max
            ("group_interval",  "2s",  "5s"),
            ("group_interval",  "10m", "5m"),
            ("repeat_interval", "10s", "1m"),
            ("repeat_interval", "100h", "72h"),
        ]
        for param, input_val, expected in cases:
            val, _ = gen.validate_and_clamp(param, input_val, "t")
            self.assertEqual(val, expected, msg=f"{param}={input_val}")

    def test_exactly_at_max_unchanged(self):
        val, warnings = gen.validate_and_clamp("repeat_interval", "72h", "t")
        self.assertEqual(val, "72h")
        self.assertEqual(warnings, [])

    def test_guardrail_constants_match_doc(self):
        """GUARDRAILS 常數與 CLAUDE.md 記載一致。"""
        self.assertEqual(gen.GUARDRAILS["group_wait"], (5, 300, "5s–5m"))
        self.assertEqual(gen.GUARDRAILS["group_interval"], (5, 300, "5s–5m"))
        self.assertEqual(gen.GUARDRAILS["repeat_interval"], (60, 259200, "1m–72h"))
        # 72h == 259200s 交叉驗證
        self.assertEqual(gen.parse_duration_seconds("72h"), 259200)


# ── 3. load_tenant_configs() ──────────────────────────────────────

class TestLoadTenantConfigs(unittest.TestCase):
    """load_tenant_configs() — routing + severity_dedup 讀取。"""

    def test_routing_and_default_dedup(self):
        """有 _routing → routing dict 有值; 無 _severity_dedup → default enable。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", """
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_wait: "30s"
""")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-a"]["receiver"]["type"], "webhook")
            self.assertEqual(routing["db-a"]["receiver"]["url"], "https://webhook.example.com/alerts")
            self.assertEqual(dedup["db-a"], "enable")

    def test_no_routing_still_tracks_dedup(self):
        """無 _routing 的 tenant 仍出現在 dedup dict 中。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-b.yaml", """
tenants:
  db-b:
    mysql_connections: "80"
""")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(dedup["db-b"], "enable")

    def test_multiple_files(self):
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://a.example.com'\n")
            _write_yaml(d, "db-b.yaml", "tenants:\n  db-b:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://b.example.com'\n")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 2)
            self.assertEqual(len(dedup), 2)

    def test_skips_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, ".hidden.yaml", "tenants:\n  hidden:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'x'\n")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(len(dedup), 0)

    def test_dedup_disable_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    _severity_dedup: 'disable'\n")
            _, dedup = gen.load_tenant_configs(d)
            self.assertEqual(dedup["db-a"], "disable")

    def test_dedup_mixed_tenants(self):
        """enable (default) + disable → dedup dict 正確區分。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            _write_yaml(d, "db-b.yaml", "tenants:\n  db-b:\n    _severity_dedup: 'disable'\n")
            _, dedup = gen.load_tenant_configs(d)
            self.assertEqual(dedup["db-a"], "enable")
            self.assertEqual(dedup["db-b"], "disable")

    def test_dedup_disable_synonyms(self):
        """disabled / off / false 全視同 disable（與 Go 行為一致）。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "t.yaml", """
tenants:
  t1:
    _severity_dedup: "disabled"
  t2:
    _severity_dedup: "off"
  t3:
    _severity_dedup: "false"
""")
            _, dedup = gen.load_tenant_configs(d)
            for t in ("t1", "t2", "t3"):
                self.assertEqual(dedup[t], "disable", msg=t)

    def test_dedup_only_no_routing_produces_inhibit(self):
        """無 _routing 的 tenant 仍可產出 inhibit rule（E2E 驗證）。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            rules, _ = gen.generate_inhibit_rules(dedup)
            self.assertEqual(len(rules), 1)

    def test_old_string_receiver_rejected(self):
        """v1.2.0 舊格式 (receiver: URL string) 在 generate_routes 應被跳過。"""
        cfg = {"db-a": {"receiver": "https://example.com", "group_wait": "30s"}}
        routes, _, warnings = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 0)
        self.assertTrue(any("must be an object" in w for w in warnings))


# ── 4. generate_routes() ─────────────────────────────────────────

class TestGenerateRoutes(unittest.TestCase):
    """generate_routes() — route + receiver 結構。"""

    def _webhook(self, url="https://example.com"):
        return {"type": "webhook", "url": url}

    def test_basic_route_and_receiver(self):
        cfg = {"db-a": {"receiver": self._webhook(), "group_wait": "30s"}}
        routes, receivers, warnings = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["receiver"], "tenant-db-a")
        self.assertEqual(routes[0]["group_wait"], "30s")
        self.assertEqual(receivers[0]["webhook_configs"][0]["url"], "https://example.com")

    def test_missing_receiver_skipped(self):
        routes, _, warnings = gen.generate_routes({"db-a": {"group_wait": "30s"}})
        self.assertEqual(len(routes), 0)
        self.assertTrue(any("missing required" in w for w in warnings))

    def test_group_by_passed_through(self):
        cfg = {"db-a": {"receiver": self._webhook(), "group_by": ["alertname", "severity"]}}
        routes, _, _ = gen.generate_routes(cfg)
        self.assertEqual(routes[0]["group_by"], ["alertname", "severity"])

    def test_timing_guardrails_applied(self):
        cfg = {"db-a": {"receiver": self._webhook(), "group_wait": "1s", "repeat_interval": "100h"}}
        routes, _, warnings = gen.generate_routes(cfg)
        self.assertEqual(routes[0]["group_wait"], "5s")
        self.assertEqual(routes[0]["repeat_interval"], "72h")
        self.assertGreaterEqual(len(warnings), 2)

    def test_multi_tenant_sorted(self):
        cfg = {"db-b": {"receiver": self._webhook("https://b.example.com")},
               "db-a": {"receiver": self._webhook("https://a.example.com")}}
        routes, _, _ = gen.generate_routes(cfg)
        self.assertEqual(routes[0]["receiver"], "tenant-db-a")
        self.assertEqual(routes[1]["receiver"], "tenant-db-b")


# ── 4b. build_receiver_config() + receiver types ─────────────────

class TestBuildReceiverConfig(unittest.TestCase):
    """build_receiver_config() — per-type receiver 解析 + 驗證。"""

    def test_webhook_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "webhook", "url": "https://example.com"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("webhook_configs", cfg)
        self.assertEqual(cfg["webhook_configs"][0]["url"], "https://example.com")

    def test_email_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "email", "to": ["a@b.com"], "smarthost": "smtp:587"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("email_configs", cfg)
        self.assertEqual(cfg["email_configs"][0]["to"], ["a@b.com"])
        self.assertEqual(cfg["email_configs"][0]["smarthost"], "smtp:587")

    def test_slack_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "slack", "api_url": "https://hooks.slack.com/x"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("slack_configs", cfg)
        self.assertEqual(cfg["slack_configs"][0]["api_url"], "https://hooks.slack.com/x")

    def test_teams_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "teams", "webhook_url": "https://outlook.office.com/x"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("msteams_configs", cfg)

    def test_webhook_with_optional_fields(self):
        cfg, _ = gen.build_receiver_config(
            {"type": "webhook", "url": "https://x.com", "send_resolved": True}, "t")
        self.assertTrue(cfg["webhook_configs"][0]["send_resolved"])

    def test_email_with_optional_fields(self):
        cfg, _ = gen.build_receiver_config(
            {"type": "email", "to": ["a@b.com"], "smarthost": "s:587",
             "from": "x@y.com", "require_tls": True}, "t")
        self.assertEqual(cfg["email_configs"][0]["from"], "x@y.com")
        self.assertTrue(cfg["email_configs"][0]["require_tls"])

    def test_missing_type(self):
        cfg, warnings = gen.build_receiver_config({"url": "https://x.com"}, "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("missing required 'receiver.type'" in w for w in warnings))

    def test_unknown_type(self):
        cfg, warnings = gen.build_receiver_config({"type": "discord"}, "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("unknown receiver type" in w for w in warnings))

    def test_missing_required_field(self):
        cfg, warnings = gen.build_receiver_config({"type": "webhook"}, "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("requires 'url'" in w for w in warnings))

    def test_email_missing_smarthost(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "email", "to": ["a@b.com"]}, "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("requires 'smarthost'" in w for w in warnings))

    def test_not_a_dict(self):
        cfg, warnings = gen.build_receiver_config("https://url.com", "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("must be an object" in w for w in warnings))

    def test_type_case_insensitive(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "Webhook", "url": "https://x.com"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("webhook_configs", cfg)

    def test_slack_with_go_template(self):
        """Slack receiver 支援 Go template 語法的 title/text。"""
        cfg, _ = gen.build_receiver_config({
            "type": "slack",
            "api_url": "https://hooks.slack.com/x",
            "channel": "#alerts",
            "title": '{{ .Status | toUpper }}: {{ .CommonLabels.alertname }}',
            "text": '{{ range .Alerts }}{{ .Annotations.summary }}{{ end }}',
        }, "t")
        entry = cfg["slack_configs"][0]
        self.assertIn("{{ .Status", entry["title"])
        self.assertIn("{{ range .Alerts }}", entry["text"])

    def test_email_with_html_template(self):
        """Email receiver 支援 html body template。"""
        cfg, _ = gen.build_receiver_config({
            "type": "email",
            "to": ["team@example.com"],
            "smarthost": "smtp:587",
            "html": '<h2>{{ .CommonLabels.alertname }}</h2>',
        }, "t")
        self.assertIn("{{ .CommonLabels.alertname }}", cfg["email_configs"][0]["html"])

    def test_all_supported_types(self):
        """RECEIVER_TYPES 常數包含所有六種 type。"""
        self.assertEqual(sorted(gen.RECEIVER_TYPES.keys()),
                         ["email", "pagerduty", "rocketchat", "slack", "teams", "webhook"])


# ── 4c. Rocket.Chat + PagerDuty receiver types ───────────────────

class TestNewReceiverTypes(unittest.TestCase):
    """Rocket.Chat + PagerDuty receiver type 驗證。"""

    def test_rocketchat_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "rocketchat", "url": "https://chat.example.com/hooks/x/y"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("webhook_configs", cfg)
        self.assertEqual(cfg["webhook_configs"][0]["url"], "https://chat.example.com/hooks/x/y")

    def test_rocketchat_metadata_not_in_am(self):
        """Rocket.Chat metadata (channel/username) 不傳給 AM config。"""
        cfg, _ = gen.build_receiver_config(
            {"type": "rocketchat", "url": "https://chat.example.com/hooks/x/y",
             "channel": "#alerts", "username": "PrometheusBot"}, "t")
        entry = cfg["webhook_configs"][0]
        self.assertNotIn("channel", entry)
        self.assertNotIn("username", entry)

    def test_pagerduty_basic(self):
        cfg, warnings = gen.build_receiver_config(
            {"type": "pagerduty", "service_key": "abc123"}, "t")
        self.assertEqual(warnings, [])
        self.assertIn("pagerduty_configs", cfg)
        self.assertEqual(cfg["pagerduty_configs"][0]["service_key"], "abc123")

    def test_pagerduty_with_optional(self):
        cfg, _ = gen.build_receiver_config(
            {"type": "pagerduty", "service_key": "abc", "severity": "critical",
             "client": "Dynamic Alerting"}, "t")
        entry = cfg["pagerduty_configs"][0]
        self.assertEqual(entry["severity"], "critical")
        self.assertEqual(entry["client"], "Dynamic Alerting")

    def test_pagerduty_missing_service_key(self):
        cfg, warnings = gen.build_receiver_config({"type": "pagerduty"}, "t")
        self.assertIsNone(cfg)
        self.assertTrue(any("requires 'service_key'" in w for w in warnings))


# ── 4d. Routing Defaults + {{tenant}} substitution ───────────────

class TestRoutingDefaults(unittest.TestCase):
    """_routing_defaults 三態合併 + {{tenant}} 替換。"""

    def test_tenant_inherits_defaults(self):
        """無 _routing 的 tenant 繼承 _routing_defaults。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "_defaults.yaml", """
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
            routing, _ = gen.load_tenant_configs(d)
            self.assertIn("db-a", routing)
            self.assertEqual(routing["db-a"]["receiver"]["type"], "email")
            self.assertEqual(routing["db-a"]["group_wait"], "30s")

    def test_tenant_overrides_receiver(self):
        """有 _routing 的 tenant 覆寫 receiver，timing 從 defaults。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["default@example.com"]
    smarthost: "smtp:587"
  group_wait: "30s"
  repeat_interval: "4h"
""")
            _write_yaml(d, "db-b.yaml", """
tenants:
  db-b:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/x"
""")
            routing, _ = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-b"]["receiver"]["type"], "slack")
            self.assertEqual(routing["db-b"]["group_wait"], "30s")
            self.assertEqual(routing["db-b"]["repeat_interval"], "4h")

    def test_tenant_disables_routing(self):
        """_routing: "disable" → 不產出路由。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp:587"
""")
            _write_yaml(d, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
""")
            routing, _ = gen.load_tenant_configs(d)
            self.assertNotIn("db-c", routing)

    def test_tenant_template_substitution(self):
        """{{tenant}} 在 receiver fields 被替換為 tenant name。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "_defaults.yaml", """
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
            routing, _ = gen.load_tenant_configs(d)
            # channel is in the receiver dict (metadata, not AM config)
            self.assertEqual(routing["db-a"]["receiver"]["channel"], "#alerts-db-a")

    def test_disabled_routing_still_tracks_dedup(self):
        """_routing: disable の tenant でも dedup は追跡される。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
    _severity_dedup: "enable"
""")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertNotIn("db-c", routing)  # routing disabled
            self.assertEqual(dedup["db-c"], "enable")  # dedup still tracked

    def test_no_defaults_no_routing(self):
        """無 _routing_defaults 也無 _routing → 不產出路由。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            routing, dedup = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(dedup["db-a"], "enable")

    def test_defaults_boundary_warning(self):
        """_routing_defaults 在 tenant 檔案中會被忽略並警告。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "db-a.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["bad@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
            routing, _ = gen.load_tenant_configs(d)
            # db-a should NOT have routing from its own _routing_defaults
            self.assertEqual(len(routing), 0)

    def test_template_in_email_to(self):
        """{{tenant}} 在 email to list 被替換。"""
        with tempfile.TemporaryDirectory() as d:
            _write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["{{tenant}}-team@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
            routing, _ = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-a"]["receiver"]["to"], ["db-a-team@example.com"])


# ── 5. generate_inhibit_rules() ──────────────────────────────────

class TestGenerateInhibitRules(unittest.TestCase):
    """generate_inhibit_rules() — per-tenant severity dedup。"""

    def test_enabled_tenant_rule_structure(self):
        """Enabled tenant → 完整 inhibit rule 結構 + equal 不含 tenant。"""
        rules, _ = gen.generate_inhibit_rules({"db-a": "enable"})
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        # source: critical + metric_group + tenant
        self.assertIn('severity="critical"', rule["source_matchers"])
        self.assertIn('metric_group=~".+"', rule["source_matchers"])
        self.assertIn('tenant="db-a"', rule["source_matchers"])
        # target: warning + metric_group + tenant
        self.assertIn('severity="warning"', rule["target_matchers"])
        self.assertIn('metric_group=~".+"', rule["target_matchers"])
        self.assertIn('tenant="db-a"', rule["target_matchers"])
        # equal 只有 metric_group（tenant 已在 matchers 中）
        self.assertEqual(rule["equal"], ["metric_group"])
        self.assertNotIn("tenant", rule["equal"])

    def test_disabled_tenant_skipped(self):
        rules, warnings = gen.generate_inhibit_rules({"db-a": "disable"})
        self.assertEqual(len(rules), 0)
        self.assertTrue(any("disabled" in w for w in warnings))

    def test_mixed_tenants(self):
        rules, _ = gen.generate_inhibit_rules({"db-a": "enable", "db-b": "disable", "db-c": "enable"})
        self.assertEqual(len(rules), 2)
        tenants = {r["source_matchers"][2] for r in rules}
        self.assertEqual(tenants, {'tenant="db-a"', 'tenant="db-c"'})

    def test_sorted_by_tenant(self):
        rules, _ = gen.generate_inhibit_rules({"db-c": "enable", "db-a": "enable", "db-b": "enable"})
        tenants = [r["source_matchers"][2] for r in rules]
        self.assertEqual(tenants, ['tenant="db-a"', 'tenant="db-b"', 'tenant="db-c"'])

    def test_empty_input(self):
        rules, warnings = gen.generate_inhibit_rules({})
        self.assertEqual(rules, [])
        self.assertEqual(warnings, [])

    def test_all_disabled(self):
        rules, warnings = gen.generate_inhibit_rules({"db-a": "disable", "db-b": "disable"})
        self.assertEqual(rules, [])
        self.assertEqual(len(warnings), 2)


# ── 6. render_output() ───────────────────────────────────────────

class TestRenderOutput(unittest.TestCase):
    """render_output() YAML 片段組合。"""

    def test_inhibit_rules_only(self):
        inhibit = [{"source_matchers": ['severity="critical"', 'tenant="db-a"'],
                     "target_matchers": ['severity="warning"', 'tenant="db-a"'],
                     "equal": ["metric_group"]}]
        output = gen.render_output([], [], inhibit)
        self.assertIn("inhibit_rules", output)
        self.assertNotIn("route:", output)

    def test_all_sections_combined(self):
        routes = [{"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"}]
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://example.com"}]}]
        inhibit = [{"source_matchers": ['severity="critical"'], "target_matchers": ['severity="warning"'], "equal": ["metric_group"]}]
        output = gen.render_output(routes, receivers, inhibit)
        for section in ("route:", "receivers:", "inhibit_rules:"):
            self.assertIn(section, output)

    def test_empty_inhibit_omitted(self):
        output = gen.render_output([], [], [])
        self.assertNotIn("inhibit_rules", output)


# ── 7. SAST ──────────────────────────────────────────────────────

class TestSASTCompliance(unittest.TestCase):
    """SAST 合規: open() 必須帶 encoding。"""

    def test_open_calls_have_encoding(self):
        source = inspect.getsource(gen)
        for call in re.findall(r"open\([^)]+\)", source):
            if "encoding=" not in call:
                self.fail(f"open() missing encoding: {call}")


if __name__ == "__main__":
    unittest.main()
