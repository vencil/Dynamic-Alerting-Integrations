"""Tests for explain_route.py --trace mode (v2.1.0 route tracing)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import explain_route as er  # noqa: E402

# #2293: --trace asks Alertmanager (`amtool config routes test --tree`) where
# an alert goes. Tests about THAT verdict need a real amtool: they skip without
# one, and FAIL without one under VIBE_REQUIRE_AMTOOL=1 (the CI Python Tests
# jobs, which install it) — fixture `amtool_required` in tests/conftest.py.
needs_amtool = pytest.mark.usefixtures("amtool_required")


# Structured receivers (ADR-007 schema). #2293: the flat `receiver_type` key
# these fixtures used to carry is not read by the generator; the old trace read
# it directly (G1), so they passed without describing a routable tenant.
_WEBHOOK = {"type": "webhook", "url": "https://hook.example.com/a"}
_SLACK = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/x"}
_EMAIL = {"type": "email", "to": ["oncall@example.com"],
          "smarthost": "smtp.example.com:587", "from": "am@example.com"}
_PAGERDUTY = {"type": "pagerduty", "service_key": "k"}


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
    dedup_configs=None,
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
        "dedup_configs": dedup_configs or {},
        "domain_policies": domain_policies or {},
        "disabled_tenants": disabled_tenants or set(),
    }


# ---------------------------------------------------------------------------
# trace_alert_routing
# ---------------------------------------------------------------------------
class TestTraceAlertRouting:
    @needs_amtool
    def test_basic_trace(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
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
            routing_defaults={"receiver": _WEBHOOK},
            routing_profiles={"team-a": {"group_wait": "15s"}},
            tenant_profile_refs={"db-a": "team-a"},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "SlowQuery")
        # Profile ref should appear in step 1
        assert trace["steps"][0]["profile_ref"] == "team-a"

    @needs_amtool
    def test_enforced_routing_shows(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
            enforced_routing={"enabled": True, "receiver": _PAGERDUTY},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Down", "critical")
        enforced_step = trace["steps"][2]  # step 3
        assert enforced_step["action"] == "enforced_routing"
        assert "pagerduty" in enforced_step.get("enforced_receiver", "")

    @needs_amtool
    def test_no_enforced_routing(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _SLACK},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        enforced_step = trace["steps"][2]
        assert "No enforced" in enforced_step["detail"]

    def test_severity_dedup_inhibition(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
            dedup_configs={"db-a": "enable"},
            all_tenants=["db-a"],
        )
        # Warning with dedup enabled → possible inhibition
        trace = er.trace_alert_routing(parsed, "db-a", "HighMem", "warning")
        inhibit_step = trace["steps"][3]
        assert inhibit_step["action"] == "inhibit_check"
        assert inhibit_step["inhibited"] == "possible"

    def test_no_inhibition_for_critical(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
            dedup_configs={"db-a": "enable"},
            all_tenants=["db-a"],
        )
        # Critical alerts are never inhibited by dedup
        trace = er.trace_alert_routing(parsed, "db-a", "HighMem", "critical")
        inhibit_step = trace["steps"][3]
        assert inhibit_step["inhibited"] is False

    @needs_amtool
    def test_domain_policy_violation(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _EMAIL},
            domain_policies={
                "production": {
                    "tenants": ["db-a"],
                    "constraints": {"forbidden_receiver_types": ["email"]},
                }
            },
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        policy_step = trace["steps"][4]
        assert policy_step["passed"] is False
        assert len(policy_step["violations"]) >= 1

    @needs_amtool
    def test_domain_policy_pass(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
            domain_policies={
                "production": {
                    "tenants": ["db-a"],
                    "constraints": {"allowed_receiver_types": ["webhook", "pagerduty"]},
                }
            },
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        policy_step = trace["steps"][4]
        assert policy_step["passed"] is True

    @needs_amtool
    def test_timing_defaults(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK,
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
            routing_defaults={"receiver": _WEBHOOK},
            all_tenants=["db-a"],
        )
        trace = er.trace_alert_routing(parsed, "db-a", "Test")
        output = er.format_trace(trace, lang="en")
        assert "Route Trace" in output
        assert "db-a" in output
        assert "Final Result" in output

    def test_chinese_output(self):
        parsed = _make_parsed(
            routing_defaults={"receiver": _WEBHOOK},
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
    @needs_amtool
    def test_metric_group_hits_override(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "metric_group=mg-conn")
        assert trace["steps"][1]["matched_sub_route"] == "overrides[0]"
        assert f"tenant-{_DEMO}-override-0" in trace["final_receiver"]

    @needs_amtool
    def test_routes_match_key_hits_route(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "team=x")
        assert trace["steps"][1]["matched_sub_route"] == "routes[0]"
        assert f"tenant-{_DEMO}-route-0" in trace["final_receiver"]

    @needs_amtool
    def test_override_wins_over_routes(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf, "--label", "team=x",
                           "--label", "metric_group=mg-conn")
        assert trace["steps"][1]["matched_sub_route"] == "overrides[0]"

    @needs_amtool
    def test_without_label_stays_on_main_receiver(self, capsys, label_conf):
        trace = _run_trace(capsys, label_conf)
        assert "matched_sub_route" not in trace["steps"][1]

    @needs_amtool
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


# ---------------------------------------------------------------------------
# #2293: --trace walks the WHOLE rendered route tree with Alertmanager's rules
# ---------------------------------------------------------------------------
_TT = "demo-tree"
_HOOK_MAIN = {"type": "webhook", "url": "https://hooks.example.com/main"}
_HOOK_NOC = {"type": "webhook", "url": "https://noc.example.com/hook"}
_HOOK_TEAM = {"type": "webhook", "url": "https://hooks.example.com/team"}


def _tree(tmp_path, routing=None, enforced=None, policy=None):
    """A conf.d with one tenant (_TT) and optional _routing_enforced / policy."""
    d = tmp_path / "conf.d"
    d.mkdir()
    tenant_routing = {"receiver": _HOOK_MAIN}
    tenant_routing.update(routing or {})
    (d / f"{_TT}.yaml").write_text(yaml.safe_dump(
        {"tenants": {_TT: {"_routing": tenant_routing}}}), encoding="utf-8")
    defaults: dict = {"defaults": {}}
    if enforced is not None:
        defaults["_routing_enforced"] = dict({"enabled": True}, **enforced)
    (d / "_defaults.yaml").write_text(yaml.safe_dump(defaults), encoding="utf-8")
    if policy is not None:
        (d / "_domain_policy.yaml").write_text(
            yaml.safe_dump({"domain_policies": policy}), encoding="utf-8")
    return d


def _trace(capsys, conf, *args, severity="warning", alertname="X"):
    rc = er.main(["--config-dir", str(conf), "--tenant", _TT, "--trace",
                  "--alertname", alertname, "--severity", severity, "--json",
                  *args])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    [trace] = json.loads(captured.out)
    return trace


def _trace_err(capsys, conf, *args):
    """``_trace`` that also returns what went to stderr."""
    rc = er.main(["--config-dir", str(conf), "--tenant", _TT, "--trace",
                  "--json", *args])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    [trace] = json.loads(captured.out)
    return trace, captured.err


def _delivered(trace):
    return [h["receiver"] for h in trace["steps"][1]["matched_routes"]]


# Scenarios shared by the fixed-expectation tests and the amtool oracle:
# (id, tree kwargs, alertname, severity, --label args, receivers in AM order)
def _enf_re(matcher):
    return {"receiver": _HOOK_NOC, "match": [matcher]}


_ENF_MATCH = {"receiver": _HOOK_NOC,
              "match": ['severity=~"critical|warning", team!="x"']}
_ESC_ROUTES = {"routes": [{"match": {"team": 'a"b'}, "receiver": _HOOK_TEAM}]}
SCENARIOS = [
    ("watchdog", {}, "Watchdog", "none", [], ["watchdog-heartbeat"]),
    ("custom", {}, "X", "warning", ["component=custom"], [f"tenant-{_TT}"]),
    ("synthetic-probe", {}, "X", "warning", ["component=synthetic-probe"],
     ["synthetic-receiver"]),
    ("sentinel", {}, "X", "warning", ["component=sentinel"],
     ["sentinel-sinkhole"]),
    ("tenant-main", {}, "X", "warning", [], [f"tenant-{_TT}"]),
    ("enforced-match-all", {"enforced": {"receiver": _HOOK_NOC}}, "X",
     "warning", [], ["platform-enforced", f"tenant-{_TT}"]),
    ("enforced-skips-watchdog", {"enforced": {"receiver": _HOOK_NOC}},
     "Watchdog", "none", [], ["watchdog-heartbeat"]),
    ("enforced-multi-matcher-hit", {"enforced": _ENF_MATCH}, "X", "warning",
     [], ["platform-enforced", f"tenant-{_TT}"]),
    ("enforced-multi-matcher-severity-miss", {"enforced": _ENF_MATCH}, "X",
     "info", [], [f"tenant-{_TT}"]),
    ("enforced-multi-matcher-neq-miss", {"enforced": _ENF_MATCH}, "X",
     "critical", ["team=x"], [f"tenant-{_TT}"]),
    ("value-with-quote", {"routing": _ESC_ROUTES}, "X", "warning",
     ['team=a"b'], [f"tenant-{_TT}-route-0"]),
    ("value-without-quote", {"routing": _ESC_ROUTES}, "X", "warning",
     ["team=ab"], [f"tenant-{_TT}"]),
    # Regex semantics (should-fix of round 2): a leading inline flag, an
    # alternation anchored as a whole, and ASCII-only \d as in Go RE2.
    ("re-inline-flag", {"enforced": _enf_re('severity=~"(?i)CRITICAL"')},
     "X", "critical", [], ["platform-enforced", f"tenant-{_TT}"]),
    ("re-alternation-anchored", {"enforced": _enf_re('severity=~"crit|warning"')},
     "X", "critical", [], [f"tenant-{_TT}"]),
    ("re-alternation-hit", {"enforced": _enf_re('severity=~"crit|warning"')},
     "X", "warning", [], ["platform-enforced", f"tenant-{_TT}"]),
    ("re-digit-ascii-only", {"enforced": _enf_re('team=~"\\\\d"')},
     "X", "warning", ["team=\u0663"], [f"tenant-{_TT}"]),
    ("re-digit-ascii-hit", {"enforced": _enf_re('team=~"\\\\d"')},
     "X", "warning", ["team=7"], ["platform-enforced", f"tenant-{_TT}"]),
    ("re-casefold-non-ascii", {"enforced": _enf_re('team=~"(?i)CAF\u00c9"')},
     "X", "warning", ["team=caf\u00e9"], ["platform-enforced", f"tenant-{_TT}"]),
    ("label-value-special-chars", {"routing": {"routes": [
        {"match": {"team": 'q"u,o=t e\nx'}, "receiver": _HOOK_TEAM}]}},
     "X", "warning", ['team=q"u,o=t e\nx'], [f"tenant-{_TT}-route-0"]),
]


def _label_args(labels):
    out = []
    for kv in labels:
        out += ["--label", kv]
    return out


class TestBaseConfigFlag:
    def test_base_config_is_trace_only_and_checked(self, capsys, tmp_path):
        conf = _tree(tmp_path)
        with pytest.raises(SystemExit) as exc:
            er.main(["--config-dir", str(conf), "--base-config", "x.yml"])
        assert exc.value.code == 2
        capsys.readouterr()
        rc = er.main(["--config-dir", str(conf), "--tenant", _TT, "--trace",
                      "--base-config", str(tmp_path / "missing.yml")])
        assert rc == 2
        assert "not a file" in capsys.readouterr().err

    @pytest.mark.parametrize("receivers", [
        [{"slack_configs": [{"channel": "#x"}]}],  # a receiver without name
        {"name": "ops"},                            # a mapping, not a list
    ], ids=["receiver-without-name", "receivers-mapping"])
    def test_malformed_base_receivers_is_unknown_not_a_crash(
            self, capsys, tmp_path, receivers):
        """assemble_configmap raises KeyError / TypeError here, not the
        ValueError of a refusal; the trace still says unknown, rc 0."""
        base = tmp_path / "base.yml"
        base.write_text(yaml.safe_dump({"route": {"receiver": "ops"},
                                        "receivers": receivers}),
                        encoding="utf-8")
        trace, err = _trace_err(capsys, _tree(tmp_path),
                                "--base-config", str(base))
        assert "WARN: the generator cannot assemble this config" in err
        assert "Traceback" not in err
        assert trace["final_receiver"] == \
            "(unknown: the generator refuses this config)"


class TestInhibitStepFromConfd:
    """Step 4 reads the ``dedup_configs`` that ``_parse_config_files``
    produces from a real conf.d (default enable, ``_severity_dedup: disable``
    opts out)."""

    @pytest.mark.parametrize("dedup, severity, expected", [
        (None, "warning", "possible"),
        (None, "critical", False),
        ("disable", "warning", False),
    ])
    def test_dedup_from_confd(self, tmp_path, dedup, severity, expected):
        conf = _tree(tmp_path)
        if dedup is not None:
            tenant_file = conf / f"{_TT}.yaml"
            doc = yaml.safe_load(tenant_file.read_text(encoding="utf-8"))
            doc["tenants"][_TT]["_severity_dedup"] = dedup
            tenant_file.write_text(yaml.safe_dump(doc), encoding="utf-8")
        parsed = er._parse_config_files(str(conf))
        step4 = er.trace_alert_routing(parsed, _TT, "X", severity)["steps"][3]
        assert step4["action"] == "inhibit_check"
        assert step4["inhibited"] == expected


@needs_amtool
def test_ambiguous_route_path_does_not_guess_timing():
    """Two rendered routes spell the same amtool path: the lookup must WARN
    and leave the timing unknown, not take the first."""
    am = {"route": {"receiver": "r", "routes": [
        {"matchers": ['team="x"'], "receiver": "x", "group_wait": "1s"},
        {"matchers": ['team="x"'], "receiver": "x", "group_wait": "2s"}]},
        "receivers": [{"name": "r"}, {"name": "x"}]}
    warnings: list[str] = []
    hits, unknown = er.run_amtool_trace(yaml.safe_dump(am), am["route"],
                                        {"team": "x"}, warn=warnings.append)
    assert unknown == ""
    assert [h["receiver"] for h in hits] == ["x"]
    assert hits[0]["nodes"] is None
    assert any("maps to 2 rendered routes" in w for w in warnings)


@needs_amtool
class TestTraceThroughAmtool:
    @pytest.mark.parametrize(
        "tree, alertname, severity, labels, expected",
        [s[1:] for s in SCENARIOS], ids=[s[0] for s in SCENARIOS])
    def test_delivered_receivers(self, capsys, tmp_path, tree, alertname,
                                 severity, labels, expected):
        trace = _trace(capsys, _tree(tmp_path, **tree), *_label_args(labels),
                       severity=severity, alertname=alertname)
        assert _delivered(trace) == expected

    def test_per_tenant_enforced_sibling_maps_without_warning(self, capsys,
                                                              tmp_path):
        """Real amtool renders both {tenant="<t>"} siblings identically; the
        lookup must still land on the tenant route, not guess."""
        conf = _tree(tmp_path, routing={"group_wait": "1m"}, enforced={
            "receiver": {"type": "webhook",
                         "url": "https://noc.example.com/{{tenant}}"},
            "group_wait": "5s"})
        trace, err = _trace_err(capsys, conf)
        assert "WARN" not in err
        assert _delivered(trace) == [f"platform-enforced-{_TT}", f"tenant-{_TT}"]
        assert trace["timing"]["group_wait"] == "1m"

    def test_watchdog_timing_is_its_own_route(self, capsys, tmp_path):
        trace = _trace(capsys, _tree(tmp_path), alertname="Watchdog",
                       severity="none")
        assert trace["timing"] == {"group_wait": "0s", "group_interval": "1m",
                                   "repeat_interval": "3m"}
        assert "watchdog-heartbeat" in trace["final_receiver"]

    def test_enforced_shown_only_when_it_matches(self, capsys, tmp_path):
        conf = _tree(tmp_path, enforced=_ENF_MATCH)
        hit = _trace(capsys, conf, severity="warning")
        assert hit["steps"][2]["enforced_receiver"] == "webhook → platform-enforced"
        miss = _trace(capsys, conf, severity="info")
        assert "enforced_receiver" not in miss["steps"][2]
        assert "not reached" in miss["steps"][2]["detail"]

    def test_enforced_does_not_replace_tenant_receiver(self, capsys, tmp_path):
        """G2: the NOC route is an additional delivery, not the tenant's."""
        trace = _trace(capsys, _tree(tmp_path, enforced={
            "receiver": {"type": "slack", "api_url": "https://hooks.slack.com/x"}}))
        assert trace["final_receiver"].startswith(f"webhook → tenant-{_TT} ")
        assert trace["steps"][2]["enforced_receiver"] == "slack → platform-enforced"

    def test_slack_receiver_type(self, capsys, tmp_path):
        """G1: the type comes from `receiver.type`, not a legacy key."""
        conf = _tree(tmp_path, routing={"receiver": {
            "type": "slack", "api_url": "https://hooks.slack.com/x",
            "channel": "#demo"}})
        trace = _trace(capsys, conf)
        assert trace["steps"][1]["receiver_type"] == "slack"
        assert trace["final_receiver"].startswith(f"slack → tenant-{_TT} ")
        er.main(["--config-dir", str(conf), "--tenant", _TT, "--trace"])
        assert f"Receiver: slack → tenant-{_TT}" in capsys.readouterr().out

    def test_policy_checks_the_real_type(self, capsys, tmp_path):
        conf = _tree(
            tmp_path,
            routing={"receiver": {"type": "slack",
                                  "api_url": "https://hooks.slack.com/x"}},
            policy={"chat-free": {"tenants": [_TT], "constraints": {
                "forbidden_receiver_types": ["slack"]}}})
        step5 = _trace(capsys, conf)["steps"][4]
        assert step5["passed"] is False
        assert "forbids receiver type 'slack'" in step5["violations"][0]

    def test_timing_inherits_parent_by_parent(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={
            "group_wait": "1m",
            "routes": [{"match": {"team": "x"}, "receiver": _HOOK_TEAM,
                        "repeat_interval": "15m"}]})
        trace = _trace(capsys, conf, "--label", "team=x")
        # group_wait from the tenant route, group_interval from the root,
        # repeat_interval from the child itself.
        assert trace["timing"] == {"group_wait": "1m", "group_interval": "10s",
                                   "repeat_interval": "15m"}
        assert trace["steps"][1]["matched_sub_route"] == "routes[0]"

    def test_base_config_root_is_used_and_its_routes_replaced(
            self, capsys, tmp_path):
        conf = _tree(tmp_path)
        base = tmp_path / "base.yml"
        base.write_text(yaml.safe_dump({"route": {
            "receiver": "base-default", "group_wait": "45s",
            "routes": [{"matchers": ['alertname="X"'], "receiver": "stale"}]},
            "receivers": [{"name": "base-default"}, {"name": "stale"}]}),
            encoding="utf-8")
        trace = _trace(capsys, conf, "--base-config", str(base))
        assert _delivered(trace) == [f"tenant-{_TT}"]
        assert trace["timing"]["group_wait"] == "45s"

    def test_noc_receiver_type_is_not_policed_as_the_tenants(self, capsys,
                                                             tmp_path):
        """Step 5 judges the tenant's receiver, not the enforced copy."""
        conf = _tree(tmp_path, enforced={"receiver": _SLACK},
                     policy={"p": {"tenants": [_TT], "constraints": {
                         "forbidden_receiver_types": ["slack"]}}})
        trace = _trace(capsys, conf)
        assert _delivered(trace) == ["platform-enforced", f"tenant-{_TT}"]
        assert trace["steps"][4]["passed"] is True

    def test_base_config_global_reaches_alertmanager(self, capsys, tmp_path):
        """The trace hands amtool the generator's own assembly, `global:`
        included — a base receiver relying on global.slack_api_url loads."""
        base = tmp_path / "base.yml"
        base.write_text(yaml.safe_dump({
            "global": {"slack_api_url": "https://hooks.slack.com/services/T/B/x"},
            "route": {"receiver": "ops", "group_wait": "10s"},
            "receivers": [{"name": "ops", "slack_configs": [{"channel": "#ops"}]}],
        }), encoding="utf-8")
        trace, err = _trace_err(capsys, _tree(tmp_path), "--base-config", str(base))
        assert "WARN" not in err
        assert _delivered(trace) == [f"tenant-{_TT}"]

    @pytest.mark.parametrize("char", ["\r", "\x0c", "\u2028"])
    def test_line_break_like_chars_in_another_route(self, capsys, tmp_path, char):
        """amtool prints another tenant's matcher value with the character in
        it; only "\\n" ends a line, so the tree is still read."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text("defaults: {}\n", encoding="utf-8")
        (d / f"{_TT}.yaml").write_text(yaml.safe_dump({"tenants": {
            _TT: {"_routing": {"receiver": _HOOK_MAIN}},
            "other": {"_routing": {"receiver": _HOOK_MAIN, "routes": [
                {"match": {"team": f"a{char}b"}, "receiver": _HOOK_TEAM}]}}}}),
            encoding="utf-8")
        trace, err = _trace_err(capsys, d)
        assert "WARN" not in err
        assert _delivered(trace) == [f"tenant-{_TT}"]
        assert trace["timing"]["group_wait"] == "10s"

    def test_only_enforced_matched_has_no_root_fallback(self, capsys, tmp_path):
        """A tenant whose receiver the generator skips has no route; the
        match-all enforced route (continue: true) is then the only match and
        Alertmanager does NOT add the root receiver."""
        conf = _tree(tmp_path, routing={"receiver": {"type": "webhook"}},
                     enforced={"receiver": _HOOK_NOC})
        trace = _trace(capsys, conf)
        assert _delivered(trace) == ["platform-enforced"]
        assert trace["final_receiver"].startswith("webhook → platform-enforced ")
        assert "only the enforced route matched, no root fallback" \
            in trace["final_receiver"]
        assert trace["steps"][1]["receiver_type"] == "webhook"


# Alertmanager itself as the oracle (#2293), on the generator's full ConfigMap.
_GAR = os.path.join(_TOOLS_DIR, "generate_alertmanager_routes.py")


def _am_quote(value: str) -> str:
    return '"' + (value.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n")) + '"'


@needs_amtool
class TestTraceAgreesWithAmtool:
    @pytest.mark.parametrize(
        "tree, alertname, severity, labels, expected",
        [s[1:] for s in SCENARIOS], ids=[s[0] for s in SCENARIOS])
    def test_same_receivers_as_amtool(self, capsys, tmp_path, tree, alertname,
                                      severity, labels, expected):
        conf = _tree(tmp_path, **tree)
        cm = tmp_path / "cm.yaml"
        r = subprocess.run(
            [sys.executable, _GAR, "--config-dir", str(conf),
             "--output-configmap", "-o", str(cm)],
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        am_yml = tmp_path / "alertmanager.yml"
        am_yml.write_text(yaml.safe_load(cm.read_text(encoding="utf-8"))
                          ["data"]["alertmanager.yml"], encoding="utf-8")
        trace = _trace(capsys, conf, *_label_args(labels), severity=severity,
                       alertname=alertname)
        am = subprocess.run(
            [shutil.which("amtool"), "config", "routes", "test",
             f"--config.file={am_yml}"]
            + [f"{k}={_am_quote(v)}" for k, v in trace["labels"].items()],
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        assert am.returncode == 0, am.stdout + am.stderr
        assert am.stdout.strip().split(",") == _delivered(trace) == expected


@needs_amtool
class TestPolicyScope:
    """Step 5 applies only the policies whose ``tenants`` list the traced
    tenant — the same scope the generator's check_domain_policies uses."""

    def _conf(self, tmp_path, tenants):
        return _tree(
            tmp_path,
            routing={"receiver": {"type": "slack",
                                  "api_url": "https://hooks.slack.com/x"}},
            policy={"chat-free": {"tenants": tenants, "constraints": {
                "forbidden_receiver_types": ["slack"]}}})

    def test_policy_for_another_tenant_does_not_apply(self, capsys, tmp_path):
        step5 = _trace(capsys, self._conf(tmp_path, ["some-other-tenant"]))["steps"][4]
        assert step5["passed"] is True
        assert "violations" not in step5
        assert "(no domain policy lists it)" in step5["detail"]

    def test_policy_without_tenants_does_not_apply(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={"receiver": {
            "type": "slack", "api_url": "https://hooks.slack.com/x"}},
            policy={"p": {"constraints": {"forbidden_receiver_types": ["slack"]}}})
        assert _trace(capsys, conf)["steps"][4]["passed"] is True

    def test_policy_listing_the_tenant_applies(self, capsys, tmp_path):
        step5 = _trace(capsys, self._conf(tmp_path, [_TT]))["steps"][4]
        assert step5["passed"] is False

    def test_agrees_with_the_generator(self, tmp_path):
        """Both sides of the fork: the generator's own verdict on the tree."""
        from generate_alertmanager_routes import load_tenant_configs
        for tenants, flagged in ((["some-other-tenant"], False), ([_TT], True)):
            d = tmp_path / str(flagged)
            d.mkdir()
            conf = self._conf(d, tenants)
            schema_warnings = load_tenant_configs(str(conf))[2]
            assert any("slack" in w for w in schema_warnings) is flagged

    def test_listed_but_constraints_null_is_said_so(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={"receiver": _SLACK},
                     policy={"p": {"tenants": [_TT], "constraints": None}})
        step5 = _trace(capsys, conf)["steps"][4]
        assert step5["passed"] is True
        assert "(no usable domain policy lists it)" in step5["detail"]
        assert "'constraints' is not a mapping" in step5["notes"][0]

    def test_tenants_not_a_list_is_said_so(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={"receiver": _SLACK},
                     policy={"p": {"tenants": _TT, "constraints": {
                         "forbidden_receiver_types": ["slack"]}}})
        step5 = _trace(capsys, conf)["steps"][4]
        assert step5["passed"] is True
        assert "'tenants' is not a list" in step5["notes"][0]

    def test_pass_claims_only_receiver_type_constraints(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={"receiver": _SLACK},
                     policy={"p": {"tenants": [_TT], "constraints": {
                         "allowed_receiver_types": ["slack"],
                         "max_repeat_interval": "1h"}}})
        step5 = _trace(capsys, conf)["steps"][4]
        assert step5["passed"] is True
        assert step5["detail"].startswith("Receiver-type constraints passed (p)")
        assert "not checked by --trace" in step5["detail"]
        assert "All domain policies passed" not in json.dumps(step5)


# The one path a real amtool cannot be made to take on demand: it answers
# non-zero. A minimal stand-in, first on PATH, that exits 1.
@pytest.fixture
def failing_amtool(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("the stand-in is a POSIX script")
    bin_dir = tmp_path / "failing-amtool-bin"
    bin_dir.mkdir()
    stub = bin_dir / "amtool"
    stub.write_text("#!/bin/sh\necho 'amtool: error: parse error' >&2\nexit 1\n",
                    encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


def test_amtool_failure_is_unknown_not_a_crash(capsys, tmp_path, failing_amtool):
    rc = er.main(["--config-dir", str(_tree(tmp_path)), "--tenant", _TT,
                  "--trace", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "failed (rc=1)" in captured.err and "parse error" in captured.err
    [trace] = json.loads(captured.out)
    assert trace["final_receiver"] == "(unknown: amtool gave no verdict)"


class TestWithoutAmtool:
    @pytest.fixture(autouse=True)
    def _no_amtool(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))

    def test_json_says_unknown_and_keeps_the_key_set(self, capsys, tmp_path):
        conf = _tree(tmp_path)
        rc = er.main(["--config-dir", str(conf), "--tenant", _TT, "--trace",
                      "--json"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "WARN: Not validated by Alertmanager: amtool not found on PATH" \
            in captured.err
        [trace] = json.loads(captured.out)
        assert set(trace) == {"tenant", "alertname", "severity", "labels",
                              "steps", "final_receiver", "inhibited",
                              "inhibit_reason", "timing"}
        assert trace["final_receiver"] == "(unknown: amtool not found)"
        step2 = trace["steps"][1]
        assert step2["matched_routes"] == []
        assert any(f"tenant-{_TT}" in row for row in step2["rendered_tree"])
        assert trace["steps"][4]["passed"] is True  # no policy at all

    def test_text_prints_the_rendered_tree(self, capsys, tmp_path):
        conf = _tree(tmp_path)
        assert er.main(["--config-dir", str(conf), "--tenant", _TT,
                        "--trace"]) == 0
        out = capsys.readouterr().out
        assert "Rendered route tree (not evaluated by Alertmanager):" in out
        assert "Receiver: (unknown: amtool not found)" in out
        assert "Timing: group_wait=(unknown)" in out

    def test_policy_with_type_constraint_is_not_evaluated(self, capsys, tmp_path):
        conf = _tree(tmp_path, routing={"receiver": _SLACK},
                     policy={"p": {"tenants": [_TT], "constraints": {
                         "forbidden_receiver_types": ["slack"]}}})
        step5 = _trace(capsys, conf)["steps"][4]
        assert step5["passed"] is None
        assert "not evaluated" in step5["detail"]
