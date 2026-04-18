"""Tests for explain_route.py (ADR-007 routing debugger)."""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest
import yaml

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from explain_route import (  # noqa: E402
    explain_tenant_routing,
    explain_profile_expansion,
    format_explanation,
    format_profile_expansion,
    main,
)
from generate_alertmanager_routes import _parse_config_files  # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================

def _write(d: str, fname: str, data: dict) -> str:
    path = os.path.join(d, fname)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    return path


@pytest.fixture
def config_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def full_config(config_dir):
    """Config dir with defaults, profiles, policies, and tenants."""
    _write(config_dir, "_defaults.yaml", {
        "defaults": {"cpu_usage": "80"},
        "_routing_defaults": {
            "receiver": {"type": "webhook", "url": "https://default.example.com"},
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        },
    })
    _write(config_dir, "_routing_profiles.yaml", {
        "routing_profiles": {
            "team-sre": {
                "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/sre"},
                "group_wait": "15s",
            },
            "orphan-profile": {
                "receiver": {"type": "pagerduty", "service_key": "abc"},
            },
        },
    })
    _write(config_dir, "db-a.yaml", {
        "tenants": {
            "db-a": {
                "cpu_usage": "90",
                "_routing_profile": "team-sre",
                "_routing": {
                    "repeat_interval": "1h",
                },
            }
        }
    })
    _write(config_dir, "db-b.yaml", {
        "tenants": {
            "db-b": {
                "cpu_usage": "85",
            }
        }
    })
    return config_dir


# ===========================================================================
# explain_tenant_routing tests
# ===========================================================================

class TestExplainTenantRouting:
    def test_four_layers(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-a")
        assert result["tenant"] == "db-a"
        assert result["profile_ref"] == "team-sre"
        assert len(result["layers"]) == 4

    def test_layer1_defaults(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-a")
        layer1 = result["layers"][0]
        assert "Layer 1" in layer1["name"]
        assert layer1["config"]["group_wait"] == "30s"

    def test_layer2_profile(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-a")
        layer2 = result["layers"][1]
        assert "Layer 2" in layer2["name"]
        assert layer2["config"]["group_wait"] == "15s"  # profile overrides default
        assert layer2["config"]["receiver"]["type"] == "slack"

    def test_layer3_tenant(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-a")
        layer3 = result["layers"][2]
        assert "Layer 3" in layer3["name"]
        assert layer3["config"]["repeat_interval"] == "1h"

    def test_final_merged(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-a")
        final = result["final"]
        # Layer 2 overrides Layer 1 group_wait
        assert final["group_wait"] == "15s"
        # Layer 2 overrides Layer 1 receiver
        assert final["receiver"]["type"] == "slack"
        # Layer 3 overrides repeat_interval
        assert final["repeat_interval"] == "1h"
        # Layer 1 group_interval passes through
        assert final["group_interval"] == "5m"

    def test_tenant_without_profile(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-b")
        assert result["profile_ref"] is None
        layer2 = result["layers"][1]
        assert layer2["config"] == {}

    def test_final_includes_defaults_for_plain_tenant(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_tenant_routing(parsed, "db-b")
        final = result["final"]
        # db-b has no overrides, should get defaults
        assert final["group_wait"] == "30s"
        assert final["receiver"]["type"] == "webhook"

    def test_enforced_routing_applied(self, config_dir):
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {"group_wait": "30s"},
            "_routing_enforced": {
                "enabled": True,
                "receiver": {"type": "webhook", "url": "https://noc.example.com"},
            },
        })
        _write(config_dir, "db-a.yaml", {
            "tenants": {"db-a": {"cpu": "90", "_routing": {"group_wait": "10s"}}},
        })
        parsed = _parse_config_files(config_dir)
        result = explain_tenant_routing(parsed, "db-a")
        layer4 = result["layers"][3]
        assert layer4["config"]["receiver"]["type"] == "webhook"
        # Final should have enforced receiver
        assert result["final"]["receiver"]["url"] == "https://noc.example.com"

    def test_empty_config(self, config_dir):
        _write(config_dir, "db-a.yaml", {
            "tenants": {"db-a": {"cpu": "80"}},
        })
        parsed = _parse_config_files(config_dir)
        result = explain_tenant_routing(parsed, "db-a")
        # All layers empty, final also empty
        assert result["final"] == {}


# ===========================================================================
# explain_profile_expansion tests
# ===========================================================================

class TestExplainProfileExpansion:
    def test_profiles_with_refs(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_profile_expansion(parsed)
        assert "team-sre" in result
        assert "db-a" in result["team-sre"]["referenced_by"]

    def test_orphan_profile(self, full_config):
        parsed = _parse_config_files(full_config)
        result = explain_profile_expansion(parsed)
        assert "orphan-profile" in result
        assert result["orphan-profile"]["referenced_by"] == []

    def test_missing_profile_ref(self, config_dir):
        _write(config_dir, "_routing_profiles.yaml", {
            "routing_profiles": {"p1": {"receiver": {"type": "slack"}}},
        })
        _write(config_dir, "db-a.yaml", {
            "tenants": {"db-a": {"_routing_profile": "nonexistent"}},
        })
        parsed = _parse_config_files(config_dir)
        result = explain_profile_expansion(parsed)
        assert "nonexistent" in result
        assert result["nonexistent"].get("error") == "profile not found"

    def test_no_profiles(self, config_dir):
        _write(config_dir, "db-a.yaml", {
            "tenants": {"db-a": {"cpu": "80"}},
        })
        parsed = _parse_config_files(config_dir)
        result = explain_profile_expansion(parsed)
        assert result == {}

    def test_multiple_tenants_same_profile(self, config_dir):
        _write(config_dir, "_routing_profiles.yaml", {
            "routing_profiles": {"shared": {"group_wait": "10s"}},
        })
        _write(config_dir, "db-a.yaml", {
            "tenants": {"db-a": {"_routing_profile": "shared"}},
        })
        _write(config_dir, "db-b.yaml", {
            "tenants": {"db-b": {"_routing_profile": "shared"}},
        })
        parsed = _parse_config_files(config_dir)
        result = explain_profile_expansion(parsed)
        assert sorted(result["shared"]["referenced_by"]) == ["db-a", "db-b"]


# ===========================================================================
# Format tests
# ===========================================================================

class TestFormatExplanation:
    def test_contains_tenant(self, full_config):
        parsed = _parse_config_files(full_config)
        explanation = explain_tenant_routing(parsed, "db-a")
        text = format_explanation(explanation)
        assert "db-a" in text
        assert "Layer 1" in text
        assert "Layer 4" in text

    def test_zh_mode(self, full_config):
        parsed = _parse_config_files(full_config)
        explanation = explain_tenant_routing(parsed, "db-a")
        text = format_explanation(explanation, lang="zh")
        assert "租戶" in text
        assert "最終合併結果" in text


class TestFormatProfileExpansion:
    def test_contains_profile(self, full_config):
        parsed = _parse_config_files(full_config)
        expansion = explain_profile_expansion(parsed)
        text = format_profile_expansion(expansion)
        assert "team-sre" in text
        assert "orphan-profile" in text
        assert "orphan" in text.lower()

    def test_zh_mode(self, full_config):
        parsed = _parse_config_files(full_config)
        expansion = explain_profile_expansion(parsed)
        text = format_profile_expansion(expansion, lang="zh")
        assert "路由設定檔展開" in text


# ===========================================================================
# CLI integration tests
# ===========================================================================

class TestCLI:
    def test_default_mode(self, full_config):
        rc = main(["--config-dir", full_config])
        assert rc == 0

    def test_tenant_filter(self, full_config, capsys):
        rc = main(["--config-dir", full_config, "--tenant", "db-a"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "db-a" in out
        assert "db-b" not in out

    def test_show_profile_expansion(self, full_config, capsys):
        rc = main(["--config-dir", full_config, "--show-profile-expansion"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "team-sre" in out

    def test_json_output(self, full_config, capsys):
        rc = main(["--config-dir", full_config, "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert data[0]["tenant"] in ("db-a", "db-b")

    def test_json_profile_expansion(self, full_config, capsys):
        rc = main(["--config-dir", full_config, "--show-profile-expansion", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "team-sre" in data

    def test_missing_config_dir(self):
        rc = main(["--config-dir", "/nonexistent/path"])
        assert rc == 1

    def test_unknown_tenant_warns(self, full_config, capsys):
        rc = main(["--config-dir", full_config, "--tenant", "ghost"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "ghost" in err
