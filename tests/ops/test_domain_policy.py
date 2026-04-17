#!/usr/bin/env python3
"""test_domain_policy.py — Webhook domain allowlist policy 測試。

驗證 validate_receiver_domains() 的 fnmatch 邊界案例：
  - 精確匹配、wildcard、subdomain
  - 多模式 OR 邏輯
  - 空 allowlist = 不限制
  - 非 webhook 類型忽略
  - generate_routes 整合 policy
"""

import os
import tempfile

import pytest
import yaml

from factories import make_receiver, make_routing_config, write_yaml

from generate_alertmanager_routes import (
    validate_receiver_domains,
    load_policy,
    generate_routes,
)


# ── validate_receiver_domains unit tests ─────────────────────


class TestValidateReceiverDomains:
    """fnmatch domain allowlist 邊界測試。"""

    def test_exact_match_allowed(self):
        """精確匹配 domain 通過。"""
        receiver = {"type": "webhook", "url": "https://hooks.example.com/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert warnings == []

    def test_exact_match_blocked(self):
        """不在 allowlist 的 domain 被阻擋。"""
        receiver = {"type": "webhook", "url": "https://evil.example.com/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert len(warnings) == 1
        assert "not in allowed_domains" in warnings[0]

    def test_wildcard_subdomain(self):
        """*.example.com 匹配所有子網域。"""
        receiver = {"type": "webhook", "url": "https://any.example.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_wildcard_blocks_different_tld(self):
        """*.example.com 不匹配 example.org。"""
        receiver = {"type": "webhook", "url": "https://hook.example.org/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert len(warnings) == 1

    def test_multiple_patterns_or_logic(self):
        """多個 pattern 任一匹配即通過。"""
        receiver = {"type": "webhook", "url": "https://slack.com/hook"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.example.com", "slack.com"])
        assert warnings == []

    def test_empty_allowlist_no_restriction(self):
        """空 allowlist = 不限制。"""
        receiver = {"type": "webhook", "url": "https://anything.evil.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", [])
        assert warnings == []

    def test_none_allowlist_no_restriction(self):
        """None allowlist = 不限制。"""
        receiver = {"type": "webhook", "url": "https://anything.com/hook"}
        warnings = validate_receiver_domains(receiver, "db-a", None)
        assert warnings == []

    def test_non_webhook_type_ignored(self):
        """非 webhook（如 pagerduty）不檢查 domain。"""
        receiver = {"type": "pagerduty", "service_key": "abc123"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_slack_api_url_checked(self):
        """Slack api_url 也進行 domain 檢查。"""
        receiver = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/X"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.slack.com"])
        assert warnings == []

    def test_slack_api_url_blocked(self):
        """Slack api_url 被阻擋。"""
        receiver = {"type": "slack", "api_url": "https://evil.slack.com/services/T/B/X"}
        warnings = validate_receiver_domains(
            receiver, "db-a", ["hooks.slack.com"])
        assert len(warnings) == 1

    def test_email_smarthost_checked(self):
        """Email 的 smarthost 也進行 domain 檢查。"""
        receiver = {"type": "email", "to": "dba@example.com", "smarthost": "smtp.example.com:587"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.allowed.com"])
        assert len(warnings) >= 1
        assert "smtp.example.com" in warnings[0]

    def test_email_smarthost_allowed(self):
        """Email smarthost 在 allowlist 中通過。"""
        receiver = {"type": "email", "to": "dba@example.com", "smarthost": "smtp.example.com:587"}
        warnings = validate_receiver_domains(receiver, "db-a", ["*.example.com"])
        assert warnings == []

    def test_non_dict_receiver_returns_empty(self):
        """非 dict receiver 回傳空 warnings。"""
        warnings = validate_receiver_domains("not-a-dict", "db-a", ["*.example.com"])
        assert warnings == []

    def test_url_with_port(self):
        """URL 含 port 的 domain 解析。"""
        receiver = {"type": "webhook", "url": "https://hooks.example.com:8443/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", ["hooks.example.com"])
        assert warnings == []

    @pytest.mark.parametrize("pattern,host,expected_pass", [
        ("*", "any.domain.com", True),
        ("*.internal.corp", "alerts.internal.corp", True),
        ("*.internal.corp", "external.com", False),
        ("hooks.example.com", "hooks.example.com", True),
        ("hooks.example.com", "HOOKS.EXAMPLE.COM", True),  # fnmatch 在 Linux 不區分大小寫（視 OS）
    ], ids=["wildcard-all", "subdomain-match", "subdomain-no-match",
            "exact-match", "case-insensitive"])
    def test_fnmatch_patterns(self, pattern, host, expected_pass):
        """fnmatch pattern 邊界驗證。"""
        receiver = {"type": "webhook", "url": f"https://{host}/alert"}
        warnings = validate_receiver_domains(receiver, "db-a", [pattern])
        if expected_pass:
            assert warnings == [], f"預期通過但被阻擋: {host} vs {pattern}"
        else:
            assert len(warnings) >= 1, f"預期阻擋但通過: {host} vs {pattern}"


# ── load_policy ──────────────────────────────────────────────


class TestLoadPolicy:
    """load_policy() YAML 載入測試。"""

    def test_valid_policy_file(self, config_dir):
        """合法 policy YAML 載入 allowed_domains。"""
        policy = {"allowed_domains": ["*.example.com", "hooks.slack.com"]}
        path = write_yaml(config_dir, "policy.yaml", yaml.dump(policy))
        domains = load_policy(path)
        assert domains == ["*.example.com", "hooks.slack.com"]

    def test_empty_policy_file(self, config_dir):
        """空 YAML policy 回傳空 list。"""
        path = write_yaml(config_dir, "policy.yaml", "")
        domains = load_policy(path)
        assert domains == []

    def test_nonexistent_policy(self):
        """不存在的 policy 檔案回傳空 list。"""
        assert load_policy("/nonexistent/policy.yaml") == []

    def test_none_path(self):
        """None path 回傳空 list。"""
        assert load_policy(None) == []

    def test_policy_without_allowed_domains_key(self, config_dir):
        """YAML 缺 allowed_domains key 回傳空 list。"""
        path = write_yaml(config_dir, "policy.yaml", yaml.dump({"other_key": "value"}))
        domains = load_policy(path)
        assert domains == []


# ── generate_routes + policy integration ─────────────────────


class TestGenerateRoutesWithPolicy:
    """generate_routes() 整合 domain policy。"""

    def test_blocked_receiver_produces_warning(self):
        """被 policy 阻擋的 receiver 產生 warning。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://evil.com/hook"},
            }
        }
        _, _, warnings = generate_routes(
            routing_configs, allowed_domains=["*.example.com"])
        domain_warns = [w for w in warnings if "not in allowed_domains" in w]
        assert len(domain_warns) >= 1

    def test_allowed_receiver_no_warning(self):
        """符合 policy 的 receiver 不產生 domain warning。"""
        routing_configs = {
            "db-a": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
            }
        }
        _, _, warnings = generate_routes(
            routing_configs, allowed_domains=["*.example.com"])
        domain_warns = [w for w in warnings if "not in allowed_domains" in w]
        assert domain_warns == []
