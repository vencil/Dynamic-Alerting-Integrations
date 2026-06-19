#!/usr/bin/env python3
"""RETIRE-ordering hard gate (#869 design item 7; matrix Gap #2).

The #869 per-tenant liveness rule TenantExporterAbsent fires whenever a tenant
that DECLARES a db_type (and so emits tenant_expected_exporter=1) has no healthy
up{job="tenant-exporters"}==1 target:

    tenant_expected_exporter unless on(tenant) (up{job="tenant-exporters"} == 1)

That makes a de-provisioning ORDER bug newly dangerous: if a PR removes a tenant's
K8s scrape target (Helm release / namespace) but LEAVES its conf.d `_metadata`
behind, the exporter keeps emitting tenant_expected_exporter{tenant}=1 while `up`
is gone forever → TenantExporterAbsent fires 100% FALSE-POSITIVE critical against a
tenant that no longer exists. The safe order is: remove conf.d (cut the metadata
source) FIRST, or remove both together — NEVER K8s-target-only.

This gate enforces that statically at PR time. It is a NET-NEW conf.d↔K8s set
comparison; the repo had ZERO prior build-time link between the two (they aligned
only by db-a/db-b naming coincidence). The matrix's "reuse blast_radius /
silencer_drift / orphan-detector" note is a CONCEPTUAL reuse (the "detect orphan,
don't auto-mutate" idiom) — none of those compare conf.d↔K8s, so there is no
advisory check to merely flip to enforcing.

What "a tenant" means here
--------------------------
We enumerate ONLY tenants that declare `_metadata.db_type` in conf.d, because that
is EXACTLY the set the collector emits tenant_expected_exporter for (collector.go
collectTenantExpectedExporter skips db_type==""). Same ruler as the metric → the
gate cannot pass while the metric over-emits, or vice versa. (Tenants without a
db_type are not monitored by TenantExporterAbsent, so a stale conf.d entry for them
carries no false-positive risk and is not this gate's concern.)

"K8s target declaration" SoT
----------------------------
Prometheus discovers tenant-exporters via service-role SD (configmap-prometheus.yaml)
keyed on the db-* namespace — there is NO central target list to diff. We proxy the
runtime target set with the repo's IaC DECLARATIONS that deploy it:
  - helm/values-<tenant>.yaml      (primary: declares the exporter deployment)
  - k8s/00-namespaces/namespaces.yaml `instance` label (secondary cross-check)
A tenant counts as "has a K8s target" if EITHER is present. (Customers running
their own GitOps may declare targets elsewhere → both dirs are overridable.)

Directions
----------
  VIOLATION (exit 1): conf.d declares db_type for <tenant> but NO K8s-target
      declaration found → de-provisioning would strand tenant_expected_exporter →
      false-positive TenantExporterAbsent. This is the dangerous order.
  WARN (exit 0):       K8s target declared but conf.d has no db_type for it →
      orphan deploy (no tenant_expected_exporter → TenantExporterAbsent can't fire
      on it). Harmless to liveness; surfaced but not blocking, so the legitimate
      "provision infra first, add conf.d later" onboarding order is not blocked.

Exit codes (per scripts/tools/_lib_exitcodes.py):
    0  No retire-ordering violations (warnings allowed)
    1  Violation(s) detected (--ci) — conf.d db_type without a K8s target
    2  Caller error (missing path, YAML parse failure)

Usage:
    python scripts/tools/lint/check_retire_drift.py            # report
    python scripts/tools/lint/check_retire_drift.py --ci       # exit 1 on violation
    python scripts/tools/lint/check_retire_drift.py --json
    python scripts/tools/lint/check_retire_drift.py \
        --config-dir components/threshold-exporter/config/conf.d \
        --helm-dir helm \
        --namespaces-file k8s/00-namespaces/namespaces.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Set

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402


def _repo_root() -> Path:
    p = Path(_THIS_DIR).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return p.parent.parent.parent


def _load_yaml(path: Path) -> dict:
    """Parse a YAML mapping; '' / non-mapping → {} so callers can .get safely."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def conf_d_declared_db_type_tenants(config_dir: Path) -> Dict[str, str]:
    """tenant → db_type for every conf.d tenant that DECLARES a non-empty db_type.

    Mirrors the collector's emit set (ResolveMetadata + db_type!="" guard):
    walk every *.yaml under config_dir except `_`-prefixed files (defaults /
    profiles) and the examples/ subtree (dev templates, never shipped); read the
    `tenants:` block; pick up `_metadata.db_type` (the _metadata value is the same
    re-serialized mapping ResolveMetadata parses). Only non-empty db_type counts.
    """
    out: Dict[str, str] = {}
    for path in sorted(config_dir.rglob("*.yaml")):
        # Skip _-prefixed config files (e.g. _defaults.yaml, _routing_profiles.yaml).
        if path.name.startswith("_"):
            continue
        # Skip the examples/ dev-template subtree (not part of a real deployment).
        if "examples" in path.relative_to(config_dir).parts:
            continue
        data = _load_yaml(path)
        tenants = data.get("tenants")
        if not isinstance(tenants, dict):
            continue
        for tenant, cfg in tenants.items():
            if not isinstance(cfg, dict):
                continue
            meta_raw = cfg.get("_metadata")
            db_type = ""
            if isinstance(meta_raw, str):
                # _metadata stored as a re-serialized YAML string (matches
                # resolve.go ResolveMetadata: yaml.Unmarshal of the scalar).
                parsed = yaml.safe_load(meta_raw)
                if isinstance(parsed, dict):
                    db_type = str(parsed.get("db_type") or "")
            elif isinstance(meta_raw, dict):
                # Also accept _metadata authored as a native mapping.
                db_type = str(meta_raw.get("db_type") or "")
            if db_type:
                out[str(tenant)] = db_type
    return out


def helm_declared_targets(helm_dir: Path) -> Set[str]:
    """Tenant stems with a helm/values-<tenant>.yaml exporter-deployment override."""
    targets: Set[str] = set()
    if not helm_dir.is_dir():
        return targets
    for path in sorted(helm_dir.glob("values-*.yaml")):
        stem = path.stem[len("values-"):]  # values-db-a -> db-a
        if stem:
            targets.add(stem)
    return targets


def namespace_declared_targets(namespaces_file: Path) -> Set[str]:
    """Tenant ids from db-* Namespace `instance` labels (secondary cross-check)."""
    targets: Set[str] = set()
    if not namespaces_file.is_file():
        return targets
    text = namespaces_file.read_text(encoding="utf-8")
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict) or doc.get("kind") != "Namespace":
            continue
        labels = ((doc.get("metadata") or {}).get("labels")) or {}
        inst = labels.get("instance")
        if inst:
            targets.add(str(inst))
    return targets


def evaluate(config_dir: Path, helm_dir: Path, namespaces_file: Path) -> dict:
    """Return {violations: [...], warnings: [...], declared: {...}, targets: [...]}."""
    declared = conf_d_declared_db_type_tenants(config_dir)
    helm_targets = helm_declared_targets(helm_dir)
    ns_targets = namespace_declared_targets(namespaces_file)
    k8s_targets = helm_targets | ns_targets

    violations: List[str] = []
    for tenant, db_type in sorted(declared.items()):
        if tenant not in k8s_targets:
            violations.append(
                f"tenant '{tenant}' declares db_type='{db_type}' in conf.d "
                f"(emits tenant_expected_exporter=1) but has NO K8s target "
                f"declaration (helm/values-{tenant}.yaml or a db-* namespace with "
                f"instance={tenant}). De-provisioning order bug: remove conf.d "
                f"_metadata BEFORE/with the K8s target, else TenantExporterAbsent "
                f"fires a false-positive critical against a dead tenant."
            )

    warnings: List[str] = []
    for tenant in sorted(k8s_targets):
        if tenant not in declared:
            warnings.append(
                f"tenant '{tenant}' has a K8s target declaration but no conf.d "
                f"db_type (orphan deploy / pre-conf.d onboarding) — not monitored "
                f"by TenantExporterAbsent. Harmless to liveness; informational."
            )

    return {
        "violations": violations,
        "warnings": warnings,
        "declared": declared,
        "k8s_targets": sorted(k8s_targets),
    }


def main() -> int:
    """CLI entry point: RETIRE-ordering conf.d↔K8s drift gate (#869)."""
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Forbid de-provisioning a K8s exporter target while its conf.d "
        "_metadata.db_type remains (#869 false-positive guard)."
    )
    repo = _repo_root()
    parser.add_argument(
        "--config-dir",
        default=str(repo / "components/threshold-exporter/config/conf.d"),
        help="conf.d directory to enumerate tenants from.",
    )
    parser.add_argument(
        "--helm-dir",
        default=str(repo / "helm"),
        help="Directory holding values-<tenant>.yaml exporter-deployment overrides.",
    )
    parser.add_argument(
        "--namespaces-file",
        default=str(repo / "k8s/00-namespaces/namespaces.yaml"),
        help="Manifest with db-* Namespace `instance` labels (secondary cross-check).",
    )
    parser.add_argument("--ci", action="store_true", help="Exit 1 on any violation.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    config_dir = Path(args.config_dir)
    if not config_dir.is_dir():
        print(f"ERROR: config dir not found: {config_dir}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        result = evaluate(config_dir, Path(args.helm_dir), Path(args.namespaces_file))
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        n_decl = len(result["declared"])
        print(f"RETIRE gate (#869): {n_decl} tenant(s) declare db_type; "
              f"{len(result['k8s_targets'])} K8s target(s).")
        for w in result["warnings"]:
            print(f"  ⚠️  WARN: {w}")
        for v in result["violations"]:
            print(f"  ❌ VIOLATION: {v}")
        if not result["violations"]:
            print("  ✅ No retire-ordering violations.")

    if result["violations"] and args.ci:
        print("\n❌ RETIRE-ordering violation: conf.d _metadata.db_type without a "
              "K8s target. Remove the conf.d entry first (or together).",
              file=sys.stderr)
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
