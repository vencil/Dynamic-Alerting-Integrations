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


def _file_skip(origin: str, exc: Exception) -> dict:
    """Skip record for a conf.d FILE that could not be loaded (malformed YAML / control
    chars / bad encoding). Quarantined fail-soft (#1008 Part B): the YAML load happens
    OUTSIDE the per-recipe try, so without this one bad file (incl. a schema-check-skipped
    meta file) would crash the whole shared compile → block every tenant's PR."""
    return {"tenant": None, "origin": origin, "name": None,
            "reason": f"{type(exc).__name__}: {exc}"}


def _dir_defaults_alerts(config_dir: Path, file_errors: List[dict]) -> Dict[Path, List[dict]]:
    """Map each directory → its _defaults.yaml top-level `_custom_alerts` list. A
    _defaults.yaml that fails to load is quarantined into `file_errors` (#1008 Part B),
    not raised."""
    out: Dict[Path, List[dict]] = {}
    for root, _dirs, files in os.walk(config_dir):
        if "_defaults.yaml" in files:
            p = Path(root) / "_defaults.yaml"
            try:
                data = _load_yaml(p)
            except Exception as exc:  # noqa: BLE001 — malformed file quarantined, not fatal
                file_errors.append(_file_skip(str(p.relative_to(config_dir)), exc))
                continue
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


def collect_instances(config_dir: Path) -> Tuple[List[Tuple[str, dict, str, bool]], List[dict]]:
    """Return (triples, file_errors): (tenant, instance, origin, is_own) tuples for every
    effective decl, PLUS skip records for conf.d files that failed to load.

    origin is a human string (the file the instance was declared in) for error messages.
    is_own distinguishes a tenant's OWN declaration from an INHERITED platform/domain
    policy (the latter is vectorized + does NOT count toward the per-tenant cap). Inherited
    instances are attributed to each tenant they land on (that is what scope / effective-
    count mean). A file that yaml.safe_load can't parse (malformed / control chars / bad
    encoding) is QUARANTINED into file_errors and skipped (#1008 Part B), never raised —
    this load is outside the per-recipe fail-soft loop, so without it one bad file would
    crash the whole compile.
    """
    config_dir = Path(config_dir)
    file_errors: List[dict] = []
    dir_alerts = _dir_defaults_alerts(config_dir, file_errors)
    triples: List[Tuple[str, dict, str, bool]] = []

    for path in sorted(config_dir.rglob("*.yaml")):
        if path.name == "_defaults.yaml":
            continue
        try:
            data = _load_yaml(path)
        except Exception as exc:  # noqa: BLE001 — malformed file quarantined, not fatal
            file_errors.append(_file_skip(str(path.relative_to(config_dir)), exc))
            continue
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
    return triples, file_errors


_NOTICE_TENANT_SAMPLE = 10   # max tenant names per lifecycle notice (CI-log readability)


def _summarize_tenants(tenants) -> str:
    """`<count> tenant(s) (<=N names[, and M more])` for a lifecycle notice line.

    A deprecated/eol SHARED recipe can be declared by hundreds of tenants (sharing
    is the whole point of vectorization); joining them all makes a multi-kilobyte
    single-line CI warning that some log collectors truncate mid-line. Lead with
    the actionable COUNT, then a bounded sample of names. Full per-tenant
    enumeration belongs in a metric/dashboard, not a log line.
    """
    names = sorted(tenants)
    shown = ", ".join(names[:_NOTICE_TENANT_SAMPLE])
    if len(names) > _NOTICE_TENANT_SAMPLE:
        shown += f", and {len(names) - _NOTICE_TENANT_SAMPLE} more"
    return f"{len(names)} tenant(s) ({shown})"


def collect_lifecycle_notices(config_dir: Path) -> List[str]:
    """Human-readable notices for non-active recipes in use (ADR-024 #6).

    One notice per (status, recipe) that a deprecated/eol recipe is declared by
    >=1 tenant, naming the affected tenants. Empty when every declared recipe is
    active. The compiler surfaces these to stderr as NON-FATAL warnings:
    deprecated/eol existing declarations KEEP compiling (no silent alert loss).
    The write-side eol *rejection* lives in tenant-api preflight, not here — the
    batch compiler must never drop a deployed tenant's rule just because the
    platform retired the recipe.
    """
    by_status: Dict[Tuple[str, str], set] = defaultdict(set)   # (status, recipe) → tenants
    triples, _file_errors = collect_instances(config_dir)   # unloadable files → build_shapes reports them
    for tenant, inst, _origin, _is_own in triples:
        if not isinstance(inst, dict):
            continue   # malformed entry (e.g. a scalar _custom_alerts) — quarantined by
            #            build_shapes (#1008 Part B); it is not a recipe for lifecycle purposes
        recipe = inst.get("recipe")
        if recipe not in _shape.RECIPES:
            continue   # unknown recipe → recipe_id() is the authority that rejects it
        status = _shape.recipe_status(recipe)
        if status != "active":
            by_status[(status, recipe)].add(tenant)

    notices: List[str] = []
    for status, recipe in sorted(by_status):
        tail = ("migrate away — it still compiles." if status == "deprecated"
                else "existing declarations still compile, but new tenant-api "
                     "writes using it are rejected until SRE clears the status.")
        notices.append(
            f"recipe {recipe!r} is {status}: "
            f"{_summarize_tenants(by_status[(status, recipe)])}; {tail}"
        )
    return notices


def _skip_record(tenant: str, origin, inst, exc: Exception) -> dict:
    """One quarantined (fail-soft) recipe for the compiler's skip report (#1008 Part B).

    reason is the human message for a known validation error, else `Type: msg` so an
    unexpected exception (e.g. a KeyError from a malformed entry) is still fully
    attributable in the CI log rather than a bare traceback.
    """
    if isinstance(exc, (CustomAlertConfigError, _shape.RecipeError)):
        reason = str(exc)
    else:
        reason = f"{type(exc).__name__}: {exc}"
    name = inst.get("name") if isinstance(inst, dict) else None
    return {"tenant": tenant, "origin": origin, "name": name, "reason": reason}


def build_shapes(config_dir: Path,
                 max_custom_recipes: int = MAX_CUSTOM_RECIPES_DEFAULT
                 ) -> Tuple[List[dict], Dict[str, int], List[dict]]:
    """Group all effective instances into shapes; return (shapes, per_tenant_count, skipped).

    Each shape dict carries the representative params + recipe_id + sorted severities
    union. per_tenant_count is the EFFECTIVE count (own + inherited) of the recipes that
    COMPILED.

    FAIL-SOFT (#1008 / F3 Part B): this is a shared CI gate, so a single bad recipe from
    ANY tenant must not abort the whole compile — that would redden the gate and block
    every tenant's PR merge (a cross-tenant availability DoS). A recipe that fails ANY
    per-recipe check (missing field, invalid shape, residual slug collision, name /
    severity uniqueness, or the OWN-recipe cap) is QUARANTINED: recorded in the returned
    `skipped` list and left out of the pack, while the rest compile. Only a config-level
    error (a negative cap) still raises — it aborts up front, below.
    """
    # Reject a nonsensical cap up front (CLI --max-custom-recipes is type=int, so
    # a negative slips through argparse) — else the cap check below rejects EVERY
    # tenant with a confusing "exceeds cap (-1)" message. 0 IS valid (= forbid
    # tenant-own recipes). CustomAlertConfigError → compile main exits 2 cleanly.
    if max_custom_recipes < 0:
        raise CustomAlertConfigError(
            f"max_custom_recipes must be >= 0 (got {max_custom_recipes})"
        )
    triples, file_errors = collect_instances(config_dir)

    # validation accumulators
    name_seen: Dict[Tuple[str, str], str] = {}              # (tenant, name) → origin
    sev_seen: Dict[Tuple[str, str, str], str] = {}          # (tenant, rid, sev) → origin
    per_tenant: Dict[str, int] = defaultdict(int)           # EFFECTIVE (own + inherited)
    own_per_tenant: Dict[str, int] = defaultdict(int)       # OWN only → the capped count

    # shape accumulators
    shapes: Dict[str, dict] = {}                            # recipe_id → shape
    shape_sev: Dict[str, set] = defaultdict(set)            # recipe_id → severities
    sig_seen: Dict[str, tuple] = {}                         # recipe_id → shape_signature
    skipped: List[dict] = list(file_errors)                 # unloadable files + quarantined recipes (fail-soft)

    # required fields — match tenant-config.schema.json's customAlertInstance.
    # threshold carries the severity. The SHAPING duration is recipe-aware:
    # `forecast` supplies `horizon` (lookback is platform-derived from it, so it
    # never takes a `window`), every other recipe supplies `window` (an empty
    # window emits invalid PromQL like `rate(m[])`).
    base_required = ("recipe", "name", "metric", "threshold")

    for tenant, inst, origin, is_own in triples:
        # FAIL-SOFT PER RECIPE (#1008 / F3 Part B). Validate each recipe in isolation;
        # on ANY error, QUARANTINE just that recipe (record + skip) and keep compiling —
        # a single bad recipe from any tenant must never abort the shared compile (which
        # would block every tenant's PR merge). Shared-state mutation happens only AFTER
        # validation succeeds, so a quarantined recipe never pollutes another recipe's
        # dedup / uniqueness / quota accounting.
        try:
            if not isinstance(inst, dict):
                raise CustomAlertConfigError(
                    f"{origin}: tenant={tenant}: _custom_alerts entry is not a mapping "
                    f"(got {type(inst).__name__}: {inst!r})"
                )
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
                _shape.validate_forecast_ratio_threshold(inst, _value)
            except _shape.RecipeError as e:
                raise CustomAlertConfigError(f"{origin}: tenant={tenant}: {e}") from e

            # recipe_id is now INJECTIVE (#1008 Part A), so this guard is a LAST-RESORT
            # backstop: a distinct shape_signature sharing a rid is only reachable via a
            # ~2^64 hash collision. Without it the loader would keep one representative
            # selector and silently emit it for the others — so keep the guard, but
            # fail-soft (quarantine), never abort.
            if rid in sig_seen and sig_seen[rid] != sig:
                raise CustomAlertConfigError(
                    f"{origin}: tenant={tenant}: recipe_id {rid!r} collides between two "
                    f"distinct shapes (residual hash collision after the injective suffix "
                    f"— please report); this recipe is quarantined"
                )

            # name unique within a tenant (its scope).
            nkey = (tenant, name)
            if nkey in name_seen:
                raise CustomAlertConfigError(
                    f"tenant={tenant}: duplicate custom-alert name {name!r} "
                    f"(in {name_seen[nkey]} and {origin}); names must be unique per tenant"
                )

            # (tenant, recipe_id, severity) unique → keeps the group_left(name) join 1:1
            skey = (tenant, rid, sev)
            if skey in sev_seen:
                raise CustomAlertConfigError(
                    f"tenant={tenant}: two {sev} custom alerts share the same shape "
                    f"{rid!r} (in {sev_seen[skey]} and {origin}); a tenant may declare "
                    f"at most one {sev} alert per shape"
                )

            # Cost guardrail (S4): cap TENANT-OWN recipes (inherited policy is vectorized,
            # O(1) in tenant count → uncapped). Enforced per-recipe — the OWN recipes
            # BEYOND the cap are quarantined instead of aborting the whole compile. The
            # survivor set is deterministic: the FIRST `cap` own recipes in file-path +
            # in-file declaration order (triples come from sorted(rglob) + list order), so
            # `--check` stays stable. ADR-024 §Custom Alerts cost guardrail.
            if is_own and own_per_tenant[tenant] >= max_custom_recipes:
                raise CustomAlertConfigError(
                    f"tenant={tenant}: own custom-alert recipe count would exceed the "
                    f"max_custom_recipes cap ({max_custom_recipes}); this recipe is "
                    f"quarantined (inherited platform/domain policy is vectorized and does "
                    f"NOT count toward the cap)"
                )
        except Exception as exc:  # noqa: BLE001 — robustness boundary: never let one recipe crash the shared compile
            skipped.append(_skip_record(tenant, origin, inst, exc))
            continue

        # ---- validation passed: commit shared state for this recipe ----
        sig_seen[rid] = sig
        name_seen[nkey] = origin
        sev_seen[skey] = origin
        # Quota counts only VALIDATED, distinct (tenant, recipe_id, severity) instances
        # (a multi-severity same-shape recipe legitimately counts as 2).
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
                # bounded per-dimension eval (e.g. per PVC; ADR-024 §Addendum) —
                # MUST be carried so emit_shape threads it into the metric-side by();
                # without it the slug gets `gb_*` but the rule stays by(tenant) and
                # the per-PVC masking fix silently no-ops.
                "group_by": inst.get("group_by") or [],
            }
        shape_sev[rid].add(sev)

    result: List[dict] = []
    for rid in sorted(shapes):
        sh = shapes[rid]
        sh["severities"] = sorted(shape_sev[rid])      # deterministic order
        result.append(sh)
    return result, dict(per_tenant), skipped


def count_recipes_per_tenant(config_dir: Path) -> Dict[str, int]:
    """Effective recipe count per tenant (own + inherited) of the recipes that COMPILED.
    The OWN-recipe cap is enforced inside build_shapes (S4); recipes beyond the cap (or
    otherwise invalid) are quarantined fail-soft, not raised (#1008 Part B)."""
    _shapes, per_tenant, _skipped = build_shapes(config_dir)
    return per_tenant
