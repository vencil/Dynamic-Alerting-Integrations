"""test_e2e_routing_profile.py — E2E tests for ADR-007 routing profile pipeline.

End-to-end verification of:
  profile parse → four-layer merge → route generation → domain policy validation → violation

These tests exercise _parse_config_files → load_tenant_configs → generate_routes
→ check_domain_policies as a single pipeline, verifying data contracts across modules.
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

pytestmark = pytest.mark.integration

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(_REPO, "scripts", "tools"))

from generate_alertmanager_routes import (  # noqa: E402
    _parse_config_files,
    load_tenant_configs,
    generate_routes,
    check_domain_policies,
    merge_routing_with_defaults,
)
from explain_route import (  # noqa: E402
    explain_tenant_routing,
    explain_profile_expansion,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _write(d: str, fname: str, data: dict) -> str:
    path = os.path.join(d, fname)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    return path


@pytest.fixture
def config_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def full_pipeline_dir(config_dir):
    """Config dir exercising profiles + policies + multiple tenants."""
    _write(config_dir, "_defaults.yaml", {
        "defaults": {"mysql_connections": "80", "pg_connections": "80"},
        "_routing_defaults": {
            "receiver": {"type": "email", "to": ["oncall@example.com"],
                         "smarthost": "smtp.example.com:587"},
            "group_by": ["alertname", "tenant"],
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        },
    })
    _write(config_dir, "_routing_profiles.yaml", {
        "routing_profiles": {
            "team-sre": {
                "receiver": {"type": "slack",
                             "api_url": "https://hooks.slack.com/sre"},
                "group_wait": "15s",
                "repeat_interval": "2h",
            },
            "team-dba": {
                "receiver": {"type": "pagerduty",
                             "service_key": "dba-key-123"},
                "group_by": ["alertname", "tenant", "severity"],
                "group_wait": "30s",
                "repeat_interval": "1h",
            },
        },
    })
    _write(config_dir, "_domain_policy.yaml", {
        "domain_policies": {
            "finance": {
                "description": "Finance compliance",
                "tenants": ["db-finance"],
                "constraints": {
                    "forbidden_receiver_types": ["slack", "webhook"],
                    "allowed_receiver_types": ["pagerduty", "email"],
                    "max_repeat_interval": "1h",
                    "min_group_wait": "30s",
                    "enforce_group_by": ["tenant", "alertname", "severity"],
                },
            },
            "standard": {
                "description": "Standard SLA",
                "tenants": ["db-standard"],
                "constraints": {
                    "max_repeat_interval": "12h",
                },
            },
        },
    })
    # Tenant: db-finance uses team-dba profile → pagerduty → should PASS policy
    _write(config_dir, "db-finance.yaml", {
        "tenants": {
            "db-finance": {
                "mysql_connections": "60",
                "_routing_profile": "team-dba",
            },
        },
    })
    # Tenant: db-standard uses team-sre profile → slack → should PASS standard policy
    _write(config_dir, "db-standard.yaml", {
        "tenants": {
            "db-standard": {
                "pg_connections": "90",
                "_routing_profile": "team-sre",
            },
        },
    })
    # Tenant: db-plain has no profile, gets defaults only
    _write(config_dir, "db-plain.yaml", {
        "tenants": {
            "db-plain": {
                "mysql_connections": "70",
            },
        },
    })
    return config_dir


# ===========================================================================
# E2E: Profile parse → route generation
# ===========================================================================

class TestProfileToRouteGeneration:
    """Verify that profiles are parsed, merged, and produce valid routes."""

    def test_profile_tenant_gets_profile_receiver(self, full_pipeline_dir):
        """Tenant with _routing_profile gets the profile's receiver in route."""
        routing_configs, dedup_configs, *_ = load_tenant_configs(
            full_pipeline_dir)
        assert "db-finance" in routing_configs
        assert routing_configs["db-finance"]["receiver"]["type"] == "pagerduty"

    def test_plain_tenant_gets_defaults(self, full_pipeline_dir):
        """Tenant without profile gets _routing_defaults receiver."""
        routing_configs, *_ = load_tenant_configs(full_pipeline_dir)
        assert "db-plain" in routing_configs
        assert routing_configs["db-plain"]["receiver"]["type"] == "email"

    def test_generate_routes_includes_profile_tenants(self, full_pipeline_dir):
        """generate_routes produces routes for tenants using profiles."""
        routing_configs, *_ = load_tenant_configs(full_pipeline_dir)
        routes, receivers, warnings = generate_routes(routing_configs)

        route_names = {r["receiver"] for r in routes}
        assert "tenant-db-finance" in route_names
        assert "tenant-db-standard" in route_names
        assert "tenant-db-plain" in route_names

    def test_profile_timing_overrides_defaults(self, full_pipeline_dir):
        """Profile timing params override _routing_defaults."""
        routing_configs, *_ = load_tenant_configs(full_pipeline_dir)
        # team-sre profile sets group_wait=15s, repeat_interval=2h
        cfg = routing_configs["db-standard"]
        assert cfg["group_wait"] == "15s"
        assert cfg["repeat_interval"] == "2h"

    def test_tenant_routing_overrides_profile(self, config_dir):
        """Tenant _routing overrides profile values (Layer 3 > Layer 2)."""
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {
                "receiver": {"type": "email", "to": ["x@x.com"],
                             "smarthost": "smtp:587"},
                "group_wait": "30s",
                "repeat_interval": "4h",
            },
        })
        _write(config_dir, "_routing_profiles.yaml", {
            "routing_profiles": {
                "p1": {
                    "receiver": {"type": "slack",
                                 "api_url": "https://hooks.slack.com/p1"},
                    "repeat_interval": "2h",
                },
            },
        })
        _write(config_dir, "db-x.yaml", {
            "tenants": {
                "db-x": {
                    "cpu": "90",
                    "_routing_profile": "p1",
                    "_routing": {
                        "repeat_interval": "30m",  # Layer 3 override
                    },
                },
            },
        })
        routing_configs, *_ = load_tenant_configs(config_dir)
        cfg = routing_configs["db-x"]
        # Layer 2 receiver (profile)
        assert cfg["receiver"]["type"] == "slack"
        # Layer 3 overrides repeat_interval
        assert cfg["repeat_interval"] == "30m"


# ===========================================================================
# E2E: Route generation → domain policy validation
# ===========================================================================

class TestRouteToPolicyValidation:
    """Verify domain policies correctly validate generated routing configs."""

    def test_compliant_tenant_no_violations(self, full_pipeline_dir):
        """Finance tenant with pagerduty profile passes finance policy."""
        routing_configs, *_ = load_tenant_configs(full_pipeline_dir)
        parsed = _parse_config_files(full_pipeline_dir)
        policies = parsed.get("domain_policies", {})

        messages = check_domain_policies(routing_configs, policies)
        # db-finance uses pagerduty (allowed), repeat_interval=1h (within limit)
        finance_violations = [m for m in messages if "db-finance" in m]
        assert len(finance_violations) == 0, \
            f"Unexpected violations: {finance_violations}"

    def test_forbidden_receiver_violation(self, config_dir):
        """Tenant with Slack receiver violates policy forbidding Slack."""
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {
                "receiver": {"type": "email", "to": ["x@x.com"],
                             "smarthost": "smtp:587"},
                "group_wait": "30s",
                "repeat_interval": "4h",
            },
        })
        _write(config_dir, "_routing_profiles.yaml", {
            "routing_profiles": {
                "slack-team": {
                    "receiver": {"type": "slack",
                                 "api_url": "https://hooks.slack.com/team"},
                },
            },
        })
        _write(config_dir, "_domain_policy.yaml", {
            "domain_policies": {
                "strict": {
                    "tenants": ["db-x"],
                    "constraints": {
                        "forbidden_receiver_types": ["slack"],
                    },
                },
            },
        })
        _write(config_dir, "db-x.yaml", {
            "tenants": {
                "db-x": {
                    "_routing_profile": "slack-team",
                },
            },
        })
        routing_configs, *_ = load_tenant_configs(config_dir)
        parsed = _parse_config_files(config_dir)
        policies = parsed.get("domain_policies", {})

        messages = check_domain_policies(routing_configs, policies)
        assert any("forbidden" in m and "db-x" in m for m in messages), \
            f"Expected forbidden violation, got: {messages}"

    def test_max_repeat_interval_violation(self, config_dir):
        """Tenant with repeat_interval exceeding policy max triggers violation."""
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {
                "receiver": {"type": "email", "to": ["x@x.com"],
                             "smarthost": "smtp:587"},
                "group_wait": "30s",
                "repeat_interval": "24h",  # exceeds policy max
            },
        })
        _write(config_dir, "_domain_policy.yaml", {
            "domain_policies": {
                "fast-response": {
                    "tenants": ["db-y"],
                    "constraints": {
                        "max_repeat_interval": "1h",
                    },
                },
            },
        })
        _write(config_dir, "db-y.yaml", {
            "tenants": {
                "db-y": {"cpu": "90"},  # inherits 24h repeat_interval
            },
        })
        routing_configs, *_ = load_tenant_configs(config_dir)
        parsed = _parse_config_files(config_dir)
        policies = parsed.get("domain_policies", {})

        messages = check_domain_policies(routing_configs, policies)
        assert any("repeat_interval" in m and "db-y" in m for m in messages), \
            f"Expected repeat_interval violation, got: {messages}"

    def test_strict_mode_returns_errors(self, config_dir):
        """strict=True turns WARN into ERROR."""
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {
                "receiver": {"type": "webhook",
                             "url": "https://hooks.example.com/alert"},
                "group_wait": "30s",
                "repeat_interval": "4h",
            },
        })
        _write(config_dir, "_domain_policy.yaml", {
            "domain_policies": {
                "no-webhook": {
                    "tenants": ["db-z"],
                    "constraints": {
                        "forbidden_receiver_types": ["webhook"],
                    },
                },
            },
        })
        _write(config_dir, "db-z.yaml", {
            "tenants": {"db-z": {"cpu": "80"}},
        })
        routing_configs, *_ = load_tenant_configs(config_dir)
        parsed = _parse_config_files(config_dir)
        policies = parsed.get("domain_policies", {})

        messages = check_domain_policies(routing_configs, policies, strict=True)
        assert any("ERROR" in m for m in messages), \
            f"Expected ERROR severity, got: {messages}"


# ===========================================================================
# E2E: explain_route consistency with route generation
# ===========================================================================

class TestExplainRouteConsistency:
    """Verify explain_route's final merged result matches actual routing config."""

    def test_explain_matches_load_tenant_configs(self, full_pipeline_dir):
        """explain_route final == load_tenant_configs for same tenant."""
        parsed = _parse_config_files(full_pipeline_dir)
        routing_configs, *_ = load_tenant_configs(full_pipeline_dir)

        for tenant in ["db-finance", "db-standard", "db-plain"]:
            explanation = explain_tenant_routing(parsed, tenant)
            explained_final = explanation["final"]
            actual = routing_configs[tenant]

            # receiver type must match
            assert explained_final.get("receiver", {}).get("type") == \
                   actual.get("receiver", {}).get("type"), \
                f"{tenant}: receiver type mismatch"

            # timing params that exist must match
            for key in ["group_wait", "repeat_interval", "group_interval"]:
                if key in actual:
                    assert explained_final.get(key) == actual[key], \
                        f"{tenant}: {key} mismatch: " \
                        f"explain={explained_final.get(key)} vs " \
                        f"actual={actual[key]}"

    def test_profile_expansion_lists_all_refs(self, full_pipeline_dir):
        """Profile expansion correctly identifies all tenant references."""
        parsed = _parse_config_files(full_pipeline_dir)
        expansion = explain_profile_expansion(parsed)

        assert "team-sre" in expansion
        assert "db-standard" in expansion["team-sre"]["referenced_by"]

        assert "team-dba" in expansion
        assert "db-finance" in expansion["team-dba"]["referenced_by"]


# ===========================================================================
# E2E: Enforced routing with profiles
# ===========================================================================

class TestEnforcedWithProfiles:
    """Verify _routing_enforced works correctly with profiles."""

    def test_enforced_overrides_profile_receiver(self, config_dir):
        """Layer 4 enforced receiver overrides profile receiver in final."""
        _write(config_dir, "_defaults.yaml", {
            "defaults": {"cpu": "80"},
            "_routing_defaults": {
                "receiver": {"type": "email", "to": ["x@x.com"],
                             "smarthost": "smtp:587"},
                "group_wait": "30s",
            },
            "_routing_enforced": {
                "enabled": True,
                "receiver": {"type": "webhook",
                             "url": "https://noc.example.com/alerts"},
            },
        })
        _write(config_dir, "_routing_profiles.yaml", {
            "routing_profiles": {
                "my-profile": {
                    "receiver": {"type": "slack",
                                 "api_url": "https://hooks.slack.com/abc"},
                    "group_wait": "10s",
                },
            },
        })
        _write(config_dir, "db-a.yaml", {
            "tenants": {
                "db-a": {"cpu": "90", "_routing_profile": "my-profile"},
            },
        })
        parsed = _parse_config_files(config_dir)
        explanation = explain_tenant_routing(parsed, "db-a")

        # Layer 4 receiver overrides everything
        assert explanation["final"]["receiver"]["type"] == "webhook"
        assert explanation["final"]["receiver"]["url"] == \
            "https://noc.example.com/alerts"
        # But Layer 2 group_wait should still be from profile (not overridden by enforced)
        assert explanation["final"]["group_wait"] == "10s"
