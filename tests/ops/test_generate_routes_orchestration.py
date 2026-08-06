"""Orchestration-layer tests for generate_alertmanager_routes.py.

Companion to test_generate_alertmanager_routes.py (which covers route-graph
construction). This file targets the OUTPUT/CLI orchestrator concern:
render_output, load_base_config, assemble_configmap, apply_to_configmap
(mocked kubectl), and main() CLI paths (dry-run, validate, output-configmap,
apply, stdout).

Renamed from test_generate_routes_extended.py in the test-refactor sweep —
the suffix change captures the actual concern instead of a generic "extended".
The two files stay split because the combined LOC (~2200) is too large for a
single comprehensive test file.
"""
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from factories import (
    make_receiver, make_routing_config, make_tenant_yaml,
    make_enforced_routing, write_yaml,
)

from generate_alertmanager_routes import (
    render_output,
    load_base_config,
    assemble_configmap,
    apply_to_configmap,
    load_tenant_configs,
    generate_routes,
    generate_inhibit_rules,
    _build_enforced_routes,
    _build_tenant_routes,
    _build_custom_alert_routes,
    _build_watchdog_route,
    _build_synthetic_probe_route,
    _build_sentinel_sinkhole_route,
    _parse_config_files,
    write_text_secure,
)
from _grar_render import _merge_routes_receivers_inhibits, _enforce_equal_labels_gated
from generate_alertmanager_routes import (
    assert_equal_labels_gated,
    assert_platform_alerts_not_tenant_silenceable,
    assert_watchdog_inhibit_immunity,
    find_tenant_silenceable_platform_inhibits,
    find_ungated_equal_label_inhibits,
    find_watchdog_suppressing_inhibits,
)

import generate_alertmanager_routes as gar


# ============================================================
# render_output
# ============================================================
class TestRenderOutput:
    """render_output() YAML fragment rendering."""

    def test_routes_only(self):
        routes = [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}]
        result = render_output(routes, [], None)
        parsed = yaml.safe_load(result)
        assert "route" in parsed
        assert parsed["route"]["routes"] == routes
        assert "receivers" not in parsed
        assert "inhibit_rules" not in parsed

    def test_receivers_only(self):
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://x.com"}]}]
        result = render_output([], receivers, None)
        parsed = yaml.safe_load(result)
        assert "receivers" in parsed
        assert "route" not in parsed

    def test_inhibit_rules_only(self):
        inhibit = [{"source_matchers": ["severity=\"critical\""],
                     "target_matchers": ["severity=\"warning\""]}]
        result = render_output([], [], inhibit)
        parsed = yaml.safe_load(result)
        assert "inhibit_rules" in parsed

    def test_all_sections(self):
        routes = [{"receiver": "r1"}]
        receivers = [{"name": "r1"}]
        inhibit = [{"source_matchers": ["a=b"]}]
        result = render_output(routes, receivers, inhibit)
        parsed = yaml.safe_load(result)
        assert "route" in parsed
        assert "receivers" in parsed
        assert "inhibit_rules" in parsed

    def test_empty_all(self):
        result = render_output([], [], [])
        parsed = yaml.safe_load(result)
        # Empty lists => sections omitted
        assert parsed is None or parsed == {}


# ============================================================
# load_base_config
# ============================================================
class TestLoadBaseConfig:
    """load_base_config() tests."""

    def test_no_path_returns_defaults(self):
        base = load_base_config(None)
        assert "global" in base
        assert "route" in base
        assert "receivers" in base
        assert "inhibit_rules" in base

    def test_nonexistent_path_returns_defaults(self):
        base = load_base_config("/nonexistent/path.yaml")
        assert "global" in base
        assert base["route"]["receiver"] == "default"

    def test_valid_file(self, tmp_path):
        config = {
            "global": {"resolve_timeout": "10m"},
            "route": {
                "receiver": "custom-default",
                "group_by": ["alertname"],
            },
            "receivers": [{"name": "custom-default"}],
            "inhibit_rules": [{"source_matchers": ["severity=\"critical\""]}],
        }
        p = tmp_path / "base.yaml"
        p.write_text(yaml.dump(config), encoding="utf-8")
        base = load_base_config(str(p))
        assert base["global"]["resolve_timeout"] == "10m"
        assert base["route"]["receiver"] == "custom-default"

    def test_partial_file_fills_defaults(self, tmp_path):
        """File with missing keys gets defaults filled in."""
        config = {"global": {"resolve_timeout": "3m"}}
        p = tmp_path / "partial.yaml"
        p.write_text(yaml.dump(config), encoding="utf-8")
        base = load_base_config(str(p))
        assert base["global"]["resolve_timeout"] == "3m"
        assert "route" in base
        assert "receivers" in base


# ============================================================
# assemble_configmap
# ============================================================
class TestAssembleConfigmap:
    """assemble_configmap() K8s ConfigMap generation."""

    def test_basic_configmap(self):
        base = load_base_config(None)
        routes = [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}]
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://x.com"}]}]
        inhibit = [{"source_matchers": ["severity=\"critical\""]}]

        cm_yaml = assemble_configmap(base, routes, receivers, inhibit)
        parsed = yaml.safe_load(cm_yaml)

        assert parsed["apiVersion"] == "v1"
        assert parsed["kind"] == "ConfigMap"
        assert parsed["metadata"]["name"] == "alertmanager-config"
        assert parsed["metadata"]["namespace"] == "monitoring"
        assert "alertmanager.yml" in parsed["data"]

        am_config = yaml.safe_load(parsed["data"]["alertmanager.yml"])
        routes_out = am_config["route"]["routes"]
        # ADR-025 D1 (#838) + S7/S8 (#741) + ADR-025 synthetic-probe + #1095: four
        # platform-static routes are always injected at the FRONT — Watchdog
        # liveness (0), Custom Alerts isolation (1), synthetic-probe sinkhole (2),
        # sentinel sinkhole (3) — all continue:false; the tenant route follows.
        assert len(routes_out) == 5
        assert routes_out[0]["matchers"] == ['alertname="Watchdog"']
        assert routes_out[0]["receiver"] == "watchdog-heartbeat"
        assert routes_out[0]["continue"] is False
        assert routes_out[1]["matchers"] == ['component="custom"']
        assert routes_out[1]["receiver"] == "custom-alerts-firehose"
        assert routes_out[1]["continue"] is False
        assert routes_out[2]["matchers"] == ['component="synthetic-probe"']
        assert routes_out[2]["receiver"] == "synthetic-receiver"
        assert routes_out[2]["continue"] is False
        assert routes_out[3]["matchers"] == ['component="sentinel"']
        assert routes_out[3]["receiver"] == "sentinel-sinkhole"
        assert routes_out[3]["continue"] is False
        assert routes_out[4]["receiver"] == "tenant-db-a"
        # Base receiver + tenant receiver + injected firehose + watchdog +
        # synthetic + sentinel sink
        names = {r["name"] for r in am_config["receivers"]}
        assert "default" in names
        assert "tenant-db-a" in names
        assert "custom-alerts-firehose" in names
        assert "watchdog-heartbeat" in names
        assert "synthetic-receiver" in names
        assert "sentinel-sinkhole" in names

    def test_custom_namespace_and_name(self):
        base = load_base_config(None)
        cm_yaml = assemble_configmap(base, [], [], [],
                                     namespace="custom-ns",
                                     configmap_name="my-config")
        parsed = yaml.safe_load(cm_yaml)
        assert parsed["metadata"]["namespace"] == "custom-ns"
        assert parsed["metadata"]["name"] == "my-config"

    def test_dedup_receivers(self):
        """Tenant receivers with same name as base are not duplicated."""
        base = load_base_config(None)
        # Add a receiver with the same name as in base
        receivers = [{"name": "default", "webhook_configs": [{"url": "https://x.com"}]}]
        cm_yaml = assemble_configmap(base, [], receivers, [])
        parsed = yaml.safe_load(cm_yaml)
        am_config = yaml.safe_load(parsed["data"]["alertmanager.yml"])
        default_count = sum(1 for r in am_config["receivers"] if r["name"] == "default")
        assert default_count == 1


# ============================================================
# ADR-025 D1 (#838) + S7/S8 (#741): platform-static route injection
# ============================================================
class TestCustomAlertIsolationInjection:
    """The four platform-static routes — Watchdog liveness (index 0), Custom
    Alerts isolation (index 1), synthetic-probe sinkhole (index 2), sentinel
    sinkhole (index 3, #1095) — plus their receivers must be present and pinned
    at the FRONT of the assembled ConfigMap, across BOTH the --output-configmap
    (assemble_configmap) and --apply (_merge_routes_receivers_inhibits) paths,
    and survive the route-REPLACE."""

    def _routes_of(self, cm_yaml):
        parsed = yaml.safe_load(cm_yaml)
        return yaml.safe_load(parsed["data"]["alertmanager.yml"])["route"]["routes"]

    def test_injected_even_with_no_tenant_routes(self):
        # empty generated routes (no tenants) → all four static routes still present
        # and pinned: Watchdog 0, custom 1, synthetic-probe 2, sentinel sink 3.
        cm_yaml = assemble_configmap(load_base_config(None), [], [], [])
        routes = self._routes_of(cm_yaml)
        assert routes[0]["matchers"] == ['alertname="Watchdog"']
        assert routes[0]["receiver"] == "watchdog-heartbeat"
        assert routes[0]["continue"] is False
        assert routes[1]["matchers"] == ['component="custom"']
        assert routes[1]["receiver"] == "custom-alerts-firehose"
        assert routes[1]["continue"] is False
        assert routes[2]["matchers"] == ['component="synthetic-probe"']
        assert routes[2]["receiver"] == "synthetic-receiver"
        assert routes[2]["continue"] is False
        assert routes[3]["matchers"] == ['component="sentinel"']
        assert routes[3]["receiver"] == "sentinel-sinkhole"
        assert routes[3]["continue"] is False

    def test_idempotent_no_duplicate(self):
        # if Watchdog / component="custom" / synthetic-probe / component="sentinel"
        # routes are already present, do not add a second of any — re-merging an
        # injected config is stable.
        existing = (_build_watchdog_route()[0] + _build_custom_alert_routes()[0]
                    + _build_synthetic_probe_route()[0]
                    + _build_sentinel_sinkhole_route()[0])
        cm_yaml = assemble_configmap(load_base_config(None), list(existing), [], [])
        routes = self._routes_of(cm_yaml)
        assert sum(1 for r in routes if 'component="custom"' in r.get("matchers", [])) == 1
        assert sum(1 for r in routes if 'alertname="Watchdog"' in r.get("matchers", [])) == 1
        assert sum(1 for r in routes if 'component="synthetic-probe"' in r.get("matchers", [])) == 1
        assert sum(1 for r in routes if 'component="sentinel"' in r.get("matchers", [])) == 1

    def test_static_routes_forced_to_front_even_when_not_first(self):
        # CodeRabbit gap (generalized to Watchdog): existing Watchdog/custom routes
        # sitting AFTER a continue:true match-all enforced route must be normalized
        # to the front (else the enforced route intercepts them first → leak). The
        # heartbeat is the most important to pin — Watchdog must end up at index 0.
        enforced = {"receiver": "platform-enforced", "continue": True}  # match-all
        existing_custom = _build_custom_alert_routes()[0][0]
        existing_wd = _build_watchdog_route()[0][0]
        existing_probe = _build_synthetic_probe_route()[0][0]
        existing_sentinel = _build_sentinel_sinkhole_route()[0][0]
        cm_yaml = assemble_configmap(
            load_base_config(None),
            [enforced, existing_custom, existing_probe, existing_sentinel,
             existing_wd], [], [])
        routes = self._routes_of(cm_yaml)
        wd_idx = [i for i, r in enumerate(routes)
                  if 'alertname="Watchdog"' in r.get("matchers", [])]
        custom_idx = [i for i, r in enumerate(routes)
                      if 'component="custom"' in r.get("matchers", [])]
        probe_idx = [i for i, r in enumerate(routes)
                     if 'component="synthetic-probe"' in r.get("matchers", [])]
        sentinel_idx = [i for i, r in enumerate(routes)
                        if 'component="sentinel"' in r.get("matchers", [])]
        assert wd_idx == [0], routes        # Watchdog pinned to index 0
        assert custom_idx == [1], routes    # custom pinned to index 1
        assert probe_idx == [2], routes     # synthetic-probe pinned to index 2
        assert sentinel_idx == [3], routes  # sentinel sink pinned to index 3
        assert routes[4]["receiver"] == "platform-enforced"

    def test_apply_path_prepends_and_preserves_silent_inhibit(self):
        # --apply replaces route.routes; Watchdog must lead (index 0), custom
        # follow (index 1), and the base CustomRecipeSilent inhibit (source has no
        # metric_group) must survive.
        existing = {
            "route": {"receiver": "default", "routes": []},
            "receivers": [{"name": "default"}],
            "inhibit_rules": [
                {"source_matchers": ['alertname="CustomRecipeSilent"'],
                 "target_matchers": ['component="custom"'],
                 "equal": ["tenant", "name"]},
            ],
        }
        tenant_routes = [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}]
        gen_inhibits = [_build_inhibit_for_test()]
        merged = _merge_routes_receivers_inhibits(
            existing, tenant_routes, [{"name": "tenant-db-a"}], gen_inhibits)
        routes = merged["route"]["routes"]
        assert routes[0]["matchers"] == ['alertname="Watchdog"']  # watchdog leads
        assert routes[1]["matchers"] == ['component="custom"']    # custom second
        assert any(r["receiver"] == "tenant-db-a" for r in routes)  # tenant route kept
        assert {r["name"] for r in merged["receivers"]} >= {
            "default", "custom-alerts-firehose", "watchdog-heartbeat",
            "sentinel-sinkhole", "tenant-db-a"}
        # the silent sentinel inhibit (no metric_group) is preserved
        assert any('alertname="CustomRecipeSilent"' in i.get("source_matchers", [])
                   for i in merged["inhibit_rules"])

    def test_apply_path_preserves_base_watchdog_receiver_url_file(self):
        # The injected watchdog-heartbeat receiver is NAME-ONLY; the --apply merge
        # must NOT clobber a richer existing definition (the base url_file secret
        # ref lives only in the live ConfigMap and would otherwise be lost).
        rich_wd = {"name": "watchdog-heartbeat",
                   "webhook_configs": [{"url_file": "/etc/alertmanager/secrets/watchdog-heartbeat-url"}]}
        existing = {
            "route": {"receiver": "default", "routes": []},
            "receivers": [{"name": "default"}, rich_wd],
            "inhibit_rules": [],
        }
        merged = _merge_routes_receivers_inhibits(
            existing, [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}],
            [{"name": "tenant-db-a"}], [])
        wd = [r for r in merged["receivers"] if r["name"] == "watchdog-heartbeat"]
        assert len(wd) == 1
        assert wd[0]["webhook_configs"][0]["url_file"] == \
            "/etc/alertmanager/secrets/watchdog-heartbeat-url"

    def test_watchdog_route_knobs(self):
        # ADR-025 D1 cadence contract on the generated artifact.
        cm_yaml = assemble_configmap(load_base_config(None), [], [], [])
        wd = self._routes_of(cm_yaml)[0]
        assert wd["receiver"] == "watchdog-heartbeat"
        assert wd["group_by"] == ["alertname"]      # not root [alertname, tenant]
        assert wd["group_wait"] == "0s"
        assert wd["group_interval"] == "1m"
        assert wd["repeat_interval"] == "3m"
        assert wd["continue"] is False               # never leaks to a human channel

    def test_assemble_path_preserves_base_watchdog_receiver_url_file(self):
        # Assemble path (--output-configmap): the base's rich watchdog-heartbeat
        # receiver (url_file) must win over the injected name-only placeholder.
        base = load_base_config(None)
        base["receivers"] = base.get("receivers", []) + [
            {"name": "watchdog-heartbeat",
             "webhook_configs": [{"url_file": "/etc/alertmanager/secrets/watchdog-heartbeat-url"}]}]
        cm_yaml = assemble_configmap(base, [], [], [])
        am = yaml.safe_load(yaml.safe_load(cm_yaml)["data"]["alertmanager.yml"])
        wd = [r for r in am["receivers"] if r["name"] == "watchdog-heartbeat"]
        assert len(wd) == 1
        assert wd[0]["webhook_configs"][0]["url_file"].endswith("watchdog-heartbeat-url")


def _build_inhibit_for_test():
    # a generated severity-dedup inhibit (source HAS metric_group → replaced on merge)
    return {"source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="db-a"'],
            "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="db-a"'],
            "equal": ["metric_group"]}


# ============================================================
# #1092 0-pre (ADR-031 hard prerequisite): custom subtree per-tenant delivery
# ============================================================
class TestCustomSubtreeTenantDelivery:
    """The injected Custom Alerts isolation route (index 1) carries per-tenant
    child routes pointing at the EXISTING tenant-<name> receivers (#1092 0-pre).
    Tenants without a valid _routing get no child and fall back to the parent
    custom-alerts-firehose; children carry ONLY matchers + receiver — grouping /
    timing ride Alertmanager-native inheritance from the parent (restating them
    would be a second SoT). Fixture tenant names (db-a/db-b) follow this file's
    existing fixture convention."""

    def _custom_route_of(self, cm_yaml):
        parsed = yaml.safe_load(cm_yaml)
        routes = yaml.safe_load(parsed["data"]["alertmanager.yml"])["route"]["routes"]
        customs = [r for r in routes if 'component="custom"' in r.get("matchers", [])]
        assert len(customs) == 1
        return customs[0]

    def test_children_injected_for_main_tenant_routes(self):
        # unsorted input on purpose — children must come out tenant-sorted
        routes = [
            {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
            {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
        ]
        receivers = [{"name": "tenant-db-a"}, {"name": "tenant-db-b"}]
        cm_yaml = assemble_configmap(load_base_config(None), routes, receivers, [])
        custom = self._custom_route_of(cm_yaml)
        # parent semantics unchanged: firehose fallback + hard isolation
        assert custom["receiver"] == "custom-alerts-firehose"
        assert custom["continue"] is False
        # children: sorted by tenant, pointing at the existing tenant receivers
        assert custom["routes"] == [
            {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
            {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
        ]
        # inheritance lock: children must NOT restate grouping/timing knobs
        for child in custom["routes"]:
            assert set(child.keys()) == {"matchers", "receiver"}, child

    def test_no_tenants_no_routes_key(self):
        # REGRESSION-CRITICAL: with no tenants the route must stay the flat
        # pre-#1092 shape — not even an empty `routes: []` key — because the
        # committed-base drift guard compares dicts exactly
        # (test_committed_base_configmap_watchdog_route_is_first).
        flat = _build_custom_alert_routes()[0][0]
        assert "routes" not in flat
        assert _build_custom_alert_routes(None)[0][0] == flat
        assert _build_custom_alert_routes([])[0][0] == flat
        # assemble path with zero tenant routes: injected custom route stays flat
        cm_yaml = assemble_configmap(load_base_config(None), [], [], [])
        assert "routes" not in self._custom_route_of(cm_yaml)

    def test_enforced_and_override_routes_not_promoted(self):
        # platform-enforced-<t> and tenant-<t>-override-<idx> routes ALSO carry
        # a tenant matcher; receiver-name equality must exclude both — promoting
        # the enforced route would funnel custom alerts into the platform NOC,
        # the exact leak the isolation subtree exists to prevent.
        routes = [
            {"matchers": ['tenant="db-a"'], "receiver": "platform-enforced-db-a",
             "continue": True},
            {"matchers": ['tenant="db-a"', 'alertname="SomeAlert"'],
             "receiver": "tenant-db-a-override-0"},
        ]
        cm_yaml = assemble_configmap(load_base_config(None), routes, [], [])
        assert "routes" not in self._custom_route_of(cm_yaml)

    def test_apply_path_children_and_idempotency(self):
        existing = {
            "route": {"receiver": "default", "routes": []},
            "receivers": [{"name": "default"}],
            "inhibit_rules": [],
        }
        gen_routes = [
            {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"},
            {"matchers": ['tenant="db-b"'], "receiver": "tenant-db-b"},
        ]
        merged = _merge_routes_receivers_inhibits(
            existing, gen_routes, [{"name": "tenant-db-a"}, {"name": "tenant-db-b"}], [])
        customs = [r for r in merged["route"]["routes"]
                   if 'component="custom"' in r.get("matchers", [])]
        assert len(customs) == 1
        assert [c["receiver"] for c in customs[0]["routes"]] == \
            ["tenant-db-a", "tenant-db-b"]
        # Re-feed the ALREADY-INJECTED route list with tenant db-b removed: the
        # stale custom route (children db-a+db-b) is dropped whole and rebuilt —
        # still exactly one custom route, no duplicated children, and the
        # removed tenant's stale child gone.
        refeed = [r for r in merged["route"]["routes"]
                  if r.get("receiver") != "tenant-db-b"]
        merged2 = _merge_routes_receivers_inhibits(
            merged, refeed, [{"name": "tenant-db-a"}], [])
        customs2 = [r for r in merged2["route"]["routes"]
                    if 'component="custom"' in r.get("matchers", [])]
        assert len(customs2) == 1
        assert customs2[0]["routes"] == [
            {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"}]

    def test_disabled_tenant_falls_back_to_firehose(self, tmp_path):
        # e2e: conf.d → load_tenant_configs → generate_routes → assemble. A
        # tenant with _routing: "disable" has no main tenant route → no child
        # (its custom alerts stay on the parent firehose); the routed tenant
        # keeps its child.
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"mysql_connections": 80}}), encoding="utf-8")
        (d / "db-a.yaml").write_text(
            make_tenant_yaml("db-a", routing=make_routing_config()), encoding="utf-8")
        (d / "db-b.yaml").write_text(
            make_tenant_yaml("db-b", routing="disable"), encoding="utf-8")
        routing_configs, _dedup, _sw, enforced, _mc = load_tenant_configs(str(d))
        routes, receivers, _rw = generate_routes(
            routing_configs, enforced_routing=enforced)
        cm_yaml = assemble_configmap(load_base_config(None), routes, receivers, [])
        custom = self._custom_route_of(cm_yaml)
        assert custom["routes"] == [
            {"matchers": ['tenant="db-a"'], "receiver": "tenant-db-a"}]
        # the disabled tenant contributes no child anywhere in the subtree
        assert not any('tenant="db-b"' in c.get("matchers", [])
                       for c in custom["routes"])
        # parent fallback receiver unchanged
        assert custom["receiver"] == "custom-alerts-firehose"


# ============================================================
# ADR-025 D1 (#838): Watchdog inhibition-immunity invariant
# ============================================================
class TestWatchdogInhibitImmunity:
    """No inhibit_rule's target_matchers may match the always-firing Watchdog
    heartbeat — else it is suppressed before egress and the external dead-man's-
    switch false-alarms. find_watchdog_suppressing_inhibits is the codified guard."""

    def test_benign_rules_pass(self):
        # The real shapes shipped in configmap-alertmanager.yaml must NOT be flagged.
        benign = [
            # severity-dedup (target severity=warning, never Watchdog's severity=none)
            {"source_matchers": ['severity="critical"', 'metric_group=~".+"', 'tenant="db-a"'],
             "target_matchers": ['severity="warning"', 'metric_group=~".+"', 'tenant="db-a"']},
            {"source_matchers": ['alertname="TenantSilentWarning"'],
             "target_matchers": ['severity="warning"'], "equal": ["tenant"]},
            {"source_matchers": ['alertname="TenantSilentCritical"'],
             "target_matchers": ['severity="critical"'], "equal": ["tenant"]},
            {"source_matchers": ['alertname="CustomRecipeSilent"'],
             "target_matchers": ['component="custom"'], "equal": ["tenant", "name"]},
        ]
        assert find_watchdog_suppressing_inhibits(benign) == []
        assert_watchdog_inhibit_immunity(benign)  # does not raise

    def test_exact_watchdog_target_flagged(self):
        bad = [{"source_matchers": ['alertname="ClusterDown"'],
                "target_matchers": ['alertname="Watchdog"']}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1
        with pytest.raises(ValueError, match="Watchdog"):
            assert_watchdog_inhibit_immunity(bad)

    def test_regex_matchall_target_flagged(self):
        # A broad alertname=~".+" target (CodeRabbit's dangerous pattern class)
        # matches Watchdog → must be flagged.
        bad = [{"source_matchers": ['alertname="ClusterDown"'],
                "target_matchers": ['alertname=~".+"']}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1

    def test_empty_target_is_matchall_flagged(self):
        # target_matchers: [] is an explicit match-all → suppresses Watchdog too.
        bad = [{"source_matchers": ['alertname="ClusterDown"'], "target_matchers": []}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1

    def test_severity_none_target_flagged(self):
        # A future rule targeting severity=none would catch Watchdog (its severity).
        bad = [{"source_matchers": ['alertname="ClusterDown"'],
                "target_matchers": ['severity="none"']}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1

    def test_negative_matcher_suppressing_watchdog_flagged(self):
        # Negative-matching trap (Gemini Day-2 review): a "suppress everything
        # that's NOT critical" rule (severity!="critical") MATCHES Watchdog's
        # severity="none" and would silently strangle the heartbeat. The != / !~
        # branches of the matcher evaluator must catch this fail-closed.
        bad_ne = [{"source_matchers": ['alertname="ClusterDown"'],
                   "target_matchers": ['severity!="critical"']}]
        assert len(find_watchdog_suppressing_inhibits(bad_ne)) == 1
        with pytest.raises(ValueError, match="Watchdog"):
            assert_watchdog_inhibit_immunity(bad_ne)
        # !~ form: "not matching the regex critical|warning" also catches none
        bad_nre = [{"source_matchers": ['alertname="ClusterDown"'],
                    "target_matchers": ['severity!~"critical|warning"']}]
        assert len(find_watchdog_suppressing_inhibits(bad_nre)) == 1
        # control: a negative matcher that EXCLUDES Watchdog must NOT be flagged
        ok = [{"source_matchers": ['alertname="ClusterDown"'],
               "target_matchers": ['alertname!="Watchdog"']}]
        assert find_watchdog_suppressing_inhibits(ok) == []

    def test_legacy_target_match_map_supported(self):
        bad = [{"source_match": {"alertname": "ClusterDown"},
                "target_match": {"alertname": "Watchdog"}}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1

    def test_legacy_target_match_re_map_supported(self):
        # regex map form: alertname=~".+" matches Watchdog
        bad = [{"source_match": {"alertname": "ClusterDown"},
                "target_match_re": {"alertname": ".+"}}]
        assert len(find_watchdog_suppressing_inhibits(bad)) == 1

    def test_validate_mode_tripwire_exits_on_watchdog_suppressing_inhibit(self):
        # The --validate regression tripwire must exit non-zero if a GENERATED
        # inhibit rule would suppress Watchdog (guards a future generator change).
        bad = [{"source_matchers": ['alertname="ClusterDown"'],
                "target_matchers": ['alertname="Watchdog"']}]
        with pytest.raises(SystemExit) as exc:
            gar._validate_mode([], [], bad, [])
        assert exc.value.code != 0

    def test_assemble_fails_closed_on_watchdog_suppressing_base_inhibit(self):
        base = load_base_config(None)
        base["inhibit_rules"] = [
            {"source_matchers": ['alertname="ClusterDown"'],
             "target_matchers": ['alertname=~".*"']}]  # would swallow Watchdog
        with pytest.raises(ValueError, match="Watchdog"):
            assemble_configmap(base, [], [], [])

    def test_committed_base_configmap_holds_invariant(self):
        # The hand-authored k8s/03-monitoring/configmap-alertmanager.yaml inhibit
        # rules must never suppress Watchdog. This is the mechanical guard on the
        # REAL deployed base (the generator only validates the generated subset).
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".."))
        cm_path = os.path.join(
            repo_root, "k8s", "03-monitoring", "configmap-alertmanager.yaml")
        cm = yaml.safe_load(open(cm_path, encoding="utf-8").read())
        am = yaml.safe_load(cm["data"]["alertmanager.yml"])
        offending = find_watchdog_suppressing_inhibits(am.get("inhibit_rules", []))
        assert offending == [], (
            f"configmap-alertmanager.yaml has inhibit rule(s) that suppress the "
            f"Watchdog heartbeat: {offending}")

    def test_committed_base_configmap_watchdog_route_is_first(self):
        # The hand-authored base must keep Watchdog as routes[0] so the committed
        # config is self-consistent with what the generator re-injects.
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".."))
        cm_path = os.path.join(
            repo_root, "k8s", "03-monitoring", "configmap-alertmanager.yaml")
        cm = yaml.safe_load(open(cm_path, encoding="utf-8").read())
        am = yaml.safe_load(cm["data"]["alertmanager.yml"])
        routes = am["route"]["routes"]
        # Drift guard: the hand-authored base route[0] must equal exactly what the
        # generator re-injects, so editing one knob in the base without the other
        # can't silently diverge.
        assert routes[0] == _build_watchdog_route()[0][0]
        assert routes[0]["matchers"] == ['alertname="Watchdog"']
        assert routes[0]["receiver"] == "watchdog-heartbeat"
        assert routes[0]["continue"] is False
        # and the receiver exists with a url_file (no inline plaintext URL)
        wd_recv = [r for r in am["receivers"] if r["name"] == "watchdog-heartbeat"]
        assert len(wd_recv) == 1
        wh = wd_recv[0]["webhook_configs"][0]
        assert "url" not in wh and wh["url_file"].endswith("watchdog-heartbeat-url")
        # Same drift guard for the other three pinned static routes — custom (index
        # 1), synthetic-probe (index 2), sentinel sinkhole (index 3, #1095) — so a
        # hand-edit to the committed base that forgets the builder (or vice-versa)
        # fails loud here, not silently in prod.
        assert routes[1] == _build_custom_alert_routes()[0][0]
        assert routes[1]["matchers"] == ['component="custom"']
        assert routes[2] == _build_synthetic_probe_route()[0][0]
        assert routes[2]["matchers"] == ['component="synthetic-probe"']
        assert routes[2]["receiver"] == "synthetic-receiver"
        assert routes[2]["continue"] is False
        assert routes[3] == _build_sentinel_sinkhole_route()[0][0]
        assert routes[3]["matchers"] == ['component="sentinel"']
        assert routes[3]["receiver"] == "sentinel-sinkhole"
        assert routes[3]["continue"] is False
        # synthetic-receiver / sentinel-sinkhole must be DEFINED in the committed
        # base (route → defined receiver; else amtool rejects the raw file).
        assert any(r["name"] == "synthetic-receiver" for r in am["receivers"])
        assert any(r["name"] == "sentinel-sinkhole" for r in am["receivers"])


# ============================================================
# A tenant must not be able to silence a PLATFORM self-monitoring alert
# ============================================================
def _committed_alertmanager_config():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    cm_path = os.path.join(
        repo_root, "k8s", "03-monitoring", "configmap-alertmanager.yaml")
    cm = yaml.safe_load(open(cm_path, encoding="utf-8").read())
    return yaml.safe_load(cm["data"]["alertmanager.yml"])


# Non-vacuity floor for every platform-pack assertion in this module. ONE copy on
# purpose: it was two (this and the alert_source contract's own), and a floor that
# exists twice is a floor that gets bumped once — the stale copy then keeps a lower
# bar without failing anything, which is the same silent-drift class the shared
# scanner below argues against (CodeRabbit, PR #1270). Grows with the pack; it is a
# floor on the SUM across every platform rules ConfigMap, so splitting the pack into
# two files keeps satisfying it instead of silently halving coverage.
_MIN_PLATFORM_ALERTS = 40


def _real_platform_label_sets():
    """Label sets of every SHIPPED platform alert, derived from the ConfigMap.

    Two sources of `tenant`, and the second is the one a rule-file reader misses:
      * rule-level `labels.tenant` (TenantMetricsOverLimit), and
      * a runtime label produced by the expr's `sum by (tenant)`
        (FederationRejectionRateAnomaly / FederationGatewayBackendErrors) — those
        rules' `labels:` blocks say nothing about tenant.
    Deriving instead of hardcoding keeps the guard from going stale when platform
    alerts are added or renamed.
    """
    import re as _re
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    path = os.path.join(
        repo_root, "k8s", "03-monitoring", "configmap-rules-platform.yaml")
    cm = yaml.safe_load(open(path, encoding="utf-8").read())
    out = []
    for body in (cm.get("data") or {}).values():
        for group in (yaml.safe_load(body) or {}).get("groups", []):
            for rule in group.get("rules", []):
                if "alert" not in rule:
                    continue
                labels = dict(rule.get("labels") or {})
                if labels.get("alert_source") != "platform":
                    continue  # Watchdog rides its own lane; guarded separately
                labels["alertname"] = rule["alert"]
                if any("tenant" in [t.strip() for t in m.group(1).split(",")]
                       for m in _re.finditer(r"\bby\s*\(([^)]*)\)", rule["expr"])):
                    labels.setdefault("tenant", "any-tenant")
                out.append(labels)
    return out


class TestPlatformAlertsNotTenantSilenceable:
    """Silent Mode is a TENANT-controlled switch; it must not reach a PLATFORM
    self-monitoring alert.

    Three platform alerts carry a `tenant` label and are severity=warning, so the
    `severity="warning"` + `tenant=~".+"` silent-mode target caught them: a tenant
    setting `_silent_mode` once would mute the platform's own failure alerts —
    including FederationGatewayBackendErrors, whose annotation states the fault is
    the platform's. Fixed by adding `alert_source=""` to the two silent-mode
    target_matchers and codified by find_tenant_silenceable_platform_inhibits.
    """

    def test_derivation_is_non_vacuous(self):
        sets = _real_platform_label_sets()
        assert len(sets) >= _MIN_PLATFORM_ALERTS, (
            f"only {len(sets)} platform alerts derived")
        tenant_bearing = sorted(s["alertname"] for s in sets if "tenant" in s)
        assert tenant_bearing == [
            "FederationGatewayBackendErrors",
            "FederationRejectionRateAnomaly",
            "TenantMetricsOverLimit",
        ], tenant_bearing
        assert all(s["severity"] == "warning"
                   for s in sets if "tenant" in s), sets

    def test_runtime_default_is_the_derived_set_not_a_sample(self):
        """The guard's DEFAULT probe set must be the full pack, not a sample.

        The render paths (`assemble_configmap`, `_merge_routes_receivers_inhibits`)
        call the assert WITHOUT passing label sets, so whatever the default is IS
        the production guarantee. A hand-written sample goes stale on the next
        alert — and did: the pack grew 5 alerts (#1259, #1266) mid-PR, none of
        them in the constant. Tests that pass the derived set explicitly cannot
        catch that divergence, which is exactly how it was missed.
        """
        from _grar_validate import (  # noqa: PLC0415
            PLATFORM_ALERT_IDENTITY_LABELS, platform_alert_identities)
        default_names = {s["alertname"] for s in platform_alert_identities()}
        derived_names = {s["alertname"] for s in _real_platform_label_sets()}
        assert default_names == derived_names, (
            "the runtime default no longer matches the shipped pack — the "
            f"guard would silently under-check: {derived_names ^ default_names}")
        sample_names = {s["alertname"] for s in PLATFORM_ALERT_IDENTITY_LABELS}
        assert sample_names < derived_names, (
            "the fallback constant must stay a STRICT subset (it may only "
            "under-report, never green-light something the full set flags)")

    def test_target_pinning_a_literal_tenant_is_still_caught(self):
        """A target pinning `tenant="db-a"` suppresses the tenant-bearing
        platform alerts for that tenant. Probing with a fixed placeholder value
        misses it (equality just fails), so the tenant value is taken FROM THE
        RULE — runtime tenants are unbounded and cannot be enumerated up front.
        """
        rule = {
            "source_matchers": ['alertname="TenantSilentWarning"', 'tenant="db-a"'],
            "target_matchers": ['severity="warning"', 'tenant="db-a"'],
            "equal": ["tenant"],
        }
        offending = find_tenant_silenceable_platform_inhibits([rule])
        assert offending, (
            "a target pinning a literal tenant slipped past the guard — it "
            "would mute the 3 tenant-bearing platform alerts for that tenant")

    def test_target_pinning_an_unsampled_platform_alertname_is_caught(self):
        """Deriving from the ConfigMap is what makes alertname coverage
        fail-closed. `ConfigReloaderNotStarting` ships in the pack but is not in
        the fallback constant; with the constant alone this rule reads as safe.
        """
        from _grar_validate import PLATFORM_ALERT_IDENTITY_LABELS  # noqa: PLC0415
        rule = {
            "source_matchers": ['alertname="TenantSilentWarning"', 'tenant=~".+"'],
            "target_matchers": ['alertname="ConfigReloaderNotStarting"'],
            "equal": ["tenant"],
        }
        assert find_tenant_silenceable_platform_inhibits([rule]), \
            "unsampled platform alertname slipped past the derived guard"
        # and prove the sample-only path is what used to miss it
        assert not find_tenant_silenceable_platform_inhibits(
            [rule], PLATFORM_ALERT_IDENTITY_LABELS), (
            "fallback constant unexpectedly covers this alertname — rewrite "
            "this test against one it genuinely does not sample")

    def test_structurally_unreachable_target_is_not_false_flagged(self):
        """Guard the guard against over-reach: a target requiring a non-empty
        tenant cannot suppress a tenantless platform alert, so naming one must
        NOT be reported. Without this, widening the probe set would trade a
        fail-open for a fail-closed that blocks legitimate config.
        """
        rule = {
            "source_matchers": ['alertname="TenantSilentWarning"', 'tenant=~".+"'],
            "target_matchers": ['alertname="ConfigReloaderNotStarting"',
                                'tenant=~".+"'],
            "equal": ["tenant"],
        }
        assert find_tenant_silenceable_platform_inhibits([rule]) == [], (
            "false positive: ConfigReloaderNotStarting carries no tenant, so a "
            'tenant=~".+" target can never match it')

    def test_identity_loader_falls_back_when_configmap_unreachable(self):
        from _grar_validate import (  # noqa: PLC0415
            PLATFORM_ALERT_IDENTITY_LABELS, platform_alert_identities)
        assert platform_alert_identities("/nonexistent/platform.yaml") == \
            PLATFORM_ALERT_IDENTITY_LABELS

    def test_committed_base_configmap_holds_invariant(self):
        am = _committed_alertmanager_config()
        offending = find_tenant_silenceable_platform_inhibits(
            am.get("inhibit_rules", []), _real_platform_label_sets())
        assert offending == [], (
            "configmap-alertmanager.yaml has tenant-triggered inhibit rule(s) that "
            f"suppress a platform alert: "
            f"{[(i, lbls.get('alertname')) for i, _r, lbls in offending]}")
        # and the default (non-derived) label sets agree
        assert_platform_alerts_not_tenant_silenceable(am.get("inhibit_rules", []))

    def test_removing_the_exclusion_reintroduces_the_defect(self):
        # Mutation self-proof: strip alert_source="" from the committed silent-mode
        # rules and the guard must go red. Without this, a green suite would not
        # prove the matcher is what is doing the work.
        am = _committed_alertmanager_config()
        mutated = []
        for rule in am["inhibit_rules"]:
            rule = dict(rule)
            rule["target_matchers"] = [
                m for m in rule.get("target_matchers", [])
                if not m.startswith("alert_source=")]
            mutated.append(rule)
        offending = find_tenant_silenceable_platform_inhibits(
            mutated, _real_platform_label_sets())
        caught = {lbls["alertname"] for _i, _r, lbls in offending}
        assert caught, "mutation was not detected — the guard proves nothing"
        with pytest.raises(ValueError, match="platform self-monitoring alert"):
            assert_platform_alerts_not_tenant_silenceable(mutated)

    def test_tenant_alerts_are_still_inhibited(self):
        # Over-correction guard: the fix must NOT stop silent mode from doing its
        # job for ordinary tenant alerts (which carry no alert_source at all).
        from _grar_validate import _matcher_matches_labels, _inhibit_target_matchers
        am = _committed_alertmanager_config()
        silent = [r for r in am["inhibit_rules"]
                  if any("TenantSilent" in m for m in r.get("source_matchers", []))]
        assert len(silent) == 2, silent
        tenant_warning = {"alertname": "MySQLHighConnections", "severity": "warning",
                          "tenant": "any-tenant", "metric_group": "mysql_connections"}
        tenant_critical = {"alertname": "MySQLHighConnectionsCritical",
                           "severity": "critical", "tenant": "any-tenant",
                           "metric_group": "mysql_connections"}
        hits = []
        for rule in silent:
            targets = _inhibit_target_matchers(rule)
            for labels in (tenant_warning, tenant_critical):
                if all(_matcher_matches_labels(m, labels) for m in targets):
                    hits.append(labels["alertname"])
        assert sorted(hits) == ["MySQLHighConnections",
                                "MySQLHighConnectionsCritical"], hits

    def test_watchdog_stays_immune_and_out_of_scope(self):
        # Watchdog has no alert_source and severity=none: it never matched the
        # warning/critical targets, so the new matcher changes nothing for it.
        am = _committed_alertmanager_config()
        assert find_watchdog_suppressing_inhibits(am.get("inhibit_rules", [])) == []
        assert_watchdog_inhibit_immunity(am.get("inhibit_rules", []))

    def test_severity_dedup_and_custom_recipe_need_no_change(self):
        # Those two families are immune for STRUCTURAL reasons (dedup targets
        # require metric_group, which zero platform alerts carry; CustomRecipe-
        # Silent targets component="custom", which none carry). Asserted so the
        # "why only two rules were touched" reasoning is mechanical, not prose.
        sets = _real_platform_label_sets()
        assert all("metric_group" not in s for s in sets)
        assert all(s.get("component") != "custom" for s in sets)
        dedup_like = [
            {"source_matchers": ['severity="critical"', 'metric_group=~".+"',
                                 'tenant="any-tenant"'],
             "target_matchers": ['severity="warning"', 'metric_group=~".+"',
                                 'tenant="any-tenant"'], "equal": ["metric_group"]},
            {"source_matchers": ['alertname="CustomRecipeSilent"', 'tenant=~".+"',
                                 'name=~".+"'],
             "target_matchers": ['component="custom"', 'tenant=~".+"', 'name=~".+"'],
             "equal": ["tenant", "name"]},
        ]
        assert find_tenant_silenceable_platform_inhibits(dedup_like, sets) == []

    def test_invariant_is_narrow_not_a_blanket_platform_ban(self):
        # A deliberate platform→platform inhibit (source NOT tenant-gated) stays
        # legal — the invariant is about TENANT-triggered silencing only.
        platform_to_platform = [
            {"source_matchers": ['alertname="ThresholdExporterAbsent"'],
             "target_matchers": ['alert_source="platform"', 'severity="warning"']}]
        assert find_tenant_silenceable_platform_inhibits(platform_to_platform) == []
        # ...but the same target with a tenant-gated source IS flagged.
        tenant_gated = [
            {"source_matchers": ['alertname="TenantSilentWarning"', 'tenant=~".+"'],
             "target_matchers": ['alert_source="platform"', 'severity="warning"']}]
        assert len(find_tenant_silenceable_platform_inhibits(tenant_gated)) == 1

    def test_assemble_fails_closed_on_tenant_silenceable_base_inhibit(self):
        base = load_base_config(None)
        base["inhibit_rules"] = [
            {"source_matchers": ['alertname="TenantSilentWarning"', 'tenant=~".+"'],
             "target_matchers": ['severity="warning"', 'tenant=~".+"'],
             "equal": ["tenant"]}]  # the pre-fix shape
        with pytest.raises(ValueError, match="platform self-monitoring alert"):
            assemble_configmap(base, [], [], [])

    def test_validate_mode_tripwire_exits_on_tenant_silenceable_inhibit(self):
        bad = [{"source_matchers": ['alertname="TenantSilentWarning"', 'tenant=~".+"'],
                "target_matchers": ['severity="warning"', 'tenant=~".+"'],
                "equal": ["tenant"]}]
        with pytest.raises(SystemExit) as exc:
            gar._validate_mode([], [], bad, [])
        assert exc.value.code != 0


# ============================================================
# #1132: equal-label-gated invariant (silent-suppression guard)
# ============================================================
class TestEqualLabelGatedInvariant:
    """Every `equal:` label must be presence-gated on some side. An ungated
    equal-label is the #1132 footgun: Alertmanager treats a label missing from
    BOTH source and target as equal, silently suppressing unrelated alerts."""

    def test_gated_rules_pass(self):
        ok = [
            {"source_matchers": ['severity="critical"', 'metric_group=~".+"'],
             "target_matchers": ['severity="warning"', 'metric_group=~".+"'],
             "equal": ["metric_group"]},
            # gated on ONE side is sufficient (target lacks it → cannot match)
            {"source_matchers": ['alertname="X"', 'tenant=~".+"'],
             "target_matchers": ['severity="warning"'], "equal": ["tenant"]},
        ]
        assert find_ungated_equal_label_inhibits(ok) == []
        assert_equal_labels_gated(ok)  # does not raise

    def test_ungated_equal_label_flagged(self):
        bad = [{"source_matchers": ['alertname="TenantSilentWarning"'],
                "target_matchers": ['severity="warning"'], "equal": ["tenant"]}]
        found = find_ungated_equal_label_inhibits(bad)
        assert len(found) == 1 and found[0][2] == ["tenant"]
        with pytest.raises(ValueError, match="#1132"):
            assert_equal_labels_gated(bad)

    def test_invalid_regex_matcher_does_not_gate(self):
        # An uncompilable regex is treated conservatively as matching empty (so it
        # does NOT presence-gate) — flag rather than give a false all-clear. This
        # is the single-implementation coverage the BYO runtime check relies on.
        bad = [{"source_matchers": ['tenant=~"["'],
                "target_matchers": ['tenant=~"["'], "equal": ["tenant"]}]
        assert len(find_ungated_equal_label_inhibits(bad)) == 1

    def test_matchall_regex_does_not_gate(self):
        # `=~".*"` matches the empty string, so it does NOT guarantee presence.
        bad = [{"source_matchers": ['tenant=~".*"'],
                "target_matchers": ['tenant=~".*"'], "equal": ["tenant"]}]
        assert len(find_ungated_equal_label_inhibits(bad)) == 1

    def test_exact_and_negempty_matchers_gate(self):
        # tenant="x" and tenant!="" both exclude the empty string → gate.
        for m in ('tenant="x"', 'tenant!=""'):
            ok = [{"source_matchers": [m], "target_matchers": ['severity="warning"'],
                   "equal": ["tenant"]}]
            assert find_ungated_equal_label_inhibits(ok) == [], m

    def test_legacy_match_map_form_gates(self):
        # Deprecated source_match / target_match_re dict forms are analysed too.
        ok = [{"source_match": {"metric_group": "connections"},
               "target_match_re": {"metric_group": ".+"}, "equal": ["metric_group"]}]
        assert find_ungated_equal_label_inhibits(ok) == []

    def test_multiple_equal_labels_partial_gate(self):
        # Only the ungated subset is reported.
        bad = [{"source_matchers": ['alertname="X"', 'name=~".+"'],
                "target_matchers": ['component="custom"', 'name=~".+"'],
                "equal": ["tenant", "name"]}]
        found = find_ungated_equal_label_inhibits(bad)
        assert len(found) == 1 and found[0][2] == ["tenant"]  # name gated, tenant not

    def test_non_dict_and_non_list_equal_skipped(self):
        # Malformed shapes degrade, don't raise (a live AM can't emit these, but
        # the finder is defensive like the Watchdog one).
        assert find_ungated_equal_label_inhibits([42, {"equal": 7}, {"no": "equal"}]) == []

    def test_enforce_strict_raises_else_warns(self, capsys):
        bad = [{"source_matchers": ['alertname="X"'],
                "target_matchers": ['severity="warning"'], "equal": ["tenant"]}]
        # strict → raise
        with pytest.raises(ValueError, match="#1132"):
            _enforce_equal_labels_gated(bad, strict=True)
        # not strict → warn to stderr, no raise
        _enforce_equal_labels_gated(bad, strict=False)
        assert "#1132" in capsys.readouterr().err

    def test_committed_base_configmap_is_gated(self):
        # Regression guard on the REAL deployed base: no hand-edit may reintroduce
        # an ungated equal-label. Mirrors the Watchdog committed-base guard.
        repo_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".."))
        cm_path = os.path.join(
            repo_root, "k8s", "03-monitoring", "configmap-alertmanager.yaml")
        with open(cm_path, encoding="utf-8") as f:
            cm = yaml.safe_load(f)
        am = yaml.safe_load(cm["data"]["alertmanager.yml"])
        offending = find_ungated_equal_label_inhibits(am.get("inhibit_rules", []))
        assert offending == [], (
            "configmap-alertmanager.yaml has inhibit rule(s) with an ungated "
            f"equal-label (#1132): {[(i, lbls) for i, _r, lbls in offending]}")


# ============================================================
# Shared rule-tree scanner for the static label-contract gates below
# ============================================================
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# ⚠️ PREFIX, not a literal filename. Both gates below are bounded by "is this a
# platform rules ConfigMap?", and a literal `configmap-rules-platform.yaml` made
# a SECOND platform rules ConfigMap unrepresentable: adding `alert_source` to it
# tripped the RESERVED assertion (red), while omitting the label left it with
# ZERO presence coverage (green) — i.e. the gate actively pushed the maintainer
# toward the broken state. Any `configmap-rules-platform*.{yaml,yml}` now counts,
# and the presence floor is a sum across the whole set.
_PLATFORM_CM_PREFIX = "configmap-rules-platform"
# Both extensions: a rules ConfigMap named `.yml` was silently skipped by the
# scanner, which is the same "escapes the gate by being named differently" hole.
_RULES_FILE_EXTS = (".yaml", ".yml")


def _is_platform_cm_location(where: str) -> bool:
    """True iff `where` names a rule inside a platform rules ConfigMap.

    `where` is "<configmap-file>:<data-key>" on the ConfigMap side and a bare
    rule-pack filename on the source side; only the former can be platform.
    """
    if ":" not in where:
        return False
    return where.split(":", 1)[0].startswith(_PLATFORM_CM_PREFIX)


def _iter_repo_alert_rules():
    """Yield (where, rule) for EVERY alerting rule the repo ships.

    Both trees: the SOURCE rule packs under rule-packs/, plus every deployed
    k8s/03-monitoring/configmap-rules-*.{yaml,yml} (the generated rule-pack
    copies — double coverage vs the source scan, harmless — AND any hand-authored
    rules ConfigMap outside rule-packs/, which is configmap-rules-platform.yaml
    today and whatever is added later). `where` is the file name, prefixed
    "<configmap>:<data-key>" for the ConfigMap side.

    Single scanner on purpose: the sentinel contract and the alert_source
    contract below are both "this discriminator is RESERVED" invariants, and a
    reserved-value claim is only as good as its coverage — two scanners would
    let one drift and silently narrow the other's guarantee.

    RECURSIVE on both trees, for the same reason the `.yml` extension is
    accepted: a reserved-value gate that a file escapes by being *placed*
    differently is no better than one it escapes by being *named* differently.
    A flat `os.listdir` would silently drop a pack moved into a subdirectory out
    of BOTH contracts (CodeRabbit, PR #1270).
    """
    packs_dir = Path(_REPO_ROOT) / "rule-packs"
    for path in sorted(packs_dir.rglob("*")):
        if not (path.is_file() and path.name.endswith(_RULES_FILE_EXTS)):
            continue
        rel = path.relative_to(packs_dir).as_posix()
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for group in (doc or {}).get("groups", []):
            for rule in group.get("rules", []):
                if "alert" in rule:
                    yield rel, rule
    k8s_dir = Path(_REPO_ROOT) / "k8s" / "03-monitoring"
    for path in sorted(k8s_dir.rglob("*")):
        if not (path.is_file() and path.name.startswith("configmap-rules-")
                and path.name.endswith(_RULES_FILE_EXTS)):
            continue
        rel = path.relative_to(k8s_dir).as_posix()
        cm = yaml.safe_load(path.read_text(encoding="utf-8"))
        for fname, body in (cm.get("data") or {}).items():
            doc = yaml.safe_load(body)
            for group in (doc or {}).get("groups", []):
                for rule in group.get("rules", []):
                    if "alert" in rule:
                        yield f"{rel}:{fname}", rule


# ============================================================
# #1095: sentinel label contract (fail-open guard)
# ============================================================
class TestSentinelLabelContract:
    """Every severity=none alert rule is a sentinel and MUST carry the static
    component="sentinel" discriminator — without it the sentinel (which carries a
    tenant label) falls through to the tenant main routes / a matcher-less
    enforced NOC route and notifies humans with severity=none noise (#1095, the
    exact latent gap shipped between v1.2.0 and v2.9.x). Watchdog is the single
    deliberate exception: severity=none but NO component — it rides its own
    index-0 route, never the sentinel sink. Scans the SOURCE rule packs plus
    EVERY k8s/03-monitoring/configmap-rules-*.yaml (the generated copies AND any
    hand-authored rules configmap — Watchdog's platform CM today, plus whatever
    is added later outside rule-packs/), so a future sentinel added without the
    label fails loud here instead of silently regressing."""

    def _iter_alert_rules(self):
        # Delegates to the module-level scanner (shared with the alert_source
        # contract below) so the two reserved-value gates cannot drift apart.
        return _iter_repo_alert_rules()

    def test_severity_none_alerts_carry_sentinel_component(self):
        seen = []
        for where, rule in self._iter_alert_rules():
            labels = rule.get("labels") or {}
            if labels.get("severity") != "none":
                continue
            if rule["alert"] == "Watchdog":
                assert "component" not in labels, (
                    "Watchdog must NOT carry a component label — it rides its "
                    "own index-0 route, not the sentinel sink (#1095)")
                continue
            seen.append(rule["alert"])
            assert labels.get("component") == "sentinel", (
                f"{where}: severity=none alert {rule['alert']!r} is missing "
                f'component="sentinel" — it would fall through to tenant/NOC '
                f"notification channels (#1095)")
        # non-vacuous: the four known sentinels must actually have been scanned
        assert set(seen) >= {
            "TenantSilentWarning", "TenantSilentCritical",
            "TenantSeverityDedupEnabled", "CustomRecipeSilent"}, seen

    def test_component_sentinel_reserved_for_severity_none(self):
        # The discriminator is RESERVED: a deliverable (severity != none) alert
        # must never ride component="sentinel" or the sinkhole would eat it.
        for where, rule in self._iter_alert_rules():
            labels = rule.get("labels") or {}
            if labels.get("component") == "sentinel":
                assert labels.get("severity") == "none", (
                    f"{where}: alert {rule['alert']!r} carries "
                    f'component="sentinel" but severity='
                    f"{labels.get('severity')!r} — the sentinel sink would "
                    f"swallow a deliverable alert (#1095)")


# ============================================================
# alert_source="platform" delivery discriminator contract
# ============================================================
class TestPlatformAlertSourceContract:
    """Every platform self-monitoring alert MUST carry `alert_source: platform`.

    WHY: platform alerts have almost no tenant to route them by (37 of the 40
    carry no `tenant` label at all — the 3 that do are TenantMetricsOverLimit via
    rule-level labels, and FederationRejectionRateAnomaly /
    FederationGatewayBackendErrors via their expr's `sum by (tenant)`, i.e. only
    at fire time) and none carry `metric_group`. Without a POSITIVE
    discriminator the only matcher an operator can put on the single
    `_routing_enforced` route is `severity="critical"` — which reaches 18 of the
    40, i.e. the majority of the platform's own self-monitoring would stay
    undeliverable no matter how the operator wires it. This is the same failure
    shape as #1095 (a discriminator that silently does not exist), inverted:
    there the label was missing from a sinkhole, here from a delivery selector.

    Two directions, both asserted, and BOTH bounded by the same
    `_is_platform_cm_location` prefix test (not a literal filename) so a second
    platform rules ConfigMap is covered by presence instead of being punished by
    reserved:
      1. presence  — every alert in ANY configmap-rules-platform*.{yaml,yml}
         except `Watchdog` carries `alert_source: platform`;
      2. reserved  — nothing OUTSIDE those ConfigMaps carries `alert_source` at
         all, so `match: ['alert_source="platform"']` cannot silently start
         picking up tenant alerts (which route by `tenant` and would then
         dual-deliver into the NOC channel).

    `Watchdog` is the deliberate exception, for the same reason it carries no
    `component`: it rides the index-0 heartbeat route with `continue: false` and
    must never be selectable by a second delivery path.
    """

    def _platform_rules(self):
        return [(where, rule) for where, rule in _iter_repo_alert_rules()
                if _is_platform_cm_location(where)]

    def test_platform_cm_discovery_is_prefix_based(self):
        # Regression guard for the scope seam: discovery must be a prefix test on
        # the ConfigMap basename, not equality with one filename, and must accept
        # both extensions. Asserted on the classifier directly so it holds even
        # while only one platform ConfigMap exists.
        assert _is_platform_cm_location("configmap-rules-platform.yaml:key")
        assert _is_platform_cm_location("configmap-rules-platform-federation.yaml:k")
        assert _is_platform_cm_location("configmap-rules-platform.yml:key")
        # rule-pack side (no ":" prefix) is never platform, whatever it is named
        assert not _is_platform_cm_location("configmap-rules-platform.yaml")
        assert not _is_platform_cm_location("rule-pack-mysql.yaml")
        assert not _is_platform_cm_location("configmap-rules-mysql.yaml:key")
        assert _RULES_FILE_EXTS == (".yaml", ".yml")

    def test_platform_alerts_carry_alert_source(self):
        seen = []
        watchdog_checked = False
        for where, rule in self._platform_rules():
            labels = rule.get("labels") or {}
            if rule["alert"] == "Watchdog":
                watchdog_checked = True
                assert "alert_source" not in labels, (
                    "Watchdog must NOT carry alert_source — it rides its own "
                    "index-0 heartbeat route with continue:false; a delivery "
                    "discriminator could pull the heartbeat into a second "
                    "channel (same rule as its missing component label)")
                continue
            seen.append(rule["alert"])
            assert labels.get("alert_source") == "platform", (
                f"{where}: platform alert {rule['alert']!r} is missing "
                f'alert_source="platform" — an operator wiring '
                f"_routing_enforced cannot select it, so it stays in the "
                f"notifier-less default receiver forever")
        # non-vacuous: the scan must actually have reached the platform pack.
        assert watchdog_checked, (
            f"Watchdog was never scanned — no {_PLATFORM_CM_PREFIX}*.yaml parsed "
            f"or was reached; every assertion above is vacuous")
        assert len(seen) >= _MIN_PLATFORM_ALERTS, (
            f"only {len(seen)} platform alerts scanned, expected >= "
            f"{_MIN_PLATFORM_ALERTS}: {sorted(seen)}")
        assert set(seen) >= {
            "ThresholdExporterAbsent", "PrometheusRuleEvaluationFailing",
            "TenantApiSingleWriterBreach", "FederationAuditPipelineSilent",
            "VectorProjectionGateStuck"}, sorted(seen)

    def test_alert_source_reserved_for_the_platform_tree(self):
        # RESERVED value: a tenant-facing alert carrying alert_source would be
        # dual-delivered into the platform/NOC channel by the operator's
        # enforced route (continue:true), which is exactly the leak the custom
        # isolation subtree exists to prevent.
        offenders = []
        for where, rule in _iter_repo_alert_rules():
            labels = rule.get("labels") or {}
            if "alert_source" not in labels:
                continue
            if not _is_platform_cm_location(where):
                offenders.append((where, rule["alert"], labels["alert_source"]))
                continue
            assert labels["alert_source"] == "platform", (
                f"{where}: alert {rule['alert']!r} carries "
                f"alert_source={labels['alert_source']!r} — the only value the "
                f"operator guide documents is \"platform\"")
        assert offenders == [], (
            "alert_source is RESERVED for the platform self-monitoring pack "
            f"({_PLATFORM_CM_PREFIX}*.yaml); these rules would be swept into the "
            f"platform NOC channel: {offenders}")


# ============================================================
# runbook_url coverage contract (#1207 續 — the last 2 of the 42)
# ============================================================
# Shape of every runbook_url in the platform tree: an absolute GitHub blob URL
# on main, optionally with an ASCII #anchor. Asserted (not merely described) by
# test_runbook_urls_resolve_inside_this_repo below.
_RUNBOOK_URL_PREFIX = (
    "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/")

# The platform alerts that still ship WITHOUT a runbook_url.
#
# SHRINK-ONLY + EXIT-LOCKED, same discipline as check_scrape_reachability's
# KNOWN_UNKNOWN_SOURCE / check_orphan_recordings' KNOWN_ORPHANS: a row may only
# be deleted, never added to paper over a new alert, and a row whose alert HAS
# since gained a runbook_url is reported as a violation (see
# test_runbook_ledger_is_exit_locked). A ledger rather than a straight
# "all 42 must have one" assertion because these two are a KNOWN, deliberately
# deferred gap — #1207's sweep took the tree from 0/34 to 30/34 and its
# follow-up to 40/42, recording that these two alone have no existing
# disposition page to point at and need IR content written first
# (CHANGELOG.md `## [Unreleased]`; the writing is tracked in #1360, which is
# also what deletes these two rows). Pointing them at a page that does not
# answer the page-out is the failure mode the upstream ecosystem keeps hitting
# (prometheus-community/helm-charts#3893: a shipped runbook_url that 404s), and
# it is strictly worse than an honest absence: the operator loses the ability
# to tell "no runbook yet" from "runbook exists, go read it".
_PLATFORM_ALERTS_WITHOUT_RUNBOOK: dict[str, str] = {
    "CronJobLastRunFailed":
        "no existing page documents what to do when a platform CronJob's last "
        "run failed. The sibling ThresholdGovernanceStale points at "
        "docs/cli-reference.md#threshold-govern, but this rule is deliberately "
        "GENERIC over the monitoring namespace (that genericity is the point of "
        "#1242), so a per-command CLI page is the wrong target the moment "
        "maintenance-scheduler is the failing CronJob. Triage lives only in the "
        "alert's own description today.",
    "MassExporterOutage":
        "no existing page documents a FLEET-WIDE tenant-exporter outage. "
        "docs/integration/troubleshooting-checklist.md 1.1.1 is the nearest "
        "entry but it triages a SINGLE threshold-exporter target (wrong "
        "workload — this alert reads the tenant-exporters job) and its advice "
        "is per-target remediation, which is exactly what this alert's "
        "description tells the operator NOT to do.",
}


class TestPlatformRunbookCoverageContract:
    """Every platform self-monitoring alert must carry a `runbook_url` that
    resolves to a page in THIS repo.

    WHY: platform alerts page the platform's own on-call, who has no tenant to
    hand the incident to. 40 of the 42 already carry one; nothing stopped the
    41st from shipping without it — the annotation is not part of any schema,
    pint cannot see this tree at all (`.pint.hcl`'s parser include list is
    rule-packs/ + tests/rulepacks/*.rules.yaml; the ConfigMap wrapper is
    unparseable, and the pint-reachable EXTRACTS mirror only ~2/3 of the tree,
    so a pint `alerts/annotation` rule would go green while a third of the
    alerts stayed uncovered), and configmap-rules-platform.yaml says so in its
    own words next to the one anchored link ("rule-YAML URLs are not linted").

    Two directions:
      1. coverage — every platform alert except the shrink-only ledger above
         carries a non-empty runbook_url;
      2. resolvability — every runbook_url that IS present names a file that
         exists on disk, and an #anchor (if any) names a heading that exists in
         that file. A dead runbook link is worse than a missing one: it costs
         the on-call a click plus the doubt about whether they have the wrong
         URL or the wrong problem.

    Reuses `_iter_repo_alert_rules` / `_is_platform_cm_location` rather than
    re-globbing, for the same reason the three contracts above share them: a
    second scanner drifts and silently narrows whichever gate lost the race.
    """

    def _platform_alerts(self):
        return [(where, rule) for where, rule in _iter_repo_alert_rules()
                if _is_platform_cm_location(where)]

    @staticmethod
    def _github_ascii_slug(heading: str) -> str:
        """GitHub's heading -> anchor slug, ASCII subset only.

        Deliberately NOT a general slugger: the repo's convention is that a
        runbook_url anchor must be ASCII (a CJK/full-width heading has no
        stable anchor — that is why the three internal-runbook links carry no
        anchor at all), and test_runbook_anchors_are_ascii pins it. Headings
        that are not pure ASCII simply do not match any anchor here, which is
        the correct outcome.
        """
        s = heading.strip().lower()
        s = re.sub(r"[^\w\s-]", "", s)
        return re.sub(r"\s+", "-", s)

    def test_every_platform_alert_carries_a_runbook_url(self):
        seen = []
        missing = []
        for where, rule in self._platform_alerts():
            name = rule["alert"]
            seen.append(name)
            url = (rule.get("annotations") or {}).get("runbook_url")
            if url:
                continue
            if name in _PLATFORM_ALERTS_WITHOUT_RUNBOOK:
                continue
            missing.append((where, name))
        # Non-vacuity first: an empty scan would make the assertion below pass
        # for the wrong reason (this is the hole #1283 fixed elsewhere).
        assert len(seen) >= _MIN_PLATFORM_ALERTS, (
            f"only {len(seen)} platform alerts scanned, expected >= "
            f"{_MIN_PLATFORM_ALERTS} — no {_PLATFORM_CM_PREFIX}*.yaml was "
            f"parsed, so every assertion here is vacuous: {sorted(seen)}")
        assert missing == [], (
            "these platform alerts ship with no `runbook_url` annotation, so "
            "the platform on-call they page has nowhere to go. FIX: add "
            f'`runbook_url: "{_RUNBOOK_URL_PREFIX}<path-in-this-repo>"` to the '
            "alert's annotations, pointing at a page that already exists and "
            "actually answers the page-out (an ADR, a docs/internal/*runbook, "
            "or a troubleshooting entry — see the 40 that do). ⛔ Do NOT invent "
            "a URL to get past this gate and do NOT add a row to "
            "_PLATFORM_ALERTS_WITHOUT_RUNBOOK: that ledger is shrink-only and "
            "exists solely for the two alerts #1207 left pending. If no such "
            "page exists yet, write the disposition content first. "
            f"Offenders: {missing}")

    def test_runbook_ledger_is_exit_locked(self):
        # The ledger must shrink, never linger: a row whose alert has gained a
        # runbook_url, or that names an alert no longer in the tree, is stale.
        by_name = {rule["alert"]: (rule.get("annotations") or {}).get("runbook_url")
                   for _, rule in self._platform_alerts()}
        gone = sorted(set(_PLATFORM_ALERTS_WITHOUT_RUNBOOK) - set(by_name))
        assert gone == [], (
            "_PLATFORM_ALERTS_WITHOUT_RUNBOOK names alert(s) that no longer "
            f"exist in the platform tree — delete the row(s): {gone}")
        fixed = sorted(name for name in _PLATFORM_ALERTS_WITHOUT_RUNBOOK
                       if by_name.get(name))
        assert fixed == [], (
            "these alerts now HAVE a runbook_url but are still listed in "
            "_PLATFORM_ALERTS_WITHOUT_RUNBOOK — delete the row(s) so the "
            f"coverage gate starts holding them: {fixed}")

    def test_runbook_urls_resolve_inside_this_repo(self):
        broken = []
        checked = 0
        for where, rule in self._platform_alerts():
            url = (rule.get("annotations") or {}).get("runbook_url")
            if not url:
                continue
            checked += 1
            if not url.startswith(_RUNBOOK_URL_PREFIX):
                broken.append((where, rule["alert"], url, "not a blob/main URL "
                               "for this repo"))
                continue
            rel, _, anchor = url[len(_RUNBOOK_URL_PREFIX):].partition("#")
            target = Path(_REPO_ROOT) / rel
            if not target.is_file():
                broken.append((where, rule["alert"], url,
                               f"{rel} does not exist in this repo"))
                continue
            if not anchor:
                continue
            slugs = {self._github_ascii_slug(line.lstrip("#"))
                     for line in target.read_text(encoding="utf-8").splitlines()
                     if line.startswith("#")}
            if anchor not in slugs:
                broken.append((where, rule["alert"], url,
                               f"no heading in {rel} slugs to #{anchor}"))
        assert checked >= _MIN_PLATFORM_ALERTS - len(
            _PLATFORM_ALERTS_WITHOUT_RUNBOOK), (
            f"only {checked} runbook_url(s) checked — the scan did not reach "
            "the platform tree, so this gate is vacuous")
        assert broken == [], (
            "these platform runbook_url(s) point at something that is not "
            "there. A 404 runbook is worse than no runbook: it burns the "
            "on-call's first click and leaves them unsure whether the link or "
            "their diagnosis is wrong. FIX: repoint the URL, or restore the "
            f"heading/file it named (renaming a linked heading rots it): {broken}")

    def test_runbook_anchors_are_ascii(self):
        # The anchor convention, pinned so it cannot erode: CJK / full-width
        # headings have no stable GitHub anchor, which is why the internal
        # runbook links deliberately carry none. An anchor that is not ASCII
        # would also silently fall out of the slug check above.
        for where, rule in self._platform_alerts():
            url = (rule.get("annotations") or {}).get("runbook_url") or ""
            _, _, anchor = url.partition("#")
            if not anchor:
                continue
            assert anchor.isascii(), (
                f"{where}: {rule['alert']} has a non-ASCII runbook_url anchor "
                f"#{anchor} — link to the file with no anchor instead (the "
                "three docs/internal/* links do exactly this)")


# ============================================================
# #1203 part 2: platform alert FEDERATION-PLANE contract
# ============================================================
# The producer tables live in the scrape gate (that is where metric -> source
# knowledge already is); this contract only reads them. Importing rather than
# restating is the same "single scanner" discipline as _iter_repo_alert_rules
# above — a second copy of the family table would drift and silently narrow
# whichever gate lost the race.
from check_scrape_reachability import (  # noqa: E402
    KNOWN_UNKNOWN_SOURCE,
    PLANE_OF_UNPINNED_SOURCE,
    VECTOR_NAMESPACE,
    extract_metrics,
)

EDGE_LOCAL = "edge-local"
PLATFORM_LOCAL = "platform-local-on-central"
UNRESOLVED = "unresolved"

# Namespaces that exist in the CENTRAL cluster under the federation topology.
_CENTRAL_NAMESPACES = frozenset({"monitoring", "tenant-api"})
# Families every cluster runs its own copy of, so the metric name alone says
# nothing about the plane — the SELECTOR names the workload, and the workload's
# home cluster is what decides. Resolving these by prefix would misclassify the
# six legitimate central kube_* readers below.
_CLUSTER_LOCAL_PREFIXES = ("kube_", "kubelet_", "container_")
# `(?<!\w)` is load-bearing: without a left boundary this also matches inside a
# LONGER label name — `exported_namespace="monitoring"` (a routine Prometheus
# relabel artifact when a job's own `namespace` label collides with the
# target's) would be read as a central-namespace pin and flip the verdict from
# UNRESOLVED to PLATFORM_LOCAL, i.e. silently default to central, which is the
# one thing this module promises not to do (CodeRabbit, PR #1290). Zero
# occurrences in the tree today — this keeps it that way.
_NAMESPACE_MATCHER_RE = re.compile(r'(?<!\w)namespace\s*=\s*"([^"]+)"')

# Alerts whose plane cannot be derived from the metric family because the
# family is cluster-local AND the selector pins no central namespace. Each one
# needs a human to say which workload the selector names and where it runs.
# Kept as an explicit table, NOT as a fallback: an underivable alert that
# silently defaulted to "central" is precisely the shape #1203 spent a whole
# investigation undoing.
_EDGE_LOCAL_BY_SELECTOR: dict[str, str] = {
    "VectorProjectionGateStuck":
        'reads kube_pod_init_container_status_waiting_reason with NO namespace '
        'selector; container="projection-gate" exists only in the Vector '
        f'DaemonSet ({VECTOR_NAMESPACE} ns), so the KSM that sees it is the '
        "EDGE cluster's, not central's (helm/vector/templates/daemonset.yaml)",
}

# Non-vacuity anchor. LITERAL on purpose: deriving the expectation from the
# derivation under test would make it self-satisfying — "an assertion derived
# from the thing it guards does not guard it" (the hole #1283 fixed in the
# nightly matrix guards, where `*_EXTRA_SCANNED` emptied both sides at once).
_EXPECTED_EDGE_LOCAL_ALERTS = frozenset({
    "VectorBufferEventsDropped",
    "TenantProjectionFanoutDiscardSpike",
    "VectorProjectionGateMismatch",
    "VectorRegistryUnreadableAtBoot",
    "VectorProjectionGateStuck",
})


def _alert_plane(alert: str, expr: str) -> tuple[str, list[str]]:
    """Which cluster's Prometheus can see every input of *expr*.

    EDGE_LOCAL wins over PLATFORM_LOCAL when an alert mixes planes: an alert is
    only evaluable where ALL its inputs exist, so one edge-only input is enough
    to make central evaluation structurally inert.
    """
    verdicts: set[str] = set()
    why: list[str] = []
    for metric in sorted(extract_metrics(expr)):
        if metric.startswith("vector_"):
            verdicts.add(EDGE_LOCAL)
            why.append(f"{metric}: vector_ family -> {VECTOR_NAMESPACE} ns (edge)")
        elif metric in PLANE_OF_UNPINNED_SOURCE:
            verdicts.add(PLANE_OF_UNPINNED_SOURCE[metric])
            why.append(f"{metric}: unpinned-ns ledger -> "
                       f"{PLANE_OF_UNPINNED_SOURCE[metric]}")
        elif metric.startswith(_CLUSTER_LOCAL_PREFIXES):
            selectors = re.findall(re.escape(metric) + r"\s*\{([^}]*)\}", expr)
            namespaces = set()
            for body in selectors:
                found = _NAMESPACE_MATCHER_RE.search(body)
                namespaces.add(found.group(1) if found else None)
            if namespaces and all(ns in _CENTRAL_NAMESPACES for ns in namespaces):
                verdicts.add(PLATFORM_LOCAL)
            elif alert in _EDGE_LOCAL_BY_SELECTOR:
                verdicts.add(EDGE_LOCAL)
                why.append(f"{metric}: declared edge-local — "
                           f"{_EDGE_LOCAL_BY_SELECTOR[alert]}")
            else:
                verdicts.add(UNRESOLVED)
                why.append(f"{metric}: cluster-local family, selector pins no "
                           f"central namespace (saw {sorted(map(str, namespaces))})")
        else:
            verdicts.add(PLATFORM_LOCAL)
    if EDGE_LOCAL in verdicts:
        return EDGE_LOCAL, why
    if UNRESOLVED in verdicts:
        return UNRESOLVED, why
    return PLATFORM_LOCAL, why


class TestPlatformAlertPlaneContract:
    """Every platform alert's evaluation plane must be DERIVABLE, not assumed.

    WHY: #1203 found five platform alerts that can only be evaluated on the
    edge cluster under the federation topology (their inputs are produced by
    the Vector DaemonSet, or by a container that only exists inside it). They
    were found by a human reading 41 expressions — nothing mechanical would
    have caught a sixth. Two gates each disclaim the question: the scrape gate
    models the SINGLE-CLUSTER face and says so in its SCOPE BOUNDARY note,
    while generate_rule_pack_split's central-input validator never sees this
    tree (its input glob is rule-packs/rule-pack-*.yaml). The handoff between
    them is one-directional, so the gap belonged to neither.

    This contract closes it WITHOUT a hand-maintained list of edge alerts: the
    plane is derived from the producer tables the scrape gate already owns, and
    the literal expectation below exists only to prove the derivation is not
    vacuous.
    """

    def _platform_alerts(self):
        return [(where, rule) for where, rule in _iter_repo_alert_rules()
                if _is_platform_cm_location(where)]

    def test_plane_table_covers_exactly_the_unpinned_ledger(self):
        # The two tables answer different questions about the same rows
        # (is it scrapeable here / which cluster is it on), so they must stay
        # key-identical: a ledger row with no plane would fall through to the
        # `else` branch of _alert_plane and be silently called central.
        assert set(PLANE_OF_UNPINNED_SOURCE) == set(KNOWN_UNKNOWN_SOURCE), (
            "PLANE_OF_UNPINNED_SOURCE must decide a plane for every "
            "KNOWN_UNKNOWN_SOURCE row. Missing: "
            f"{sorted(set(KNOWN_UNKNOWN_SOURCE) - set(PLANE_OF_UNPINNED_SOURCE))}; "
            "stale: "
            f"{sorted(set(PLANE_OF_UNPINNED_SOURCE) - set(KNOWN_UNKNOWN_SOURCE))}")
        assert set(PLANE_OF_UNPINNED_SOURCE.values()) <= {EDGE_LOCAL, PLATFORM_LOCAL}

    def test_every_platform_alert_has_a_derivable_plane(self):
        unresolved = []
        for where, rule in self._platform_alerts():
            plane, why = _alert_plane(rule["alert"], str(rule.get("expr", "")))
            if plane == UNRESOLVED:
                unresolved.append((where, rule["alert"], why))
        assert unresolved == [], (
            "these platform alerts read a cluster-local metric family whose "
            "selector pins no central namespace, so which cluster can evaluate "
            "them is undecidable. Pin the namespace in the selector, or add the "
            "alert to _EDGE_LOCAL_BY_SELECTOR naming the workload and its home "
            f"cluster: {unresolved}")

    def test_edge_local_set_matches_the_reviewed_expectation(self):
        derived = {rule["alert"]
                   for _, rule in self._platform_alerts()
                   if _alert_plane(rule["alert"],
                                   str(rule.get("expr", ""))) [0] == EDGE_LOCAL}
        # Non-vacuity first: an empty derivation would make the equality below
        # trivially checkable only in one direction.
        assert derived, (
            "no platform alert derived as edge-local — the scan reached no "
            "platform ConfigMap, or extract_metrics stopped returning vector_ "
            "metrics; every plane assertion here is vacuous")
        assert derived == _EXPECTED_EDGE_LOCAL_ALERTS, (
            "the set of platform alerts that can ONLY be evaluated on an edge "
            "cluster changed. A NEW one means the platform tree grew an alert "
            "the central Prometheus can never fire (#1203); a REMOVED one means "
            "an alert's inputs moved plane. Either way it is a topology "
            "decision, not a test to update reflexively. "
            f"new={sorted(derived - _EXPECTED_EDGE_LOCAL_ALERTS)} "
            f"gone={sorted(_EXPECTED_EDGE_LOCAL_ALERTS - derived)}")

    def test_selector_declared_alerts_are_still_underivable(self):
        # Exit-lock on _EDGE_LOCAL_BY_SELECTOR: an entry whose selector later
        # gains a central namespace (or whose metric leaves the cluster-local
        # families) would keep forcing an edge verdict that the code no longer
        # supports. Same shrink-or-stay-justified discipline as the scrape
        # gate's KNOWN_UNKNOWN_SOURCE.
        by_name = {rule["alert"]: str(rule.get("expr", ""))
                   for _, rule in self._platform_alerts()}
        for alert in _EDGE_LOCAL_BY_SELECTOR:
            assert alert in by_name, (
                f"{alert} is declared edge-local but no longer exists in the "
                "platform tree — drop the entry")
            metrics = extract_metrics(by_name[alert])
            assert any(m.startswith(_CLUSTER_LOCAL_PREFIXES) for m in metrics), (
                f"{alert} no longer reads a cluster-local metric family, so its "
                "plane is derivable now — remove it from _EDGE_LOCAL_BY_SELECTOR "
                "instead of pinning a verdict by hand")

    @pytest.mark.parametrize("expr,expected", [
        # cluster-local family pinned to a central namespace -> central
        ('max(kube_pod_container_status_restarts_total'
         '{namespace="monitoring",container="x"}) > 3', PLATFORM_LOCAL),
        # ... the SAME metric with no namespace selector is NOT derivable
        ('max(kube_pod_container_status_restarts_total{container="x"}) > 3',
         UNRESOLVED),
        # a central namespace that is not ours is still not central
        ('max(kube_pod_status_phase{namespace="vector"}) > 0', UNRESOLVED),
        # vector_ family is edge whatever the shape of the expression
        ('sum(rate(vector_sink_errors_total[5m])) > 0', EDGE_LOCAL),
        # metric directly before `)` and at end-of-string: the split tool's
        # narrow extractor misses both, which would have hidden an edge input
        ('sum(vector_buffer_discarded_events_total) and up{job="x"}', EDGE_LOCAL),
        ('up{job="x"} and vector_sink_errors_total', EDGE_LOCAL),
        ('absent(vector_tenant_projection_gate_info)', EDGE_LOCAL),
        # ledger row resolves through PLANE_OF_UNPINNED_SOURCE
        ('absent_over_time(tenant_log_query_requests_total[10m])', PLATFORM_LOCAL),
        ('increase(alertmanager_notifications_failed_total[15m]) > 0',
         PLATFORM_LOCAL),
        # one edge input is enough, even mixed with central ones
        ('sum(vector_buffer_discarded_events_total) + '
         'sum(alertmanager_notifications_failed_total)', EDGE_LOCAL),
    ])
    def test_plane_classifier(self, expr, expected):
        assert _alert_plane("SyntheticAlert", expr)[0] == expected


# ============================================================
# apply_to_configmap (mocked kubectl/curl)
# ============================================================
class TestApplyToConfigmap:
    """apply_to_configmap() with mocked subprocess calls."""

    def _mock_subprocess(self, monkeypatch, kubectl_get_stdout, kubectl_get_rc=0,
                         kubectl_create_rc=0, kubectl_apply_rc=0, curl_rc=0):
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.stderr = ""
            result.stdout = ""

            if "get" in cmd and "configmap" in cmd:
                result.returncode = kubectl_get_rc
                result.stdout = kubectl_get_stdout
            elif "create" in cmd and "configmap" in cmd:
                result.returncode = kubectl_create_rc
                result.stdout = "apiVersion: v1\nkind: ConfigMap\ndata: {}"
            elif "apply" in cmd:
                result.returncode = kubectl_apply_rc
                result.stdout = ""
            elif "curl" in cmd:
                result.returncode = curl_rc
            else:
                result.returncode = 0

            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        return calls

    def test_successful_apply(self, monkeypatch):
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default", "routes": []},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm))
        routes = [{"receiver": "t1", "matchers": ['tenant="t1"']}]
        receivers = [{"name": "t1", "webhook_configs": [{"url": "https://x.com"}]}]
        result = apply_to_configmap(routes, receivers, [], "monitoring", "am-config")
        assert result is True

    def test_kubectl_get_fails(self, monkeypatch):
        self._mock_subprocess(monkeypatch, "", kubectl_get_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_empty_configmap_data(self, monkeypatch):
        existing_cm = {"data": {}}
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm))
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_kubectl_apply_fails(self, monkeypatch):
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default"},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm),
                              kubectl_apply_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_reload_fails_still_returns_true(self, monkeypatch):
        """If curl reload fails, apply still returns True (ConfigMap was updated)."""
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default"},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm), curl_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is True


# ============================================================
# main() CLI paths
# ============================================================
class TestMainCLI:
    """main() CLI entry point tests."""

    def _make_config_dir(self, tmp_path):
        """Create a minimal config dir for testing main()."""
        d = tmp_path / "conf.d"
        d.mkdir()
        defaults = {"defaults": {"mysql_connections": 80}}
        (d / "_defaults.yaml").write_text(yaml.dump(defaults), encoding="utf-8")
        tenant = {"tenants": {"db-a": {
            "mysql_connections": "70",
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
            },
            "_severity_dedup": "enable",
        }}}
        (d / "db-a.yaml").write_text(yaml.dump(tenant), encoding="utf-8")
        return str(d)

    def test_dry_run(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--dry-run")
        gar.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "route" in out.lower() or "receiver" in out.lower()

    def test_validate_mode(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--validate")
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Validation" in out or "OK" in out

    def test_stdout_output(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir)
        gar.main()
        out = capsys.readouterr().out
        assert "route" in out.lower() or "receiver" in out.lower()

    def test_output_file(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        out_file = str(tmp_path / "output.yaml")
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "-o", out_file)
        gar.main()
        assert os.path.isfile(out_file)
        content = open(out_file, encoding="utf-8").read()
        assert "route" in content.lower() or "receiver" in content.lower()

    def test_output_configmap(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--output-configmap")
        gar.main()
        out = capsys.readouterr().out
        parsed = yaml.safe_load(out)
        assert parsed["kind"] == "ConfigMap"

    def test_output_configmap_dry_run(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--output-configmap", "--dry-run")
        gar.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "ConfigMap" in out

    def test_output_configmap_to_file(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        out_file = str(tmp_path / "cm.yaml")
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--output-configmap", "-o", out_file)
        gar.main()
        assert os.path.isfile(out_file)

    def test_policy_flag(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        policy = tmp_path / "policy.yaml"
        policy.write_text(yaml.dump({"allowed_domains": ["hooks.example.com"]}),
                          encoding="utf-8")
        cli_argv("generate_alertmanager_routes", "--config-dir", config_dir, "--policy", str(policy))
        gar.main()
        out = capsys.readouterr().out
        assert "Policy" in out or "route" in out.lower()

    def test_empty_config_dir(self, tmp_path, monkeypatch, capsys, cli_argv):
        d = tmp_path / "empty"
        d.mkdir()
        cli_argv("generate_alertmanager_routes", "--config-dir", str(d))
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 0

    def test_validate_with_errors(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Validate mode with bad config should exit 1."""
        d = tmp_path / "conf.d"
        d.mkdir()
        defaults = {"defaults": {"mysql_connections": 80}}
        (d / "_defaults.yaml").write_text(yaml.dump(defaults), encoding="utf-8")
        # Tenant with missing receiver url
        tenant = {"tenants": {"db-a": {
            "_routing": {"receiver": {"type": "webhook"}},  # missing url
            "_severity_dedup": "enable",
        }}}
        (d / "db-a.yaml").write_text(yaml.dump(tenant), encoding="utf-8")
        cli_argv("generate_alertmanager_routes", "--config-dir", str(d), "--validate")
        with pytest.raises(SystemExit) as exc:
            gar.main()
        # May be 0 or 1 depending on whether it generates valid routes


# ============================================================
# _parse_config_files edge cases
# ============================================================
class TestParseConfigFilesEdge:
    """Edge cases for _parse_config_files."""

    def test_empty_directory(self, tmp_path):
        result = _parse_config_files(str(tmp_path))
        assert result["all_tenants"] == []
        assert result["explicit_routing"] == {}

    def test_dotfile_ignored(self, tmp_path):
        """Files starting with . are ignored."""
        (tmp_path / ".hidden.yaml").write_text(
            yaml.dump({"tenants": {"x": {"foo": 1}}}), encoding="utf-8")
        result = _parse_config_files(str(tmp_path))
        assert "x" not in result["all_tenants"]

    def test_unparseable_yaml_skipped(self, tmp_path):
        """Bad YAML files are skipped with warning."""
        (tmp_path / "bad.yaml").write_text("key: [unclosed", encoding="utf-8")
        result = _parse_config_files(str(tmp_path))
        assert result["all_tenants"] == []
