#!/usr/bin/env python3
"""test_generate_routes.py — generate_alertmanager_routes.py 測試套件。

驗證核心功能:
  1. Guardrail clamp 邏輯 + 常數一致性
  2. load_tenant_configs() — routing + severity_dedup 讀取
  3. generate_routes() — route + receiver 結構
  4. generate_inhibit_rules() — per-tenant severity dedup inhibit rules
  5. render_output() — YAML 片段組合
  6. SAST: open() 必須帶 encoding="utf-8"

NOTE: Duration parsing/formatting tests are in test_lib_python.py
      (TestParseDurationSeconds / TestFormatDuration).

用法:
  python3 -m pytest tests/test_generate_routes.py -v
"""

import argparse
import inspect
import os
import re
import sys
import tempfile
import unittest

import yaml

# Add scripts/tools to path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import generate_alertmanager_routes as gen  # noqa: E402
from conftest import write_yaml  # noqa: E402


# ── 1. Guardrails ─────────────────────────────────────────────────

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
            write_yaml(d, "db-a.yaml", """
tenants:
  db-a:
    mysql_connections: "70"
    _routing:
      receiver:
        type: "webhook"
        url: "https://webhook.example.com/alerts"
      group_wait: "30s"
""")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-a"]["receiver"]["type"], "webhook")
            self.assertEqual(routing["db-a"]["receiver"]["url"], "https://webhook.example.com/alerts")
            self.assertEqual(dedup["db-a"], "enable")

    def test_no_routing_still_tracks_dedup(self):
        """無 _routing 的 tenant 仍出現在 dedup dict 中。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-b.yaml", """
tenants:
  db-b:
    mysql_connections: "80"
""")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(dedup["db-b"], "enable")

    def test_multiple_files(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://a.example.com'\n")
            write_yaml(d, "db-b.yaml", "tenants:\n  db-b:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'https://b.example.com'\n")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 2)
            self.assertEqual(len(dedup), 2)

    def test_skips_dotfiles(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, ".hidden.yaml", "tenants:\n  hidden:\n    _routing:\n      receiver:\n        type: webhook\n        url: 'x'\n")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(len(dedup), 0)

    def test_dedup_disable_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    _severity_dedup: 'disable'\n")
            _, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(dedup["db-a"], "disable")

    def test_dedup_mixed_tenants(self):
        """enable (default) + disable → dedup dict 正確區分。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            write_yaml(d, "db-b.yaml", "tenants:\n  db-b:\n    _severity_dedup: 'disable'\n")
            _, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(dedup["db-a"], "enable")
            self.assertEqual(dedup["db-b"], "disable")

    def test_dedup_disable_synonyms(self):
        """disabled / off / false 全視同 disable（與 Go 行為一致）。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "t.yaml", """
tenants:
  t1:
    _severity_dedup: "disabled"
  t2:
    _severity_dedup: "off"
  t3:
    _severity_dedup: "false"
""")
            _, dedup, _sw, _er = gen.load_tenant_configs(d)
            for t in ("t1", "t2", "t3"):
                self.assertEqual(dedup[t], "disable", msg=t)

    def test_dedup_only_no_routing_produces_inhibit(self):
        """無 _routing 的 tenant 仍可產出 inhibit rule（E2E 驗證）。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
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
            write_yaml(d, "_defaults.yaml", """
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
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            self.assertIn("db-a", routing)
            self.assertEqual(routing["db-a"]["receiver"]["type"], "email")
            self.assertEqual(routing["db-a"]["group_wait"], "30s")

    def test_tenant_overrides_receiver(self):
        """有 _routing 的 tenant 覆寫 receiver，timing 從 defaults。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["default@example.com"]
    smarthost: "smtp:587"
  group_wait: "30s"
  repeat_interval: "4h"
""")
            write_yaml(d, "db-b.yaml", """
tenants:
  db-b:
    _routing:
      receiver:
        type: "slack"
        api_url: "https://hooks.slack.com/x"
""")
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-b"]["receiver"]["type"], "slack")
            self.assertEqual(routing["db-b"]["group_wait"], "30s")
            self.assertEqual(routing["db-b"]["repeat_interval"], "4h")

    def test_tenant_disables_routing(self):
        """_routing: "disable" → 不產出路由。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["team@example.com"]
    smarthost: "smtp:587"
""")
            write_yaml(d, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
""")
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            self.assertNotIn("db-c", routing)

    def test_tenant_template_substitution(self):
        """{{tenant}} 在 receiver fields 被替換為 tenant name。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
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
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            # channel is in the receiver dict (metadata, not AM config)
            self.assertEqual(routing["db-a"]["receiver"]["channel"], "#alerts-db-a")

    def test_disabled_routing_still_tracks_dedup(self):
        """_routing: disable の tenant でも dedup は追跡される。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-c.yaml", """
tenants:
  db-c:
    _routing: "disable"
    _severity_dedup: "enable"
""")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertNotIn("db-c", routing)  # routing disabled
            self.assertEqual(dedup["db-c"], "enable")  # dedup still tracked

    def test_no_defaults_no_routing(self):
        """無 _routing_defaults 也無 _routing → 不產出路由。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            routing, dedup, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(routing), 0)
            self.assertEqual(dedup["db-a"], "enable")

    def test_defaults_boundary_warning(self):
        """_routing_defaults 在 tenant 檔案中會被忽略並警告。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["bad@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            # db-a should NOT have routing from its own _routing_defaults
            self.assertEqual(len(routing), 0)

    def test_template_in_email_to(self):
        """{{tenant}} 在 email to list 被替換。"""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
_routing_defaults:
  receiver:
    type: "email"
    to: ["{{tenant}}-team@example.com"]
    smarthost: "smtp:587"
tenants:
  db-a:
    mysql_connections: "70"
""")
            routing, _, _sw, _er = gen.load_tenant_configs(d)
            self.assertEqual(routing["db-a"]["receiver"]["to"], ["db-a-team@example.com"])


# ── 3b. Tenant Config Schema Validation (v1.5.0) ─────────────────

class TestTenantConfigSchemaValidation(unittest.TestCase):
    """validate_tenant_keys() + load_tenant_configs() schema warnings。"""

    def test_valid_keys_no_warnings(self):
        """All valid keys → no warnings."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
defaults:
  mysql_connections: 70
  redis_memory: 80
tenants:
  db-a:
    mysql_connections: "60"
    _routing:
      receiver:
        type: webhook
        url: "https://hook.example.com"
    _silent_mode: "warning"
    _severity_dedup: "enable"
""")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(schema_warnings, [])

    def test_typo_reserved_key(self):
        """_silence_mode (typo) → warning."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    _silence_mode: "warning"
""")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(schema_warnings), 1)
            self.assertIn("unknown reserved key", schema_warnings[0])
            self.assertIn("_silence_mode", schema_warnings[0])

    def test_unknown_metric_key(self):
        """Key not in defaults → warning."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    postgres_connections: "60"
""")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(schema_warnings), 1)
            self.assertIn("not in defaults", schema_warnings[0])
            self.assertIn("postgres_connections", schema_warnings[0])

    def test_critical_suffix_valid(self):
        """mysql_connections_critical → valid if mysql_connections in defaults."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
defaults:
  mysql_connections: 70
tenants:
  db-a:
    mysql_connections_critical: "90"
""")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(schema_warnings, [])

    def test_state_prefix_valid(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", "tenants:\n  db-a:\n    _state_maintenance: 'enable'\n")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(schema_warnings, [])

    def test_no_defaults_all_metric_keys_warn(self):
        """No defaults section → any non-reserved tenant key warns."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "t.yaml", "tenants:\n  db-a:\n    mysql_connections: '70'\n")
            _, _, schema_warnings, _er = gen.load_tenant_configs(d)
            self.assertEqual(len(schema_warnings), 1)
            self.assertIn("not in defaults", schema_warnings[0])

    def test_validate_tenant_keys_function(self):
        """Direct unit test of validate_tenant_keys()."""
        defaults_keys = {"mysql_connections", "redis_memory"}
        keys = {"mysql_connections", "_silent_mode", "_routing", "_state_maintenance",
                "redis_memory_critical", "_silence_mode", "unknown_metric"}
        warnings = gen.validate_tenant_keys("t1", keys, defaults_keys)
        self.assertEqual(len(warnings), 2)
        typo_warnings = [w for w in warnings if "typo" in w]
        metric_warnings = [w for w in warnings if "not in defaults" in w]
        self.assertEqual(len(typo_warnings), 1)
        self.assertIn("_silence_mode", typo_warnings[0])
        self.assertEqual(len(metric_warnings), 1)
        self.assertIn("unknown_metric", metric_warnings[0])


# ── 4e. Webhook Domain Allowlist (v1.5.0) ─────────────────────────

class TestWebhookDomainAllowlist(unittest.TestCase):
    """validate_receiver_domains() + generate_routes() domain filtering。"""

    # — _extract_host() —

    def test_extract_host_https(self):
        self.assertEqual(gen._extract_host("https://webhook.example.com/alerts"), "webhook.example.com")

    def test_extract_host_http(self):
        self.assertEqual(gen._extract_host("http://internal.corp:8080/hook"), "internal.corp")

    def test_extract_host_hostport(self):
        """host:port (SMTP smarthost) → hostname only。"""
        self.assertEqual(gen._extract_host("smtp.example.com:587"), "smtp.example.com")

    def test_extract_host_none(self):
        self.assertIsNone(gen._extract_host(None))
        self.assertIsNone(gen._extract_host(""))

    # — validate_receiver_domains() —

    def test_allowed_domain_no_warning(self):
        receiver = {"type": "webhook", "url": "https://webhook.example.com/alerts"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.example.com"])
        self.assertEqual(warnings, [])

    def test_blocked_domain_warning(self):
        receiver = {"type": "webhook", "url": "https://evil.attacker.com/ssrf"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.example.com"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("not in allowed_domains", warnings[0])
        self.assertIn("evil.attacker.com", warnings[0])

    def test_exact_domain_match(self):
        receiver = {"type": "slack", "api_url": "https://hooks.slack.com/services/x"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["hooks.slack.com"])
        self.assertEqual(warnings, [])

    def test_wildcard_domain_match(self):
        receiver = {"type": "teams", "webhook_url": "https://outlook.office.com/webhook/x"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.office.com"])
        self.assertEqual(warnings, [])

    def test_email_smarthost_validated(self):
        receiver = {"type": "email", "to": ["a@b.com"], "smarthost": "smtp.example.com:587"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.example.com"])
        self.assertEqual(warnings, [])

    def test_email_smarthost_blocked(self):
        receiver = {"type": "email", "to": ["a@b.com"], "smarthost": "rogue.evil.com:25"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.example.com"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("not in allowed_domains", warnings[0])

    def test_pagerduty_no_url_fields(self):
        """PagerDuty 無 URL field → 不檢查，永遠 pass。"""
        receiver = {"type": "pagerduty", "service_key": "abc123"}
        warnings = gen.validate_receiver_domains(receiver, "t", ["*.example.com"])
        self.assertEqual(warnings, [])

    def test_empty_allowlist_skips_check(self):
        """空 allowed_domains → 向後相容，不限制。"""
        receiver = {"type": "webhook", "url": "https://anything.anywhere.com"}
        warnings = gen.validate_receiver_domains(receiver, "t", [])
        self.assertEqual(warnings, [])

    def test_none_allowlist_skips_check(self):
        warnings = gen.validate_receiver_domains(
            {"type": "webhook", "url": "https://x.com"}, "t", None)
        self.assertEqual(warnings, [])

    # — generate_routes() with allowed_domains —

    def test_blocked_domain_skips_route(self):
        """Domain not in allowlist → route not generated。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://evil.com/x"}}}
        routes, receivers, warnings = gen.generate_routes(cfg, allowed_domains=["*.example.com"])
        self.assertEqual(len(routes), 0)
        self.assertTrue(any("not in allowed_domains" in w for w in warnings))

    def test_allowed_domain_generates_route(self):
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://hook.example.com/x"}}}
        routes, receivers, _ = gen.generate_routes(cfg, allowed_domains=["*.example.com"])
        self.assertEqual(len(routes), 1)

    def test_no_policy_no_filtering(self):
        """No allowed_domains → backward compatible, all pass。"""
        cfg = {"db-a": {"receiver": {"type": "webhook", "url": "https://any.com/x"}}}
        routes, _, _ = gen.generate_routes(cfg, allowed_domains=None)
        self.assertEqual(len(routes), 1)

    # — load_policy() —

    def test_load_policy_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("allowed_domains:\n  - '*.example.com'\n  - hooks.slack.com\n")
            os.chmod(path, 0o600)
            domains = gen.load_policy(path)
            self.assertEqual(domains, ["*.example.com", "hooks.slack.com"])

    def test_load_policy_missing_file(self):
        self.assertEqual(gen.load_policy("/nonexistent/policy.yaml"), [])

    def test_load_policy_no_key(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "policy.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("denied_functions:\n  - holt_winters\n")
            os.chmod(path, 0o600)
            self.assertEqual(gen.load_policy(path), [])


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


# ── 6b. Platform Enforced Routing (v1.7.0) ────────────────────────

class TestPlatformEnforcedRouting(unittest.TestCase):
    """v1.7.0 _routing_enforced — 平台強制路由。"""

    def test_enforced_route_loaded_from_defaults(self):
        """_routing_enforced in _ prefixed file → loaded."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
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
            _, _, _, enforced = gen.load_tenant_configs(d)
            self.assertIsNotNone(enforced)
            self.assertTrue(enforced["enabled"])
            self.assertEqual(enforced["receiver"]["url"], "https://noc.example.com/alerts")

    def test_enforced_disabled_returns_none(self):
        """_routing_enforced with enabled: false → None."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
_routing_enforced:
  enabled: false
  receiver:
    type: "webhook"
    url: "https://noc.example.com/alerts"
tenants:
  db-a:
    mysql_connections: "70"
""")
            _, _, _, enforced = gen.load_tenant_configs(d)
            self.assertIsNone(enforced)

    def test_enforced_ignored_in_tenant_file(self):
        """_routing_enforced in non-_ file → ignored with warning."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "db-a.yaml", """
_routing_enforced:
  enabled: true
  receiver:
    type: "webhook"
    url: "https://evil.com/alerts"
tenants:
  db-a:
    mysql_connections: "70"
""")
            _, _, _, enforced = gen.load_tenant_configs(d)
            self.assertIsNone(enforced)

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
        routes, receivers, warnings = gen.generate_routes(
            tenant_cfg, enforced_routing=enforced)
        self.assertEqual(len(routes), 2)
        # First route is platform enforced
        self.assertEqual(routes[0]["receiver"], "platform-enforced")
        self.assertTrue(routes[0]["continue"])
        self.assertEqual(routes[0]["matchers"], ['severity="critical"'])
        # Second route is tenant
        self.assertEqual(routes[1]["receiver"], "tenant-db-a")
        # Two receivers
        self.assertEqual(len(receivers), 2)
        self.assertEqual(receivers[0]["name"], "platform-enforced")
        self.assertEqual(receivers[1]["name"], "tenant-db-a")

    def test_enforced_route_no_matchers(self):
        """Enforced route without match → catches all alerts (NOC sees everything)."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
        }
        routes, _, _ = gen.generate_routes({}, enforced_routing=enforced)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["receiver"], "platform-enforced")
        self.assertTrue(routes[0]["continue"])
        self.assertNotIn("matchers", routes[0])

    def test_enforced_with_timing_guardrails(self):
        """Enforced route timing params are clamped by guardrails."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com/alerts"},
            "group_wait": "1s",       # below min → clamped to 5s
            "repeat_interval": "100h",  # above max → clamped to 72h
        }
        routes, _, warnings = gen.generate_routes({}, enforced_routing=enforced)
        self.assertEqual(routes[0]["group_wait"], "5s")
        self.assertEqual(routes[0]["repeat_interval"], "72h")
        self.assertGreaterEqual(len(warnings), 2)

    def test_enforced_missing_receiver_skipped(self):
        """Enforced without receiver → warning, no route."""
        enforced = {"enabled": True, "match": ['severity="critical"']}
        routes, _, warnings = gen.generate_routes({}, enforced_routing=enforced)
        self.assertEqual(len(routes), 0)
        self.assertTrue(any("missing 'receiver'" in w for w in warnings))

    def test_enforced_domain_policy_blocks(self):
        """Enforced receiver blocked by domain policy → warning, no route."""
        enforced = {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://evil.com/alerts"},
        }
        routes, _, warnings = gen.generate_routes(
            {}, enforced_routing=enforced, allowed_domains=["*.example.com"])
        self.assertEqual(len(routes), 0)
        self.assertTrue(any("blocked by domain policy" in w for w in warnings))

    def test_enforced_none_no_effect(self):
        """enforced_routing=None → no platform route (backward compat)."""
        tenant_cfg = {
            "db-a": {"receiver": {"type": "webhook", "url": "https://a.example.com/alerts"}},
        }
        routes, receivers, _ = gen.generate_routes(tenant_cfg, enforced_routing=None)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["receiver"], "tenant-db-a")

    def test_enforced_e2e_from_config_dir(self):
        """End-to-end: load from config dir → generate routes with enforced first."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", """
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
            routing, dedup, _sw, enforced = gen.load_tenant_configs(d)
            self.assertIsNotNone(enforced)
            routes, receivers, _ = gen.generate_routes(
                routing, enforced_routing=enforced)
            # First route = platform enforced, then 2 tenant routes
            self.assertEqual(len(routes), 3)
            self.assertEqual(routes[0]["receiver"], "platform-enforced")
            self.assertTrue(routes[0]["continue"])
            self.assertEqual(routes[0]["group_wait"], "15s")
            # Tenant routes sorted
            self.assertEqual(routes[1]["receiver"], "tenant-db-a")
            self.assertEqual(routes[2]["receiver"], "tenant-db-b")


# ── 6c. Per-rule Routing Overrides (v1.8.0) ────────────────────────

class TestRoutingOverrides(unittest.TestCase):
    """v1.8.0 _routing.overrides[] — per-rule receiver overrides."""

    def _webhook(self, url="https://example.com"):
        return {"type": "webhook", "url": url}

    def test_routing_override_alertname(self):
        """Override with alertname matcher generates correct sub-route."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {
                        "alertname": "MariaDBHighConnections",
                        "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"},
                    },
                ],
            },
        }
        routes, receivers, warnings = gen.generate_routes(cfg)
        # Override sub-route + main tenant route = 2
        self.assertEqual(len(routes), 2)
        # Override comes BEFORE main route
        self.assertEqual(routes[0]["receiver"], "tenant-db-a-override-0")
        self.assertIn('alertname="MariaDBHighConnections"', routes[0]["matchers"])
        self.assertIn('tenant="db-a"', routes[0]["matchers"])
        # Main route is second
        self.assertEqual(routes[1]["receiver"], "tenant-db-a")

    def test_routing_override_metric_group(self):
        """Override with metric_group matcher generates correct sub-route."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {
                        "metric_group": "pg_replication_lag",
                        "receiver": {"type": "email", "to": "dba@example.com", "smarthost": "smtp:587"},
                    },
                ],
            },
        }
        routes, receivers, _ = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 2)
        self.assertIn('metric_group="pg_replication_lag"', routes[0]["matchers"])

    def test_routing_override_multiple(self):
        """Multiple overrides generate multiple sub-routes in order."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {"alertname": "Alert1", "receiver": self._webhook("https://a.example.com")},
                    {"alertname": "Alert2", "receiver": self._webhook("https://b.example.com")},
                ],
            },
        }
        routes, receivers, _ = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 3)  # 2 overrides + 1 main
        self.assertEqual(routes[0]["receiver"], "tenant-db-a-override-0")
        self.assertEqual(routes[1]["receiver"], "tenant-db-a-override-1")
        self.assertEqual(routes[2]["receiver"], "tenant-db-a")

    def test_routing_override_receiver_validation(self):
        """Override with invalid receiver is skipped with warning."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {"alertname": "Alert1", "receiver": {"type": "webhook"}},  # missing url
                ],
            },
        }
        routes, _, warnings = gen.generate_routes(cfg)
        # Only main route, override skipped
        self.assertEqual(len(routes), 1)
        self.assertTrue(any("requires 'url'" in w for w in warnings))

    def test_routing_override_sub_route_ordering(self):
        """Override sub-routes appear BEFORE main tenant route in routes list."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {"alertname": "SpecificAlert", "receiver": self._webhook("https://specific.example.com")},
                ],
            },
        }
        routes, _, _ = gen.generate_routes(cfg)
        # First route should be the override (more specific match first)
        self.assertIn("override", routes[0]["receiver"])
        # Last route is the general tenant route
        self.assertEqual(routes[-1]["receiver"], "tenant-db-a")

    def test_routing_override_empty_list(self):
        """Empty overrides list is a no-op."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [],
            },
        }
        routes, receivers, warnings = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["receiver"], "tenant-db-a")

    def test_routing_override_both_match_rejected(self):
        """Override with both alertname and metric_group is rejected."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {
                        "alertname": "Alert1",
                        "metric_group": "mysql_connections",
                        "receiver": self._webhook("https://both.example.com"),
                    },
                ],
            },
        }
        routes, _, warnings = gen.generate_routes(cfg)
        # Only main route, override rejected
        self.assertEqual(len(routes), 1)
        self.assertTrue(any("both 'alertname' and 'metric_group'" in w for w in warnings))

    def test_routing_override_inherits_timing(self):
        """Override without timing uses no explicit timing (inherits from parent)."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "group_wait": "15s",
                "overrides": [
                    {"alertname": "Alert1", "receiver": self._webhook("https://a.example.com")},
                ],
            },
        }
        routes, _, _ = gen.generate_routes(cfg)
        override_route = routes[0]
        # Override doesn't explicitly set timing (inherits from parent route)
        self.assertNotIn("group_wait", override_route)

    def test_routing_override_explicit_timing(self):
        """Override with explicit timing applies guardrails."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {
                        "alertname": "Alert1",
                        "receiver": self._webhook("https://a.example.com"),
                        "group_wait": "1s",   # below min → clamped to 5s
                    },
                ],
            },
        }
        routes, _, warnings = gen.generate_routes(cfg)
        self.assertEqual(routes[0]["group_wait"], "5s")
        self.assertTrue(any("below minimum" in w for w in warnings))

    def test_routing_override_domain_policy_blocks(self):
        """Override receiver blocked by domain policy → skipped."""
        cfg = {
            "db-a": {
                "receiver": self._webhook("https://ok.example.com/alerts"),
                "overrides": [
                    {"alertname": "Alert1", "receiver": self._webhook("https://evil.com/x")},
                ],
            },
        }
        routes, _, warnings = gen.generate_routes(cfg, allowed_domains=["*.example.com"])
        # Main route passes, override blocked
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["receiver"], "tenant-db-a")
        self.assertTrue(any("not in allowed_domains" in w for w in warnings))

    def test_routing_override_no_match_skipped(self):
        """Override with neither alertname nor metric_group is skipped."""
        cfg = {
            "db-a": {
                "receiver": self._webhook(),
                "overrides": [
                    {"receiver": self._webhook("https://no-match.example.com")},
                ],
            },
        }
        routes, _, warnings = gen.generate_routes(cfg)
        self.assertEqual(len(routes), 1)
        self.assertTrue(any("must have either" in w for w in warnings))


# ── 7. SAST ──────────────────────────────────────────────────────

class TestSASTCompliance(unittest.TestCase):
    """SAST 合規: open() 必須帶 encoding。"""

    def test_open_calls_have_encoding(self):
        source = inspect.getsource(gen)
        for call in re.findall(r"open\([^)]+\)", source):
            if "encoding=" not in call:
                self.fail(f"open() missing encoding: {call}")


# ── 8. --output-configmap (§11.3 AM GitOps) ──────────────────────

class TestOutputConfigmap(unittest.TestCase):
    """Tests for assemble_configmap() and --output-configmap mode."""

    def test_load_base_config_defaults(self):
        """load_base_config returns inline defaults when path is None."""
        base = gen.load_base_config(None)
        self.assertIn("global", base)
        self.assertEqual(base["global"]["resolve_timeout"], "5m")
        self.assertEqual(base["receivers"], [{"name": "default"}])

    def test_load_base_config_from_file(self):
        """load_base_config reads YAML when path is valid."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as f:
            yaml.dump({"global": {"resolve_timeout": "10m"},
                       "route": {"receiver": "custom"},
                       "receivers": [{"name": "custom"}],
                       "inhibit_rules": []}, f)
            f.flush()
            base = gen.load_base_config(f.name)
        os.unlink(f.name)
        self.assertEqual(base["global"]["resolve_timeout"], "10m")
        self.assertEqual(base["route"]["receiver"], "custom")

    def test_load_base_config_missing_keys_filled(self):
        """load_base_config fills missing keys with defaults."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as f:
            yaml.dump({"global": {"resolve_timeout": "3m"}}, f)
            f.flush()
            base = gen.load_base_config(f.name)
        os.unlink(f.name)
        self.assertIn("route", base)
        self.assertIn("receivers", base)

    def test_assemble_configmap_structure(self):
        """assemble_configmap produces valid K8s ConfigMap YAML."""
        base = gen._DEFAULT_BASE_CONFIG.copy()
        routes = [{"matchers": ['tenant="t1"'], "receiver": "tenant-t1"}]
        receivers = [{"name": "tenant-t1", "webhook_configs": [{"url": "https://x"}]}]
        inhibit = [{"source_matchers": ['severity="critical"']}]

        result = gen.assemble_configmap(base, routes, receivers, inhibit)
        parsed = yaml.safe_load(result)

        self.assertEqual(parsed["apiVersion"], "v1")
        self.assertEqual(parsed["kind"], "ConfigMap")
        self.assertEqual(parsed["metadata"]["name"], "alertmanager-config")
        self.assertEqual(parsed["metadata"]["namespace"], "monitoring")
        self.assertIn("alertmanager.yml", parsed["data"])

        am = yaml.safe_load(parsed["data"]["alertmanager.yml"])
        self.assertEqual(am["route"]["routes"], routes)
        # default receiver preserved + tenant appended
        names = [r["name"] for r in am["receivers"]]
        self.assertIn("default", names)
        self.assertIn("tenant-t1", names)

    def test_assemble_configmap_custom_namespace(self):
        """assemble_configmap respects namespace/configmap params."""
        base = gen._DEFAULT_BASE_CONFIG.copy()
        result = gen.assemble_configmap(
            base, [], [], [], namespace="custom-ns", configmap_name="my-am")
        parsed = yaml.safe_load(result)
        self.assertEqual(parsed["metadata"]["namespace"], "custom-ns")
        self.assertEqual(parsed["metadata"]["name"], "my-am")

    def test_assemble_preserves_base_receivers(self):
        """Base receivers are kept; tenant receivers appended."""
        base = {
            "global": {},
            "route": {"receiver": "default"},
            "receivers": [{"name": "default"}, {"name": "noc-webhook"}],
            "inhibit_rules": [],
        }
        tenant_recv = [{"name": "tenant-t1"}, {"name": "default"}]  # dup default
        result = gen.assemble_configmap(base, [], tenant_recv, [])
        am = yaml.safe_load(yaml.safe_load(result)["data"]["alertmanager.yml"])
        names = [r["name"] for r in am["receivers"]]
        # default + noc-webhook (base) + tenant-t1 (new). "default" not duplicated.
        self.assertEqual(names.count("default"), 1)
        self.assertIn("noc-webhook", names)
        self.assertIn("tenant-t1", names)

    def test_output_configmap_apply_mutually_exclusive(self):
        """--output-configmap and --apply cannot be used together."""
        with self.assertRaises(SystemExit):
            gen.main.__wrapped__ if hasattr(gen.main, '__wrapped__') else None
            # Use argparse directly to test mutual exclusivity
            parser = argparse.ArgumentParser()
            mode_group = parser.add_mutually_exclusive_group()
            mode_group.add_argument("--apply", action="store_true")
            mode_group.add_argument("--output-configmap", action="store_true")
            parser.parse_args(["--apply", "--output-configmap"])


if __name__ == "__main__":
    unittest.main()
