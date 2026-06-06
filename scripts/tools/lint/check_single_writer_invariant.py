#!/usr/bin/env python3
"""Single-writer invariant lint for the tenant-api write plane (ADR-023).

Codifies ADR-023's "機制強制（三層）" as a commit/CI-time STATIC guard
(layer 2). Layer 1 is the Helm `fail` guard inside the chart template, which
only fires when helm actually renders; this lint catches the same breach
earlier and across BOTH deployment sources — the Helm chart AND the raw
`k8s/04-tenant-api/deployment.yaml` — so a drift in either is a red CI, not a
latent production foot-gun.

WHY: tenant-api's write path (`internal/gitops/Writer`) serializes ALL writes
on one in-process `sync.Mutex`, and PR-mode dedup ("one open PR per tenant")
lives in an in-memory tracker (`internal/platform/tracker.go`). Neither
coordinates across pods. So a SECOND replica is not horizontal scaling — it is
two writers racing on one git working tree (emptyDir: dual-writer push race;
RWO PVC: tree corruption / Multi-Attach stall) plus duplicate PRs for one
tenant. The only thing standing between the platform and that breach is that
nobody raises `replicaCount` and nobody drops `strategy: Recreate`. This lint
turns "nobody does that" into "CI won't let you".

WHAT THIS CHECKS (the two halves of the runtime invariant):
  1. `replicaCount` / `spec.replicas` == 1 on every tenant-api Deployment
     source (Helm values + raw manifest).
  2. `strategy.type: Recreate` is present — replicaCount=1 ALONE is not enough:
     the default RollingUpdate surges a second Ready pod during a rollout, a
     phantom-writer window where two pods each hold their own write lock
     (ADR-023 §A). Both the raw manifest and the Helm template must pin it.
  3. The Helm template still carries the layer-1 `fail` guard (so removing the
     guard, not just bumping the value, is also caught).
  4. No autoscaler manifest (native HPA or KEDA ScaledObject/ScaledJob) is
     shipped under the tenant-api deploy sources — it would drive replicas>1 and
     bypass the replicaCount guard.

WHAT THIS DOES **NOT** CHECK (deliberately):
  - Runtime scaling vectors. `kubectl scale --replicas=2`, a hand-patched live
    Deployment, an HPA that a KEDA controller generates at runtime from a
    ScaledObject, a GitOps controller reconciling such a patch, or a Kustomize
    overlay applied by Argo/Flux — all mutate replicas at RUNTIME, outside any
    static/render-time guard. Their only defense is the deferred A3 K8s Lease
    (ADR-023 layer-3). This lint + the Helm `fail` guard close the
    *config-authoring* vector, not the runtime one.
  - Generic Deployments. This is a NAMED invariant about ONE component
    (tenant-api, the sole stateful write plane). Other services scale freely;
    flagging their replica counts would be noise. The target set is an explicit
    allow-list (`_TARGETS`), not a glob.
  - `writeMode`. ADR-023's original layer-1 spec gated the guard on
    `writeMode != read-only`, but the binary has NO read-only mode
    (`--write-mode` ∈ {direct, pr, pr-github, pr-gitlab}) and values.yaml has no
    such field — writes are ALWAYS on, so the guard is unconditional on
    replicaCount. (This lint codifies the corrected invariant; the ADR §A
    校正 records the same.)

Exit codes:
    0  All tenant-api Deployment sources honor the single-writer invariant
    1  A source violates it (--ci)
    2  Error (a target file is missing / unparseable / no targets)

Usage:
    python scripts/tools/lint/check_single_writer_invariant.py        # report
    python scripts/tools/lint/check_single_writer_invariant.py --ci   # exit 1
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List

import yaml

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover
    def try_utf8_stdout() -> None:  # type: ignore
        pass

# Explicit allow-list: the tenant-api Deployment sources that MUST honor the
# single-writer invariant. NOT a glob — this is a named, single-component
# invariant (see module docstring).
_HELM_VALUES = "helm/tenant-api/values.yaml"
_HELM_TEMPLATE = "helm/tenant-api/templates/deployment.yaml"
_RAW_MANIFEST = "k8s/04-tenant-api/deployment.yaml"
_TARGETS = [_HELM_VALUES, _HELM_TEMPLATE, _RAW_MANIFEST]

# `strategy:` block (optionally with a comment line) whose `type:` is Recreate.
# DESIGN INTENT — Recreate MUST be hardcoded as a literal. A parameterized
# `type: {{ .Values.strategyType | default "Recreate" }}` passes Helm and even
# defaults to Recreate, yet deliberately FAILS this literal-match check: we
# forbid making the single-writer-critical strategy overridable via values
# (an operator could then flip it to RollingUpdate and reopen the phantom-writer
# window). The "rigid" regex is the enforcement, not a limitation.
_RECREATE_RE = re.compile(
    r"strategy:\s*\n(?:\s*#[^\n]*\n)*\s*type:\s*Recreate\b"
)
# The layer-1 fail guard: a `gt ... replicaCount ... 1` test that calls `fail`.
# Tolerant of arg order / formatting — just proves a fail-guard keyed on
# replicaCount is present in the template.
_GUARD_RE = re.compile(
    r"\.Values\.replicaCount.*?\bfail\b|\bfail\b.*?\.Values\.replicaCount",
    re.DOTALL,
)
# An autoscaler targeting tenant-api would drive replicas > 1 and BYPASS the
# replicaCount guard (the guard fires on helm render; an autoscaler mutates
# replicas at runtime). The autoscaler MANIFEST is a commit-time artifact, so it
# IS lintable here — both native HPA and KEDA (ScaledObject/ScaledJob, which a
# KEDA controller expands into an HPA). Covered = the committed autoscaler spec.
# OUT OF SCOPE (→ Layer 3 K8s Lease only): runtime mutations no static check can
# see — `kubectl scale`, an HPA a controller generates at runtime, a GitOps
# controller reconciling a hand-patched `replicas`, or a Kustomize overlay
# applied by Argo/Flux.
_AUTOSCALER_SCAN_DIRS = ["helm/tenant-api/templates", "k8s/04-tenant-api"]
_AUTOSCALER_RE = re.compile(
    r"kind:\s*(HorizontalPodAutoscaler|ScaledObject|ScaledJob)\b"
)


def check_raw_deployment(data: dict) -> List[str]:
    """Pure core: violations for a parsed k8s Deployment manifest dict.

    Empty list = honors the invariant (replicas==1 AND strategy.type==Recreate).
    A non-Deployment doc returns [] (caller filters by kind)."""
    if not isinstance(data, dict) or data.get("kind") != "Deployment":
        return []
    out: List[str] = []
    spec = data.get("spec") or {}
    replicas = spec.get("replicas")
    if replicas != 1:
        out.append(
            f"spec.replicas={replicas!r} (MUST be 1 — single-writer; "
            f"multi-replica corrupts the shared git tree)"
        )
    strat = (spec.get("strategy") or {})
    if strat.get("type") != "Recreate":
        out.append(
            f"spec.strategy.type={strat.get('type')!r} (MUST be 'Recreate' — "
            f"RollingUpdate surges a phantom second writer, ADR-023 §A)"
        )
    return out


def check_helm_values(values: dict) -> List[str]:
    """Pure core: violations for parsed helm values (replicaCount==1)."""
    if not isinstance(values, dict):
        return ["values root is not a mapping"]
    rc = values.get("replicaCount")
    if rc != 1:
        return [
            f"replicaCount={rc!r} (MUST be 1 — single-writer; horizontal write "
            f"scaling needs a K8s Lease, ADR-023 Deferred A3, not a bump)"
        ]
    return []


def template_has_recreate(text: str) -> bool:
    """The Helm Deployment template pins strategy.type: Recreate."""
    return bool(_RECREATE_RE.search(text))


def template_has_guard(text: str) -> bool:
    """The Helm template still carries the layer-1 replicaCount fail guard."""
    return bool(_GUARD_RE.search(text))


def has_autoscaler(text: str) -> bool:
    """A manifest/template declares an autoscaler (HPA or KEDA ScaledObject/Job)."""
    return bool(_AUTOSCALER_RE.search(text))


def find_autoscaler(repo: Path) -> List[str]:
    """Violations for any autoscaler manifest shipped under the tenant-api deploy
    sources. An autoscaler would drive replicas>1, bypassing the replicaCount
    guard. Covers committed HPA + KEDA specs; runtime-generated scaling is out of
    scope (→ Layer 3 Lease)."""
    out: List[str] = []
    for d in _AUTOSCALER_SCAN_DIRS:
        base = repo / d
        if not base.exists():
            continue
        for f in sorted(base.rglob("*")):
            if f.suffix not in (".yaml", ".yml", ".tpl") or not f.is_file():
                continue
            if has_autoscaler(f.read_text(encoding="utf-8")):
                out.append(
                    f"{f.relative_to(repo).as_posix()}: autoscaler "
                    f"(HPA/ScaledObject) present — tenant-api is single-writer; "
                    f"it would drive replicas>1 and bypass the guard (ADR-023)"
                )
    return out


def _yaml_docs(path: Path):
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def check_targets(repo: Path) -> List[str]:
    """Run all checks against the live target files. Returns violation lines
    (each prefixed with the relative path). Raises FileNotFoundError if a
    target is missing (caller-error: the invariant's subject moved)."""
    findings: List[str] = []

    # 1. Helm values — replicaCount == 1
    vpath = repo / _HELM_VALUES
    values = yaml.safe_load(vpath.read_text(encoding="utf-8")) or {}
    for v in check_helm_values(values):
        findings.append(f"{_HELM_VALUES}: {v}")

    # 2. Helm template — strategy Recreate + layer-1 guard present
    tpath = repo / _HELM_TEMPLATE
    ttext = tpath.read_text(encoding="utf-8")
    if not template_has_recreate(ttext):
        findings.append(f"{_HELM_TEMPLATE}: missing `strategy:\\n  type: Recreate`")
    if not template_has_guard(ttext):
        findings.append(
            f"{_HELM_TEMPLATE}: missing the layer-1 `fail` guard on replicaCount>1 "
            f"(a removed guard lets a bad value render silently)"
        )

    # 3. Raw manifest — Deployment replicas==1 + strategy Recreate
    rpath = repo / _RAW_MANIFEST
    deploys = [d for d in _yaml_docs(rpath) if isinstance(d, dict)
               and d.get("kind") == "Deployment"]
    if not deploys:
        findings.append(f"{_RAW_MANIFEST}: no Deployment document found")
    for d in deploys:
        for v in check_raw_deployment(d):
            findings.append(f"{_RAW_MANIFEST}: {v}")

    # 4. No autoscaler (HPA / KEDA ScaledObject) may target tenant-api
    findings.extend(find_autoscaler(repo))

    return findings


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Single-writer invariant lint for tenant-api (ADR-023)")
    parser.add_argument("--ci", action="store_true", help="exit 1 on violation")
    args = parser.parse_args()

    repo = Path(_THIS_DIR).resolve()
    for parent in [repo, *repo.parents]:
        if (parent / ".git").exists():
            repo = parent
            break

    missing = [t for t in _TARGETS if not (repo / t).exists()]
    if missing:
        print(f"ERROR: tenant-api Deployment source(s) not found: {missing} "
              f"(the single-writer invariant's subject moved — update _TARGETS)",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        findings = check_targets(repo)
    except yaml.YAMLError as exc:
        print(f"ERROR: YAML parse failure: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    if findings:
        print("❌ tenant-api single-writer invariant (ADR-023) violated:",
              file=sys.stderr)
        for f in findings:
            print(f"  ❌ {f}", file=sys.stderr)
        print("\nThe write plane is a single writer with no cross-pod "
              "coordination. Keep replicaCount=1 + strategy: Recreate on every "
              "Deployment source. To scale writes, implement a K8s Lease "
              "(ADR-023 Deferred option A3).", file=sys.stderr)
        return EXIT_VIOLATION if args.ci else EXIT_OK
    print(f"✅ tenant-api single-writer invariant holds across {len(_TARGETS)} "
          f"Deployment source(s) (replicaCount=1 + strategy: Recreate + guard).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
