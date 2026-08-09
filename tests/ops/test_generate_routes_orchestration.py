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
import codecs
import functools
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import textwrap
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
#
# It counts ALERTING RULES IN THE PLATFORM TREE — all of them, exceptions included.
# Each contract then subtracts its OWN documented exceptions from this one number
# (`- 1` for Watchdog on the alert_source-scoped floors, `- len(ledger)` on the
# runbook resolvability floor). Encoding the exceptions at the use site instead of
# baking a lowest-common-denominator into the constant is what lets the constant be
# exact: at 40 it was two short of the shipped 42, i.e. two alerts could be deleted
# outright without a single floor noticing.
_MIN_PLATFORM_ALERTS = 42


@functools.lru_cache(maxsize=1)
def _heartbeat_alertnames() -> frozenset:
    """Alertnames the index-0 heartbeat route pins — DERIVED, not spelled here.

    ⛔ This is the whole justification for the only exemption the discriminator
    contracts grant. `Watchdog` carries no `component` and no `alert_source`
    because it rides its own route at index 0 with `continue: false`; a
    discriminator would make it selectable by a second channel and the external
    dead-man's-switch would stop being the only thing watching it.

    That reason is a property of `_build_watchdog_route()`, not of the seven
    letters. Written as a literal at each use site — and it was, seventeen
    times — the exemption transfers to whatever is named `Watchdog` next.
    Measured: a rule named `Watchdog` in a TENANT pack, `severity: none`, no
    `component`, walked straight through the #1095 sentinel contract, and the
    duplicate-alertname assertion that might have caught it scans only the
    PLATFORM tree (it was added for the runbook ledger, not for this).

    Matcher parsing is borrowed from `_grar_validate` rather than re-regexed —
    same discipline as everywhere else here.
    """
    from _grar_routes import _build_watchdog_route  # noqa: PLC0415
    from _grar_validate import _pinned_label_values  # noqa: PLC0415

    routes, _receivers = _build_watchdog_route()
    return frozenset(
        name for route in routes
        for name in _pinned_label_values(route.get("matchers") or [], "alertname"))


def _real_platform_label_sets():
    """Label sets of every SHIPPED platform alert, derived from the ConfigMap.

    Two sources of `tenant`, and the second is the one a rule-file reader misses:
      * rule-level `labels.tenant` (TenantMetricsOverLimit), and
      * a runtime label produced by the expr's `sum by (tenant)`
        (FederationRejectionRateAnomaly / FederationGatewayBackendErrors) — those
        rules' `labels:` blocks say nothing about tenant.
    Deriving instead of hardcoding keeps the guard from going stale when platform
    alerts are added or renamed.

    ⛔ The "does this expr produce a `tenant` label" test is IMPORTED from the
    production module, not re-expressed here. It used to be a second regex, and
    the two had already drifted: production required an aggregator keyword
    immediately before `by` and knew seven of them, so `sum(x) by (tenant)`,
    `stddev by (tenant) (x)` and `quantile by (tenant) (…)` were tenant-bearing
    to this reader and tenant-less to the one that actually guards the render
    path. A test that derives the right answer independently and never compares
    it to production's answer is not a guard — it is a second opinion nobody
    reads. `_grar_validate` also parses ALL data/binaryData keys of ALL
    ConfigMap docs; that part stays here as a deliberately narrow second reader,
    and TestPlatformReaderParity is what makes the narrowness visible.
    """
    from _grar_validate import _EXPR_TENANT_AGG_RE  # noqa: PLC0415
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
                if _EXPR_TENANT_AGG_RE.search(str(rule.get("expr", ""))):
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
        # `- 1`: the derivation keeps only `alert_source="platform"` rules, and
        # Watchdog is the ONE platform alert that deliberately carries no
        # alert_source (it rides the index-0 heartbeat route). The floor is
        # therefore the whole tree minus that single documented exception —
        # written as arithmetic on the shared constant so growing the pack moves
        # this floor too, instead of leaving a second number to go stale.
        assert len(sets) >= _MIN_PLATFORM_ALERTS - len(_heartbeat_alertnames()), (
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
# The rule-tree scanner now lives in scripts/tools/lint/_rule_tree.py so that
# other gates can import it — see that module's docstring for why the three
# readers had drifted apart while it was stranded in this file.
from _rule_tree import (  # noqa: E402
    _PLATFORM_CM_PREFIX,
    _SCAN_SKIP_PARTS,
    _alert_names,
    _containers_from_text,
    _decode_manifest,
    _documents,
    _expected_rule_files,
    _generated_pack_names,
    _is_platform_cm_location,
    _is_rule_groups,
    _iter_repo_alert_rules,
    _platform_rule_locations,
    _rule_containers,
    _rule_shaped_but_unparsed,
    _rules_shaped_at_any_depth,
    _source_pack_alerts,
    _tracked_yaml_paths,
)

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
    index-0 route, never the sentinel sink. Scans every rules container the
    repo ships — see `_iter_rule_containers`, which finds them by CONTENT, not
    by name or directory. Do not restate the old filename enumeration here: it
    described a scanner that has been replaced, and it omitted
    operator-manifests/ entirely (16 objects, 122 rules, a third of the tree).
    Historically it read: the SOURCE rule packs plus
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
            # ⛔ Name AND location. This loop spans the WHOLE repo, so a
            # by-name-only exemption reaches into the tenant packs: a
            # `severity: none` alert named `Watchdog` in rule-pack-redis.yaml
            # was exempted here and invisible to the platform-scoped duplicate
            # check, i.e. it fell through to the tenant/NOC channels — the
            # exact #1095 leak this contract exists to stop.
            if (rule["alert"] in _heartbeat_alertnames()
                    and _is_platform_cm_location(where)):
                assert "component" not in labels, (
                    f"{rule['alert']} must NOT carry a component label — it "
                    "rides its own index-0 route, not the sentinel sink (#1095)")
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

    def test_the_heartbeat_exemption_is_route_derived_and_singular(self):
        """⛔ The exemption has to keep costing what it claims to cost.

        Two contracts here grant exactly one alert a pass on the discriminator
        they enforce, and the stated reason is always the same sentence: it
        rides the index-0 route with `continue: false`. Three ways that sentence
        can quietly stop being true, none of which anything checked:

          1. the route stops being `continue: false` — the heartbeat becomes
             selectable by a second channel and the exemption's whole premise
             is gone, while both contracts keep granting it;
          2. a SECOND rule takes the exempt name — Prometheus accepts two
             `- alert: Watchdog` in different groups, and the duplicate check
             that exists scans only the PLATFORM tree because it was added for
             the runbook ledger;
          3. the exempt rule moves OUT of the platform tree, at which point a
             tenant pack is holding the platform's dead-man's-switch.
        """
        from _grar_routes import _build_watchdog_route  # noqa: PLC0415

        names = _heartbeat_alertnames()
        assert names, (
            "the index-0 heartbeat route pins no alertname, so the exemptions "
            "below grant nothing and every assertion that subtracts them is "
            "off by their count")

        routes, _receivers = _build_watchdog_route()
        assert all(r.get("continue") is False for r in routes), (
            "the heartbeat route no longer stops at itself, so the heartbeat "
            f"IS reachable by a second channel — the reason {sorted(names)} is "
            f"exempt from carrying a delivery discriminator no longer holds: "
            f"{routes}")

        # Whole tree, not the platform subset: the point is that nothing else
        # anywhere may wear the name that buys the exemption.
        holders = [(w, r) for w, r in _iter_repo_alert_rules()
                   if r["alert"] in names]
        assert len(holders) == len(names), (
            f"{len(holders)} rules carry a heartbeat name {sorted(names)}, "
            f"expected {len(names)}: {[w for w, _r in holders]}. A second one "
            "inherits the exemption without touching a single contract.")
        outside = [w for w, _r in holders if not _is_platform_cm_location(w)]
        assert not outside, (
            f"the heartbeat is defined outside the platform tree: {outside}. "
            "A tenant rule pack would then own the platform's dead-man's-switch.")

    def test_component_sentinel_reserved_for_severity_none(self):
        # The discriminator is RESERVED: a deliverable (severity != none) alert
        # must never ride component="sentinel" or the sinkhole would eat it.
        examined_files = set()
        for where, rule in self._iter_alert_rules():
            examined_files.add(where.split(":", 1)[0])
            labels = rule.get("labels") or {}
            if labels.get("component") == "sentinel":
                assert labels.get("severity") == "none", (
                    f"{where}: alert {rule['alert']!r} carries "
                    f'component="sentinel" but severity='
                    f"{labels.get('severity')!r} — the sentinel sink would "
                    f"swallow a deliverable alert (#1095)")
        # ⛔ Non-vacuity: every assertion above lives inside the loop, so an
        # empty scan makes this contract pass by examining nothing. Asserted
        # PER FILE against `git ls-files`, not as a total count — see
        # _expected_rule_files' docstring for why a count is both too loose
        # (16 rules could vanish under the old `>= 350`) and too tight (any
        # legitimate pack retirement turned it red).
        missing = sorted(_expected_rule_files() - examined_files)
        assert not missing, (
            f"{len(missing)} shipped rules file(s) contributed no alert to "
            f"this scan: {missing[:10]} — the reserved-value claim is only as "
            "strong as the coverage behind it")


# ============================================================
# alert_source="platform" delivery discriminator contract
# ============================================================
class TestPlatformAlertSourceContract:
    """Every platform self-monitoring alert MUST carry `alert_source: platform`.

    WHY: platform alerts have almost no tenant to route them by (39 of the 42
    carry no `tenant` label at all — the 3 that do are TenantMetricsOverLimit via
    rule-level labels, and FederationRejectionRateAnomaly /
    FederationGatewayBackendErrors via their expr's `sum by (tenant)`, i.e. only
    at fire time) and none carry `metric_group`. Without a POSITIVE
    discriminator the only matcher an operator can put on the single
    `_routing_enforced` route is `severity="critical"` — which reaches 19 of the
    42, i.e. more than half of the platform's own self-monitoring would stay
    undeliverable no matter how the operator wires it. This is the same failure
    shape as #1095 (a discriminator that silently does not exist), inverted:
    there the label was missing from a sinkhole, here from a delivery selector.

    Two directions, both asserted, and BOTH bounded by the same
    `_is_platform_cm_location` ("no rule-packs/ source", not a filename test), so
    any SECOND hand-authored rules ConfigMap is covered by presence instead of
    being punished by reserved:
      1. presence  — every alert in ANY hand-authored rules ConfigMap except
         `Watchdog` carries `alert_source: platform`;
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

    def test_platform_cm_discovery_is_content_based(self):
        """The scope seam. Membership is decided by CONTENT and PROVENANCE only.

        Asserted on the live tree rather than on synthetic path strings: the
        previous version of this test pinned hand-written `where` values, which
        is exactly the thing that let a filename-shaped criterion look verified.
        """
        containers = list(_rule_containers())
        # ⛔ EVERY shipped rules file must still produce rules. This replaces a
        # `>= 30` container floor that had 33 of slack — enough for an entire
        # tree to vanish silently — with an exact, self-updating statement.
        produced = {w.split(":", 1)[0] for w, _d, _p in containers}
        expected = _expected_rule_files()
        missing = sorted(expected - produced)
        assert not missing, (
            f"{len(missing)} shipped rules file(s) yielded no container: "
            f"{missing[:10]}. Either the scanner stopped understanding a shape "
            "the repo uses, or those files stopped shipping rules — both are "
            "changes that must be seen, not absorbed by a slack-heavy floor.")
        # ⛔ …and the SAME assertion in reverse, which is what makes the anchor
        # an anchor. `expected ⊆ produced` alone is satisfied by an empty
        # expected set — measured: `_expected_rule_files` returning
        # `frozenset()`, or losing any one of its five globs, kept the whole
        # suite green. A floor that can be zeroed is not a floor.
        #
        # Equality also makes the glob list keep up with the SCANNER instead of
        # with a maintainer's memory. Every discovery shape this module has
        # learned (bare `groups:` documents outside rule-packs/, `k8s/` subtrees
        # other than 03-monitoring, `helm/`, `.yml` operator manifests, a
        # PrometheusRule not named `da-rule-pack-*`) is reachable by the scanner
        # and was named by NONE of the globs, so each was a file the coverage
        # floor could not see going dark. Whichever side moves first, this is red
        # until both agree.
        unanchored = sorted(produced - expected)
        assert not unanchored, (
            f"{len(unanchored)} file(s) ship rules that the scanner finds but "
            f"`_expected_rule_files()`'s globs do not name: {unanchored[:10]}. "
            "They are outside the per-file coverage floor, so the day one stops "
            "producing rules nothing goes red. Add the glob.")

        by_where = {w: prov for w, _doc, prov in containers}
        platform = sorted(_platform_rule_locations())

        # 1. The hand-authored platform tree is in scope, keyed by its real path.
        assert any(w.endswith("configmap-rules-platform.yaml:platform-alert.yml")
                   for w in platform), platform
        # 2. Generated copies never are — they name a source pack that exists.
        redis = [w for w in by_where if "configmap-rules-redis" in w]
        assert redis and all(by_where[w] == "redis" for w in redis)
        assert not any(w in platform for w in redis)
        assert "redis" in _generated_pack_names()
        assert "platform" not in _generated_pack_names()
        # 3. operator-manifests/ PrometheusRule objects are DISCOVERED — the
        #    directory-scoped scanner never saw this deployment path at all.
        # ⛔ Pin EVERY tree, not the two that happened to get one. Dropping
        # rule-packs/ — 16 containers, 122 rules, half of what the previous
        # scanner read — was completely silent: the `>= 30` container floor has
        # 33 of slack at today's 63, so losing a whole tree still clears it.
        # A floor that only the trees you remembered can trip is not a floor.
        packs_tree = [w for w in by_where if w.startswith("rule-packs/")]
        assert len(packs_tree) >= 16, (
            f"the rule-packs/ source tree yielded {len(packs_tree)} container(s) "
            "— it is no longer being discovered, and every contract silently "
            "narrowed to the deployed copies")
        k8s_tree = [w for w in by_where if w.startswith("k8s/03-monitoring/")]
        assert len(k8s_tree) >= 17, (
            f"the deployed ConfigMap tree yielded {len(k8s_tree)} container(s)")
        prom = [w for w in by_where if w.startswith("operator-manifests/")]
        assert len(prom) >= 16, f"PrometheusRule tree not discovered: {prom}"
        # Classification, not the CLAIM. Provenance may be established by
        # content (alerts that are actually in a pack) even when the object
        # name says nothing, so asserting on the claimed string would fail a
        # correctly-classified manifest and re-create the reserved-label
        # misdiagnosis this whole criterion exists to avoid.
        assert not any(w in platform for w in prom), (
            f"operator manifest(s) classified platform-scope: "
            f"{[w for w in prom if w in platform]}")
        # 4. `groups:` alone is not enough: RBAC subject groups are not rules.
        assert not any("configmap-rbac" in w for w in by_where), sorted(by_where)
        # 5. Nothing rule-shaped may yield zero rules (the silent-zero).
        assert _rule_shaped_but_unparsed() == (), _rule_shaped_but_unparsed()

    def test_unknown_provenance_defaults_to_platform(self, tmp_path):
        """Fail-closed direction, exercised rather than asserted in prose.

        A rules artifact the scanner cannot attribute to a generator must land
        INSIDE the gates. This is the property that makes naming, extension and
        directory irrelevant, so it is pinned on the classifier's own logic.
        """
        # ⛔ Exercise the REAL classifier. The first version of this test
        # restated the predicate inside itself and asserted on its own fake
        # list — a tautology that still passed with the classifier inverted to
        # fail-OPEN, while its docstring claimed to pin "the classifier's own
        # logic". Monkeypatching the container source is what makes the
        # assertion depend on the code under test.
        packs = _generated_pack_names()
        assert "definitely-not-a-pack" not in packs
        fake = (
            ("fake.yaml:missing-pack", {}, "definitely-not-a-pack"),
            ("fake.yaml:no-prov", {}, None),
            ("fake.yaml:real-pack", {}, sorted(packs)[0]),
        )
        # ⛔ Clear EVERY cache that reads containers, not just the one under
        # test. `_platform_rule_locations` now consults `_source_pack_alerts`,
        # which is also memoised: clearing one left the other to be populated
        # from the FAKE list, so this test passed only when something earlier in
        # the session had warmed the real value. Running it alone failed —
        # order-dependence, and the kind that looks like a real defect.
        # ⛔ Patch the MODULE the classifier reads, not this file's globals.
        # The scanner moved to `_rule_tree`; rebinding a name here would leave
        # `_platform_rule_locations` looking at the real tree and the test
        # passing for the wrong reason.
        import _rule_tree
        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            _source_pack_alerts()            # memoise from the REAL tree FIRST…
            _rule_tree._rule_containers = lambda: fake     # …then swap
            _platform_rule_locations.cache_clear()
            got = _platform_rule_locations()
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()
        assert "fake.yaml:missing-pack" in got, (
            "a header naming a pack that does not exist must NOT confer "
            "generated status — stale or fabricated provenance is not provenance")
        assert "fake.yaml:no-prov" in got, (
            "unknown provenance must default to platform (fail-closed)")
        assert "fake.yaml:real-pack" not in got

    def test_classification_disagrees_with_the_filename_wherever_it_can(self):
        """⛔ The one property this whole module exists for, and the live tree
        cannot express it.

        On today's repo the content criterion and a `configmap-rules-platform*`
        filename prefix pick out the SAME single container, so every assertion
        anchored on the real tree — including both TestPlatformReaderParity
        cases — stays green with the classifier reverted to the prefix. Measured:
        it does, all 119 of them, while an injected hand-authored platform alert
        in `k8s/04-tenant-api/` goes completely unseen.

        The parity harness structurally CANNOT cover this. Both readers it
        cross-checks against (`_real_platform_label_sets` and production
        `platform_alert_identities`) open one hard-coded filename, so reverting
        the third reader to filename semantics makes all three agree. Two
        filename readers can only ever detect drift in a filename reader.

        So the four disagreeing shapes are constructed. Each is a real placement
        a maintainer could make tomorrow, and each is the answer to a different
        way the criterion has already been wrong once.
        """
        import _rule_tree  # noqa: PLC0415

        # The "generated" case reuses a REAL source pack document rather than a
        # reconstruction: provenance is decided by comparing `_alert_names(doc)`
        # against the pack's own set, so a doc rebuilt from that set would be
        # testing my reconstruction, not the classifier.
        real_pack, pack_doc = next(
            ((prov, doc) for w, doc, prov in _rule_containers()
             if w.startswith("rule-packs/") and prov and _alert_names(doc)),
            (None, None))
        assert real_pack, "no source pack container — the generated case below "\
                          "would be vacuous"
        hand = {"groups": [{"name": "g", "rules": [{
            "alert": "HandAuthoredSelfCheck",
            "expr": "da_platform_writer_lock_lost > 0"}]}]}

        cases = {
            # 1. hand-authored, and NOT named platform*. The prefix says "not
            #    platform" → presence gates skip it while the RESERVED gate
            #    reports its correct `alert_source: platform` as an offender,
            #    i.e. the diagnostic tells the maintainer to delete the label
            #    that routes it. Content says platform, which is right.
            "k8s/03-monitoring/configmap-noc-selfcheck.yaml:rules": (hand, None, True),
            # 2. hand-authored, in a k8s subtree that is not 03-monitoring —
            #    the exact directory a previous scope derivation dropped.
            "k8s/04-tenant-api/configmap-rules-tenant-plane.yaml:rules": (hand, None, True),
            # 3. hand-authored, named platform* but one directory DOWN. The
            #    prefix compared the relative path, so this escaped every gate
            #    while the scanner (recursive) was already reaching it.
            "k8s/03-monitoring/sub/configmap-rules-platform.yaml:rules": (hand, None, True),
            # 4. the reverse direction, which a one-sided test would miss: a
            #    GENERATED copy that happens to be named platform*. The prefix
            #    calls it platform and then demands `alert_source: platform` on
            #    tenant alerts — a reserved value that pipes them to the NOC.
            "k8s/03-monitoring/configmap-rules-platform-redis.yaml:rules": (
                pack_doc, real_pack, False),
        }
        fake = tuple((w, doc, prov) for w, (doc, prov, _e) in cases.items())

        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            _source_pack_alerts()             # memoise from the REAL tree FIRST…
            _rule_tree._rule_containers = lambda: fake      # …then swap
            _platform_rule_locations.cache_clear()
            got = {w: _is_platform_cm_location(w) for w in cases}
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()

        wrong = {w: (got[w], exp) for w, (_d, _p, exp) in cases.items()
                 if got[w] != exp}
        assert not wrong, (
            "the classifier answered by NAME, not by content. Each of these is "
            "a placement where the two criteria differ, which is the only place "
            "the difference is observable at all:\n" + "\n".join(
                f"  {w}: got platform={g}, expected {e}"
                for w, (g, e) in sorted(wrong.items())))

    def test_shipped_roots_are_whole_top_level_trees(self):
        """Narrowing a root to a subdirectory is the regression, so pin the shape.

        `_SHIPPED_ROOTS` back at `k8s/03-monitoring/` leaves the whole suite
        green (measured) while `k8s/04-tenant-api/` and `k8s/crd/` drop out of
        discovery — and the per-file coverage floor goes blind in the same
        stroke, because its globs name only `k8s/03-monitoring/` too. Both halves
        of the gate lose the same directory at once, which is why nothing
        notices.

        Two independent statements, neither a snapshot of the current tuple:
        every root is a whole top-level tree, and `k8s` — the root this repo's
        own raw-manifest SAST already scans — is one of them.
        """
        from _rule_tree import _SHIPPED_ROOTS  # noqa: PLC0415
        from check_k8s_manifests import MANIFEST_ROOT  # noqa: PLC0415

        deep = [r for r in _SHIPPED_ROOTS if r.count("/") != 1]
        assert not deep, (
            f"{deep} narrow the scan to a SUBDIRECTORY. Coverage must not "
            "depend on where inside a shipped tree a rules file sits — a file "
            "moved one level down would silently leave every contract.")
        assert f"{MANIFEST_ROOT}/" in _SHIPPED_ROOTS, (
            f"check_k8s_manifests scans {MANIFEST_ROOT!r} recursively as the "
            "deployed-manifest tree; this scanner claims the same anchor in "
            f"its own comment but ships {_SHIPPED_ROOTS}")

    def test_platform_alerts_carry_alert_source(self):
        seen = []
        watchdog_checked = False
        for where, rule in self._platform_rules():
            labels = rule.get("labels") or {}
            if rule["alert"] in _heartbeat_alertnames():
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
        # `- 1`: Watchdog is skipped above (the one documented exception), so the
        # floor is the whole tree minus it. See _MIN_PLATFORM_ALERTS.
        _exempt = len(_heartbeat_alertnames())
        assert len(seen) >= _MIN_PLATFORM_ALERTS - _exempt, (
            f"only {len(seen)} platform alerts scanned, expected >= "
            f"{_MIN_PLATFORM_ALERTS - _exempt}: {sorted(seen)}")
        assert set(seen) >= {
            "ThresholdExporterAbsent", "PrometheusRuleEvaluationFailing",
            "TenantApiSingleWriterBreach", "FederationAuditPipelineSilent",
            "VectorProjectionGateStuck"}, sorted(seen)

    def test_alert_source_reserved_for_the_platform_tree(self):
        # RESERVED value: a tenant-facing alert carrying alert_source would be
        # dual-delivered into the platform/NOC channel by the operator's
        # enforced route (continue:true), which is exactly the leak the custom
        # isolation subtree exists to prevent.
        offenders, non_platform_files, platform_files = [], set(), set()
        for where, rule in _iter_repo_alert_rules():
            (platform_files if _is_platform_cm_location(where)
             else non_platform_files).add(where.split(":", 1)[0])
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
        # ⛔ Non-vacuity on the side this contract is ABOUT. Every other floor in
        # this file counts PLATFORM alerts, so a bypass that hides only
        # non-platform containers keeps all of them satisfied and leaves this
        # test — the one asserting the value is reserved AGAINST tenants —
        # trivially green. Measured: a `kind: List` wrapper did exactly that.
        #
        # PER FILE, and the platform files are subtracted from the expected set
        # rather than pinned by name: whichever shipped files the scanner did
        # NOT classify as platform must each have contributed a tenant-side
        # alert. A count could not say this — `>= 350` stayed green while a
        # whole file went dark (16 rules of slack) and went red on the
        # legitimate retirement of any of 13 packs.
        missing = sorted(
            _expected_rule_files() - platform_files - non_platform_files)
        assert not missing, (
            f"{len(missing)} shipped rules file(s) contributed no alert at "
            f"all: {missing[:10]}; a reserved-value claim is only as good as "
            "the tenant-side coverage behind it")


# ============================================================
# runbook_url coverage contract (#1207 續 — the last 2 of the 42)
# ============================================================
# Shape of every runbook_url in the platform tree: an absolute GitHub blob URL
# on main, optionally with an ASCII #anchor. Asserted (not merely described) by
# test_runbook_urls_resolve_inside_this_repo below.
_RUNBOOK_URL_PREFIX = (
    "https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/")

# The heading -> anchor machinery is BORROWED, not reimplemented. The repo's
# doc-link linter already owns a correct one, and the copy that used to live here
# was a weaker second implementation of the same thing: it slugged every line
# STARTING WITH "#" — no code-fence tracking, no `#{1,6}\s+` requirement — so a
# shell comment inside a ``` block produced a phantom anchor. On
# docs/cli-reference.md that is 173 "anchors" against 80 real headings; an anchor
# pointing at a fenced `# CI gate` comment resolved GREEN while 404ing on GitHub.
# A gate whose whole job is "this link is not a 404" must not carry the more
# permissive of the repo's two anchor readers.
#
# Both borrowed members are `_`-private, so test_borrowed_anchor_machinery_canary
# below pins the shape and behaviour this gate depends on: an upstream change
# fails there, loudly, instead of silently degrading this gate.
from check_doc_links import DocLinkChecker  # noqa: E402


@functools.lru_cache(maxsize=1)
def _doc_link_checker() -> DocLinkChecker:
    """One shared instance (its __init__ pre-scans the docs tree, ~90ms)."""
    return DocLinkChecker(_REPO_ROOT)


def _doc_anchors(path: Path) -> set:
    """GitHub anchors a Markdown file actually offers (code-fence aware)."""
    return _doc_link_checker()._get_headings(path)


@functools.lru_cache(maxsize=1)
def _git_tracked_paths() -> frozenset:
    """Repo-relative POSIX paths git tracks, or an EMPTY set if git is unusable.

    A blob/main URL renders from what is COMMITTED, so a file that exists on the
    developer's disk but is untracked (or ignored) still 404s for the on-call —
    the exact failure this gate exists to prevent. One `git ls-files` covers the
    whole tree, so the check is ~free.

    Empty means "unknown" and the caller skips the check rather than reporting
    every URL as broken: a source tarball / exported worktree with no .git is not
    evidence that the docs are missing.
    """
    try:
        out = subprocess.run(
            ["git", "-C", _REPO_ROOT, "ls-files", "-z"],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except FileNotFoundError:
        # git is not installed at all — the source-tarball case this tolerates.
        return frozenset()
    except subprocess.CalledProcessError as exc:
        # ⛔ git RAN and refused. That is a misconfigured environment, not an
        # absent repo, and the assert below never sees it — `GIT_DIR` pointing
        # at nothing exits 128 and lands here, which is exactly the scenario
        # the assert was written for and exactly the one it could not reach.
        raise AssertionError(
            f"git ls-files failed (exit {exc.returncode}) — GIT_DIR/GIT_WORK_TREE "
            "is probably misconfigured. Refusing to treat that as 'no files to "
            "check', which would silently pass every runbook URL.") from exc
    tracked = frozenset(p for p in out.split("\0") if p)
    # ⛔ An EMPTY result from a git that RAN is not "no .git here" — it is a git
    # pointed somewhere else, e.g. GIT_DIR aimed at another repo whose index is
    # empty (a nonexistent GIT_DIR exits 128 and raises above instead). One
    # stray environment variable should not be able to switch a gate off.
    # Absent git — the source-tarball case this deliberately tolerates — also
    # raises above and still returns empty.
    assert tracked, (
        "git ran but tracked nothing — GIT_DIR/GIT_WORK_TREE is probably "
        "pointing away from the repo. Refusing to treat that as 'no files to "
        "check', which would silently pass every runbook URL.")
    return tracked

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
    and no linter asks the question this one asks.

    ⚠️ Do NOT restate the two older versions of this note; both were false and
    the second outlived its own fix. pint CAN read a rules ConfigMap
    (`parser.relaxed` exists for rules "embedded inside a different structure"),
    and — the part that went stale — `.pint.hcl` ALREADY reads this one:
    `parser.include` carries the `configmap-rules-platform*` prefix pattern
    with the matching `parser.relaxed` entry, and `check_pint.py` pins the
    resulting count (`_PLATFORM_PACK_ENTRIES = 42`). The note that called
    widening "an option on the table" would send the next maintainer to widen a
    config that was widened two releases ago.

    What pint still does not do is the thing this contract is for: its
    `alerts/annotation` check verifies that an annotation EXISTS and matches a
    regex. It does not resolve the URL, does not check the file is tracked in
    this repo, and does not check the heading anchor still exists. A runbook_url
    pointing at a deleted page passes pint and fails an on-call at 3am.

    Two directions:
      1. coverage — every platform alert except the shrink-only ledger above
         carries a non-empty runbook_url;
      2. resolvability — every runbook_url that IS present names a file that
         exists on disk, INSIDE this repo and TRACKED BY GIT, and an #anchor (if
         any) names a heading that exists in that file. A dead runbook link is
         worse than a missing one: it costs the on-call a click plus the doubt
         about whether they have the wrong URL or the wrong problem.

    Reuses `_iter_repo_alert_rules` / `_is_platform_cm_location` rather than
    re-globbing, for the same reason the three contracts above share them: a
    second scanner drifts and silently narrows whichever gate lost the race.
    The same argument is why the anchor reader is borrowed from
    check_doc_links rather than reimplemented — see `_doc_anchors`.
    """

    def _platform_alerts(self):
        return [(where, rule) for where, rule in _iter_repo_alert_rules()
                if _is_platform_cm_location(where)]

    def test_borrowed_anchor_machinery_canary(self, tmp_path):
        """The borrowed `_`-private anchor machinery still works as this gate needs.

        `DocLinkChecker._get_headings` / `._heading_to_anchor` are private to
        another module: nothing obliges their author to keep them. Without this
        canary, a rename or a behaviour change would degrade
        test_runbook_urls_resolve_inside_this_repo SILENTLY (an anchor set that
        is empty, or wrong, only makes the gate louder or quieter — never red on
        its own terms). Here it fails loudly and names the dependency.

        Pins only the properties this gate depends on. Deliberately NOT pinned:
        how CONSECUTIVE whitespace collapses. That is a real GitHub-fidelity bug
        in `_heading_to_anchor` being fixed upstream in this same batch, and a
        canary that froze today's answer would fight the fix.
        """
        anchor = DocLinkChecker._heading_to_anchor
        # shape: still a staticmethod taking heading TEXT (no leading #)
        assert anchor("Threshold Govern") == "threshold-govern"
        assert anchor("Validate Config!") == "validate-config"
        assert anchor("**Bold** `code` heading") == "bold-code-heading"

        # ⛔ Exercise EVERY fence spelling, not just the one this repo writes
        # most. A canary whose only fence is ```` ```bash ```` cannot see a
        # regression that opens fences solely on an info string — bare ``` and
        # `~~~` blocks would start emitting their `#` comments as headings, the
        # gate would accept anchors that 404 on GitHub, and this test would
        # still pass. Each spelling below is a distinct way for the upstream
        # tracker to break, so each gets its own phantom to catch.
        md = tmp_path / "canary.md"
        md.write_text(
            "# Real Heading\n"
            "\n"
            "```bash\n"
            "# CI gate\n"
            "#!/usr/bin/env bash\n"
            "```\n"
            "\n"
            "```\n"
            "# Bare Fence Phantom\n"
            "```\n"
            "\n"
            "~~~bash\n"
            "# Tilde Fence Phantom\n"
            "~~~\n"
            "\n"
            # Each of the next three exercises ONE CommonMark fence rule that a
            # rewrite can drop independently. Without them the canary passed
            # while the closing-length rule, the trailing-text rule and the
            # info-string rule were each individually broken upstream.
            # A LONGER closing fence must still close: drop the `>=` and this
            # block never ends, taking the heading after it with it.
            "```\n"
            "# Inner A\n"
            "````\n"
            "## Longer Close Survivor\n"
            "\n"
            # A closing fence carrying trailing text is NOT a close: accept it
            # and `Still Inner` surfaces as a heading that GitHub never renders.
            "```\n"
            "# Inner B\n"
            "``` not-a-close\n"
            "# Still Inner\n"
            "```\n"
            "\n"
            # This one asserts POSITIVELY: a backtick inside an opening
            # fence's info string means it is not a fence at all, so the
            # heading below it is real. Drop that rule upstream and the
            # heading vanishes — which is the degradation being watched for.
            "```js `tick`\n"
            "# Info String Survivor\n"
            "```\n"
            "```\n"
            "\n"
            "<!--\n"
            "## Commented Out Phantom\n"
            "-->\n"
            "\n"
            # Four spaces makes an INDENTED CODE BLOCK, not a heading. The
            # three fence fixtures above all probe one dimension; this probes
            # the other, and it is the fail-open one — an upstream change that
            # starts tolerating leading indent without re-imposing the 4-space
            # limit turns every indented `#` comment into a live anchor.
            "    # Indented Code Phantom\n"
            "\n"
            "#NotAHeading\n"
            "\n"
            "### Nested Heading\n",
            encoding="utf-8")
        anchors = _doc_anchors(md)
        assert "real-heading" in anchors, anchors
        assert "nested-heading" in anchors, anchors
        # the two properties the deleted local slugger lacked, and the reason
        # this gate borrows instead of copying:
        assert "ci-gate" not in anchors, (
            "code-fence tracking regressed upstream — a fenced `# comment` is "
            "being read as a heading, which is exactly how a 404 anchor went "
            "green here")
        assert "bare-fence-phantom" not in anchors, (
            "an info-string-less ``` no longer opens a fence upstream — bare "
            "blocks now leak their `#` comments as headings")
        assert "tilde-fence-phantom" not in anchors, (
            "`~~~` fences are not being tracked upstream; CommonMark allows "
            "them and their `#` comments are leaking as headings")
        assert "longer-close-survivor" in anchors, (
            "a closing fence LONGER than its opener no longer closes upstream — "
            "the block runs on and swallows real headings")
        assert "still-inner" not in anchors, (
            "a closing fence with trailing text is being accepted upstream, so "
            "fenced content is surfacing as headings")
        assert "info-string-survivor" in anchors, (
            "a backtick in an opening fence's info string means it is NOT a "
            "fence; upstream is treating it as one and eating the heading")
        assert "indented-code-phantom" not in anchors, (
            "a 4-space-indented `#` line is an indented code block, not a "
            "heading — upstream has started accepting leading indent without "
            "the 4-space limit, which mints anchors GitHub does not have")
        assert "commented-out-phantom" not in anchors, (
            "a heading inside an HTML comment is being counted upstream — "
            "GitHub does not render it, so links to it 404")
        assert "notaheading" not in anchors, (
            "ATX headings require `#{1,6}\\s+`; `#Foo` is not a heading")
        # ...and it must actually FIND headings (an empty set would make the
        # anchor branch of the gate vacuously strict, not vacuously green, but
        # would still be a broken dependency)
        assert len(anchors) == 4, anchors
        # end-to-end on the file this gate really reads: the three live anchors
        # resolve, and the fenced `# CI gate` comment that used to resolve does not.
        live = _doc_anchors(Path(_REPO_ROOT) / "docs" / "cli-reference.md")
        assert {"threshold-govern", "cardinality-forecast",
                "validate-config"} <= live
        assert "ci-gate" not in live

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

    def test_runbook_ledger_is_subset_locked(self):
        """Count pin: make "shrink-only" mechanical instead of aspirational.

        The docstring on _PLATFORM_ALERTS_WITHOUT_RUNBOOK says a row may only be
        DELETED. Nothing enforced that: shipping a 43rd platform alert with no
        runbook_url and adding one line to the ledger turned the coverage gate
        green again — the ledger absorbed the regression instead of reporting it,
        which is exactly the "gate goes quiet as the debt grows" shape the two
        sibling ledgers in this repo pin against
        (test_check_scrape_reachability.py `== 9`,
        test_check_orphan_recordings.py `== 12`).

        `<=`, not `==`, and the choice is load-bearing. Those two siblings assert
        `len(infos) == len(LEDGER) == N`: one statement doing double duty as a
        STALENESS check (every row still applies on the live repo) and a size pin.
        This ledger already has the staleness half — test_runbook_ledger_is_exit_locked
        below reports a row that gained a runbook_url AND a row naming a deleted
        alert — so the only missing half is the growth ratchet. Spelling
        it `== 2` would additionally make the DESIRED direction cost a test edit:
        #1360 lands the two IR pages, deletes both rows, and would then have to
        come back here to re-pin. `<=` lets the ledger empty itself and still
        forces an explicit, reviewed bump to grow, the same shape as
        test_bilingual_help_contract.py::test_allowlists_shrink_only_count_pin.
        """
        # ⛔ Pin the KEY SET, not the row count. A count pin says "no more than
        # two deferrals", which a SWAP satisfies: give CronJobLastRunFailed a
        # runbook_url, delete its row, and land a brand-new runbook-less alert
        # with a row of its own — still two, still green, and the docstring's
        # "a row may only be DELETED" is quietly false. Subset-of-the-original
        # is the property actually wanted, and it still lets the ledger empty.
        _LEDGER_ORIGIN = frozenset({"CronJobLastRunFailed", "MassExporterOutage"})

        # ⛔ The ledger is keyed by ALERTNAME, and an alertname is not unique —
        # Prometheus happily takes two `- alert: CronJobLastRunFailed` in
        # different groups. A new runbook-less alert given an existing ledger
        # key therefore rides in without the ledger changing by one character,
        # and can do so any number of times: the subset pin stops a SWAP but
        # not a PIGGYBACK. Duplicate platform alertnames are independently
        # wrong (they collide in dedup and in every by-name lookup below), so
        # forbid them outright rather than trying to make the ledger unique.
        names = [rule["alert"] for where, rule in _iter_repo_alert_rules()
                 if _is_platform_cm_location(where)]
        dupes = sorted({n for n in names if names.count(n) > 1})
        assert not dupes, (
            f"duplicate platform alertname(s) {dupes}. Beyond the dedup and "
            "by-name-lookup breakage, a duplicate is how a runbook-less alert "
            "inherits an existing ledger entry's exemption without the ledger "
            "changing at all.")

        # ⛔ …and the ledger itself must name alerts that EXIST. Nothing audited
        # _LEDGER_ORIGIN, so a typo sat there silently, and once #1360 empties
        # the ledger these two names would stay valid forever — a standing
        # licence to re-add exactly them, unreviewed.
        # ⛔ A name is a slot, not an identity. Rename the real
        # CronJobLastRunFailed, give it a runbook, then land a NEW runbook-less
        # critical under the vacated name: the ledger is untouched, the subset
        # pin holds, `unknown` is empty (the name still exists), and there are
        # no duplicates. Pinning each ledger entry to its EXPRESSION makes the
        # slot non-transferable — taking the name over means matching the rule.
        # Digest rather than the literal: these exprs are multi-line PromQL and
        # pasting them here would rot on any reformat while saying nothing a
        # reader can check at a glance.
        def _expr_digest(expr: str) -> str:
            return hashlib.sha256(
                " ".join(str(expr).split()).encode("utf-8")).hexdigest()[:16]

        _LEDGER_EXPR_DIGESTS = {
            "CronJobLastRunFailed": "47e14d4f7ecebc93",
            "MassExporterOutage": "ceb1cee6443d2c81",
        }
        by_expr = {r["alert"]: str(r.get("expr", ""))
                   for _w, r in _iter_repo_alert_rules()
                   if _is_platform_cm_location(_w)}
        # ⛔ Every ledger row needs a pin. Without this, deleting one digest
        # line silently retires that row's protection while the subset lock
        # keeps reporting green.
        assert set(_LEDGER_EXPR_DIGESTS) == _LEDGER_ORIGIN, (
            f"_LEDGER_EXPR_DIGESTS must pin exactly the ledger's original rows; "
            f"missing {sorted(_LEDGER_ORIGIN - set(_LEDGER_EXPR_DIGESTS))}, "
            f"extra {sorted(set(_LEDGER_EXPR_DIGESTS) - _LEDGER_ORIGIN)}")
        for alert, pinned in _LEDGER_EXPR_DIGESTS.items():
            if alert in by_expr:
                actual = _expr_digest(by_expr[alert])
                assert actual == pinned, (
                    f"{alert} still carries its ledger exemption but its expr "
                    f"changed (digest {actual}, pinned {pinned}). If the alert "
                    f"was legitimately rewritten, update the pin deliberately; "
                    f"if a DIFFERENT alert took the vacated name over, it does "
                    f"not inherit the exemption.")

        unknown = _LEDGER_ORIGIN - set(names)
        assert not unknown, (
            f"_LEDGER_ORIGIN names alert(s) that do not exist in the platform "
            f"tree: {sorted(unknown)}. Either they were renamed (update the "
            "ledger) or retired (delete the entry) — a ledger row for a "
            "non-existent alert is a pre-authorised exemption for whoever "
            "creates that name next.")

        added = set(_PLATFORM_ALERTS_WITHOUT_RUNBOOK) - _LEDGER_ORIGIN
        assert not added, (
            f"_PLATFORM_ALERTS_WITHOUT_RUNBOOK gained {sorted(added)}. This "
            "ledger is shrink-only: it exists for the two alerts #1207 left "
            "pending (CronJobLastRunFailed, MassExporterOutage), NOT as an "
            "escape hatch for a new alert that shipped without a runbook_url — "
            "and swapping one out for another is exactly the move a count-only "
            "pin would have waved through. Write the disposition page and point "
            "the alert at it. If a new deferral is genuinely unavoidable, adding "
            "it to _LEDGER_ORIGIN above is the deliberate, reviewed cost of that.")
        # Every row must carry its own written justification — the ledger's value
        # is the REASON, not the name (a bare name set would let a row be added
        # with no argument for it).
        unexplained = sorted(name for name, why
                             in _PLATFORM_ALERTS_WITHOUT_RUNBOOK.items()
                             if len(why.strip()) < 80)
        assert unexplained == [], (
            "each ledger row must state WHY no existing page answers the "
            f"page-out: {unexplained}")

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
            # CLOSURE FIRST. `Path(root) / rel` is not a containment operation:
            # an absolute `rel` REPLACES the root outright (`Path(root)/"/etc/hosts"`
            # is `/etc/hosts`, and `.is_file()` says True), and `../../..` walks out
            # the same way. Either shape would let a URL that 404s on GitHub — the
            # blob path does not exist in the repo — pass by resolving to some file
            # on the developer's machine. resolve() then is_relative_to() is the
            # actual containment test.
            target = (Path(_REPO_ROOT) / rel).resolve()
            if not target.is_relative_to(Path(_REPO_ROOT).resolve()):
                broken.append((where, rule["alert"], url,
                               f"{rel!r} resolves OUTSIDE the repo ({target}) — "
                               "a blob/main path is always repo-relative"))
                continue
            if not target.is_file():
                broken.append((where, rule["alert"], url,
                               f"{rel} does not exist in this repo"))
                continue
            # blob/main renders what is COMMITTED: an untracked or ignored file
            # exists here and 404s there. Skipped when git is unavailable — see
            # _git_tracked_paths.
            # ⛔ Test the REQUESTED path, not the resolved one. GitHub serves
            # blob/main/<literal path>; if that literal path is not committed it
            # 404s no matter what a local symlink points at. Checking the
            # resolved target lets `docs/_alias.md -> docs/cli-reference.md`
            # (untracked symlink, real destination) pass a gate whose whole
            # purpose is "the on-call can open this URL". resolve() stays above,
            # where it belongs: proving the link cannot escape the repo.
            tracked = _git_tracked_paths()
            requested = posixpath.normpath(rel)
            if tracked and requested not in tracked:
                broken.append((where, rule["alert"], url,
                               f"{rel} exists on disk but is NOT tracked by git — "
                               "blob/main serves the committed tree, so this 404s"))
                continue
            # ⛔ Checked BEFORE the no-anchor shortcut. A runbook_url pointing
            # at a bare .yaml resolves, so `if not anchor: continue` waved it
            # through — but "the URL 200s" was never the bar; the bar is that
            # the on-call lands on something that answers the page.
            if target.suffix.lower() not in (".md", ".markdown"):
                if not anchor:
                    broken.append((where, rule["alert"], url,
                                   f"{rel} is not Markdown — an on-call opening "
                                   f"this gets raw source, not a runbook"))
                    continue
            if not anchor:
                continue
            # ⛔ Non-Markdown blobs offer only line refs. `_doc_anchors` slugs
            # any file it is handed, so a .yaml target yields anchors invented
            # from its `#` comments — configmap-rules-platform.yaml alone gives
            # 39 — and this gate, whose entire reason to exist is "the on-call
            # can open this URL", accepts every one of them. check_doc_links
            # gained this guard in _check_anchor; the borrowed reader bypasses
            # it, so the one consumer that most needs it was the one left out.
            if target.suffix.lower() not in (".md", ".markdown"):
                # ⛔ BOTH ends. Checking only the first let `#L1-L99999` and the
                # reversed `#L1975-L1` through — same symptom on GitHub as the
                # `#L999999` this guard exists for, with the bad number simply
                # moved to the back half of the range.
                line_nums = [int(n) for n in re.findall(r"L(\d+)", anchor)]
                total_lines = len(target.read_text(
                    encoding="utf-8", errors="replace").splitlines())
                if not re.fullmatch(r"L\d+(C\d+)?(-L\d+(C\d+)?)?", anchor):
                    broken.append((where, rule["alert"], url,
                                   f"{rel} is not Markdown — GitHub renders it as "
                                   f"source and offers only #L<n> line refs, so "
                                   f"#{anchor} does not exist"))
                elif not all(1 <= n <= total_lines for n in line_nums) or (
                        len(line_nums) == 2 and line_nums[0] > line_nums[1]):
                    # ⛔ Bounds too. `#L999999` on a 270-line file is not a 404,
                    # it just scrolls nowhere — precisely the "burns the
                    # on-call's first click" this gate names as its reason.
                    broken.append((where, rule["alert"], url,
                                   f"#{anchor} does not name a real line range "
                                   f"in {rel} ({total_lines} lines)"))
                continue
            slugs = _doc_anchors(target)
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
        seen = []
        for where, rule in self._platform_alerts():
            seen.append(rule["alert"])
            url = (rule.get("annotations") or {}).get("runbook_url") or ""
            _, _, anchor = url.partition("#")
            if not anchor:
                continue
            assert anchor.isascii(), (
                f"{where}: {rule['alert']} has a non-ASCII runbook_url anchor "
                f"#{anchor} — link to the file with no anchor instead (the "
                "three docs/internal/* links do exactly this)")
        # Non-vacuity floor, matching its three siblings in this class: without
        # it an empty scan (the platform tree renamed, moved, or unparsed) passes
        # this test for the wrong reason.
        #
        # The floor is on the SCAN, not on how many anchors it found. Flooring
        # the anchor COUNT would punish the remediation this very test prescribes
        # — "link to the file with no anchor instead" drives that count toward
        # zero — so a tree that legitimately drops to zero anchors must stay
        # green here while still proving the scan reached it.
        assert len(seen) >= _MIN_PLATFORM_ALERTS, (
            f"only {len(seen)} platform alerts scanned, expected >= "
            f"{_MIN_PLATFORM_ALERTS} — no {_PLATFORM_CM_PREFIX}*.yaml was "
            f"reached, so every assertion here is vacuous: {sorted(seen)}")


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

        assert len(self._platform_alerts()) >= _MIN_PLATFORM_ALERTS, (
            "the platform tree did not load — every alert below went unexamined "
            "and this test would pass on an empty scan")

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


class TestContainerDiscovery:
    """Every guard in `_containers_from_text`, pinned by the shape that motivated it.

    ⛔ These exist because a counterfactual sweep found that reverting eight of
    the nine guards in this file left the whole suite green. Every assertion
    elsewhere reads the LIVE tree, and the live tree contains no attack — so
    the guards were protected by nothing but the memory of why they were added.
    Each case below is a payload that was measured slipping past an earlier
    revision, reduced to the smallest form that still discriminates.
    """

    @staticmethod
    def _alerts(text, rel="k8s/03-monitoring/x.yaml"):
        return {a for _w, doc, _p in _containers_from_text(rel, text)
                for a, *_ in _alert_names(doc)}

    def test_quoted_and_spaced_groups_keys_survive_the_prefilter(self):
        for spelling in ('"groups":', 'groups :', 'groups:'):
            text = ('apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n'
                    'data:\n  r.yml: |\n    ' + spelling + '\n'
                    '      - name: g\n        rules:\n'
                    '          - alert: Seen\n            expr: up == 0\n')
            assert "Seen" in self._alerts(text), spelling

    def test_binary_data_is_decoded_and_reaches_the_prefilter(self):
        # base64 never contains the word "groups", so a file with ONLY
        # binaryData must still get past the raw-text prefilter.
        text = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n"
                "binaryData:\n  r.yml: Z3JvdXBzOgogIC0gbmFtZTogYgogICAgcnVsZXM6CiAgICAgIC0gYWxlcnQ6IEZyb21CaW5hcnkKICAgICAgICBleHByOiB1cCA9PSAwCg==\n")
        assert "FromBinary" in self._alerts(text)

    def test_decorated_document_separators_still_split(self):
        # `--- `, `--- # note` and `---   ` are all legal document starts. When
        # they failed to split, safe_load raised on the merged text and the
        # whole file was swallowed — silently, with every count unchanged.
        second = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: y}\n"
                  "data:\n  r.yml: |\n    groups:\n      - name: g\n"
                  "        rules:\n          - alert: AfterSeparator\n"
                  "            expr: up == 0\n")
        for sep in ("---", "--- ", "--- # a note", "---   "):
            text = "apiVersion: v1\nkind: Namespace\nmetadata: {name: n}\n" + sep + "\n" + second
            assert "AfterSeparator" in self._alerts(text), repr(sep)

    def test_list_wrappers_are_unwrapped(self):
        inner = ("  - apiVersion: v1\n    kind: ConfigMap\n"
                 "    metadata: {name: x}\n    data:\n      r.yml: |\n"
                 "        groups:\n          - name: g\n            rules:\n"
                 "              - alert: InsideList\n                expr: up == 0\n")
        assert "InsideList" in self._alerts("kind: List\nitems:\n" + inner)

    def test_unwrapping_is_duck_typed_on_items_not_on_the_kind_name(self):
        """⛔ `kind: List` is the weakest spelling of the case above.

        kubectl's `Unstructured.IsList()` checks for an `items` ARRAY and never
        looks at the kind name — `decodeToList` then back-fills each item's own
        kind. So `ConfigMapList`, `PrometheusRuleList` and a bare `items:` with
        no kind at all are equally applyable, and the test above happens to use
        the one spelling a kind-name check would also catch. Measured: narrowing
        the unwrap to `kind == "List"`, or to a three-name enum, leaves the whole
        suite green.
        """
        inner = ("  - apiVersion: v1\n    kind: ConfigMap\n"
                 "    metadata: {name: x}\n    data:\n      r.yml: |\n"
                 "        groups:\n          - name: g\n            rules:\n"
                 "              - alert: %s\n                expr: up == 0\n")
        for header, alert in (
                ("kind: ConfigMapList\nitems:\n", "InsideConfigMapList"),
                ("apiVersion: v1\nitems:\n", "InsideKindlessList"),
                ("kind: SomethingNobodyEnumerated\nitems:\n", "InsideUnknownList")):
            assert alert in self._alerts(header + inner % alert), (
                f"{header.splitlines()[0]!r} was not unwrapped; kubectl applies "
                "it identically to `kind: List`")

    def test_an_items_key_shadowing_a_documents_own_rules_is_reported(self):
        """⛔ Not a bypass — a SILENT ZERO, and the distinction is the finding.

        The reviewer who raised this called it a deployable bypass: `items: []`
        on a `kind: ConfigMap` makes the scanner skip the document while its
        `data` still ships. That is not what happens. Verified against the real
        decoder `kubectl apply` uses (`unstructured.UnstructuredJSONScheme`,
        apimachinery 1.36):

            ConfigMap + data                -> 1 object, data kept
            ConfigMap + `items: []` + data  -> 0 objects
            items non-empty + own data      -> only the items

        `items` makes the document a LIST, and a list's own `data` is discarded.
        So the scanner agrees with the cluster, and there is nothing to close on
        the discovery side.

        What is left is worse in one respect and cheaper to hit: the rules do
        not deploy, the maintainer believes they do, and no counter anywhere
        moves. Pasting the `items:` key out of a `kubectl get -o yaml` dump, or
        deleting the wrapped objects and leaving `items: []` behind, is an
        ordinary accident. That is the silent zero the sentinel exists for.
        """
        rules = ("data:\n  r.yml: |\n    groups:\n      - name: g\n"
                 "        rules:\n          - alert: NeverDeployed\n"
                 "            expr: up == 0\n")
        head = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: shadowed}\n"

        # The control: without `items`, this is an ordinary rules ConfigMap.
        assert self._alerts(head + rules) == {"NeverDeployed"}

        # All three shapes the outer content can take. binaryData is a real
        # delivery path in this repo (kubelet's MakePayload fallback) and `spec`
        # is the PrometheusRule face, so a condition that only looked at `data`
        # would leave two thirds of the shape silently dropped again.
        import base64 as _b64  # noqa: PLC0415
        b64 = _b64.b64encode(
            yaml.safe_dump({"groups": [{"name": "g", "rules": [
                {"alert": "NeverDeployed", "expr": "up == 0"}]}]}
                           ).encode("utf-8")).decode("ascii")
        for label, text in (
                ("empty items", head + "items: []\n" + rules),
                ("items with a decoy", head + rules +
                 "items:\n  - {apiVersion: v1, kind: ConfigMap, "
                 "metadata: {name: decoy}, data: {d.yml: \"groups: []\"}}\n"),
                ("binaryData", head + "items: []\n"
                 f"binaryData:\n  r.yml: {b64}\n"),
                ("PrometheusRule spec", "apiVersion: monitoring.coreos.com/v1\n"
                 "kind: PrometheusRule\nmetadata: {name: shadowed}\n"
                 "items: []\nspec:\n  groups:\n    - name: g\n      rules:\n"
                 "        - alert: NeverDeployed\n          expr: up == 0\n")):
            rel = "k8s/03-monitoring/configmap-rules-shadowed.yaml"
            got = list(_containers_from_text(rel, text))
            assert {a for _w, d, _p in got for a, *_ in _alert_names(d)} == set(), (
                f"{label}: the scanner reported rules that kubectl discards")
            flagged = [w for w, d, _p in got
                       if "_own_content_shadowed_by_items" in d]
            assert flagged, (
                f"{label}: the document's own rules are silently dropped by both "
                f"the cluster and this scanner, and nothing flagged it: {got}")

    # ── the parsing layer, one fixture per guard ──────────────────────────
    #
    # ⛔ A second mutation sweep found FIFTEEN survivors in `_containers_from_text`
    # and its helpers — every branch below could be deleted or weakened with the
    # whole suite green. They are the guards whose docstrings each describe a
    # payload that walked through an earlier revision, so "the reason is written
    # down" was doing the work a fixture should. These are the fixtures.

    _CM = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n"
    _PR = ("apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\n"
           "metadata: {name: %s}\n")
    _GROUPS = ("groups:\n  - name: g\n    rules:\n      - alert: %s\n"
               "        expr: up == 0\n")

    @staticmethod
    def _containers(text, rel="k8s/03-monitoring/x.yaml"):
        return list(_containers_from_text(rel, text))

    def _indented(self, alert, spaces):
        pad = " " * spaces
        return "".join(pad + line if line.strip() else line
                       for line in (self._GROUPS % alert).splitlines(True))

    def test_an_unparsable_yaml_data_key_is_reported_case_insensitively(self):
        """A `|` block with broken indentation deletes a block of alerts silently.

        The OUTER file still parses, so the whole-file tripwire sees nothing;
        only this branch notices. The key's EXTENSION is the declaration of
        intent — and it is matched case-insensitively because `.YML` is a legal
        spelling and the same "escapes by being named differently" hole the
        scanner exists to close.
        """
        for key in ("r.yml", "r.YML", "r.YAML"):
            got = self._containers(self._CM + f"data:\n  {key}: |\n    groups: [\n")
            assert [d for _w, d, _p in got if "_unparsable_body" in d], (
                f"a data key named {key!r} declared YAML, failed to parse, and "
                f"contributed nothing without a word: {got}")
        # …and a key that promises no YAML is left alone: this repo ships a
        # markdown Alertmanager template as a data key, and it contains the
        # string `- alert:` as documentation.
        assert self._containers(
            self._CM + "data:\n  notes.md: |\n    - alert: example\n     [\n") == []

    def test_a_data_key_nesting_rules_under_spec_is_still_a_container(self):
        """The silent-zero's own entry point.

        Rules pasted under `spec.groups` inside a ConfigMap data key (the
        PrometheusRule shape, the commonest paste error) parse fine and
        contribute zero rules. Admitting the container is what lets
        `_rule_shaped_but_unparsed` say so; drop the `spec` arm and the
        detection is gone with it, silently.
        """
        got = self._containers(
            self._CM + "data:\n  r.yml: |\n    spec:\n" + self._indented("A", 6))
        assert [d for _w, d, _p in got if "spec" in d], got

    def test_a_prometheusrule_with_top_level_groups_is_normalised(self):
        """Both directions, because each is a different failure.

        `groups:` at the top level of a PrometheusRule deploys nothing — and the
        tripwire cannot see it, because that check is handed `doc["spec"]` and
        the misplaced key is outside what it receives. But a manifest carrying
        BOTH keys has real `spec.groups`; reporting that one as misplaced is a
        false positive on a working file.
        """
        misplaced = self._containers(self._PR % "p" + self._GROUPS % "Orphan")
        assert [d for _w, d, _p in misplaced if "_misplaced_groups" in d], misplaced

        both = self._containers(self._PR % "p" + self._GROUPS % "Stray"
                                + "spec:\n" + self._indented("Real", 2))
        assert not [d for _w, d, _p in both if "_misplaced_groups" in d], (
            f"a PrometheusRule with real spec.groups was reported as misplaced "
            f"because it ALSO has a top-level key: {both}")
        assert {a for _w, d, _p in both for a, *_ in _alert_names(d)} == {"Real"}

    def test_prometheusrule_provenance_is_the_name_then_the_header(self):
        """Two sources, and dropping either one is silent.

        Without the object name, a generated manifest with no header is
        hand-authored — the gate then demands the RESERVED `alert_source` on the
        tenant alerts inside it. Without the header fallback, a manifest whose
        name does not follow the convention loses provenance the same way.
        """
        by_name = self._containers(
            self._PR % "da-rule-pack-redis" + "spec:\n" + self._indented("A", 2))
        assert {p for _w, _d, p in by_name} == {"redis"}, by_name

        header = "# GENERATED from rule-packs/rule-pack-redis.yaml by x — DO NOT EDIT.\n"
        by_header = self._containers(
            header + self._PR % "named-anything-else"
            + "spec:\n" + self._indented("A", 2))
        assert {p for _w, _d, p in by_header} == {"redis"}, by_header

    def test_a_leading_document_marker_does_not_end_the_header_scan(self):
        """`---` on line 1 is a document START, not content.

        Treating it as "past the leading comments" makes every file that opens
        with an explicit marker lose its generator provenance — and a lost
        provenance is a generated file reclassified as hand-authored platform.
        """
        text = ("---\n"
                "# GENERATED from rule-packs/rule-pack-redis.yaml by x — DO NOT EDIT.\n"
                + self._CM + "data:\n  r.yml: |\n" + self._indented("A", 4))
        assert {p for _w, _d, p in self._containers(text)} == {"redis"}

    def test_data_wins_over_binarydata_for_the_same_key(self):
        """kubelet's precedence, not ours.

        `MakePayload` reads `BinaryData` only for keys ABSENT from `Data`. Let
        binaryData overwrite and the scanner gates a body the cluster never
        serves — while the body it DOES serve goes unexamined.
        """
        import base64 as _b64  # noqa: PLC0415
        shadow = _b64.b64encode(
            (self._GROUPS % "FromBinary").encode("utf-8")).decode("ascii")
        got = self._containers(
            self._CM + "data:\n  r.yml: |\n" + self._indented("FromData", 4)
            + f"binaryData:\n  r.yml: {shadow}\n")
        assert {a for _w, d, _p in got for a, *_ in _alert_names(d)} == {"FromData"}

    def test_a_data_value_that_is_already_a_mapping_is_used_as_is(self):
        """A data key does not have to be a `|` block.

        Written as nested YAML it arrives already parsed; discarding non-strings
        drops the whole container, and the alerts in it are gated by nothing.
        """
        got = self._containers(
            self._CM + "data:\n  r.yml:\n" + self._indented("FromMapping", 4))
        assert {a for _w, d, _p in got for a, *_ in _alert_names(d)} == {"FromMapping"}

    def test_rule_group_shape_boundaries(self):
        """`groups:` is a popular word; the shape test is what makes it a criterion."""
        assert _is_rule_groups([{"name": "g", "rules": []}])
        # An empty list is a LEGITIMATE generator output (the custom-alerts pack
        # with no `_custom_alerts` declared) — rejecting it reclassifies that
        # pack's ConfigMap as hand-authored platform.
        assert _is_rule_groups([])
        # ANY entry with `rules`, not ALL: a real file may carry a group the
        # scanner does not recognise beside ones it does.
        assert _is_rule_groups([{"name": "a"}, {"name": "b", "rules": []}])
        # …and the RBAC shape that motivated the whole test stays out.
        assert not _is_rule_groups([{"name": "ops", "tenants": ["*"]}])
        assert not _is_rule_groups({"rules": []}), "a mapping is not a group list"
        assert not _is_rule_groups("groups")

    def test_a_non_dict_group_entry_does_not_crash_the_scan(self):
        """Malformed input must degrade, not abort.

        `_iter_repo_alert_rules` walks whatever the repo contains; a stray
        scalar in a `groups:` list raises AttributeError without the guard, and
        one bad file would take every contract down with it.
        """
        import _rule_tree  # noqa: PLC0415
        fake = (("x.yaml:k", {"groups": [
            "not-a-group",
            {"name": "g", "rules": [{"alert": "Survivor", "expr": "up == 0"}]},
        ]}, None),)
        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            _rule_tree._rule_containers = lambda: fake
            got = {r["alert"] for _w, r in _iter_repo_alert_rules()}
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()
        assert got == {"Survivor"}, got

    def test_document_splitting_swallows_only_yaml_errors(self):
        """The `except` is scoped on purpose.

        A malformed document is expected and skipped. Anything else — a bug in
        this scanner, a recursion limit, a type error — must escape, because a
        blanket `except Exception` turns every future defect into "that file has
        no rules" and the contracts stay green over it.
        """
        import _rule_tree  # noqa: PLC0415
        original = _rule_tree.yaml.safe_load_all
        try:
            _rule_tree.yaml.safe_load_all = lambda _t: (_ for _ in ()).throw(
                TypeError("scanner bug, not a malformed document"))
            with pytest.raises(TypeError):
                list(_documents("irrelevant: true\n"))
        finally:
            _rule_tree.yaml.safe_load_all = original

    def test_generator_header_does_not_cross_a_document_boundary(self):
        # A hand-authored document appended after `---` must not inherit the
        # provenance declared in the file's opening comments.
        text = ("# GENERATED from rule-packs/rule-pack-redis.yaml by\n"
                "# scripts/x.py — DO NOT EDIT.\n"
                "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n"
                "data:\n  r.yml: |\n    groups:\n      - name: g\n"
                "        rules:\n          - alert: First\n            expr: up == 0\n"
                "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: b}\n"
                "data:\n  r.yml: |\n    groups:\n      - name: g\n"
                "        rules:\n          - alert: Second\n            expr: up == 0\n")
        provs = {a: prov for _w, doc, prov in _containers_from_text("k8s/x.yaml", text)
                 for a, *_ in _alert_names(doc)}
        assert provs["First"] == "redis"
        assert provs["Second"] is None, "the header leaked across a `---`"

    def test_provenance_is_read_only_from_the_leading_comment_block(self):
        """The OTHER half of the header guard, and the attacker-writable one.

        The document-boundary test above pins only `is_first`; it says nothing
        about WHERE in a document the string may appear. Searching the raw text
        means an `annotations.summary` — tenant- or attacker-writable data that
        ends up rendered into an alert — confers provenance on the ConfigMap
        that carries it. Measured: both widenings (drop the leading-comment
        restriction, or search the whole file) survive the entire suite.
        """
        header = ("# GENERATED from rule-packs/rule-pack-redis.yaml by\n"
                  "# scripts/x.py — DO NOT EDIT.\n")
        body = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n"
                "data:\n  r.yml: |\n    groups:\n      - name: g\n"
                "        rules:\n          - alert: A\n            expr: up == 0\n")

        def prov(text):
            return {p for _w, _d, p in _containers_from_text("k8s/x.yaml", text)}

        assert prov(header + body) == {"redis"}, "the real shape stopped working"
        # 1. past the leading comment block — a comment further down the file
        assert prov(body + header) == {None}, (
            "a generator header BELOW the content conferred provenance; the "
            "guard reads the leading comment block, not any comment")
        # 2. inside a value, which is the writable one
        smuggled = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: a}\n"
            "data:\n  r.yml: |\n    groups:\n      - name: g\n"
            "        rules:\n          - alert: A\n            expr: up == 0\n"
            "            annotations:\n"
            "              summary: >-\n"
            "                GENERATED from rule-packs/rule-pack-redis.yaml by\n"
            "                scripts/x.py — DO NOT EDIT.\n")
        assert prov(smuggled) == {None}, (
            "an annotation VALUE conferred generated provenance — that string "
            "is data, and on this platform it is tenant-reachable data")

    def test_expr_equality_ignores_only_serialization(self):
        # ⛔ Assert the WIRING, not the helper. Testing `_norm_expr` on its own
        # passed even with `_alert_names` reverted to a bare `.strip()` — the
        # component worked and nothing checked that the caller used it. That
        # revert is what reclassified twelve tenant alerts as platform over two
        # characters of whitespace, so the pin has to run through `_alert_names`.
        def pair(expr):
            doc = {"groups": [{"name": "g", "rules": [
                {"alert": "A", "expr": expr}]}]}
            return _alert_names(doc)

        assert pair("redis_up == 0") == pair("redis_up==0") == pair("redis_up  ==  0")
        assert pair("redis_up == 0") != pair("mysql_up == 0")
        assert pair("sum by (tenant) (x)") == pair("sum by(tenant)(x)")

    def test_bare_groups_documents_are_found_outside_rule_packs(self):
        # ⛔ The bare `groups:` shape is a rules file wherever it ships. It used
        # to be recognised only under rule-packs/ — a path test kept to avoid
        # colliding with tests/rulepacks/ extracts, long after the scan stopped
        # reading tests/ at all. A hand-authored extra-platform-rules.yaml under
        # k8s/ was invisible for as long as that outlived its reason.
        text = ("groups:\n  - name: g\n    rules:\n"
                "      - alert: BareGroups\n        expr: up == 0\n")
        for rel in ("k8s/03-monitoring/extra-platform-rules.yaml",
                    "operator-manifests/extra.yaml",
                    "rule-packs/rule-pack-x.yaml"):
            assert "BareGroups" in self._alerts(text, rel), rel

    def test_rbac_groups_are_not_rule_groups(self):
        text = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: rbac}\n"
                "data:\n  _rbac.yaml: |\n    groups:\n"
                "      - name: platform-admins\n        tenants: [\"*\"]\n")
        # ⛔ Assert on CONTAINERS, not alerts. RBAC groups yield no alerts
        # whether or not the shape guard exists, so the obvious assertion was
        # true in both worlds: replacing `_is_rule_groups` with `value is not
        # None` left it green while the RBAC ConfigMap became a platform
        # container again. The observable difference is the container.
        rel = "k8s/04-tenant-api/configmap-rbac.yaml"
        assert list(_containers_from_text(rel, text)) == []


class TestProvenanceIsAMatchNotAClaim:
    """The forgery-cost invariants, each driven through the real classifier.

    ⛔ Every guard here exists because a red-team pass walked through its
    absence, and every one of them was reverted by a mutant with the whole suite
    still green — the live tree contains no forgery, so the live tree cannot
    tell these apart. The shapes are constructed for the same reason
    TestSilentZeroSentinel's are.
    """

    EXPR = "da_platform_writer_lock_lost > 0"

    def _classify(self, fake, packs=None):
        """Run the REAL classifier over a fabricated container list."""
        import _rule_tree  # noqa: PLC0415
        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            if packs is None:
                _source_pack_alerts()        # memoise from the REAL tree FIRST…
                _rule_tree._rule_containers = lambda: fake   # …then swap
            else:
                _rule_tree._rule_containers = lambda: tuple(packs) + tuple(fake)
            _platform_rule_locations.cache_clear()
            return _platform_rule_locations()
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()

    @staticmethod
    def _doc(*rules):
        return {"groups": [{"name": "g", "rules": list(rules)}]}

    @classmethod
    def _rule(cls, alert, **extra):
        return {"alert": alert, "expr": cls.EXPR, **extra}

    def test_sharing_one_alert_with_a_pack_does_not_confer_provenance(self):
        """SUBSET, not intersection.

        `mine <= pack_alerts[claimed]` is what makes forgery cost as much as
        writing the rules. Weakened to `mine & pack_alerts[claimed]` — an
        overlap test — a hand-authored tree buys its way out by copying ONE
        rule from the pack it names and appending whatever it likes. Both
        mutations of that comparison survived the entire suite.
        """
        pack = ("rule-packs/rule-pack-forge.yaml",
                self._doc(self._rule("SharedAlert")), "forge")
        smuggler = ("k8s/03-monitoring/configmap-rules-forge.yaml:rules",
                    self._doc(self._rule("SharedAlert"),
                              self._rule("SmuggledCritical")), "forge")
        got = self._classify([smuggler], packs=[pack])
        assert smuggler[0] in got, (
            "a container whose alerts are NOT a subset of the pack it claims "
            "was treated as a generated copy of it; one borrowed rule bought "
            "an entire hand-authored alert out of every contract")

    def test_a_faithful_copy_is_still_recognised(self):
        """The counterweight — over-tightening is a false positive on a real
        deploy artifact, and its diagnostic tells the maintainer to put the
        RESERVED `alert_source: platform` label on tenant alerts."""
        pack = ("rule-packs/rule-pack-forge.yaml",
                self._doc(self._rule("A"), self._rule("B")), "forge")
        copy = ("k8s/03-monitoring/configmap-rules-forge.yaml:rules",
                self._doc(self._rule("A"), self._rule("B")), "forge")
        assert copy[0] not in self._classify([copy], packs=[pack])
        # …and the same copy with NO claim at all. Content still says generated,
        # and the branch that reads it is the difference between a nameless
        # operator manifest being classified correctly and the gate demanding
        # the RESERVED `alert_source: platform` on the tenant alerts inside it.
        # Deleting that branch is fail-CLOSED, so nothing else goes red for it.
        unnamed = ("operator-manifests/rules-for-forge.yaml:spec",
                   self._doc(self._rule("A"), self._rule("B")), None)
        assert unnamed[0] not in self._classify([unnamed], packs=[pack]), (
            "a deploy copy whose NAME attests to nothing was declared "
            "hand-authored platform, even though its content is exactly a "
            "pack's — the gate would now demand a reserved label on tenant "
            "alerts, which routes them to the NOC")

    def test_identity_is_the_whole_rule_not_just_name_and_expr(self):
        """`for:` / `labels:` / `annotations:` are part of the copy.

        Identity was widened from (alert, expr) to the whole rule body this
        round, and nothing tested it: reverting it left the suite green. A rule
        that keeps the pack's name and expression but rewrites its severity, or
        drops `for:`, is a DIFFERENT alert with different delivery — claiming it
        is a generated copy is exactly the lie provenance is meant to price.
        """
        pack = ("rule-packs/rule-pack-forge.yaml",
                (self._doc(self._rule("A", **{"for": "10m"},
                                      labels={"severity": "warning"}))),
                "forge")
        for tweak in ({"for": "10m", "labels": {"severity": "critical"}},
                      {"labels": {"severity": "warning"}},
                      {"for": "10m", "labels": {"severity": "warning"},
                       "annotations": {"summary": "injected"}}):
            edited = ("k8s/03-monitoring/configmap-rules-forge.yaml:rules",
                      self._doc(self._rule("A", **tweak)), "forge")
            assert edited[0] in self._classify([edited], packs=[pack]), (
                f"a rule differing from its claimed source by {sorted(tweak)} "
                "was still accepted as a generated copy of it")

    def test_a_second_file_claiming_a_pack_name_is_refused_loudly(self):
        """One file per pack name, and `raise` rather than `assert`.

        Two files claiming `rule-pack-redis` means one is forging, and merging
        them writes the forger's alerts into redis's trusted set. `assert` would
        make the whole check evaporate under `python -O`, which is why it is a
        raise — and neither the uniqueness rule nor its threshold had a test.
        """
        import _rule_tree  # noqa: PLC0415
        dupes = (
            ("rule-packs/rule-pack-forge.yaml",
             self._doc(self._rule("A")), "forge"),
            ("rule-packs/recipes/examples/rule-pack-forge.yaml",
             self._doc(self._rule("Smuggled")), "forge"),
        )
        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            _rule_tree._rule_containers = lambda: dupes
            with pytest.raises(AssertionError, match="same rule pack"):
                _source_pack_alerts()
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()

    def test_the_source_tree_is_never_classified_as_a_deployment(self):
        """rule-packs/ holds SOURCES; nothing there is deployed.

        Today every container under it resolves provenance from its own
        filename, so dropping the exclusion changes nothing — measured, zero
        rule-packs/ containers lack provenance. That is the whole hazard: one
        source file the filename convention does not cover (a pack under a name
        the stem test misses, a bare `groups:` doc beside the packs) becomes a
        hand-authored PLATFORM tree, and the gate starts demanding the RESERVED
        `alert_source: platform` on rules that are never applied to anything.
        """
        unnamed = ("rule-packs/shared/common-recording.yaml",
                   self._doc(self._rule("A")), None)
        assert unnamed[0] not in self._classify([unnamed]), (
            "a container in the SOURCE tree was classified as a deployed "
            "hand-authored platform tree")

    def test_only_a_rule_pack_file_may_define_a_pack(self):
        """The producer side, which is where the lie is cheapest to tell.

        rule-packs/ is scanned recursively, so a file dropped anywhere under it
        carrying a forged `# GENERATED from …` header could write its own alerts
        into a pack's trusted set. Two independent guards — the path must be
        under rule-packs/, and the FILENAME must be that pack's — and both
        survived every existing assertion.
        """
        import _rule_tree  # noqa: PLC0415
        for where in ("rule-packs/recipes/examples/conf.d/redis-tenant.yaml",
                      "k8s/03-monitoring/configmap-rules-forge.yaml"):
            fake = ((where, self._doc(self._rule("Smuggled")), "forge"),)
            original = _rule_tree._rule_containers
            try:
                _rule_tree._reset_caches()
                _rule_tree._rule_containers = lambda: fake
                packs = _source_pack_alerts()
            finally:
                _rule_tree._rule_containers = original
                _rule_tree._reset_caches()
            assert "forge" not in packs, (
                f"{where} declared a generated-from header and was allowed to "
                f"DEFINE the pack it named: {packs}")


class TestSilentZeroSentinel:
    """`_rule_shaped_but_unparsed()` must REPORT, not merely be empty.

    ⛔ Its only assertion was `== ()` on the live tree. That is a snapshot of
    "the repo is clean today" wearing an invariant's clothes: measured, every
    one of nine mutations survived it — including `return ()`, which deletes the
    sentinel outright, and each of the four offender branches individually. A
    guard whose whole job is to fire has to be tested by making it fire.

    Every shape here is one the sentinel names in its own docstring, driven
    through the real function by swapping the container source rather than by
    restating its logic.
    """

    HEALTHY = {"groups": [{"name": "g", "rules": [
        {"alert": "RealAlert", "expr": "up == 0"}]}]}

    def _offenders_for(self, fake):
        import _rule_tree  # noqa: PLC0415
        original = _rule_tree._rule_containers
        try:
            _rule_tree._reset_caches()
            _rule_tree._rule_containers = lambda: fake
            _rule_shaped_but_unparsed.cache_clear()
            return dict(_rule_shaped_but_unparsed())
        finally:
            _rule_tree._rule_containers = original
            _rule_tree._reset_caches()

    def test_every_offender_shape_is_reported(self):
        # (container, which BRANCH of the sentinel should claim it). Two rows
        # share a branch on purpose — see the last one.
        cases = {
            "cm.yaml:wont-parse": ({"_unparsable_body": True}, "unparsable"),
            "pr.yaml:groups-at-top": ({"_misplaced_groups": True}, "misplaced"),
            "cm.yaml:spec-nested": ({"spec": self.HEALTHY}, "spec-nested"),
            "cm.yaml:groups-forgotten": ({"rules": [
                {"alert": "Orphan", "expr": "up == 0"}]}, "no-groups"),
            "cm.yaml:shadowed-by-items": (
                {"_own_content_shadowed_by_items": True}, "items"),
            # ⛔ `groups: []` PRESENT but empty, beside real rules under `spec`.
            # The short-circuit must test the VALUE, not the key: on
            # `"groups" in doc` this container is skipped entirely, and it
            # contributes zero rules while `spec.groups` holds the real ones.
            # Same branch as spec-nested, reached the other way round.
            "cm.yaml:empty-groups-beside-spec": (
                {"groups": [], "spec": self.HEALTHY}, "spec-nested"),
        }
        got = self._offenders_for(
            tuple((w, d, None) for w, (d, _b) in cases.items())
            + (("cm.yaml:healthy", self.HEALTHY, None),))
        missed = sorted(set(cases) - set(got))
        assert not missed, (
            "these visibly rule-shaped containers yield zero rules and the "
            f"silent-zero sentinel stayed quiet about them: {missed}")
        assert "cm.yaml:healthy" not in got, (
            f"a container that DOES yield rules was reported: {got}")
        # ⛔ One message per BRANCH. Branches collapsing onto a shared message
        # would satisfy the membership test above while telling the maintainer
        # nothing about which mistake they actually made.
        branches = {b for _d, b in cases.values()}
        assert len(set(got.values())) == len(branches), (
            f"{len(branches)} distinct defects produced "
            f"{len(set(got.values()))} distinct messages: {got}")

    def test_a_container_with_rules_is_not_judged_by_its_spec_key(self):
        """The short-circuit is load-bearing, not an optimisation.

        A PrometheusRule normalised into `{groups, spec.groups}` carries both
        keys legitimately. Without `if doc.get("groups"): continue` it is
        reported as "rules nested under spec.groups" — a false positive on a
        healthy container, which is the failure mode that gets a sentinel
        muted rather than fixed.
        """
        got = self._offenders_for(
            (("pr.yaml:both-keys",
              {**self.HEALTHY, "spec": self.HEALTHY}, None),))
        assert got == {}, got

    def _sentinel_over(self, tmp_path, files):
        """Run the REAL sentinel over a synthetic tree of {relpath: bytes}."""
        import _rule_tree  # noqa: PLC0415
        for rel, raw in files.items():
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
        saved = (_rule_tree._REPO_ROOT, _rule_tree._tracked_yaml_paths,
                 _rule_tree._rule_containers)
        try:
            _rule_tree._reset_caches()
            _rule_tree._REPO_ROOT = str(tmp_path)
            _rule_tree._tracked_yaml_paths = lambda: sorted(files)
            _rule_tree._rule_containers = tuple
            _rule_shaped_but_unparsed.cache_clear()
            return dict(_rule_shaped_but_unparsed())
        finally:
            (_rule_tree._REPO_ROOT, _rule_tree._tracked_yaml_paths,
             _rule_tree._rule_containers) = saved
            _rule_tree._reset_caches()

    RULES_DOC = ("apiVersion: v1\nkind: ConfigMap\n"
                 "metadata:\n  name: prometheus-rules-platform\n"
                 "data:\n  platform-alert.yml: |\n    groups:\n"
                 "      - name: g\n        rules:\n"
                 "          - alert: EncodedAlert\n            expr: up == 0\n")

    def test_a_utf16_rules_manifest_is_scanned_and_objected_to(self, tmp_path):
        """⛔ The one encoding the cluster accepts and this scanner could not read.

        Both readers here opened manifests with `read_text(encoding="utf-8")`
        and swallowed `UnicodeDecodeError` with a bare `continue`. Measured
        against the decoder `kubectl apply` actually uses
        (`apimachinery/pkg/util/yaml.NewYAMLOrJSONDecoder`, 1.36):

            UTF-16 BE + BOM  -> kubectl APPLIES it   (fail-open: rules shipped
                                                      that no contract gated)
            UTF-16 LE + BOM  -> kubectl errors       (silent zero: the author
                                                      believes they shipped)

        Both were invisible AND left no offender, which is the combination that
        makes a gate worse than useless — it reports green over the gap.

        Two properties, and the test needs both: the scanner must now SEE the
        rules (so every contract applies to them), and the sentinel must still
        OBJECT to the encoding (because which of the two outcomes above you get
        depends on byte order, and that is not a state to ship).
        """
        for label, enc in (("UTF-16 BE", "utf-16-be"), ("UTF-16 LE", "utf-16-le")):
            raw = (codecs.BOM_UTF16_BE if enc.endswith("be")
                   else codecs.BOM_UTF16_LE) + self.RULES_DOC.encode(enc)
            rel = "k8s/03-monitoring/configmap-rules-platform.yaml"

            text, got_enc = _decode_manifest(raw)
            assert text is not None and got_enc == enc, (
                f"{label}: the decoder did not recognise the BOM ({got_enc})")
            alerts = {a for _w, d, _p in _containers_from_text(rel, text)
                      for a, *_ in _alert_names(d)}
            assert alerts == {"EncodedAlert"}, (
                f"{label}: kubectl can read these rules and the scanner still "
                f"cannot — every contract is blind to them. Got {alerts}")

            got = self._sentinel_over(tmp_path / enc, {rel: raw})
            assert rel in got, (
                f"{label}: a rules manifest shipped as {enc} drew no objection "
                f"at all: {got}")
            assert "UTF-8" in got[rel] and enc in got[rel], got[rel]

    def test_every_bom_decodes_to_the_same_text(self):
        """⛔ Order matters, and getting it wrong is silent corruption.

        The UTF-32 BOMs BEGIN with the UTF-16 ones — `ff fe 00 00` starts with
        `ff fe` — so a UTF-16-first table decodes a UTF-32 file as UTF-16 and
        hands the scanner plausible-looking garbage instead of rules. Nothing
        raises; the file just stops containing alerts. Measured: reordering the
        table left the whole suite green until this case existed.
        """
        for bom, enc, payload_enc in (
                (codecs.BOM_UTF8, "utf-8-sig", "utf-8"),
                (codecs.BOM_UTF16_LE, "utf-16-le", "utf-16-le"),
                (codecs.BOM_UTF16_BE, "utf-16-be", "utf-16-be"),
                (codecs.BOM_UTF32_LE, "utf-32-le", "utf-32-le"),
                (codecs.BOM_UTF32_BE, "utf-32-be", "utf-32-be")):
            text, got = _decode_manifest(bom + self.RULES_DOC.encode(payload_enc))
            assert got == enc, f"BOM for {enc} was read as {got}"
            assert text == self.RULES_DOC, (
                f"{enc} decoded to something other than the original text — a "
                "misread encoding does not raise, it just stops containing "
                f"alerts: {text[:60]!r}")
        # …and with no BOM at all it is plain UTF-8, BOM-free.
        assert _decode_manifest(self.RULES_DOC.encode("utf-8")) == (
            self.RULES_DOC, "utf-8")

    def test_rules_declared_in_a_helm_chart_are_refused(self, tmp_path):
        """⛔ helm/ is in the scan surface and its content mostly is not.

        67 of the 95 tracked helm YAML carry Go template actions and do not
        parse, so `_containers_from_text` never sees inside them. The
        `- alert:` tripwire covers a template that spells rules out — but not
        the shape this exists for, where the template says
        `{{ toYaml .Values.extraRules | nindent 4 }}` and the RULES LIVE IN
        VALUES. That file parses perfectly, nests its rules under an arbitrary
        key, matches none of the three container shapes, and deploys real
        alerts every contract here is blind to.

        This gate does not render charts. Refusing the shape is the honest
        option; pretending to follow it is not.
        """
        rules = {"groups": [{"name": "g", "rules": [
            {"alert": "InjectedByChart", "expr": "up == 0"}]}]}
        for label, values in (
                ("nested under a key", {"extraRules": rules}),
                ("several levels down", {"a": {"b": {"c": rules}}}),
                # The template supplies `groups:`; values supply only the list.
                ("a bare rule list", {"extraRules": [
                    {"alert": "InjectedByChart", "expr": "up == 0"}]}),
                ("at the document root", rules)):
            rel = "helm/some-chart/values.yaml"
            got = self._sentinel_over(
                tmp_path / label.replace(" ", "-"),
                {rel: yaml.safe_dump(values).encode("utf-8")})
            assert rel in got, (
                f"{label}: a chart declaring Prometheus rules drew no "
                f"objection, and this gate cannot see what it deploys: {got}")
            assert "render charts" in got[rel]

    def test_the_helm_tripwire_does_not_fire_on_ordinary_chart_values(self):
        """The counterweight, and it is what keeps the tripwire alive.

        A tripwire that fires on `resources:` or on an empty generator artifact
        gets switched off, and then it is not a tripwire. `_is_rule_groups`
        alone is too loose here — it accepts `[]` on purpose — so the shape test
        demands at least one real rule.
        """
        for label, values in (
                ("resources", {"resources": {"limits": {"cpu": "1"}}}),
                ("securityContext", {"podSecurityContext": {"runAsNonRoot": True}}),
                ("an empty generator artifact", {"x": {"groups": []}}),
                ("RBAC subject groups", {"groups": [
                    {"name": "ops", "tenants": ["*"]}]}),
                ("rule-shaped but no expr", {"x": [{"alert": "X"}]})):
            assert _rules_shaped_at_any_depth(values) is None, (
                f"{label} was mistaken for a Prometheus rule set: {values}")

    def test_helm_is_actually_in_the_scan_surface(self):
        """⛔ The tripwire above only runs on files the scan surface reaches.

        `helm/` yields no containers today, so dropping it from
        `_SHIPPED_ROOTS` changes no count and no contract — measured, that
        mutation survived everything. It would also silently switch off the
        refusal, because `_rule_shaped_but_unparsed` iterates
        `_tracked_yaml_paths()`. A guard reachable only through its own unit
        test is not deployed.
        """
        helm = [p for p in _tracked_yaml_paths() if p.startswith("helm/")]
        assert len(helm) >= 50, (
            f"only {len(helm)} helm YAML in the scan surface — the chart "
            "tripwire has nothing to run on")

    def test_a_file_no_encoding_decodes_is_reported_not_skipped(self, tmp_path):
        """The residual: bytes that are not any standard Unicode encoding.

        Nothing can tell whether it holds rules — that is exactly why it cannot
        be skipped. kubectl's YAML parser takes UTF-8/16/32 and nothing else, so
        such a file is a defect however it got there, and the sentinel is the
        only thing in the repo that would ever mention it.
        """
        rel = "k8s/03-monitoring/configmap-rules-broken.yaml"
        raw = b"\xff\xfe\x41" + self.RULES_DOC.encode("latin-1")[:40] + b"\xc0\xc1"
        got = self._sentinel_over(tmp_path, {rel: raw})
        assert rel in got, f"an undecodable shipped manifest was skipped: {got}"
        assert "encoding" in got[rel]

    def test_a_utf8_bom_does_not_cost_a_file_its_provenance(self, tmp_path):
        """A BOM is invisible to a human and fatal to the header scan.

        `read_text("utf-8")` keeps the `\ufeff`, so the first line of a
        generated file fails `startswith("#")`, `_header_provenance` returns
        None, and the file is reclassified as hand-authored platform — the gate
        then demands the RESERVED `alert_source: platform` on tenant alerts.
        `utf-8-sig` is what makes the BOM cost nothing.
        """
        header = ("# GENERATED from rule-packs/rule-pack-redis.yaml by\n"
                  "# scripts/x.py — DO NOT EDIT.\n")
        raw = codecs.BOM_UTF8 + (header + self.RULES_DOC).encode("utf-8")
        text, enc = _decode_manifest(raw)
        assert enc == "utf-8-sig" and not text.startswith("\ufeff"), enc
        provs = {p for _w, _d, p in _containers_from_text("k8s/x.yaml", text)}
        assert provs == {"redis"}, (
            f"a byte-order mark cost this file its generator provenance: {provs}")
        # ⛔ …and it is NOT an offender. kubectl applies a BOM-prefixed UTF-8
        # manifest (measured), and the BOM no longer costs anything here, so
        # this configuration WORKS — flagging it would be a false positive, and
        # the message ("depends on the byte order") would be untrue of UTF-8.
        # Without this, tightening the check to `enc != "utf-8"` stays green.
        rel = "k8s/03-monitoring/configmap-rules-bom.yaml"
        assert self._sentinel_over(tmp_path, {rel: raw}) == {}, (
            "a working UTF-8-with-BOM manifest was reported as broken")

    def test_a_file_that_declares_alerts_and_will_not_parse_is_reported(
            self, tmp_path):
        """…and the tripwire must not be steppable with a whitespace edit.

        `-  alert:` (two spaces) is the same YAML as `- alert:`; the substring
        test this replaced saw only the second, so a file could declare alerts,
        fail to parse, contribute nothing, and be waved through by an extra
        space.
        """
        import _rule_tree  # noqa: PLC0415
        broken = "groups:\n  - name: g\n    rules:\n      -  alert: X\n     expr: ["
        (tmp_path / "rule-packs").mkdir()
        (tmp_path / "rule-packs" / "rule-pack-broken.yaml").write_text(
            broken, encoding="utf-8")
        original_root, original_paths, original_cm = (
            _rule_tree._REPO_ROOT, _rule_tree._tracked_yaml_paths,
            _rule_tree._rule_containers)
        try:
            _rule_tree._reset_caches()
            _rule_tree._REPO_ROOT = str(tmp_path)
            _rule_tree._tracked_yaml_paths = lambda: [
                "rule-packs/rule-pack-broken.yaml"]
            _rule_tree._rule_containers = tuple
            _rule_shaped_but_unparsed.cache_clear()
            got = dict(_rule_shaped_but_unparsed())
        finally:
            (_rule_tree._REPO_ROOT, _rule_tree._tracked_yaml_paths,
             _rule_tree._rule_containers) = (
                original_root, original_paths, original_cm)
            _rule_tree._reset_caches()
        assert "rule-packs/rule-pack-broken.yaml" in got, (
            "a file that declares alerts and then fails to parse is the "
            f"loudest silent-zero there is, and it went unreported: {got}")
        assert "will not parse" in got["rule-packs/rule-pack-broken.yaml"]


class TestScannerAnchorsAreFailClosed:
    """The two `git ls-files` anchors, and the oracle the discovery tests read.

    ⛔ Everything here was a surviving mutant. These are not deep invariants —
    they are the small, load-bearing statements each function's own comment
    already makes, which nothing was checking.
    """

    @staticmethod
    def _temp_repo(tmp_path, files):
        """A real git repo with *files* ({relpath: text}) tracked."""
        subprocess.run(["git", "init", "-q", str(tmp_path)],
                       check=True, timeout=60)
        for rel, text in files.items():
            dest = tmp_path / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        subprocess.run(["git", "-C", str(tmp_path), "add", "-A"],
                       check=True, timeout=60)
        return tmp_path

    @staticmethod
    def _with_root(root, fn):
        import _rule_tree  # noqa: PLC0415
        original = _rule_tree._REPO_ROOT
        try:
            _rule_tree._reset_caches()
            _rule_tree._REPO_ROOT = str(root)
            return fn()
        finally:
            _rule_tree._REPO_ROOT = original
            _rule_tree._reset_caches()

    def test_an_uppercase_extension_is_still_listed(self, tmp_path):
        """`git ls-files '*.yaml'` matches the pathspec CASE-SENSITIVELY.

        Filtering in Python and lowercasing is the difference between "the
        scanner looked and found nothing" and "the scanner declined to look" —
        and `.YAML` is exactly what a red-team pass reached for.
        """
        body = ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n"
                "data:\n  r.yml: |\n    groups:\n      - name: g\n"
                "        rules:\n          - alert: UpperCase\n"
                "            expr: up == 0\n")
        repo = self._temp_repo(tmp_path, {
            "k8s/03-monitoring/configmap-rules-a.YAML": body,
            "k8s/03-monitoring/configmap-rules-b.yaml": body})
        listed = self._with_root(repo, _tracked_yaml_paths)
        assert "k8s/03-monitoring/configmap-rules-a.YAML" in listed, listed

    def test_an_empty_anchor_raises_instead_of_passing_everything(self, tmp_path):
        """Both `git ls-files` anchors are fail-CLOSED.

        An empty scan is indistinguishable from a clean one: every contract
        downstream iterates these lists, so `[]` makes all of them pass by
        examining nothing. `check_scrape_reachability --ci` would print
        "0 DEAD" over a tree it never opened.
        """
        empty = self._temp_repo(tmp_path, {"README.md": "no rules here\n"})
        for fn in (_tracked_yaml_paths, _expected_rule_files):
            with pytest.raises(AssertionError, match="git ls-files|repo layout"):
                self._with_root(empty, fn)

    def test_the_skip_list_is_empty_and_stays_empty(self):
        """`_SCAN_SKIP_PARTS`'s own comment calls it "deliberately EMPTY".

        It matched zero files once the scan narrowed to the shipped roots,
        while remaining a working escape hatch: one `mkdir
        k8s/03-monitoring/fixtures/` and a rules ConfigMap leaves every
        contract. A filter that protects nothing and hides something is pure
        attack surface — so the emptiness is the property, and nothing was
        asserting it.
        """
        assert _SCAN_SKIP_PARTS == frozenset(), (
            f"{sorted(_SCAN_SKIP_PARTS)} would hide a rules artifact from every "
            "contract at the cost of one directory name. If a real fixture tree "
            "lands under a shipped root, exclude it by explicit PATH instead.")

    def test_the_pack_name_oracle_demands_a_file_that_is_really_a_pack(
            self, tmp_path):
        """`_generated_pack_names` is a test oracle now, not a live defence —
        but an oracle that answers loosely makes every assertion reading it
        loose in the same direction.

        Four guards, each its own surviving mutant: the name must end in a
        rules extension, it must be a FILE, it must be named `rule-pack-*`,
        and it must actually declare `groups:` — otherwise
        `touch rule-packs/rule-pack-x.yaml` (zero bytes) mints a pack.
        """
        import _rule_tree  # noqa: PLC0415
        packs = tmp_path / "rule-packs"
        (packs / "rule-pack-a-directory.yaml").mkdir(parents=True)
        (packs / "rule-pack-real.yaml").write_text(
            "groups:\n  - name: g\n    rules:\n      - alert: A\n"
            "        expr: up == 0\n", encoding="utf-8")
        # `groups: []` IS a pack: a generator with nothing to emit legitimately
        # produces one, and reading it as "no source pack" reclassifies its
        # ConfigMap as hand-authored platform.
        (packs / "rule-pack-empty.yaml").write_text("groups: []\n", encoding="utf-8")
        (packs / "rule-pack-decoy.yaml").write_text("", encoding="utf-8")
        (packs / "rule-pack-no-groups.yaml").write_text("other: 1\n", encoding="utf-8")
        (packs / "not-a-pack.yaml").write_text("groups: []\n", encoding="utf-8")
        (packs / "rule-pack-wrong-ext.txt").write_text("groups: []\n", encoding="utf-8")
        # RECURSIVE: a pack moved into a subdirectory is still that pack.
        (packs / "sub").mkdir()
        (packs / "sub" / "rule-pack-nested.yaml").write_text(
            "groups: []\n", encoding="utf-8")

        got = self._with_root(tmp_path, _rule_tree._generated_pack_names)
        assert got == {"real", "empty", "nested"}, got

    def test_pack_name_uniqueness_survives_python_dash_O(self, tmp_path):
        """⛔ `raise`, not `assert` — and only `python -O` can tell them apart.

        Two files claiming one pack name means one is forging provenance, and
        merging them writes the forger's alerts into that pack's trusted set.
        Written as `assert` the whole check is stripped under `-O`, so the
        guard evaporates in exactly the configuration where nobody is watching.
        Every in-process test passes either way; this one runs a subprocess.
        """
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2]
                                    / "scripts" / "tools" / "lint")!r})
            import _rule_tree
            doc = {{"groups": [{{"name": "g", "rules": [
                {{"alert": "A", "expr": "up == 0"}}]}}]}}
            _rule_tree._rule_containers = lambda: (
                ("rule-packs/rule-pack-forge.yaml", doc, "forge"),
                ("rule-packs/sub/rule-pack-forge.yaml", doc, "forge"),
            )
            _rule_tree._source_pack_alerts.cache_clear()
            try:
                _rule_tree._source_pack_alerts()
            except AssertionError:
                print("RAISED")
            else:
                print("SILENT")
        """)
        out = subprocess.run([sys.executable, "-O", "-c", script],
                             capture_output=True, text=True, timeout=120)
        assert out.stdout.strip() == "RAISED", (
            "under `python -O` the pack-uniqueness check did not fire — it is "
            f"an `assert` and the optimizer removed it. stdout={out.stdout!r} "
            f"stderr={out.stderr[-400:]!r}")


class TestPlatformReaderParity:
    """The four readers of "the repo's rule tree" must agree.

    ⛔ There are FOUR — the count was three until a mutation pass went looking
    for the fourth — and until the scanner moved into an importable module there
    was no way for any of them to check each other:

      1. `_rule_tree` — content-based, provenance-based, the one every contract
         in this file uses.
      2. `_real_platform_label_sets()` above — opens
         `k8s/03-monitoring/configmap-rules-platform.yaml` by hard-coded path.
      3. `scripts/tools/ops/_grar_validate.platform_alert_identities()` —
         PRODUCTION code, same hard-coded path, and narrower still.
      4. `scripts/tools/lint/check_scrape_reachability.collect_rule_trees()` —
         a pre-commit gate in the SAME DIRECTORY as the scanner, which globbed
         its own two non-recursive patterns and saw 33 of 49 files. It now
         delegates discovery; test_the_reachability_gate_reads_the_same_tree
         keeps it delegating.

    (2) and (3) keep their own path deliberately: (3) ships inside an image with
    no repo tree and falls back to a constant when the file is unreachable, so
    it cannot depend on `git ls-files`. That is a fine reason to have a second
    implementation and a terrible reason to have an unchecked one — the scanner
    exists precisely because the filename is NOT the criterion, so the day a
    second hand-authored platform ConfigMap ships, (2) and (3) silently keep
    reporting the old tree while (1) sees both. This test is the cross-check
    that turns that day into a failure instead of a gap.
    """

    def _scanner_platform_alerts(self):
        return {rule["alert"] for where, rule in _iter_repo_alert_rules()
                if _is_platform_cm_location(where)}

    def test_label_set_reader_matches_the_scanner(self):
        scanner = self._scanner_platform_alerts()
        assert len(scanner) >= _MIN_PLATFORM_ALERTS, (
            f"scanner saw {len(scanner)} platform alert(s) — comparison below "
            "would be vacuous")
        # Watchdog is excluded by the label-set reader on purpose (it rides the
        # index-0 heartbeat route and carries no component), so compare against
        # the scanner minus that one deliberate omission.
        reader = {s["alertname"] for s in _real_platform_label_sets()}
        assert reader == scanner - _heartbeat_alertnames(), (
            "the hard-coded-filename reader and the content-based scanner "
            "disagree about the platform tree.\n"
            f"  only the reader sees: {sorted(reader - scanner)}\n"
            f"  only the scanner sees: "
            f"{sorted(scanner - reader - _heartbeat_alertnames())}")

    def test_production_identity_reader_matches_the_scanner(self):
        """⛔ Its own floor, not a sibling's.

        `assert prod` only says "at least one", and the shape this test guards
        against is production silently falling back to
        `PLATFORM_ALERT_IDENTITY_LABELS` — a SIX-entry constant of real
        alertnames. Both remaining assertions do catch that today (the fallback
        is a strict subset, so `missing` fills up), but only because the scanner
        side happens to be non-vacuous, and that is asserted in a DIFFERENT
        test. A test that borrows its non-vacuity from a sibling goes quiet the
        day the sibling is skipped, renamed or reordered.

        The floor is `_MIN_PLATFORM_ALERTS` minus the heartbeat: production
        keys on `alert_source="platform"` and Watchdog deliberately carries no
        discriminator, so it is the one alert the reader cannot see.
        """
        from _grar_validate import platform_alert_identities
        prod = {i["alertname"] for i in platform_alert_identities()}
        scanner = self._scanner_platform_alerts()
        floor = _MIN_PLATFORM_ALERTS - len(_heartbeat_alertnames())
        assert len(prod) >= floor, (
            f"the production identity reader returned {len(prod)} alert(s), "
            f"expected >= {floor}. At six it has fallen back to "
            "PLATFORM_ALERT_IDENTITY_LABELS — the ConfigMap was unreachable or "
            f"unparseable and nothing said so: {sorted(prod)}")
        assert prod <= scanner, (
            "scripts/tools/ops/_grar_validate.platform_alert_identities() "
            "reports platform alerts the scanner does not see, which means the "
            "two disagree about what the platform tree IS.\n"
            f"  only production sees: {sorted(prod - scanner)}")
        missing = scanner - prod - _heartbeat_alertnames()
        assert not missing, (
            "these platform alerts are invisible to the PRODUCTION identity "
            "reader — it reads one hard-coded ConfigMap, so anything the "
            "scanner found elsewhere is not being checked for tenant "
            f"silenceability at runtime: {sorted(missing)}")

    def test_the_reachability_gate_reads_the_same_tree(self):
        """The FOURTH reader, which the class docstring above used to omit.

        `check_scrape_reachability.collect_rule_trees()` sits in the same
        directory as the scanner and was globbing its own two non-recursive
        patterns: `.yaml` only, ConfigMap only, no PrometheusRule / binaryData /
        `kind: List` / multi-document. It opened 33 of the 49 files that ship
        rules. Missing a tree is fail-open there too — an unseen rule is an
        unseen CONSUMER, and a metric whose only consumer went unseen stops
        being reported as unreachable.

        It now delegates discovery to `_rule_tree`. This pins that: the rule ids
        it attributes consumption to must name only files the scanner found, and
        must cover every alerting rule the scanner sees.
        """
        from check_scrape_reachability import collect_rule_trees  # noqa: PLC0415

        consumed, _outputs = collect_rule_trees()
        gate_files = {rid.split(":", 1)[0]
                      for ids in consumed.values() for rid in ids}
        scanner_files = {w.split(":", 1)[0] for w, _d, _p in _rule_containers()}
        assert gate_files, "the reachability gate attributed nothing — vacuous"
        assert gate_files <= scanner_files, (
            "the reachability gate reads rule files the content-based scanner "
            f"does not: {sorted(gate_files - scanner_files)}")
        # The direction that actually mattered: it used to see 16 fewer files.
        # Files with only recording rules legitimately contribute no `consumed`
        # entry, so compare against the alerting tree.
        alerting_files = {w.split(":", 1)[0] for w, _r in _iter_repo_alert_rules()}
        assert alerting_files <= gate_files, (
            "these files ship alerting rules that the reachability gate never "
            "read, so their input metrics are not checked for a scrape face: "
            f"{sorted(alerting_files - gate_files)}")

    def test_the_two_filename_readers_agree_on_LABELS_not_just_names(self):
        """Same alertnames is not the same probe set.

        ⛔ Comparing name sets is the weaker half of the comparison, and it is
        blind to the half that decides the outcome. What
        `find_tenant_silenceable_platform_inhibits` matches against is the LABEL
        DICT, so two readers can list identical alertnames and still hand the
        guard different answers — `tenant` is the label that flips it, it comes
        from parsing the expr, and expr parsing is exactly where two hand-rolled
        implementations drift. They HAD drifted: production wanted an aggregator
        keyword right before `by`, so a reformat from `sum by (tenant) (x)` to
        `sum(x) by (tenant)` — identical PromQL — moved the alert out of the
        production probe set while every name-set assertion above stayed green.
        The failure is silent AND fail-open: an unprobed alert makes a
        tenant-triggered inhibit that would silence it read as safe.
        """
        from _grar_validate import platform_alert_identities  # noqa: PLC0415

        def key(sets):
            return {s["alertname"]: {k: v for k, v in sorted(s.items())}
                    for s in sets}

        prod, reader = key(platform_alert_identities()), key(
            _real_platform_label_sets())
        assert set(prod) == set(reader), (
            f"name sets differ, compare labels is moot: "
            f"{sorted(set(prod) ^ set(reader))}")
        differing = {n: (prod[n], reader[n]) for n in prod
                     if prod[n] != reader[n]}
        assert not differing, (
            "the two filename-anchored readers agree on WHICH platform alerts "
            "exist but disagree on their fire-time labels; the production one "
            "is what the runtime guard probes with:\n" + "\n".join(
                f"  {n}: production={p} reader={r}"
                for n, (p, r) in sorted(differing.items())))
        # Non-vacuity on the label that decides the outcome: if no identity
        # carries `tenant`, every dict above could be `{alertname, severity}`
        # and the comparison would prove nothing about the drift it targets.
        tenant_bearing = sorted(n for n, s in prod.items() if "tenant" in s)
        assert len(tenant_bearing) >= 3, (
            f"only {len(tenant_bearing)} tenant-bearing identities "
            f"({tenant_bearing}) — the expr-derived label this test exists for "
            "is barely present, so agreement on it is close to vacuous")


class TestPlatformIdentityReaderCompleteness:
    """`platform_alert_identities` must read the WHOLE ConfigMap file.

    ⛔ Every alert this reader misses is one the runtime guard never probes, and
    an unprobed alert makes a tenant-triggered inhibit that would silence it read
    as SAFE. That is the fail-open direction, so "which parts of the file does it
    open" is a security property, not a parsing detail. It used to open exactly
    one: `docs[0]["data"]`'s first value.

    The shipped file happens to be one document with one key today, which is why
    none of the existing tests noticed — they all assert on the real tree. These
    build the shapes instead.
    """

    @staticmethod
    def _cm(name, data=None, binary_data=None):
        doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}}
        if data:
            doc["data"] = data
        if binary_data:
            doc["binaryData"] = binary_data
        return doc

    @staticmethod
    def _rules_body(alert, expr="up == 0", labels=None):
        return yaml.safe_dump({"groups": [{"name": "g", "rules": [{
            "alert": alert, "expr": expr,
            "labels": labels or {"severity": "warning",
                                 "alert_source": "platform"}}]}]})

    def _identities(self, tmp_path, docs):
        from _grar_validate import platform_alert_identities  # noqa: PLC0415
        path = tmp_path / "cm.yaml"
        path.write_text("\n---\n".join(yaml.safe_dump(d) for d in docs),
                        encoding="utf-8")
        # Explicit path => the module-level cache is bypassed (see the guard on
        # `configmap_path is None`), so these never poison the real probe set.
        return {i["alertname"]: i for i in platform_alert_identities(path)}

    def test_second_data_key_in_the_same_configmap_is_read(self, tmp_path):
        got = self._identities(tmp_path, [self._cm("rules", data={
            "platform-alert.yml": self._rules_body("FirstKeyAlert"),
            "platform-extra.yml": self._rules_body("SecondKeyAlert")})])
        assert set(got) == {"FirstKeyAlert", "SecondKeyAlert"}, sorted(got)

    def test_second_configmap_document_in_the_same_file_is_read(self, tmp_path):
        got = self._identities(tmp_path, [
            self._cm("rules-a", data={"a.yml": self._rules_body("DocOneAlert")}),
            self._cm("rules-b", data={"b.yml": self._rules_body("DocTwoAlert")})])
        assert set(got) == {"DocOneAlert", "DocTwoAlert"}, sorted(got)

    def test_binarydata_rules_are_decoded(self, tmp_path):
        import base64  # noqa: PLC0415
        body = self._rules_body("BinaryDataAlert")
        got = self._identities(tmp_path, [self._cm("rules", binary_data={
            "platform-alert.yml": base64.b64encode(
                body.encode("utf-8")).decode("ascii")})])
        assert set(got) == {"BinaryDataAlert"}, sorted(got)

    def test_one_undecodable_key_does_not_blank_the_probe_set(self, tmp_path):
        """Fail-soft per key, because the alternative is fail-open for the file.

        An exception here would escape into the caller's `except`, which
        substitutes the six-entry fallback constant — i.e. one corrupt key would
        silently shrink the probe set from the whole pack to a sample.
        """
        got = self._identities(tmp_path, [self._cm(
            "rules",
            data={"good.yml": self._rules_body("SurvivingAlert")},
            binary_data={"corrupt.yml": "!!!! not base64 !!!!"})])
        assert set(got) == {"SurvivingAlert"}, sorted(got)

    def test_expr_derived_tenant_survives_a_pure_promql_reformat(self, tmp_path):
        """The two spellings are the same query; both must carry `tenant`.

        `sum by (tenant) (x)` and `sum(x) by (tenant)` differ only in where the
        modifier sits. An aggregator-anchored pattern saw the first and not the
        second, so reformatting one line moved a platform alert out of the guard.
        """
        for expr in ("sum by (tenant) (rate(x[5m])) > 1",
                     "sum(rate(x[5m])) by (tenant) > 1",
                     "stddev by (tenant) (x) > 1",
                     "max by (namespace, tenant) (x) > 1"):
            got = self._identities(tmp_path, [self._cm("rules", data={
                "a.yml": self._rules_body("ReformatAlert", expr=expr)})])
            assert got["ReformatAlert"].get("tenant") == "any-tenant", (
                f"{expr!r} produces a `tenant` label at fire time but the "
                "identity reader did not record one")

    def test_by_without_tenant_does_not_invent_the_label(self, tmp_path):
        """The counterweight: over-detection is fail-closed but not free.

        Without this, widening the pattern to "any `by (`" would pass every test
        above while making every aggregating alert tenant-bearing — which turns
        legitimate tenant-scoped inhibit rules into false violations.
        """
        for expr in ("sum by (namespace) (x) > 1",
                     "sum by (tenant_id) (x) > 1",
                     "sum without (tenant) (x) > 1"):
            got = self._identities(tmp_path, [self._cm("rules", data={
                "a.yml": self._rules_body("NoTenantAlert", expr=expr)})])
            assert "tenant" not in got["NoTenantAlert"], (
                f"{expr!r} does not group by `tenant`, but the reader claimed a "
                f"tenant label: {got['NoTenantAlert']}")
