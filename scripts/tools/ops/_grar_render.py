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

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))  # Repo subdir layout


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
    if not path or not os.path.isfile(path):
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
    if routes:
        if "route" not in existing:
            existing["route"] = {}
        existing["route"]["routes"] = routes

    if receivers:
        # Keep default receiver, replace tenant receivers
        existing_names = {r["name"] for r in receivers}
        kept = [r for r in existing.get("receivers", [])
                if r["name"] not in existing_names]
        existing["receivers"] = kept + receivers

    if inhibit_rules:
        # Keep non-generated inhibit rules (e.g., Silent Mode sentinel rules)
        kept_rules = [r for r in existing.get("inhibit_rules", [])
                      if not any('metric_group' in m for m in r.get("source_matchers", []))]
        existing["inhibit_rules"] = kept_rules + inhibit_rules

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
