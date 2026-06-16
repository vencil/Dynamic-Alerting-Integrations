"""Output rendering + Alertmanager ConfigMap operations.

PR-3a (v2.8.0) extracted these helpers out of generate_alertmanager_routes.py
to bring the main file under the line-count cap. All symbols are re-exported
from generate_alertmanager_routes for backwards-compatible test imports.

Functions:
  Output rendering:
    render_output(...)           → fragment YAML (route + receivers + inhibit_rules)
    load_base_config(path)       → base AM YAML or _DEFAULT_BASE_CONFIG fallback
    assemble_configmap(...)      → full K8s ConfigMap YAML for GitOps PR

  ConfigMap operations (--apply mode, K8s cluster deploy):
    _read_existing_configmap(...)         → kubectl get + parse
    _merge_routes_receivers_inhibits(...) → merge generated into existing
    _apply_merged_configmap(...)          → kubectl apply via stdin
    _reload_alertmanager(namespace)       → curl POST /-/reload
    apply_to_configmap(...)              → orchestrate read → merge → apply → reload
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout

from _grar_routes import (  # noqa: E402
    _build_custom_alert_routes, _build_watchdog_route, _build_synthetic_probe_route)
from _grar_validate import assert_watchdog_inhibit_immunity  # noqa: E402


def _inject_custom_alert_isolation(routes: list[dict], receivers: list[dict]) -> tuple[list[dict], list[dict]]:
    """Prepend the three platform-static top-of-tree routes — the Watchdog liveness
    route (index 0, ADR-025 D1 / #838), the Custom Alerts isolation route (index 1,
    #741 S7/S8), then the synthetic-probe sinkhole route (index 2, ADR-025 interop) —
    plus their receivers, so the final ConfigMap always pins them AHEAD of the
    enforced NOC route and they survive the route-REPLACE that --apply performs on
    route.routes.

    Order is load-bearing. Watchdog MUST be index 0 (highest priority) so its
    heartbeat can never be intercepted by a broader earlier route; the custom
    isolation route follows at index 1, the synthetic-probe sink at index 2. The
    three matchers are mutually exclusive (alertname="Watchdog" vs component="custom"
    vs component="synthetic-probe"), so none shadows another, but the positions are
    pinned for determinism and audit.

    Idempotent: any pre-existing Watchdog / component="custom" / component=
    "synthetic-probe" route is dropped and re-prepended canonically (so re-merging an
    already-injected config does not duplicate or mis-order), and the name-only
    placeholder receivers are added only if absent — a richer existing/base
    definition (e.g. watchdog-heartbeat's url_file) is preserved, never duplicated or
    clobbered, here.
    """
    wd_routes, wd_receivers = _build_watchdog_route()
    cust_routes, cust_receivers = _build_custom_alert_routes()
    probe_routes, probe_receivers = _build_synthetic_probe_route()

    def _is_watchdog(r: dict) -> bool:
        return 'alertname="Watchdog"' in r.get("matchers", [])

    def _is_custom(r: dict) -> bool:
        return 'component="custom"' in r.get("matchers", [])

    def _is_probe(r: dict) -> bool:
        return 'component="synthetic-probe"' in r.get("matchers", [])

    rest = [r for r in (routes or [])
            if not _is_watchdog(r) and not _is_custom(r) and not _is_probe(r)]
    # Order is load-bearing + pinned for determinism: Watchdog (0) → Custom (1) →
    # synthetic-probe (2), all ahead of the enforced NOC match-all. The three
    # matchers are mutually exclusive so none shadows another.
    out_routes = wd_routes + cust_routes + probe_routes + rest

    have = {r["name"] for r in (receivers or [])}
    add_recv = [r for r in (wd_receivers + cust_receivers + probe_receivers)
                if r["name"] not in have]
    out_receivers = list(receivers or []) + add_recv
    return out_routes, out_receivers


def render_output(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict] | None = None) -> str:
    """Render Alertmanager route + receiver + inhibit config as YAML fragment.

    Constructs a clean YAML dictionary containing the tenant routing config
    (route tree + receivers + inhibit rules) suitable for merging into an
    existing alertmanager.yml or for --dry-run output.

    Args:
        routes: list of Alertmanager route dicts
        receivers: list of Alertmanager receiver dicts
        inhibit_rules: optional list of inhibit_rule dicts (severity dedup)

    Returns:
        YAML string fragment with keys: route (with nested routes), receivers, inhibit_rules.
        Empty sections are omitted from the output.
    """
    # Build the fragment as a clean dict
    fragment = {}

    if routes:
        fragment["route"] = {
            "routes": routes,
        }

    if receivers:
        fragment["receivers"] = receivers

    if inhibit_rules:
        fragment["inhibit_rules"] = inhibit_rules

    return yaml.dump(fragment, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── §11.3 AM GitOps: --output-configmap ────────────────────────────

# Minimal inline defaults when --base-config is not provided
_DEFAULT_BASE_CONFIG = {
    "global": {"resolve_timeout": "5m"},
    "route": {
        "group_by": ["alertname", "tenant"],
        "group_wait": "10s",
        "group_interval": "10s",
        "repeat_interval": "12h",
        "receiver": "default",
    },
    "receivers": [{"name": "default"}],
    "inhibit_rules": [],
}


def load_base_config(path: str | None) -> dict:
    """Load base Alertmanager config from YAML file.

    Returns dict with global, route, receivers, inhibit_rules.
    Falls back to _DEFAULT_BASE_CONFIG on any error.
    """
    if not path or not Path(path).is_file():
        return dict(_DEFAULT_BASE_CONFIG)
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    # Ensure required keys exist
    for key in ("global", "route", "receivers", "inhibit_rules"):
        if key not in data:
            data[key] = _DEFAULT_BASE_CONFIG[key]
    return data


def assemble_configmap(base: dict, routes: list[dict], receivers: list[dict], inhibit_rules: list[dict],
                       namespace: str = "monitoring", configmap_name: str = "alertmanager-config") -> str:
    """Merge tenant routing fragments into base Alertmanager config and wrap as K8s ConfigMap.

    Merges generated routes, receivers, and inhibit_rules into a base Alertmanager
    configuration, then wraps the result as a Kubernetes ConfigMap YAML suitable
    for kubectl apply or GitOps workflows.

    Merge Strategy:
      - Routes: replace base route.routes with generated tenant routes
      - Receivers: append tenant receivers to base receivers (dedup by name)
      - Inhibit Rules: append tenant rules to base rules
      - Global/Other: preserve from base config

    Args:
        base: base Alertmanager config dict (from load_base_config or YAML file)
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace for ConfigMap (default: monitoring)
        configmap_name: ConfigMap name (default: alertmanager-config)

    Returns:
        Complete Kubernetes ConfigMap YAML string (apiVersion, kind, metadata, data.alertmanager.yml).
    """
    merged = dict(base)

    # S7/S8 (#741): ensure the Custom Alerts isolation route + firehose receiver
    # are present and FIRST, regardless of what generate_routes produced.
    routes, receivers = _inject_custom_alert_isolation(routes, receivers)

    # Merge routes into base route
    merged_route = dict(merged.get("route", {}))
    merged_route["routes"] = routes
    merged["route"] = merged_route

    # Merge receivers: keep base receivers, append tenant receivers
    base_names = {r["name"] for r in merged.get("receivers", [])}
    tenant_receivers = [r for r in receivers if r["name"] not in base_names]
    merged["receivers"] = list(merged.get("receivers", [])) + tenant_receivers

    # Merge inhibit_rules: keep base rules, append tenant rules
    merged["inhibit_rules"] = list(merged.get("inhibit_rules", [])) + list(inhibit_rules or [])

    # ADR-025 D1: fail-closed if any inhibit rule (base or generated) would
    # suppress the always-firing Watchdog heartbeat — it must always egress.
    assert_watchdog_inhibit_immunity(merged["inhibit_rules"])

    # Render alertmanager.yml content
    am_yml = yaml.dump(merged, default_flow_style=False,
                       allow_unicode=True, sort_keys=False)

    # Wrap in ConfigMap structure
    configmap = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": configmap_name,
            "namespace": namespace,
        },
        "data": {
            "alertmanager.yml": am_yml,
        },
    }
    return yaml.dump(configmap, default_flow_style=False,
                     allow_unicode=True, sort_keys=False)


# ============================================================
# ConfigMap Operations (K8s cluster deployment)
# ============================================================

def _read_existing_configmap(namespace: str, configmap_name: str) -> tuple[dict | None, list[str]]:
    """Read existing Alertmanager ConfigMap from K8s cluster.

    Returns (config_dict, warnings) — config_dict is None if read failed.
    """
    warnings = []
    result = subprocess.run(
        ["kubectl", "get", "configmap", configmap_name, "-n", namespace, "-o", "json"],
        capture_output=True, text=True, timeout=60, encoding='utf-8',
    )
    if result.returncode != 0:
        warnings.append(f"ERROR: Failed to read ConfigMap {configmap_name}: {result.stderr}")
        return None, warnings

    cm = json.loads(result.stdout)
    existing_yml = cm.get("data", {}).get("alertmanager.yml", "")
    if not existing_yml:
        warnings.append("ERROR: ConfigMap has no 'alertmanager.yml' key")
        return None, warnings

    existing = yaml.safe_load(existing_yml)
    return existing, warnings


def _merge_routes_receivers_inhibits(existing: dict, routes: list[dict],
                                     receivers: list[dict], inhibit_rules: list[dict]) -> dict:
    """Merge generated routes, receivers, and inhibit rules into existing config.

    Returns merged config dict.
    """
    # S7/S8 (#741): keep the Custom Alerts isolation route + firehose receiver
    # present and FIRST across --apply (which REPLACES route.routes).
    routes, receivers = _inject_custom_alert_isolation(routes, receivers)

    if routes:
        if "route" not in existing:
            existing["route"] = {}
        existing["route"]["routes"] = routes

    if receivers:
        # Generated tenant receivers REPLACE the existing same-named ones (they
        # are regenerated from conf.d each run). BUT the injected platform-static
        # placeholders (custom-alerts-firehose / watchdog-heartbeat are emitted
        # NAME-ONLY) must NOT clobber a richer base definition — most critically
        # watchdog-heartbeat's webhook_configs[].url_file, which lives only in the
        # base ConfigMap. For a name-only placeholder, defer to the existing
        # definition if one is present; otherwise add the placeholder so the route
        # still resolves.
        existing_by_name = {r["name"]: r for r in existing.get("receivers", [])}
        gen_names = {r["name"] for r in receivers}

        def _resolve(r: dict) -> dict:
            if set(r.keys()) == {"name"} and r["name"] in existing_by_name:
                return existing_by_name[r["name"]]
            return r

        merged_gen = [_resolve(r) for r in receivers]
        kept = [r for r in existing.get("receivers", [])
                if r["name"] not in gen_names]
        existing["receivers"] = kept + merged_gen

    if inhibit_rules:
        # Keep non-generated inhibit rules (e.g., Silent Mode sentinel rules)
        kept_rules = [r for r in existing.get("inhibit_rules", [])
                      if not any('metric_group' in m for m in r.get("source_matchers", []))]
        existing["inhibit_rules"] = kept_rules + inhibit_rules

    # ADR-025 D1: fail-closed on the FINAL inhibit set (validated even when no
    # inhibit rules were generated this run) — the Watchdog heartbeat must never
    # be inhibited before it reaches the external dead-man's-switch.
    assert_watchdog_inhibit_immunity(existing.get("inhibit_rules", []))

    return existing


def _apply_merged_configmap(merged_yml: str, namespace: str, configmap_name: str) -> bool:
    """Apply merged ConfigMap to K8s cluster.

    Returns True if successful, False otherwise.
    """
    apply_result = subprocess.run(
        ["kubectl", "create", "configmap", configmap_name,
         f"--from-literal=alertmanager.yml={merged_yml}",
         "-n", namespace, "--dry-run=client", "-o", "yaml"],
        capture_output=True, text=True, timeout=60, encoding='utf-8',
    )
    if apply_result.returncode != 0:
        print(f"ERROR: Failed to generate ConfigMap: {apply_result.stderr}", file=sys.stderr)
        return False

    pipe_result = subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=apply_result.stdout, capture_output=True, text=True, timeout=120, encoding='utf-8',
    )
    if pipe_result.returncode != 0:
        print(f"ERROR: kubectl apply failed: {pipe_result.stderr}", file=sys.stderr)
        return False

    print(f"ConfigMap {namespace}/{configmap_name} updated")
    return True


def _reload_alertmanager(namespace: str) -> bool:
    """Reload Alertmanager configuration via HTTP POST.

    Returns True on success (or if warning-level failure), False on critical error.
    """
    svc_url = f"http://alertmanager.{namespace}.svc.cluster.local:9093"
    reload_result = subprocess.run(
        ["curl", "-sf", "-X", "POST", f"{svc_url}/-/reload"],
        capture_output=True, text=True, timeout=60, encoding='utf-8',
    )
    if reload_result.returncode != 0:
        print(f"WARN: Alertmanager reload failed (is --web.enable-lifecycle enabled?)",
              file=sys.stderr)
        print("ConfigMap was updated — Alertmanager will pick up changes on next restart")
        return True

    print("Alertmanager reloaded")
    return True


def apply_to_configmap(routes: list[dict], receivers: list[dict], inhibit_rules: list[dict], namespace: str, configmap_name: str) -> bool:
    """Merge generated routing config into existing Alertmanager ConfigMap and reload.

    Applies tenant routing configuration directly to a running Alertmanager cluster.
    This is the --apply mode for immediate deployment without GitOps workflow.

    Process:
      1. kubectl get configmap → extract alertmanager.yml
      2. Merge generated routes, receivers, inhibit_rules into existing config
      3. kubectl apply ConfigMap with merged config
      4. curl POST /-/reload to trigger Alertmanager configuration reload

    Notes:
      - Keeps existing base routes/receivers, appends tenant-generated ones
      - Preserves non-generated inhibit rules (e.g., Silent Mode sentinel rules)
      - Requires Alertmanager --web.enable-lifecycle flag for /-/reload to work

    Args:
        routes: generated tenant route dicts
        receivers: generated tenant receiver dicts
        inhibit_rules: generated inhibit_rules for severity dedup
        namespace: K8s namespace where ConfigMap is located
        configmap_name: ConfigMap name (typically alertmanager-config)

    Returns:
        True if merge and reload succeeded, False otherwise.
    """
    # 1. Read existing ConfigMap
    existing, read_warnings = _read_existing_configmap(namespace, configmap_name)
    if existing is None:
        for w in read_warnings:
            print(w, file=sys.stderr)
        return False

    # 2. Merge fragment into existing config
    existing = _merge_routes_receivers_inhibits(existing, routes, receivers, inhibit_rules)
    merged_yml = yaml.dump(existing, default_flow_style=False,
                           allow_unicode=True, sort_keys=False)

    # 3. Apply updated ConfigMap
    if not _apply_merged_configmap(merged_yml, namespace, configmap_name):
        return False

    # 4. Reload Alertmanager
    return _reload_alertmanager(namespace)
