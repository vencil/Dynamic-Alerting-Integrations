"""test_parse_platform_config.py — Unit tests for refactored parser sub-functions.

Tests _parse_platform_config() and _parse_tenant_overrides() individually,
verifying each branch and edge case of the refactored monolith.
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from generate_alertmanager_routes import (  # noqa: E402
    _parse_platform_config,
    _parse_tenant_overrides,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _empty_result() -> dict:
    """Return a fresh result dict matching _parse_config_files() initial state."""
    return {
        "all_tenants": [],
        "defaults_keys": set(),
        "optional_override_keys": set(),
        "routing_defaults": {},
        "enforced_routing": None,
        "explicit_routing": {},
        "disabled_tenants": set(),
        "dedup_configs": {},
        "metadata_configs": {},
        "tenant_keys": {},
        "routing_profiles": {},
        "domain_policies": {},
        "tenant_profile_refs": {},
        # These two were missing while the docstring above claimed parity —
        # _parse_platform_config only survived it because the domain-policy
        # branch uses setdefault. Buckets read with plain [] (defaults_keys,
        # optional_override_keys) would KeyError instead, so the helper has to
        # actually match.
        "policy_misplacements": [],
        "policy_file_errors": [],
    }


# ===========================================================================
# _parse_platform_config tests
# ===========================================================================

class TestParsePlatformConfig:
    """Unit tests for _parse_platform_config()."""

    def test_defaults_keys_extracted(self):
        result = _empty_result()
        data = {"defaults": {"cpu": "80", "mem": "85"}}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["defaults_keys"] == {"cpu", "mem"}

    def test_defaults_non_dict_ignored(self):
        result = _empty_result()
        data = {"defaults": "not-a-dict"}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["defaults_keys"] == set()

    # ── #1189 / TRK-337 optional_overrides（宣告但無值的第三格）──────────

    def test_optional_overrides_extracted_from_platform_file(self):
        result = _empty_result()
        data = {"optional_overrides": ["oracle_wait_time_rate", "db2_deadlock_rate"]}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["optional_override_keys"] == {
            "oracle_wait_time_rate", "db2_deadlock_rate"}

    def test_optional_overrides_ignored_from_tenant_file(self):
        """⛔ 平台範疇：租戶自有檔列 key＝自我授權繞過寫入閘門。"""
        result = _empty_result()
        data = {"optional_overrides": ["anything_i_want"]}
        _parse_platform_config(data, "db-a.yaml", result)
        assert result["optional_override_keys"] == set()

    # ⚠️ 這三格的差別是「吵 vs 安靜」，不只是結果集合：非 list 要吵、null 要
    # 安靜、非字串項目要吵。parser 的註解說明了為何那聲吵是承重的——同一份
    # bytes 在 Go 側會讓整個檔案 unmarshal 失敗、連 defaults 一起消失，所以
    # Python 這邊安靜丟掉就會讓 CI 對「會清空寫入平面的設定」保持綠。只斷言
    # 集合等於沒把那個區別釘住。
    def test_optional_overrides_non_list_ignored(self, capsys):
        """非 list（含 dict / 字串）不得被當成成員集合，且必須吵。"""
        for bad in ("not-a-list", {"a": 1}, 42):
            result = _empty_result()
            _parse_platform_config(
                {"optional_overrides": bad}, "_defaults.yaml", result)
            assert result["optional_override_keys"] == set(), bad
            assert "WARN" in capsys.readouterr().err, bad

    def test_optional_overrides_null_is_empty_not_error(self, capsys):
        """`optional_overrides:`（null）是合法的空宣告，schema 也允許——不得吵。"""
        result = _empty_result()
        _parse_platform_config(
            {"optional_overrides": None}, "_defaults.yaml", result)
        assert result["optional_override_keys"] == set()
        assert "WARN" not in capsys.readouterr().err

    def test_optional_overrides_non_string_entries_dropped(self, capsys):
        """混入非字串項目時只收字串，不整包丟棄也不 raise，但要吵。"""
        result = _empty_result()
        _parse_platform_config(
            {"optional_overrides": ["ok_key", 7, None, {"k": "v"}]},
            "_defaults.yaml", result)
        assert result["optional_override_keys"] == {"ok_key"}
        err = capsys.readouterr().err
        assert "WARN" in err
        # 吵的內容要說出「Go 會整檔失敗」，否則讀 log 的人不知道嚴重度
        assert "whole file" in err

    def test_optional_overrides_unions_across_platform_files(self):
        """多個 `_` 前綴檔的宣告取聯集（與 defaults_keys 同語意）。"""
        result = _empty_result()
        _parse_platform_config({"optional_overrides": ["a"]}, "_defaults.yaml", result)
        _parse_platform_config({"optional_overrides": ["b"]}, "_extra.yaml", result)
        assert result["optional_override_keys"] == {"a", "b"}

    def test_routing_defaults_from_underscore_file(self):
        result = _empty_result()
        data = {"_routing_defaults": {"group_wait": "30s"}}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["routing_defaults"] == {"group_wait": "30s"}

    def test_routing_defaults_ignored_from_tenant_file(self, capsys):
        result = _empty_result()
        data = {"_routing_defaults": {"group_wait": "30s"}}
        _parse_platform_config(data, "db-a.yaml", result)
        assert result["routing_defaults"] == {}
        assert "WARN" in capsys.readouterr().err

    def test_routing_enforced_enabled(self):
        result = _empty_result()
        data = {"_routing_enforced": {
            "enabled": True,
            "receiver": {"type": "webhook", "url": "https://noc.example.com"},
        }}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["enforced_routing"]["enabled"] is True
        assert result["enforced_routing"]["receiver"]["type"] == "webhook"

    def test_routing_enforced_disabled(self):
        result = _empty_result()
        data = {"_routing_enforced": {"enabled": False}}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["enforced_routing"] is None

    def test_routing_enforced_non_dict_warns(self, capsys):
        result = _empty_result()
        data = {"_routing_enforced": "bad"}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["enforced_routing"] is None
        assert "WARN" in capsys.readouterr().err

    def test_routing_enforced_from_tenant_file_warns(self, capsys):
        result = _empty_result()
        data = {"_routing_enforced": {"enabled": True}}
        _parse_platform_config(data, "db-a.yaml", result)
        assert result["enforced_routing"] is None
        assert "WARN" in capsys.readouterr().err

    def test_routing_profiles_from_correct_file(self):
        result = _empty_result()
        data = {"routing_profiles": {
            "team-sre": {"group_wait": "15s"},
            "team-dba": {"repeat_interval": "1h"},
        }}
        _parse_platform_config(data, "_routing_profiles.yaml", result)
        assert "team-sre" in result["routing_profiles"]
        assert "team-dba" in result["routing_profiles"]

    def test_routing_profiles_from_wrong_file_warns(self, capsys):
        result = _empty_result()
        data = {"routing_profiles": {"team-sre": {"group_wait": "15s"}}}
        _parse_platform_config(data, "_defaults.yaml", result)
        assert result["routing_profiles"] == {}
        assert "WARN" in capsys.readouterr().err

    def test_routing_profiles_non_dict_warns(self, capsys):
        result = _empty_result()
        data = {"routing_profiles": "bad"}
        _parse_platform_config(data, "_routing_profiles.yaml", result)
        assert result["routing_profiles"] == {}
        assert "WARN" in capsys.readouterr().err

    def test_routing_profiles_yml_extension(self):
        result = _empty_result()
        data = {"routing_profiles": {"p1": {"group_wait": "10s"}}}
        _parse_platform_config(data, "_routing_profiles.yml", result)
        assert "p1" in result["routing_profiles"]

    def test_domain_policies_from_correct_file(self):
        result = _empty_result()
        data = {"domain_policies": {
            "finance": {"tenants": ["db-a"], "constraints": {}},
        }}
        _parse_platform_config(data, "_domain_policy.yaml", result)
        assert "finance" in result["domain_policies"]

    def test_domain_policies_from_wrong_file_warns(self, capsys):
        result = _empty_result()
        data = {"domain_policies": {"finance": {}}}
        _parse_platform_config(data, "db-a.yaml", result)
        assert result["domain_policies"] == {}
        assert "WARN" in capsys.readouterr().err

    def test_domain_policies_non_dict_warns(self, capsys):
        result = _empty_result()
        data = {"domain_policies": "bad"}
        _parse_platform_config(data, "_domain_policy.yaml", result)
        assert result["domain_policies"] == {}
        assert "WARN" in capsys.readouterr().err

    def test_domain_policies_yml_extension(self):
        result = _empty_result()
        data = {"domain_policies": {"p1": {}}}
        _parse_platform_config(data, "_domain_policy.yml", result)
        assert "p1" in result["domain_policies"]

    def test_empty_data_noop(self):
        result = _empty_result()
        _parse_platform_config({}, "_defaults.yaml", result)
        assert result["defaults_keys"] == set()
        assert result["routing_defaults"] == {}
        assert result["enforced_routing"] is None

    def test_multiple_calls_accumulate(self):
        result = _empty_result()
        _parse_platform_config(
            {"defaults": {"cpu": "80"}}, "_defaults.yaml", result)
        _parse_platform_config(
            {"routing_profiles": {"p1": {}}}, "_routing_profiles.yaml", result)
        _parse_platform_config(
            {"domain_policies": {"d1": {}}}, "_domain_policy.yaml", result)
        assert result["defaults_keys"] == {"cpu"}
        assert "p1" in result["routing_profiles"]
        assert "d1" in result["domain_policies"]


# ===========================================================================
# _parse_tenant_overrides tests
# ===========================================================================

class TestParseTenantOverrides:
    """Unit tests for _parse_tenant_overrides()."""

    def test_tenant_added_to_all_tenants(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"cpu": "80"}, result)
        assert "db-a" in result["all_tenants"]

    def test_tenant_keys_collected(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"cpu": "80", "mem": "85"}, result)
        assert result["tenant_keys"]["db-a"] == {"cpu", "mem"}

    def test_routing_profile_extracted(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_routing_profile": "team-sre"}, result)
        assert result["tenant_profile_refs"]["db-a"] == "team-sre"

    def test_routing_profile_stripped(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_routing_profile": "  team-sre  "}, result)
        assert result["tenant_profile_refs"]["db-a"] == "team-sre"

    def test_routing_profile_non_string_ignored(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_routing_profile": 123}, result)
        assert "db-a" not in result["tenant_profile_refs"]

    def test_routing_profile_empty_string_ignored(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_routing_profile": ""}, result)
        assert "db-a" not in result["tenant_profile_refs"]

    def test_dedup_default_enable(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"cpu": "80"}, result)
        assert result["dedup_configs"]["db-a"] == "enable"

    def test_dedup_explicit_disable(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_severity_dedup": "disable"}, result)
        assert result["dedup_configs"]["db-a"] == "disable"

    def test_dedup_explicit_enable(self):
        result = _empty_result()
        _parse_tenant_overrides(
            "db-a", {"_severity_dedup": "enable"}, result)
        assert result["dedup_configs"]["db-a"] == "enable"

    def test_metadata_extracted(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {
            "_metadata": {"owner": "team-x", "tier": "tier-1"},
        }, result)
        assert result["metadata_configs"]["db-a"]["owner"] == "team-x"

    def test_metadata_tenant_placeholder(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {
            "_metadata": {"runbook_url": "https://wiki/{{tenant}}"},
        }, result)
        assert "db-a" in result["metadata_configs"]["db-a"]["runbook_url"]

    def test_metadata_non_dict_ignored(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"_metadata": "bad"}, result)
        assert "db-a" not in result["metadata_configs"]

    def test_routing_explicit(self):
        result = _empty_result()
        routing = {"receiver": {"type": "slack", "api_url": "https://x"}}
        _parse_tenant_overrides("db-a", {"_routing": routing}, result)
        assert result["explicit_routing"]["db-a"] == routing

    def test_routing_disabled_string(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"_routing": "disable"}, result)
        assert "db-a" in result["disabled_tenants"]
        assert "db-a" not in result["explicit_routing"]

    def test_routing_none_no_explicit(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {"cpu": "80"}, result)
        assert "db-a" not in result["explicit_routing"]
        assert "db-a" not in result["disabled_tenants"]

    def test_multiple_tenants_independent(self):
        result = _empty_result()
        _parse_tenant_overrides("db-a", {
            "_routing_profile": "p1",
            "_severity_dedup": "disable",
        }, result)
        _parse_tenant_overrides("db-b", {
            "_routing_profile": "p2",
            "_routing": {"receiver": {"type": "slack", "api_url": "x"}},
        }, result)
        assert result["tenant_profile_refs"]["db-a"] == "p1"
        assert result["tenant_profile_refs"]["db-b"] == "p2"
        assert result["dedup_configs"]["db-a"] == "disable"
        assert result["dedup_configs"]["db-b"] == "enable"
        assert "db-a" not in result["explicit_routing"]
        assert "db-b" in result["explicit_routing"]

    def test_routing_profile_with_explicit_routing(self):
        """Tenant can have both _routing_profile and _routing overrides."""
        result = _empty_result()
        _parse_tenant_overrides("db-a", {
            "_routing_profile": "team-sre",
            "_routing": {"repeat_interval": "30m"},
        }, result)
        assert result["tenant_profile_refs"]["db-a"] == "team-sre"
        assert result["explicit_routing"]["db-a"]["repeat_interval"] == "30m"
