"""Tests for explain_route.py --trace mode (v2.1.0 route tracing)."""
from __future__ import annotations

import json
import os
import sys

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
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


# ---------------------------------------------------------------------------
# --label KEY=VALUE (#2264)：讓 trace 能帶 metric_group / routes 的 match key
# ---------------------------------------------------------------------------
_DEMO = "demo-label"

_LABEL_TREE = {"tenants": {_DEMO: {"_routing": {
    "receiver": {"type": "webhook", "url": "https://hooks.example.com/main"},
    "overrides": [{"metric_group": "mg-conn",
                   "receiver": {"type": "webhook",
                                "url": "https://hooks.example.com/mg"}}],
    "routes": [{"match": {"team": "x"},
                "receiver": {"type": "webhook",
                             "url": "https://hooks.example.com/teamx"}}],
}}}}


@pytest.fixture
def label_conf(tmp_path):
    d = tmp_path / "conf.d"
    d.mkdir()
    (d / f"{_DEMO}.yaml").write_text(yaml.safe_dump(_LABEL_TREE),
                                     encoding="utf-8")
    return d


def _run_trace(capsys, conf, *extra):
    rc = er.main(["--config-dir", str(conf), "--tenant", _DEMO, "--trace",
                  "--json", *extra])
    out = capsys.readouterr().out
    assert rc == 0
    [trace] = json.loads(out)
    return trace


class TestTraceLabelCli:
    def test_metric_group_hits_override(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "metric_group=mg-conn")
        assert trace["steps"][1]["matched_sub_route"] == "overrides[0]"
        assert f"tenant-{_DEMO}-override-0" in trace["final_receiver"]

    def test_routes_match_key_hits_route(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "team=x")
        assert trace["steps"][1]["matched_sub_route"] == "routes[0]"
        assert f"tenant-{_DEMO}-route-0" in trace["final_receiver"]

    def test_override_wins_over_routes(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "team=x",
                           "--label", "metric_group=mg-conn")
        assert trace["steps"][1]["matched_sub_route"] == "overrides[0]"

    def test_without_label_stays_on_main_receiver(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf)
        assert "matched_sub_route" not in trace["steps"][1]

    def test_repeatable_value_with_equals_and_empty(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "q=a=b",
                           "--label", "empty=", "--label", "team=y")
        assert trace["labels"]["q"] == "a=b"
        assert trace["labels"]["empty"] == ""
        assert trace["labels"]["team"] == "y"
        assert "matched_sub_route" not in trace["steps"][1]

    def test_json_labels_include_extra_and_dedicated(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--alertname", "A",
                           "--severity", "critical", "--label", "team=x")
        assert trace["labels"] == {"alertname": "A", "tenant": _DEMO,
                                   "severity": "critical", "team": "x"}

    @pytest.mark.parametrize("args, needle", [
        (["--label", "novalue"], "KEY=VALUE"),
        (["--label", "=v"], "invalid label name"),
        (["--label", "1bad=v"], "invalid label name"),
        (["--label", "bad-key=v"], "invalid label name"),
        (["--label", "team\n=x"], "invalid label name"),
        (["--label", "alertname=X"], "--alertname"),
        (["--label", "severity=critical"], "--severity"),
        (["--label", "tenant=other"], "--tenant"),
        (["--label", "team=x", "--label", "team=y"], "more than once"),
    ])
    def test_invalid_label_is_caller_error(self, capsys, label_conf, args, needle):
        with pytest.raises(SystemExit) as exc:
            er.main(["--config-dir", str(label_conf), "--tenant", _DEMO,
                     "--trace", *args])
        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert needle in captured.err
        assert captured.out == ""

    def test_label_without_trace_is_caller_error(self, capsys, label_conf):
        with pytest.raises(SystemExit) as exc:
            er.main(["--config-dir", str(label_conf), "--label", "team=x"])
        assert exc.value.code == 2
        assert "only with --trace" in capsys.readouterr().err

    def test_control_chars_in_bad_label_are_escaped(self, capsys, label_conf):
        with pytest.raises(SystemExit):
            er.main(["--config-dir", str(label_conf), "--tenant", _DEMO,
                     "--trace", "--label", "evil\n[PASS] ok"])
        assert "\n[PASS] ok" not in capsys.readouterr().err


class TestTraceExtraLabelsReserved:
    @pytest.mark.parametrize("key", ["alertname", "severity", "tenant"])
    def test_reserved_key_raises(self, key):
        parsed = _make_parsed(all_tenants=[_DEMO])
        with pytest.raises(ValueError, match=key):
            er.trace_alert_routing(parsed, _DEMO, "A", "warning",
                                   extra_labels={key: "v"})
