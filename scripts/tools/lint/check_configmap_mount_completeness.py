#!/usr/bin/env python3
"""Guard: every `configmap-rules-*.yaml` must be mounted into Prometheus.

The `rules` projected volume in `k8s/03-monitoring/deployment-prometheus.yaml`
is a hand-maintained **by-name** list of `configMap` sources. A new rule-pack
ConfigMap (e.g. `configmap-rules-liveness.yaml`) that is NOT added to that list
is silently never projected into `/etc/prometheus/rules/*.yml` → its alert/
recording rules are dead code in the actual deployment, even though promtool
unit tests (which point straight at the rule YAML) stay green.

This burned #869: `prometheus-rules-liveness` shipped but unmounted →
`TenantExporterAbsent` never loaded in prod. No existing check caught it
(promtool tests expr semantics; check_doc_k8s_refs validates doc→manifest
paths; the 3-copy gate compares rule-pack↔configmap↔operator — none verify
configmap→projected-volume mount completeness).

Checks (per configmap-rules-*.yaml):
  1. its `metadata.name` MUST appear as a `configMap.name` in the deployment's
     `rules` projected volume — else the whole pack is unmounted (VIOLATION).
  2. every `items[].key` the deployment projects for that ConfigMap MUST be a
     real `data:` key (else the path projects nothing → rule file missing).
  3. every `data:` key in the ConfigMap MUST be projected by some `items[].key`
     (else that rule file is silently dropped even though the pack is mounted).

Exit codes: 0 ok / 1 violation / 2 caller error (see _lib_exitcodes).

Usage:
    python3 scripts/tools/lint/check_configmap_mount_completeness.py            # human
    python3 scripts/tools/lint/check_configmap_mount_completeness.py --ci       # CI
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Set

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # scripts/tools
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402

MONITOR_DIR = Path("k8s/03-monitoring")
DEPLOYMENT = MONITOR_DIR / "deployment-prometheus.yaml"
RULES_VOLUME_NAME = "rules"

# `configmap-rules-custom-*.yaml` are tenant/customer-managed packs (migrate_rule.py
# exports), NOT platform gold-standard packs — the deployment projected volume is a
# hand-maintained list of platform packs only; custom packs are mounted/applied by
# the customer or compiled dynamically (see deployment-prometheus.yaml header note:
# "大型客戶可…改用 migrate_rule.py 轉出的 自訂規則包"). They are out of scope for the
# "platform forgot to mount a new gold-standard pack" guard this lint enforces.
CUSTOMER_MANAGED_PREFIX = "configmap-rules-custom-"


def configmap_rule_packs(monitor_dir: Path) -> Dict[str, Set[str]]:
    """{metadata.name: {data keys}} for every configmap-rules-*.yaml."""
    out: Dict[str, Set[str]] = {}
    for path in sorted(monitor_dir.glob("configmap-rules-*.yaml")):
        if path.name.startswith(CUSTOMER_MANAGED_PREFIX):
            continue  # customer-managed, not in the platform gold-standard mount list
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or doc.get("kind") != "ConfigMap":
            continue
        name = ((doc.get("metadata") or {}).get("name")) or ""
        data = doc.get("data")
        keys = set(data.keys()) if isinstance(data, dict) else set()
        if name:
            out[str(name)] = keys
    return out


def deployment_mounts(deployment_path: Path) -> Dict[str, Set[str]]:
    """{configMap.name: {projected items[].key}} from the `rules` projected volume."""
    out: Dict[str, Set[str]] = {}
    for doc in yaml.safe_load_all(deployment_path.read_text(encoding="utf-8")):
        if not isinstance(doc, dict) or doc.get("kind") != "Deployment":
            continue
        volumes = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {}).get("volumes") or []
        for vol in volumes:
            if not isinstance(vol, dict) or vol.get("name") != RULES_VOLUME_NAME:
                continue
            sources = ((vol.get("projected") or {}).get("sources")) or []
            for src in sources:
                cm = (src or {}).get("configMap") if isinstance(src, dict) else None
                if not isinstance(cm, dict):
                    continue
                name = cm.get("name")
                if not name:
                    continue
                items = cm.get("items") or []
                keys = {it.get("key") for it in items if isinstance(it, dict) and it.get("key")}
                # No `items:` means the whole ConfigMap is projected (all keys).
                out[str(name)] = keys
    return out


def check(packs: Dict[str, Set[str]], mounts: Dict[str, Set[str]]) -> List[str]:
    """Return a list of human-readable violation strings (empty = ok).

    `packs` / `mounts` are precomputed by the caller (configmap_rule_packs /
    deployment_mounts) so each manifest is parsed exactly once.
    """
    violations: List[str] = []

    for name, data_keys in sorted(packs.items()):
        if name not in mounts:
            violations.append(
                f"ConfigMap '{name}' (configmap-rules-*.yaml) is NOT mounted in "
                f"{deployment_path}'s '{RULES_VOLUME_NAME}' projected volume → its rules "
                f"are never loaded by Prometheus (dead code). Add a configMap source for it."
            )
            continue
        item_keys = mounts[name]
        if not item_keys:
            # whole-ConfigMap projection: every data key reaches /etc/prometheus/rules
            continue
        for k in sorted(item_keys - data_keys):
            violations.append(
                f"ConfigMap '{name}': projected items[].key '{k}' has no matching data: "
                f"key → that path projects nothing (rule file missing). Fix the key/path."
            )
        for k in sorted(data_keys - item_keys):
            violations.append(
                f"ConfigMap '{name}': data: key '{k}' is not projected by any items[].key → "
                f"that rule file is silently dropped. Add an items entry (key+path)."
            )
    return violations


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ci", action="store_true", help="CI mode (machine-terse output)")
    parser.add_argument("--monitor-dir", type=Path, default=MONITOR_DIR)
    parser.add_argument("--deployment", type=Path, default=None,
                        help="override deployment manifest (default: <monitor-dir>/deployment-prometheus.yaml)")
    args = parser.parse_args(argv)

    monitor_dir: Path = args.monitor_dir
    deployment: Path = args.deployment or (monitor_dir / "deployment-prometheus.yaml")

    if not monitor_dir.is_dir():
        print(f"Error: monitor dir not found: {monitor_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    if not deployment.is_file():
        print(f"Error: deployment manifest not found: {deployment}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        packs = configmap_rule_packs(monitor_dir)
        mounts = deployment_mounts(deployment)
        violations = check(packs, mounts)
    except yaml.YAMLError as exc:
        print(f"Error: failed to parse YAML: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    n_packs = len(packs)
    if violations:
        print(f"ConfigMap mount completeness: {len(violations)} violation(s) "
              f"across {n_packs} configmap-rules-* pack(s):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return EXIT_VIOLATION

    if not args.ci:
        print(f"ConfigMap mount completeness: {n_packs} configmap-rules-* pack(s) "
              f"all mounted in {deployment.name} with matching keys.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
