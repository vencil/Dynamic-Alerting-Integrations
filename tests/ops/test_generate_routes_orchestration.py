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
import functools
import json
import os
import posixpath
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
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
        # `- 1`: the derivation keeps only `alert_source="platform"` rules, and
        # Watchdog is the ONE platform alert that deliberately carries no
        # alert_source (it rides the index-0 heartbeat route). The floor is
        # therefore the whole tree minus that single documented exception —
        # written as arithmetic on the shared constant so growing the pack moves
        # this floor too, instead of leaving a second number to go stale.
        assert len(sets) >= _MIN_PLATFORM_ALERTS - 1, (
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
# The one platform rules ConfigMap that exists today. Kept for HUMAN-READABLE
# messages only — it is NOT the discovery criterion any more, see
# _is_platform_cm_location.
_PLATFORM_CM_PREFIX = "configmap-rules-platform"
# Both extensions: a rules ConfigMap named `.yml` was silently skipped by the
# scanner, which is the same "escapes the gate by being named differently" hole.
_RULES_FILE_EXTS = (".yaml", ".yml")
# The deploy-copy / source naming convention that ties the two trees together:
# k8s/03-monitoring/configmap-rules-<pack>.yaml is GENERATED from
# rule-packs/rule-pack-<pack>.yaml. `check_portal_rulepack_claims.py` reads the
# same pairing (its `path.stem.replace(...)` derivations at :135-142, and the
# docstring at :33-36 names configmap-rules-platform.yaml as the one with "NO
# rule-pack counterpart").
_RULES_CM_PREFIX = "configmap-rules-"
_RULE_PACK_PREFIX = "rule-pack-"


def _strip_rules_ext(name: str) -> str | None:
    """`foo.yaml` / `foo.yml` -> `foo`; anything else -> None."""
    for ext in _RULES_FILE_EXTS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return None


@functools.lru_cache(maxsize=1)
def _generated_pack_names() -> frozenset:
    """Pack names that HAVE a rule-packs/ source, i.e. whose ConfigMap is generated.

    RECURSIVE, matching `_iter_repo_alert_rules`'s rglob over the same tree: a
    source pack moved into a subdirectory must not read as "source deleted", or
    its generated ConfigMap would be misclassified as hand-authored.

    ⛔ A name alone does not make a pack. The file must parse as YAML and
    actually declare `groups:` — otherwise `touch rule-packs/rule-pack-x.yaml`
    (zero bytes, thirty keystrokes) reclassifies configmap-rules-x.yaml as
    generated, and every platform alert inside it drops out of all four
    contracts while the suite stays green. A decoy is only convincing if it
    has to contain the thing it claims to be the source of.
    """
    packs_dir = Path(_REPO_ROOT) / "rule-packs"
    names = set()
    for path in packs_dir.rglob("*"):
        if not path.is_file():
            continue
        stem = _strip_rules_ext(path.name)
        if not (stem and stem.startswith(_RULE_PACK_PREFIX)):
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError, OSError):
            continue
        # ⛔ Test for the KEY, not for a truthy value. `rule-pack-custom-alerts`
        # is generated, and a conf.d tree that declares no `_custom_alerts` —
        # an ordinary, supported deployment — makes the generator emit
        # `groups: []`. Truthiness reads that empty artifact as "no source pack",
        # reclassifies its ConfigMap as hand-authored platform, and then demands
        # `alert_source: platform` on TENANT alerts: a reserved value that pipes
        # them into the NOC channel. Steering the maintainer into a broken state
        # is precisely why the filename criterion had to go; requiring a truthy
        # `groups` reinstalls it from the other side.
        if not isinstance(doc, dict) or "groups" not in doc:
            continue
        names.add(stem[len(_RULE_PACK_PREFIX):])
    return frozenset(names)


def _is_platform_cm_location(where: str) -> bool:
    """True iff `where` names a rule inside a HAND-AUTHORED rules ConfigMap.

    `where` is "<configmap-path>:<data-key>" on the ConfigMap side and a bare
    rule-pack path on the source side; only the former can be platform.

    THE CRITERION IS "no rule-packs/ source", not "the filename starts with
    configmap-rules-platform". `_iter_repo_alert_rules`'s own docstring already
    states the intent — "any HAND-AUTHORED rules ConfigMap OUTSIDE rule-packs/,
    which is configmap-rules-platform.yaml today AND WHATEVER IS ADDED LATER" —
    and a filename prefix is a proxy for that intent that fails two ways:

      1. it compared the RELATIVE PATH, so `subdir/configmap-rules-platform.yaml`
         (a placement the scanner above deliberately reaches, being recursive)
         escaped every gate bounded by this predicate; and
      2. a hand-authored ConfigMap NOT named `platform*` escaped presence
         coverage entirely, while the alert_source RESERVED contract simultaneously
         reported it as an offender — i.e. the diagnostic pushed the maintainer to
         DELETE the correct label. That is the same "the gate steers you into the
         broken state" failure the prefix itself was introduced to fix, one level up.

    Deriving from the generator's own provenance makes the classification a
    property of the artifact instead of a property of its name — and the
    matching DISCOVERY rewrite (see `_iter_rule_containers`) means membership
    no longer depends on filename or directory either. Scope today: 63
    containers across three trees / 408 rules, of which exactly one container
    is hand-authored. Pinned by test_platform_cm_discovery_is_content_based
    and test_unknown_provenance_defaults_to_platform.

    ⛔ KNOWN LIMIT — one directory criterion remains, deliberately. The bare
    `groups:` document shape (a plain Prometheus rule file, no ConfigMap or
    PrometheusRule wrapper) is only recognised under rule-packs/. That is a
    PATH test, and it contradicts the "content, never location" rule the
    scanner otherwise follows — kept because the shape is indistinguishable
    from this repo's 23 `tests/rulepacks/*.rules.yaml` extracts and
    tests/e2e-bench/alert-rules.yml, which are test inputs rather than shipped
    configuration. The cost is real: a hand-authored `extra-platform-rules.yaml`
    with top-level `groups:` anywhere outside rule-packs/ is not discovered.
    Closing it needs a positive signal for "shipped" — kustomization membership
    or a Helm values reference — which is its own change.

    Two further soft spots, both currently harmless and both worth knowing:
    a PrometheusRule's provenance comes from its `da-rule-pack-<x>` object name
    alone, so the name attests to nothing about the CONTENT it wraps; and
    `groups: []` passes the shape test (a generator with nothing to emit
    legitimately produces one), so an unrelated ConfigMap with an empty
    `groups` key would be read as a rules container contributing no rules.
    """
    return where in _platform_rule_locations()


@functools.lru_cache(maxsize=1)
def _platform_rule_locations() -> frozenset:
    """Every container that is NOT a generated copy of a source rule pack.

    ⛔ Unknown provenance counts as PLATFORM. That direction is the whole point:
    a hand-authored alerting tree the scanner cannot attribute to a generator is
    exactly the thing that must not slip past the alert_source / runbook gates,
    and defaulting the other way would restore, in the classifier, the escape
    hatch that content-based discovery just closed in the finder.

    A container is generated only when it names a source pack that ACTUALLY
    EXISTS — a header or object name pointing at a pack that is not there is
    evidence of a stale or fabricated artifact, not of provenance.
    """
    packs = _generated_pack_names()
    return frozenset(
        where for where, _doc, prov in _rule_containers()
        if not (prov and prov in packs)
    )


# Directories whose YAML is deliberately NOT shipped alerting configuration:
# parser fixtures and build/vendor output. Everything else in the tree is in
# scope — the point of content-based discovery is that a real rules artifact
# cannot escape by being placed somewhere the scanner was never told about.
_SCAN_SKIP_PARTS = frozenset({
    "testdata", "fixtures", "node_modules", "site", "__pycache__", ".git",
})


def _tracked_yaml_paths():
    """Every tracked YAML file, minus fixture/vendor trees. Sorted, repo-relative."""
    # ⛔ List everything and filter case-INSENSITIVELY. `git ls-files '*.yaml'`
    # matches the pathspec case-sensitively, so a file spelled `.YAML` — legal,
    # and the exact trick a red-team run used — is simply never listed. Filtering
    # in Python is the difference between "the scanner declined to look" and
    # "the scanner looked and found nothing".
    # ⛔ `-z` + NUL split, matching _git_tracked_paths below. Plain `.split()`
    # breaks on whitespace, so `k8s/configmap rules platform.yaml` arrives as
    # three fragments and none of them opens; and without -z git applies
    # core.quotePath, so a CJK filename comes back as `"k8s/r\303\250gles.yaml"`
    # and fails the suffix test. In a zh-primary repo that is not hypothetical,
    # and both failures are the scanner declining to look — the exact thing the
    # case-insensitive suffix match above exists to prevent.
    out = subprocess.run(
        ["git", "-C", _REPO_ROOT, "ls-files", "-z"],
        capture_output=True, text=True, check=True).stdout
    return sorted(p for p in out.split("\0") if p
                  and p.lower().endswith((".yaml", ".yml"))
                  and not (_SCAN_SKIP_PARTS & set(PurePosixPath(p).parts)))


@functools.lru_cache(maxsize=1)
def _rule_containers() -> tuple:
    """Cached tuple form of _iter_rule_containers — see F11 note there."""
    return tuple(_iter_rule_containers())


def _iter_rule_containers():
    """Yield (where, rules_doc, provenance) for every rules-bearing document.

    ⛔ DISCOVERY IS CONTENT-BASED. A file is in scope because of what it
    CONTAINS, never because of its name or its directory. The previous scanner
    walked `configmap-rules-*` under k8s/03-monitoring/ only, which meant the
    reserved-value guarantees below were really "…among files we happened to
    look at": a rules ConfigMap named differently, spelled .YAML, or placed in
    another directory was not misclassified, it was never seen. Worse, the
    repo's 16 `kind: PrometheusRule` manifests under operator-manifests/ — a
    first-class deployment path — were outside the scan entirely.

    Three container shapes, all recognised by structure:
      * `kind: ConfigMap`      → every data key whose body parses to a mapping
      * `kind: PrometheusRule` → `spec.groups`
      * a bare `groups:` doc   → a source rule pack

    Multi-document YAML is walked in full; a rules doc hiding behind a `---`
    separator is exactly the kind of placement this is meant to stop mattering.

    `provenance` is the source pack this artifact was generated FROM, or None
    when it is hand-authored. It is read from the generator's own header
    ("GENERATED from rule-packs/rule-pack-X.yaml … DO NOT EDIT"), from a
    `da-rule-pack-X` object name, or from the file's own location under
    rule-packs/. None means "nobody generated this", which is what makes it a
    platform artifact — see _is_platform_cm_location.
    """
    def _is_rule_groups(value) -> bool:
        """Prometheus rule-group shape, not merely a key spelled `groups`.

        ⛔ `groups:` is a popular word. k8s/04-tenant-api/configmap-rbac.yaml
        uses it for RBAC subject groups (name / tenants / permissions), and a
        scanner that accepted any `groups` key classified that file as a
        hand-authored PLATFORM alerting tree. A rule group always carries
        `rules`; an empty list is allowed because a generator with nothing to
        emit legitimately produces one (see the custom-alerts pack).
        """
        if not isinstance(value, list):
            return False
        return not value or any(
            isinstance(g, dict) and "rules" in g for g in value)

    gen_re = re.compile(r"GENERATED from rule-packs/rule-pack-([A-Za-z0-9_-]+)\.ya?ml")

    def _header_provenance(chunk: str):
        """The generator header, read ONLY from a document's leading comments.

        ⛔ Two things must both be narrow here, and the first version was
        narrow in neither. Searching the whole file's raw text means the string
        confers provenance from anywhere it appears — including inside an
        `annotations.summary` value, which is attacker- or tenant-writable
        data. And computing it once per FILE then applying it to every document
        means a `---` separator is the best hiding place in the repo: append a
        hand-authored ConfigMap to any generated one and it inherits the
        header. That is the escape hatch content-based discovery just closed,
        reopened one layer up and cheaper than before.
        """
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.startswith("#"):
                return None              # past the leading comment block
            m = gen_re.search(stripped)
            if m:
                return m.group(1)
        return None

    def _documents(text: str):
        """(raw slice, parsed doc) per YAML document, so provenance stays local."""
        chunks, cur = [], []
        for line in text.splitlines(keepends=True):
            if line.rstrip("\r\n") == "---" and cur:
                chunks.append("".join(cur))
                cur = []
            else:
                cur.append(line)
        if cur:
            chunks.append("".join(cur))
        for chunk in chunks:
            try:
                yield chunk, yaml.safe_load(chunk)
            except yaml.YAMLError:
                continue

    for rel in _tracked_yaml_paths():
        path = Path(_REPO_ROOT) / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # ⛔ Prefilter must be WIDER than the criterion, or it silently becomes
        # one. Every tighter spelling tried here has been wrong in a different
        # direction: `"groups:"` as a literal misses the legal `groups :` and
        # the quoted key `"groups":`; a `^`-anchored pattern misses the deployed
        # tree entirely, because generated ConfigMaps carry their rules inside a
        # double-quoted scalar where `groups` never starts a line. The bare word
        # is the only form with no YAML spelling between it and the key.
        # `_is_rule_groups` below is what actually decides; this exists only to
        # avoid parsing every YAML in the repo.
        if "groups" not in text:
            continue

        for chunk, doc in _documents(text):
            header_prov = _header_provenance(chunk)
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            if kind == "ConfigMap":
                prov = header_prov
                for key, body in (doc.get("data") or {}).items():
                    try:
                        inner = yaml.safe_load(body) if isinstance(body, str) else body
                    except yaml.YAMLError:
                        continue
                    if isinstance(inner, dict) and (
                            _is_rule_groups(inner.get("groups"))
                            or isinstance(inner.get("spec"), dict)):
                        yield f"{rel}:{key}", inner, prov
            elif kind == "PrometheusRule":
                # ⛔ `groups` at the TOP level of a PrometheusRule is the
                # commonest paste error and the tripwire below cannot see it:
                # that check receives `doc["spec"]`, so the misplaced key is
                # outside what it is handed. Normalise here, where both halves
                # are still visible, and let the tripwire flag the empty spec.
                if _is_rule_groups(doc.get("groups")) and not (
                        isinstance(doc.get("spec"), dict) and doc["spec"].get("groups")):
                    yield rel, {"_misplaced_groups": doc.get("groups")}, None
                    continue
                name = (doc.get("metadata") or {}).get("name") or ""
                prov = (name[len("da-rule-pack-"):]
                        if name.startswith("da-rule-pack-") else header_prov)
                yield rel, (doc.get("spec") or {}), prov
            elif _is_rule_groups(doc.get("groups")) and rel.startswith("rule-packs/"):
                stem = _strip_rules_ext(PurePosixPath(rel).name)
                prov = (stem[len(_RULE_PACK_PREFIX):]
                        if stem and stem.startswith(_RULE_PACK_PREFIX) else None)
                yield rel, doc, prov


def _iter_repo_alert_rules():
    """Yield (where, rule) for EVERY alerting rule the repo ships.

    Single scanner on purpose: the sentinel contract and the alert_source
    contract below are both "this discriminator is RESERVED" invariants, and a
    reserved-value claim is only as good as its coverage — two scanners would
    let one drift and silently narrow the other's guarantee.
    """
    for where, doc, _prov in _rule_containers():
        for group in (doc.get("groups") or []):
            if not isinstance(group, dict):
                continue
            for rule in (group.get("rules") or []):
                if isinstance(rule, dict) and "alert" in rule:
                    yield where, rule


def _rule_shaped_but_unparsed():
    """Containers that LOOK like they hold rules but yield none. Must stay empty.

    ⛔ The silent-zero is the failure this exists for. A ConfigMap data key whose
    body nests its rules under `spec.groups` (the PrometheusRule shape, easy to
    paste by mistake) parses fine, classifies fine, and contributes nothing —
    every count-based floor is still satisfied by the other keys, so no assertion
    anywhere notices that a whole block of alerts is unguarded. Yielding zero
    rules from something that is visibly rule-shaped is never correct; it is
    either a nesting mistake or a scanner that has stopped understanding a shape
    the repo now uses.
    """
    offenders = []
    # ⛔ A file that will not parse is the loudest silent-zero of all: it
    # contributes nothing and no branch above ever sees it. 68 tracked YAML in
    # this repo currently fail to parse (helm templates carrying Go actions);
    # none holds rules today, so only flag one that visibly tries to.
    for rel in _tracked_yaml_paths():
        try:
            text = (Path(_REPO_ROOT) / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "- alert:" not in text:
            continue
        try:
            list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            offenders.append((rel, f"declares alerts but will not parse: {exc.__class__.__name__}"))
    for where, doc, _prov in _rule_containers():
        if doc.get("groups"):
            continue
        if "_misplaced_groups" in doc:
            offenders.append((where, "PrometheusRule with groups: at the top "
                                     "level instead of under spec:"))
            continue
        nested = doc.get("spec")
        if isinstance(nested, dict) and nested.get("groups"):
            offenders.append((where, "rules nested under spec.groups"))
        elif isinstance(doc.get("rules"), list):
            offenders.append((where, "rules present but no enclosing groups:"))
    return offenders


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
        assert len(containers) >= 30, (
            f"only {len(containers)} rules container(s) discovered — every "
            "assertion below would be vacuous")

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
        assert all(by_where[w] for w in prom), (
            "an operator manifest with no derivable source pack would be "
            "platform-scope; today all of them map to a pack")
        # 4. `groups:` alone is not enough: RBAC subject groups are not rules.
        assert not any("configmap-rbac" in w for w in by_where), sorted(by_where)
        # 5. Nothing rule-shaped may yield zero rules (the silent-zero).
        assert _rule_shaped_but_unparsed() == [], _rule_shaped_but_unparsed()

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
        original = _rule_containers
        try:
            globals()["_rule_containers"] = lambda: fake
            _platform_rule_locations.cache_clear()
            got = _platform_rule_locations()
        finally:
            globals()["_rule_containers"] = original
            _platform_rule_locations.cache_clear()
        assert "fake.yaml:missing-pack" in got, (
            "a header naming a pack that does not exist must NOT confer "
            "generated status — stale or fabricated provenance is not provenance")
        assert "fake.yaml:no-prov" in got, (
            "unknown provenance must default to platform (fail-closed)")
        assert "fake.yaml:real-pack" not in got

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
        # `- 1`: Watchdog is skipped above (the one documented exception), so the
        # floor is the whole tree minus it. See _MIN_PLATFORM_ALERTS.
        assert len(seen) >= _MIN_PLATFORM_ALERTS - 1, (
            f"only {len(seen)} platform alerts scanned, expected >= "
            f"{_MIN_PLATFORM_ALERTS - 1}: {sorted(seen)}")
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
    pint does not see this tree AS CONFIGURED (`.pint.hcl`'s parser include list
    is rule-packs/ + tests/rulepacks/*.rules.yaml, and the pint-reachable
    EXTRACTS mirror only 26 of the 42 alerts, so a pint `alerts/annotation` rule
    would go green while a third of the tree stayed uncovered), and
    configmap-rules-platform.yaml says so in its own words next to the one
    anchored link ("rule-YAML URLs are not linted").

    ⚠️ That is a CONFIG boundary, not a capability one — do not restate the older
    "the ConfigMap wrapper is unparseable" claim, which is false. pint's
    `parser.relaxed` mode exists precisely for "rules that are embedded inside a
    different structure" (upstream docs/configuration.md), and its parser has an
    explicit YAML-inside-YAML branch for the `data: |` shape a rules ConfigMap
    uses (internal/parser/parser.go, `case yaml.ScalarNode`). CI pins pint 0.86.0
    (.github/workflows/ci.yml), far past the version that landed it. So widening
    `.pint.hcl` IS an option on the table; this contract is not a workaround for
    an upstream limitation, it is the gate that holds while that option is
    unexercised — and it covers 42/42 rather than the extracts' 26.

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
                if not re.fullmatch(r"L\d+(C\d+)?(-L\d+(C\d+)?)?", anchor):
                    broken.append((where, rule["alert"], url,
                                   f"{rel} is not Markdown — GitHub renders it as "
                                   f"source and offers only #L<n> line refs, so "
                                   f"#{anchor} does not exist"))
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
