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
only by db-a/db-b naming coincidence). The matrix's "reuse config_diff /
silencer_drift / orphan-detector" note (blast_radius until #3230) is a
CONCEPTUAL reuse (the "detect orphan, don't auto-mutate" idiom) — none of
those compare conf.d↔K8s, so there is no advisory check to merely flip to
enforcing.

What "a tenant" means here
--------------------------
We enumerate ONLY tenants whose served `_metadata.db_type` is non-empty, because
that is EXACTLY the set the collector emits tenant_expected_exporter for (collector.go
collectTenantExpectedExporter skips db_type==""). Same ruler as the metric → the
gate cannot pass while the metric over-emits, or vice versa. (Tenants without a
db_type are not monitored by TenantExporterAbsent, so a stale conf.d entry for them
carries no false-positive risk and is not this gate's concern.)

The db_type is the exporter's own reading, from `da-guard served-values`
(`_lib_tenant_values.load_served_tree`; #2858): a db_type a tenant gets from
a profile or a root platform file's `tenants:` entry, the per-key layering of
`_metadata`, `optional_overrides` switching a profile's `_metadata` off and a
non-string db_type are all the exporter's answer, not re-derived here. Which
tenants are judged is still this lint's own walk (below): the examples/
subtree is excluded even though the exporter would load it.

"K8s target declaration" SoT
----------------------------
Prometheus discovers tenant-exporters via service-role SD (configmap-prometheus.yaml)
keyed on the db-* namespace — there is NO central target list to diff. We proxy the
runtime target set with the repo's IaC DECLARATIONS that deploy it:
  - helm/values-<tenant>.yaml      (primary: declares the exporter deployment)
  - k8s/00-namespaces/namespaces.yaml `instance` label (secondary cross-check)
A tenant counts as "has a K8s target" if EITHER is present. (Customers running
their own GitOps may declare targets elsewhere → both dirs are overridable.)

The namespaces file is a Kubernetes manifest, so it is read the way kubectl
reads it (ADR-036 §12, #3206): by `da-crdecode`, every document `kubectl
apply -f` would apply. A label value Kubernetes does not read as a string —
`instance: 010` is the number 8, `instance: yes` is true — is refused by
kubectl (a label takes only strings), so the Namespace never exists: it is
reported as an error naming the file and the document (exit 2), never taken
as its text and never skipped.

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
    2  Caller error (missing path, da-guard or da-crdecode missing or
       failing, a namespaces file kubectl would refuse — including an
       `instance` label that is not a string —, or a conf.d file the
       exporter drops)

da-guard: `$DA_GUARD_BINARY`, else `da-guard` on `$PATH`; da-crdecode:
`$DA_CRDECODE_BINARY`, else `da-crdecode` on `$PATH`. The pre-commit hook and
CI run this through scripts/ops/with_da_guard.sh and
scripts/ops/with_da_crdecode.sh, which build them.

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

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_io import exit_on_yaml_file_error  # noqa: E402
from _lib_compat import require_project_root  # noqa: E402  (#2852)
from _lib_io import YamlFileError, safe_label  # noqa: E402
# The namespaces manifest is Kubernetes YAML, not conf.d: PyYAML.
import _lib_consumer_read  # noqa: E402  (ADR-036 §12: manifests as kubectl reads them)
# ADR-036 step 4 (#2906): conf.d's tenant files and their `tenants:` keys are
# `da-guard parse-tree`'s, as the db_type is `da-guard served-values`'.
import _lib_parse_cache  # noqa: E402
from _lib_tenant_values import (  # noqa: E402
    exit_on_served_values_error,
    load_served_tree,
    print_load_warnings,
)


def _repo_root() -> Path:
    """This tree's root, never an enclosing project's (#2852)."""
    return require_project_root(_THIS_DIR)


def conf_d_declared_db_type_tenants(config_dir: Path,
                                     binary: str | None = None) -> Dict[str, str]:
    """tenant → db_type for every judged tenant whose served db_type is non-empty.

    WHICH TENANTS: the keys of the `tenants:` mapping of every tenant file
    `da-guard parse-tree` selects (ADR-036 step 4, #2906: the exporter's own
    walk — `.yaml` / `.yml` in any case, `.`-prefixed entries skipped, a
    `_`-prefixed platform file never a tenant file, #1603 / #2360), read by
    yaml.v3 (keys as source text, #2216), except the examples/ subtree (dev
    templates, never shipped). A tenant file parse-tree cannot read, or a
    path that is or may hide one (`_lib_parse_tree.TenantScope.unknown`),
    raises YamlFileError naming it (exit 2): which tenants the tree
    declares is unknown.

    WHICH db_type: `_metadata.db_type` as /metrics serves it (`da-guard
    served-values`, #2858) — the value collectTenantExpectedExporter reads,
    with every layer the exporter applies (root platform files' `tenants:`
    entries, the tenant file, the profile filling what neither writes unless
    `optional_overrides` declares `_metadata`). A tenant the exporter serves
    no metadata for counts as none. Raises what `load_served_tree` raises,
    and ParseTreeError for no usable parse-tree (exit 2).
    """
    reader = _lib_parse_cache.reader_for(config_dir)
    # Both halves of the tenant set's scope (#3206): a path that is or may
    # hide a tenant file (a link to a directory, a directory Go could not
    # list) leaves the set unknown exactly as an unreadable tenant file does.
    # The examples/ dev-template subtree is left out of both (not part of a
    # real deployment).
    declared, unknown = _lib_parse_cache.declared_tenants(
        reader, exclude=lambda rel: "examples" in rel.split("/"))
    if unknown:
        raise YamlFileError(str(config_dir / unknown[0].file), unknown[0])
    judged: Set[str] = set(declared)
    if not judged:
        return {}

    tree = load_served_tree(config_dir, binary=binary)
    print_load_warnings(tree)
    out: Dict[str, str] = {}
    for tenant in sorted(judged):
        served = tree.tenants.get(tenant)
        meta = served.values.get("_metadata") if served is not None else None
        db_type = meta.get("db_type") if isinstance(meta, dict) else None
        if isinstance(db_type, str) and db_type:
            out[tenant] = db_type
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


class NamespacesFileError(Exception):
    """The namespaces file cannot be read as kubectl reads it, or kubectl
    would refuse it. ``str()`` is one line; ``detail_lines`` carries
    da-crdecode's own reason."""

    def __init__(self, message: str, detail_lines: List[str] | None = None) -> None:
        super().__init__(message)
        self.detail_lines = list(detail_lines or [])


def namespace_declared_targets(namespaces_file: Path) -> Set[str]:
    """Tenant ids from db-* Namespace `instance` labels (secondary cross-check).

    The file is read as kubectl reads it (`da-crdecode`, every document;
    ADR-036 §12). Raises :class:`NamespacesFileError` when it cannot be, or
    when a Namespace's `instance` label is not a string in that reading
    (`instance: 010` is 8): kubectl refuses such a label, so the value is
    reported, not taken as its text.
    """
    targets: Set[str] = set()
    if not namespaces_file.is_file():
        return targets
    try:
        documents = _lib_consumer_read.kubernetes_documents(str(namespaces_file))
    except _lib_consumer_read.ConsumerReadError as exc:
        raise NamespacesFileError(
            f"{namespaces_file}: cannot be read as kubectl reads it: {exc}",
            exc.stderr_lines) from exc
    for index, doc in enumerate(documents, start=1):
        if not isinstance(doc, dict) or doc.get("kind") != "Namespace":
            continue
        metadata = doc.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        if not isinstance(labels, dict) or "instance" not in labels:
            continue
        inst = labels["instance"]
        if not isinstance(inst, str):
            name = metadata.get("name") if isinstance(metadata, dict) else None
            raise NamespacesFileError(
                f"{namespaces_file}: document {index} (Namespace "
                f"{safe_label(repr(name))}): label `instance` is "
                f"{safe_label(json.dumps(inst))} as Kubernetes reads it, not a string; "
                "kubectl refuses a label that is not a string, so this Namespace "
                "is never created. Quote the value (`instance: \"010\"`).")
        if inst:
            targets.add(inst)
    return targets


def evaluate(config_dir: Path, helm_dir: Path, namespaces_file: Path,
             binary: str | None = None) -> dict:
    """Return {violations: [...], warnings: [...], declared: {...}, targets: [...]}."""
    declared = conf_d_declared_db_type_tenants(config_dir, binary=binary)
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


@exit_on_yaml_file_error  # #1654: a file that does not decode → rc 2, named
@exit_on_served_values_error  # da-guard missing or failing → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # #2906: no usable parse-tree → rc 2
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
    except NamespacesFileError as exc:
        print(f"ERROR: {safe_label(str(exc))}", file=sys.stderr)
        for line in exc.detail_lines:
            print(f"  da-crdecode| {safe_label(line)}", file=sys.stderr)
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
