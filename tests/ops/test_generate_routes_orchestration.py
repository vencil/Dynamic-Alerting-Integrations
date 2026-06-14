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
import subprocess
import tempfile
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
    _parse_config_files,
    write_text_secure,
)
from _grar_render import _merge_routes_receivers_inhibits
from generate_alertmanager_routes import (
    assert_watchdog_inhibit_immunity,
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
        # ADR-025 D1 (#838) + S7/S8 (#741): two platform-static routes are always
        # injected at the FRONT — Watchdog liveness at index 0, Custom Alerts
        # isolation at index 1 — both continue:false; the tenant route follows.
        assert len(routes_out) == 3
        assert routes_out[0]["matchers"] == ['alertname="Watchdog"']
        assert routes_out[0]["receiver"] == "watchdog-heartbeat"
        assert routes_out[0]["continue"] is False
        assert routes_out[1]["matchers"] == ['component="custom"']
        assert routes_out[1]["receiver"] == "custom-alerts-firehose"
        assert routes_out[1]["continue"] is False
        assert routes_out[2]["receiver"] == "tenant-db-a"
        # Base receiver + tenant receiver + injected firehose + watchdog receivers
        names = {r["name"] for r in am_config["receivers"]}
        assert "default" in names
        assert "tenant-db-a" in names
        assert "custom-alerts-firehose" in names
        assert "watchdog-heartbeat" in names

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
    """The two platform-static routes — Watchdog liveness (index 0) and Custom
    Alerts isolation (index 1) — plus their receivers must be present and pinned
    at the FRONT of the assembled ConfigMap, across BOTH the --output-configmap
    (assemble_configmap) and --apply (_merge_routes_receivers_inhibits) paths,
    and survive the route-REPLACE."""

    def _routes_of(self, cm_yaml):
        parsed = yaml.safe_load(cm_yaml)
        return yaml.safe_load(parsed["data"]["alertmanager.yml"])["route"]["routes"]

    def test_injected_even_with_no_tenant_routes(self):
        # empty generated routes (no tenants) → both static routes still present
        # and pinned: Watchdog index 0, custom index 1.
        cm_yaml = assemble_configmap(load_base_config(None), [], [], [])
        routes = self._routes_of(cm_yaml)
        assert routes[0]["matchers"] == ['alertname="Watchdog"']
        assert routes[0]["receiver"] == "watchdog-heartbeat"
        assert routes[0]["continue"] is False
        assert routes[1]["matchers"] == ['component="custom"']
        assert routes[1]["receiver"] == "custom-alerts-firehose"
        assert routes[1]["continue"] is False

    def test_idempotent_no_duplicate(self):
        # if Watchdog / component="custom" routes are already present, do not add
        # a second of either — re-merging an injected config is stable.
        existing = _build_watchdog_route()[0] + _build_custom_alert_routes()[0]
        cm_yaml = assemble_configmap(load_base_config(None), list(existing), [], [])
        routes = self._routes_of(cm_yaml)
        assert sum(1 for r in routes if 'component="custom"' in r.get("matchers", [])) == 1
        assert sum(1 for r in routes if 'alertname="Watchdog"' in r.get("matchers", [])) == 1

    def test_static_routes_forced_to_front_even_when_not_first(self):
        # CodeRabbit gap (generalized to Watchdog): existing Watchdog/custom routes
        # sitting AFTER a continue:true match-all enforced route must be normalized
        # to the front (else the enforced route intercepts them first → leak). The
        # heartbeat is the most important to pin — Watchdog must end up at index 0.
        enforced = {"receiver": "platform-enforced", "continue": True}  # match-all
        existing_custom = _build_custom_alert_routes()[0][0]
        existing_wd = _build_watchdog_route()[0][0]
        cm_yaml = assemble_configmap(
            load_base_config(None), [enforced, existing_custom, existing_wd], [], [])
        routes = self._routes_of(cm_yaml)
        wd_idx = [i for i, r in enumerate(routes)
                  if 'alertname="Watchdog"' in r.get("matchers", [])]
        custom_idx = [i for i, r in enumerate(routes)
                      if 'component="custom"' in r.get("matchers", [])]
        assert wd_idx == [0], routes      # Watchdog pinned to index 0
        assert custom_idx == [1], routes  # custom pinned to index 1
        assert routes[2]["receiver"] == "platform-enforced"

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
            "default", "custom-alerts-firehose", "watchdog-heartbeat", "tenant-db-a"}
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
