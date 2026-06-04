"""conf.d tree walk + _custom_alerts inheritance + shape grouping (ADR-024, #741 S2).

Scope = declaration level (ADR-017/018): a recipe declared in a directory's
`_defaults.yaml` applies to every tenant in that subtree; a tenant-leaf recipe
applies to that tenant only. Inheritance for `_custom_alerts` is UNION across
levels (a tenant's own list ADDS to inherited platform/domain lists) — unlike
the standard array-replace merge, because a tenant must not silently wipe a
platform/domain policy recipe.

The scope is NOT baked into the emitted rule: the vectorised `on(tenant)` join
auto-scopes because only declaring tenants will have a `user_threshold{recipe_id}`
series (emitted by the exporter in S3). The tree walk here exists for validation
(name/severity uniqueness, cap counting) and to compute each shape's severity
union (which per-severity branches to emit).
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from . import shape as _shape


class CustomAlertConfigError(ValueError):
    """A declaration tree is invalid (rejected at compile time)."""


# Per-tenant cap on TENANT-OWN custom-alert recipes (ADR-024 §Custom Alerts cost
# guardrail, S4). Bounds the rule-count explosion: custom-alert rules grow
# ~ N_tenants × (own recipes) because each tenant's unique-metric recipe is its
# OWN shape (NOT vectorized across tenants). INHERITED platform/domain recipes
# are vectorized (one shared rule for the whole subtree, O(1) in tenant count),
# so they do NOT count toward this cap. Provisional default; the final value is
# to be back-derived from a rule-eval-duration benchmark (ADR-024 AC #5).
# Override via build_shapes(..., max_custom_recipes=) / `--max-custom-recipes`.
MAX_CUSTOM_RECIPES_DEFAULT = 20


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _dir_defaults_alerts(config_dir: Path) -> Dict[Path, List[dict]]:
    """Map each directory → its _defaults.yaml top-level `_custom_alerts` list."""
    out: Dict[Path, List[dict]] = {}
    for root, _dirs, files in os.walk(config_dir):
        if "_defaults.yaml" in files:
            data = _load_yaml(Path(root) / "_defaults.yaml")
            alerts = data.get("_custom_alerts") or []
            if alerts:
                out[Path(root).resolve()] = list(alerts)
    return out


def _inherited_for(leaf_dir: Path, config_dir: Path,
                   dir_alerts: Dict[Path, List[dict]]) -> List[dict]:
    """All ancestor _defaults.yaml _custom_alerts that apply to a leaf dir."""
    inherited: List[dict] = []
    d = leaf_dir.resolve()
    stop = config_dir.resolve()
    chain: List[Path] = []
    while True:
        chain.append(d)
        if d == stop or d.parent == d:
            break
        d = d.parent
    # platform (shallowest) first → domain → subdomain, so cap-count / ordering
    # is deterministic and platform policy reads first.
    for d in reversed(chain):
        inherited.extend(dir_alerts.get(d, []))
    return inherited


def collect_instances(config_dir: Path) -> List[Tuple[str, dict, str, bool]]:
    """Return (tenant, instance, origin, is_own) tuples for every effective decl.

    origin is a human string (the file the instance was declared in) for error
    messages. is_own distinguishes a tenant's OWN declaration from an INHERITED
    platform/domain policy (the latter is vectorized + does NOT count toward the
    per-tenant cap). Inherited instances are attributed to each tenant they land
    on (that is what scope / effective-count mean).
    """
    config_dir = Path(config_dir)
    dir_alerts = _dir_defaults_alerts(config_dir)
    triples: List[Tuple[str, dict, str, bool]] = []

    for path in sorted(config_dir.rglob("*.yaml")):
        if path.name == "_defaults.yaml":
            continue
        data = _load_yaml(path)
        tenants = data.get("tenants") or {}
        if not isinstance(tenants, dict):
            continue
        inherited = _inherited_for(path.parent, config_dir, dir_alerts)
        for tenant, cfg in tenants.items():
            if not isinstance(cfg, dict):
                continue
            own = cfg.get("_custom_alerts") or []
            rel = path.relative_to(config_dir)
            for inst in inherited:
                triples.append((tenant, inst, f"{rel} (inherited _defaults.yaml)", False))
            for inst in own:
                triples.append((tenant, inst, str(rel), True))
    return triples


def build_shapes(config_dir: Path,
                 max_custom_recipes: int = MAX_CUSTOM_RECIPES_DEFAULT
                 ) -> Tuple[List[dict], Dict[str, int]]:
    """Group all effective instances into shapes; return (shapes, per_tenant_count).

    Each shape dict carries the representative params + recipe_id + sorted
    severities union. per_tenant_count is the EFFECTIVE count (own + inherited).
    Raises CustomAlertConfigError on a uniqueness violation OR when a tenant's
    OWN recipe count exceeds `max_custom_recipes` (the cost guardrail — inherited
    policy is vectorized and not counted; see MAX_CUSTOM_RECIPES_DEFAULT).
    """
    # Reject a nonsensical cap up front (CLI --max-custom-recipes is type=int, so
    # a negative slips through argparse) — else the cap check below rejects EVERY
    # tenant with a confusing "exceeds cap (-1)" message. 0 IS valid (= forbid
    # tenant-own recipes). CustomAlertConfigError → compile main exits 2 cleanly.
    if max_custom_recipes < 0:
        raise CustomAlertConfigError(
            f"max_custom_recipes must be >= 0 (got {max_custom_recipes})"
        )
    triples = collect_instances(config_dir)

    # validation accumulators
    name_seen: Dict[Tuple[str, str], str] = {}              # (tenant, name) → origin
    sev_seen: Dict[Tuple[str, str, str], str] = {}          # (tenant, rid, sev) → origin
    per_tenant: Dict[str, int] = defaultdict(int)           # EFFECTIVE (own + inherited)
    own_per_tenant: Dict[str, int] = defaultdict(int)       # OWN only → the capped count

    # shape accumulators
    shapes: Dict[str, dict] = {}                            # recipe_id → shape
    shape_sev: Dict[str, set] = defaultdict(set)            # recipe_id → severities
    sig_seen: Dict[str, tuple] = {}                         # recipe_id → shape_signature

    # required fields — match tenant-config.schema.json's customAlertInstance.
    # threshold carries the severity. The SHAPING duration is recipe-aware:
    # `forecast` supplies `horizon` (lookback is platform-derived from it, so it
    # never takes a `window`), every other recipe supplies `window` (an empty
    # window emits invalid PromQL like `rate(m[])`).
    base_required = ("recipe", "name", "metric", "threshold")

    for tenant, inst, origin, is_own in triples:
        shape_required = ("horizon",) if inst.get("recipe") == "forecast" else ("window",)
        missing = [f for f in base_required + shape_required if f not in inst]
        if missing:
            raise CustomAlertConfigError(
                f"{origin}: tenant={tenant}: _custom_alerts entry missing "
                f"required field(s) {missing}: {inst!r}"
            )
        name = str(inst["name"])
        try:
            rid = _shape.recipe_id(inst)
            sig = _shape.shape_signature(inst)
            _value, sev = _shape.parse_threshold(inst["threshold"])
        except _shape.RecipeError as e:
            raise CustomAlertConfigError(f"{origin}: tenant={tenant}: {e}") from e

        # recipe_id is a sanitised slug, so two DIFFERENT shapes (e.g. selector
        # values "5.." vs "5__") could collapse to the same rid. The loader dedups
        # by rid and keeps one representative selector, which would silently emit
        # the wrong selector for the others. Fail fast on a slug collision.
        if rid in sig_seen and sig_seen[rid] != sig:
            raise CustomAlertConfigError(
                f"{origin}: tenant={tenant}: recipe_id {rid!r} collides between two "
                f"distinct shapes (selector/param values differ but sanitise to the "
                f"same slug); rename a selector value to disambiguate"
            )
        sig_seen[rid] = sig

        # name unique within a tenant (its scope). Inheritance never double-counts
        # the same instance, so any repeat is a genuine collision.
        nkey = (tenant, name)
        if nkey in name_seen:
            raise CustomAlertConfigError(
                f"tenant={tenant}: duplicate custom-alert name {name!r} "
                f"(in {name_seen[nkey]} and {origin}); names must be unique per tenant"
            )
        name_seen[nkey] = origin

        # (tenant, recipe_id, severity) unique → keeps the group_left(name) join 1:1
        skey = (tenant, rid, sev)
        if skey in sev_seen:
            raise CustomAlertConfigError(
                f"tenant={tenant}: two {sev} custom alerts share the same shape "
                f"{rid!r} (in {sev_seen[skey]} and {origin}); a tenant may declare "
                f"at most one {sev} alert per shape"
            )
        sev_seen[skey] = origin
        # Quota is charged AFTER all uniqueness/validation checks above, so it
        # counts only a VALIDATED, distinct (tenant, recipe_id, severity) instance
        # — NOT a copy-paste duplicate nor a re-declaration of an inherited policy
        # shape (both fail loud at the name/severity checks above, before reaching
        # here). So a tenant never "loses" quota to a dedup'd duplicate. A
        # multi-severity same-shape recipe (warning + critical) legitimately counts
        # as 2: it compiles to two distinct alert rules.
        per_tenant[tenant] += 1
        if is_own:
            own_per_tenant[tenant] += 1

        if rid not in shapes:
            shapes[rid] = {
                "recipe_id": rid,
                "recipe": inst["recipe"],
                "metric": inst["metric"],
                "op": inst.get("op", ">"),
                "window": inst.get("window", ""),
                "quantile": inst.get("quantile", "0.99"),
                "denominator_metric": inst.get("denominator_metric", ""),
                "horizon": inst.get("horizon", ""),          # forecast predict-ahead
                "capacity_metric": inst.get("capacity_metric", ""),  # forecast ratio mode
                "selectors": inst.get("selectors") or {},
                "selectors_re": inst.get("selectors_re") or {},
                "for": inst.get("for", "1m"),
            }
        shape_sev[rid].add(sev)

    # Cost guardrail (S4): cap TENANT-OWN recipes (inherited policy is vectorized,
    # O(1) in tenant count → uncapped). Fail loud at compile time (deterministic,
    # actionable) rather than silently truncate, so a tenant's GitOps PR surfaces
    # the over-cap clearly. ADR-024 §Custom Alerts cost guardrail / AC #5.
    for tenant in sorted(own_per_tenant):
        if own_per_tenant[tenant] > max_custom_recipes:
            raise CustomAlertConfigError(
                f"tenant={tenant}: {own_per_tenant[tenant]} own custom-alert recipes "
                f"exceeds the max_custom_recipes cap ({max_custom_recipes}); reduce the "
                f"tenant's own _custom_alerts (inherited platform/domain policy is "
                f"vectorized and does NOT count toward this cap)"
            )

    result: List[dict] = []
    for rid in sorted(shapes):
        sh = shapes[rid]
        sh["severities"] = sorted(shape_sev[rid])      # deterministic order
        result.append(sh)
    return result, dict(per_tenant)


def count_recipes_per_tenant(config_dir: Path) -> Dict[str, int]:
    """Effective recipe count per tenant (own + inherited). The OWN-recipe cap is
    enforced inside build_shapes (S4) — this raises CustomAlertConfigError if any
    tenant is over cap."""
    _shapes, per_tenant = build_shapes(config_dir)
    return per_tenant
