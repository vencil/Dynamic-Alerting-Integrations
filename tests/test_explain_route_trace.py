"""Tests for explain_route.py --trace mode (v2.1.0 route tracing)."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import explain_route as er  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _make_parsed(
    *,
    routing_defaults=None,
    routing_profiles=None,
    tenant_profile_refs=None,
    explicit_routing=None,
    enforced_routing=None,
    all_tenants=None,
    dedup_tenants=None,
    domain_policies=None,
    disabled_tenants=None,
):
    """Build a mock parsed config dict."""
    return {
        "routing_defaults": routing_defaults or {},
        "routing_profiles": routing_profiles or {},
        "tenant_profile_refs": tenant_profile_refs or {},
        "explicit_routing": explicit_routing or {},
        "enforced_routing": enforced_routing,
        "all_tenants": all_tenants or ["db-a"],
        "dedup_tenants": dedup_tenants or {},
        "domain_policies": domain_policies or {},
        "disabled_tenants": disabled_tenants or set(),
    }


# ---------------------------------------------------------------------------
# trace_alert_routing
# ---------------------------------------------------------------------------
class TestTraceAlertRouting:
    def test_basic_trace(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook", "receiver_url": "http://hook.io"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "HighCPU", "critical")
        assert trace["tenant"] == "db-a"
        assert trace["alertname"] == "HighCPU"
        assert trace["severity"] == "critical"
        assert len(trace["steps"]) == 5
        assert "webhook" in trace["final_receiver"]

    def test_profile_applied(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            routing_profiles={"team-a": {"channel": "#team-a-alerts"}},
            tenant_profile_refs={"db-a": "team-a"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "SlowQuery")
        # Profile ref should appear in step 1
        assert trace["steps"][0]["profile_ref"] == "team-a"

    def test_enforced_routing_shows(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            enforced_routing={"enabled": True, "receiver_type": "pagerduty",
                              "channel": "infra-oncall"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Down", "critical")
        enforced_step = trace["steps"][2]  # step 3
        assert enforced_step["action"] == "enforced_routing"
        assert "pagerduty" in enforced_step.get("enforced_receiver", "")

    def test_no_enforced_routing(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "slack"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        enforced_step = trace["steps"][2]
        assert "No enforced" in enforced_step["detail"]

    def test_severity_dedup_inhibition(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            dedup_tenants={"db-a": {"enabled": True}},
            all_tenants=["db-a"],
        )
        # Warning with dedup enabled → possible inhibition
        trace = er.trace_alert_routing(parsed, "db-a", "HighMem", "warning")
        inhibit_step = trace["steps"][3]
        assert inhibit_step["action"] == "inhibit_check"
        assert inhibit_step["inhibited"] == "possible"

    def test_no_inhibition_for_critical(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            dedup_tenants={"db-a": {"enabled": True}},
            all_tenants=["db-a"],
        )
        # Critical alerts are never inhibited by dedup
        trace = er.trace_alert_routing(parsed, "db-a", "HighMem", "critical")
        inhibit_step = trace["steps"][3]
        assert inhibit_step["inhibited"] is False

    def test_domain_policy_violation(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "email"},
            domain_policies={
                "production": {
                    "constraints": {"forbidden_receiver_types": ["email"]},
                }
            },
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        policy_step = trace["steps"][4]
        assert policy_step["passed"] is False
        assert len(policy_step["violations"]) >= 1

    def test_domain_policy_pass(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            domain_policies={
                "production": {
                    "constraints": {"allowed_receiver_types": ["webhook", "pagerduty"]},
                }
            },
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        policy_step = trace["steps"][4]
        assert policy_step["passed"] is True

    def test_timing_defaults(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook",
                              "group_wait": "10s", "repeat_interval": "1h"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        assert trace["timing"]["group_wait"] == "10s"
        assert trace["timing"]["repeat_interval"] == "1h"

    def test_extra_labels(self):
        parsed = _make_parsed(all_tenants=["db-a"])
        trace = er.trace_alert_routing(
            parsed, "db-a", "CustomAlert", "warning",
            extra_labels={"namespace": "production", "db": "mysql"},
        )
        assert trace["labels"]["namespace"] == "production"
        assert trace["labels"]["db"] == "mysql"


# ---------------------------------------------------------------------------
# format_trace
# ---------------------------------------------------------------------------
class TestFormatTrace:
    def test_english_output(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        output = er.format_trace(trace, lang="en")
        assert "Route Trace" in output
        assert "db-a" in output
        assert "Final Result" in output

    def test_chinese_output(self):
        parsed = _make_parsed(
            routing_defaults={"receiver_type": "webhook"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        output = er.format_trace(trace, lang="zh")
        assert "路由追蹤" in output
        assert "最終結果" in output
