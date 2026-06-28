#!/usr/bin/env python3
"""verify_tenant_projections.py — fail-closed correctness gate for tenant log
projections (ADR-021 Phase 2(a) / #908).

WHY THIS EXISTS
---------------
`tenantProjections` (helm/vector values) is the ENTIRE trust root of tenant log
isolation: each entry `{tenantId, accountId}` decides which VictoriaLogs partition
(AccountID) a tenant's sanitized logs are written to. The accountId is allocated,
once and immutably, by tenant-api into `_account_registry.yaml` (the SSOT, a
separate conf.d git repo).

The chart's render-time `{{ fail }}` guard already rejects DUPLICATE accountId /
tenantId, and `values.schema.json` rejects a non-int / <1000 accountId. But NONE
of them catch a **unique-but-wrong** accountId — e.g. `{tenant-alpha: 1001}` when
the registry allocates 1000. That value is unique, an int, >=1000, so every
existing guard passes it — and the tenant's logs then land silently in tenant
1001's partition = a cross-tenant leak. There is also no committed deploy pipeline
(operators run `helm upgrade --set`), so a hand-copied projection list is never
even guaranteed a PR review.

This gate closes exactly that hole: it compares the deployed `tenantProjections`
against the live registry and FAILS CLOSED on any mismatch. It is meant to run as
a Vector init-container (the only un-bypassable enforcement point: Vector cannot
load its config without the init-container having run), reading the registry
read-only from the same conf.d mount the threshold-exporter already uses.

THE INVARIANT (MVP — needs only the registry)
---------------------------------------------
    forall p in tenantProjections:  registry.allocations[p.tenantId] == p.accountId

A projection whose tenantId is absent from the registry, or whose accountId does
not match the registry's allocation, is a violation. (The COMPLETENESS direction
— every log-fed-enabled tenant HAS a projection — needs an enablement SSOT, a
deferred conf.d flag; see #908 decision record. It is intentionally NOT enforced
here.)

FAIL-CLOSED + FAIL-AVAILABLE
----------------------------
A violation (config bug) or an unreadable/untrustworthy registry (infra) must
NOT let the wrong projection take effect, but must also NOT take Vector down — the
platform full copy to partition 0:0 is independent of tenantProjections and is the
safe default. So the gate DEGRADES to 0:0-only (drops the tenant projection) and
alerts loudly, rather than crashing. An `enforce` mode is offered for paranoid
environments where a config-bug mismatch should hard-fail the pod instead.

`evaluate()` is a PURE function `(registry, projections) -> Verdict` so CI can test
the security logic with synthetic fixtures, decoupled from any cluster (the
plane-split means CI has no real registry). `main()` is the thin init-container
glue that loads files, calls `evaluate()`, selects the effective config, and emits
a Prometheus textfile metric.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # PyYAML is the only third-party dep; the init-container image bakes it in.
    import yaml
except ImportError:  # pragma: no cover - exercised only in a misbuilt image
    yaml = None

# Mirror the Go SSOT (components/tenant-api/internal/federation/account/registry.go):
# the on-disk registry is schema v1; tenant ids start at 1000 (0 = platform default
# partition, 1..999 reserved). Keep in lockstep with FirstTenantAccountID there.
_SCHEMA_VERSION = "v1"
_FIRST_TENANT_ACCOUNT_ID = 1000

# Verdict categories. A config-bug (mismatch) and an infra problem
# (registry_unreadable) are DISTINCT so the alert can carry the right reason label
# and on-call is not sent to debug a config typo when conf.d simply failed to mount.
CAT_OK = "ok"
CAT_MISMATCH = "mismatch"
CAT_REGISTRY_UNREADABLE = "registry_unreadable"


@dataclass
class Verdict:
    category: str
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.category == CAT_OK


def evaluate(registry: dict[str, Any], projections: list[dict[str, Any]]) -> Verdict:
    """Pure core. Validate the anti-leak invariant against an already-parsed
    registry and projection list. Returns a Verdict; does not raise on data
    content. A structurally untrustworthy registry (unknown schema, malformed
    allocations) yields CAT_REGISTRY_UNREADABLE — fail-closed, never "assume ok".
    """
    # Refuse a newer/unknown registry rather than silently validating against a
    # shape this binary does not understand (mirrors Go account.Parse fail-closed).
    sv = registry.get("schema_version")
    if sv not in (None, "", _SCHEMA_VERSION):
        return Verdict(
            CAT_REGISTRY_UNREADABLE,
            [f"registry schema_version {sv!r} is newer/unknown (want {_SCHEMA_VERSION!r}); refusing to validate"],
        )

    allocations = registry.get("allocations")
    if allocations is None:
        allocations = {}
    if not isinstance(allocations, dict):
        return Verdict(
            CAT_REGISTRY_UNREADABLE,
            [f"registry `allocations` is {type(allocations).__name__}, want a mapping"],
        )

    violations: list[str] = []
    for i, p in enumerate(projections):
        if not isinstance(p, dict):
            violations.append(f"projection[{i}] is {type(p).__name__}, want a mapping with tenantId/accountId")
            continue
        tid = p.get("tenantId")
        acct = p.get("accountId")
        if tid is None or acct is None:
            violations.append(f"projection[{i}] is missing tenantId and/or accountId: {p!r}")
            continue
        if tid not in allocations:
            violations.append(
                f"tenantId {tid!r} (projected accountId {acct}) is NOT allocated in the registry — "
                "an unknown or typo'd tenantId; projecting it risks writing to a foreign/未配 partition"
            )
            continue
        want = allocations[tid]
        if acct != want:
            violations.append(
                f"tenantId {tid!r} projects accountId {acct} but the registry allocates {want} "
                f"— unique-but-wrong: this tenant's logs would land in partition {want} (cross-tenant leak)"
            )

    return Verdict(CAT_MISMATCH if violations else CAT_OK, violations)


def load_yaml(path: Path) -> Any:
    """Load a YAML document, raising a clear error the caller maps to a verdict."""
    if yaml is None:
        raise RuntimeError("PyYAML is not installed in the init-container image")
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def load_registry(path: Path) -> dict[str, Any]:
    """Parse `_account_registry.yaml`. A blank file (GitOps has not written it yet)
    parses to an empty registry at the reserved floor — NOT an error, matching the
    Go reader — so a brand-new cluster with no allocations and no projections is OK.
    """
    doc = load_yaml(path)
    if doc is None:  # blank/whitespace file
        return {"schema_version": _SCHEMA_VERSION, "next_account_id": _FIRST_TENANT_ACCOUNT_ID, "allocations": {}}
    if not isinstance(doc, dict):
        raise ValueError(f"registry root is {type(doc).__name__}, want a mapping")
    return doc


def load_projections(path: Path) -> list[dict[str, Any]]:
    """Parse the deployed tenantProjections (a YAML/JSON list the chart renders to a
    mounted file). A blank/absent projection set is an empty list — the all-0:0
    baseline, trivially valid."""
    doc = load_yaml(path)
    if doc is None:
        return []
    if not isinstance(doc, list):
        raise ValueError(f"projections root is {type(doc).__name__}, want a list")
    return doc


# Prometheus textfile-collector format. The init-container writes this to a shared
# volume a node-exporter/Vector textfile collector scrapes, so the degrade is
# loud even though Vector stays up. 1 = condition holds.
_METRIC_NAME = "vector_tenant_projection_gate_info"


def render_metric(verdict: Verdict, mode: str) -> str:
    lines = [
        f"# HELP {_METRIC_NAME} Fail-closed gate verdict for tenantProjections vs the account registry (#908). 1=active.",
        f"# TYPE {_METRIC_NAME} gauge",
        f'{_METRIC_NAME}{{category="{verdict.category}",mode="{mode}"}} 1',
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fail-closed tenantProjections-vs-registry gate (#908).")
    ap.add_argument("--registry", required=True, type=Path, help="path to _account_registry.yaml (conf.d, read-only)")
    ap.add_argument("--projections", required=True, type=Path, help="path to the rendered tenantProjections list")
    ap.add_argument("--mode", choices=("degrade", "enforce"), default="degrade",
                    help="degrade (default): mismatch -> drop tenant projection, stay up. enforce: mismatch -> fail pod.")
    # Config-dir fragment model ("render, don't transform"): the tenant routing is a
    # SEPARATE Vector config file. The base (0:0-only) is always placed; the tenant
    # FRAGMENT is placed only when the gate passes. Component presence is decided by
    # file presence, never by mutating a config in place.
    ap.add_argument("--base-config", type=Path, help="base Vector config (0:0-only); ALWAYS placed into --config-dir")
    ap.add_argument("--fragment-config", type=Path, help="tenant-routing fragment; placed into --config-dir ONLY when the gate passes")
    ap.add_argument("--config-dir", type=Path, help="writable Vector --config-dir the init-container populates (Vector loads this)")
    ap.add_argument("--metrics-file", type=Path, help="Prometheus textfile to write the verdict metric to")
    args = ap.parse_args(argv)

    # Load + evaluate. A load failure on the REGISTRY is fail-available (degrade);
    # a load failure on the PROJECTIONS is a config bug (treat as mismatch).
    try:
        registry = load_registry(args.registry)
    except Exception as e:  # noqa: BLE001 - any read/parse failure is the infra path
        verdict = Verdict(CAT_REGISTRY_UNREADABLE, [f"cannot read registry {args.registry}: {e}"])
    else:
        try:
            projections = load_projections(args.projections)
        except Exception as e:  # noqa: BLE001
            verdict = Verdict(CAT_MISMATCH, [f"cannot read projections {args.projections}: {e}"])
        else:
            verdict = evaluate(registry, projections)

    # Report loudly to stderr (init-container logs) regardless of action.
    if verdict.ok:
        print(f"[tenant-projection-gate] OK — all projections match the registry (mode={args.mode}).", file=sys.stderr)
    else:
        print(f"[tenant-projection-gate] {verdict.category.upper()} (mode={args.mode}):", file=sys.stderr)
        for v in verdict.violations:
            print(f"  - {v}", file=sys.stderr)

    if args.metrics_file:
        args.metrics_file.write_text(render_metric(verdict, args.mode), encoding="utf-8")

    # Populate the config-dir + decide the exit code.
    #   - The base (0:0-only) config is ALWAYS placed, so Vector has a valid topology
    #     even on degrade.
    #   - OK                  -> ALSO place the tenant fragment,  exit 0 (full mode).
    #   - MISMATCH + enforce  -> exit 1 (init fails -> pod will not start; loud).
    #   - MISMATCH + degrade  -> fragment NOT placed,  exit 0 (Vector up, 0:0-only).
    #   - REGISTRY_UNREADABLE -> fragment NOT placed,  exit 0 ALWAYS — an infra hiccup
    #     must not self-DoS, and degraded IS the secure state (platform copy only).
    _place(args.base_config, args.config_dir, _BASE_NAME)
    if verdict.ok:
        _place(args.fragment_config, args.config_dir, _FRAGMENT_NAME)
        return 0
    if verdict.category == CAT_MISMATCH and args.mode == "enforce":
        print("[tenant-projection-gate] enforce mode: refusing to start Vector with a mismatched projection.", file=sys.stderr)
        return 1
    _remove(args.config_dir, _FRAGMENT_NAME)  # defensive: ensure 0:0-only on a re-run
    return 0


# Lexical order matters: Vector merges --config-dir files; the base must load before
# the fragment. The numeric prefixes pin that order.
_BASE_NAME = "00-base.yaml"
_FRAGMENT_NAME = "30-tenant-routing.yaml"


def _place(src: Path | None, config_dir: Path | None, name: str) -> None:
    """Copy a staged config file into the config-dir under `name`. No-op when the
    paths are not wired (a CI/dry-run invocation that only wants the verdict)."""
    if src is None or config_dir is None:
        return
    config_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, config_dir / name)


def _remove(config_dir: Path | None, name: str) -> None:
    """Ensure a config-dir file is absent (idempotent — a fresh emptyDir has none)."""
    if config_dir is None:
        return
    target = config_dir / name
    if target.exists():
        target.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
