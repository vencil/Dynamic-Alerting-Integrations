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


def collect_instances(config_dir: Path) -> List[Tuple[str, dict, str]]:
    """Return (tenant, instance, origin) triples for every effective declaration.

    origin is a human string (the file the instance was declared in) for error
    messages. Inherited platform/domain instances are attributed to each tenant
    they land on (that is what cap-counting / scope mean).
    """
    config_dir = Path(config_dir)
    dir_alerts = _dir_defaults_alerts(config_dir)
    triples: List[Tuple[str, dict, str]] = []

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
                triples.append((tenant, inst, f"{rel} (inherited _defaults.yaml)"))
            for inst in own:
                triples.append((tenant, inst, str(rel)))
    return triples


def build_shapes(config_dir: Path) -> Tuple[List[dict], Dict[str, int]]:
    """Group all effective instances into shapes; return (shapes, per_tenant_count).

    Each shape dict carries the representative params + recipe_id + sorted
    severities union. Raises CustomAlertConfigError on a uniqueness violation.
    """
    triples = collect_instances(config_dir)

    # validation accumulators
    name_seen: Dict[Tuple[str, str], str] = {}              # (tenant, name) → origin
    sev_seen: Dict[Tuple[str, str, str], str] = {}          # (tenant, rid, sev) → origin
    per_tenant: Dict[str, int] = defaultdict(int)

    # shape accumulators
    shapes: Dict[str, dict] = {}                            # recipe_id → shape
    shape_sev: Dict[str, set] = defaultdict(set)            # recipe_id → severities
    sig_seen: Dict[str, tuple] = {}                         # recipe_id → shape_signature

    # required fields — match tenant-config.schema.json's customAlertInstance
    # required[]. `window`/`threshold` are NOT optional: an empty window emits
    # invalid PromQL (`rate(m[])`), and the threshold carries the severity.
    required = ("recipe", "name", "metric", "window", "threshold")

    for tenant, inst, origin in triples:
        missing = [f for f in required if f not in inst]
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
        per_tenant[tenant] += 1

        if rid not in shapes:
            shapes[rid] = {
                "recipe_id": rid,
                "recipe": inst["recipe"],
                "metric": inst["metric"],
                "op": inst.get("op", ">"),
                "window": inst.get("window", ""),
                "quantile": inst.get("quantile", "0.99"),
                "denominator_metric": inst.get("denominator_metric", ""),
                "selectors": inst.get("selectors") or {},
                "selectors_re": inst.get("selectors_re") or {},
                "for": inst.get("for", "1m"),
            }
        shape_sev[rid].add(sev)

    result: List[dict] = []
    for rid in sorted(shapes):
        sh = shapes[rid]
        sh["severities"] = sorted(shape_sev[rid])      # deterministic order
        result.append(sh)
    return result, dict(per_tenant)


def count_recipes_per_tenant(config_dir: Path) -> Dict[str, int]:
    """Cap-enforcement SEAM (S4): effective recipe count per tenant. Log-only now."""
    _shapes, per_tenant = build_shapes(config_dir)
    return per_tenant
