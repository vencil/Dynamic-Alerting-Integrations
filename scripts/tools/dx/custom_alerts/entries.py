"""What the custom-alert compiler compiles: the exporter's own reading of
every tenant's `_custom_alerts` (#3042).

The compiler builds one rule per shape for exactly the entries the exporter
serves, plus the switched-off ones (so switching one back on needs no
recompile). It does not walk the conf.d tree and does not judge an entry
itself: which list a tenant has (tenant file, root platform file, profile),
whether an entry is valid, and whether it repeats an earlier entry's shape
and severity are all the exporter's resolver's answers, read from da-guard
served-values' per-tenant `custom_alerts` (`entries_from_tree`). The one
check left here is parity: this side's recipe_id for an entry must equal
the exporter's, or the rule would join a series the exporter does not
serve — the compile is refused (`CustomAlertParityError`).

`entries_from_specs` is the path without da-guard, for the recipe-preview
image (which ships no Go binary): recipes judged by this module's own
validation, the way the compiler judged them before #3042.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from . import shape as _shape

#: Statuses the compiler builds a rule for: served, and switched off (no row
#: by design; a rule so that switching back on needs no recompile).
COMPILED_STATUSES = ("served", "disabled")

_NOTICE_TENANT_SAMPLE = 10   # max tenant names per lifecycle notice (CI-log readability)


class CustomAlertConfigError(ValueError):
    """A declaration tree is invalid (rejected at compile time)."""


class CustomAlertParityError(CustomAlertConfigError):
    """The exporter serves an entry this compiler reads differently (another
    recipe_id, or a spec it rejects): the two implementations of the recipe
    rules have parted. Never fail-soft — a rule built on this side's reading
    would join a series the exporter does not serve."""


class Entry(NamedTuple):
    """One element of a tenant's `_custom_alerts`, as the exporter judges it
    (`_lib_tenant_values.CustomAlertEntry` plus its tenant)."""
    tenant: str
    index: int
    spec: dict
    status: str
    recipe_id: Optional[str]
    severities: Tuple[str, ...]
    reason: Optional[str] = None


class TreeEntries(NamedTuple):
    """`entries_from_tree`'s answer: every tenant's entries, the tenants
    whose `_custom_alerts` does not decode ({tenant: reason}), and the files
    the exporter's load skips (do not decode / cannot be read) — their
    tenants have no entry, as the exporter serves them none."""
    entries: List[Entry]
    block_errors: Dict[str, str]
    parse_failed: List[str]
    unreadable: List[str]
    # The served-values tree read (`_lib_tenant_values.ServedTree`), for its
    # load warnings; None for entries given directly.
    served: object = None


def entries_from_tree(config_dir: Path, binary: Optional[str] = None) -> TreeEntries:
    """Every tenant's `_custom_alerts` entries as the exporter serves the tree
    at `config_dir` (da-guard served-values). Raises what `load_served_tree`
    raises — except for files the load skips, which are named in the answer
    while the rest of the tree is read (the exporter serves the rest too)."""
    import _lib_tenant_values as tv  # lazy: the recipe-preview image ships neither it nor da-guard

    # A directory symlink the exporter does not follow is named on da-guard's
    # own stderr (`… is a symlink to a directory and is not followed`), which
    # the compiler prints (`print_load_warnings`).
    tree = tv.load_served_tree(config_dir, binary=binary, partial=True)
    entries: List[Entry] = []
    block_errors: Dict[str, str] = {}
    for tenant in sorted(tree.tenants):
        values = tree.tenants[tenant]
        if values.custom_alerts is None:
            raise tv.ServedValuesError(
                f"da-guard {tv.SUBCOMMAND}: tenant {tenant!r} has no custom_alerts — this da-guard is "
                "older than this tool: upgrade or rebuild it", None, "", stale=True)
        if values.custom_alerts_error:
            block_errors[tenant] = values.custom_alerts_error
        for e in values.custom_alerts:
            entries.append(Entry(tenant, e.index, e.spec, e.status, e.recipe_id, e.severities, e.reason))
    return TreeEntries(entries, block_errors, list(tree.parse_failed),
                       [f"{u.file} ({u.reason})" for u in tree.unreadable], tree)


def entries_from_specs(specs_by_tenant: Dict[str, List[dict]]) -> List[Entry]:
    """Entries for recipes given directly — the recipe-preview path, without
    da-guard. Each recipe is judged by this module's own rules
    (`python_entry`); an invalid one raises."""
    out: List[Entry] = []
    for tenant in sorted(specs_by_tenant):
        for i, inst in enumerate(specs_by_tenant[tenant]):
            out.append(python_entry(tenant, i, inst))
    return out


def _is_switched_off(inst: dict, value: str) -> bool:
    """The exporter's own opt-out test: an SLO objective of exactly `disable`
    (resolveSloBurnRate); a threshold value any of disable / disabled / off /
    false, any case (isDisabled)."""
    if inst.get("recipe") == "slo_burn_rate":
        return str(inst.get("objective", "")).strip() == "disable"
    return value.strip().lower() in ("disable", "disabled", "off", "false")


def python_entry(tenant: str, index: int, inst: object) -> Entry:
    """One recipe judged by this compiler's own rules: required fields,
    `shape.recipe_id` (the shape validation), the threshold's severity and
    the forecast ratio band. Raises CustomAlertConfigError naming the
    tenant and the entry."""
    where = f"tenant={tenant}: _custom_alerts[{index}]"
    if not isinstance(inst, dict):
        raise CustomAlertConfigError(f"{where} is not a mapping (got {type(inst).__name__}: {inst!r})")
    # Required fields — the recipe contract's (shape.BASE_REQUIRED +
    # shape.RECIPE_RULES, generated from pkg/config/recipe_contract.json); an
    # unknown recipe is left to shape.recipe_id to refuse.
    recipe = inst.get("recipe")
    rules = _shape.RECIPE_RULES.get(recipe)
    required = _shape.BASE_REQUIRED + (rules[0] if rules else ())
    missing = [f for f in required if f not in inst]
    if missing:
        raise CustomAlertConfigError(f"{where}: missing required field(s) {missing}: {inst!r}")
    try:
        rid = _shape.recipe_id(inst)
        if recipe == "slo_burn_rate":
            sevs: Tuple[str, ...] = ("critical", "warning")
            value = ""
        else:
            value, sev = _shape.parse_threshold(inst["threshold"])
            _shape.validate_forecast_ratio_threshold(inst, value)
            sevs = (sev,)
    except _shape.RecipeError as e:
        raise CustomAlertConfigError(f"{where}: {e}") from e
    status = "disabled" if _is_switched_off(inst, value) else "served"
    return Entry(tenant, index, inst, status, rid, sevs)


def _shape_of(inst: dict, rid: str) -> dict:
    """The shape a rule is emitted from: the entry's shape parameters, with
    the defaults the recipes apply."""
    is_slo = inst.get("recipe") == "slo_burn_rate"
    return {
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
        # bounded per-dimension eval (e.g. per PVC; ADR-024 §Addendum) — MUST
        # be carried so emit_shape threads it into the metric-side by();
        # without it the slug gets `gb_*` but the rule stays by(tenant).
        "group_by": inst.get("group_by") or [],
        # slo_burn_rate fast-window bad-event floor (ADR-031) — a shape
        # component the emitter writes as a literal (slow tier = ×6).
        "min_events": _shape._normalize_min_events(inst) if is_slo else None,
    }


def shapes_from_entries(entries: Iterable[Entry]) -> Tuple[List[dict], List[Entry]]:
    """(shapes, not compiled). One shape per recipe_id across the pack, with
    the union of its entries' severities; the entries the exporter does not
    serve (rejected, duplicate) are returned, not compiled.

    Raises CustomAlertParityError when this side's recipe_id for an entry the
    exporter serves (or would, switched back on) is not the exporter's — or
    this side rejects its spec — and CustomAlertConfigError when two distinct
    shapes share a recipe_id (a residual hash collision)."""
    shapes: Dict[str, dict] = {}
    severities: Dict[str, set] = defaultdict(set)
    signatures: Dict[str, tuple] = {}
    skipped: List[Entry] = []
    for e in entries:
        if e.status not in COMPILED_STATUSES:
            skipped.append(e)
            continue
        where = f"tenant={e.tenant}: _custom_alerts[{e.index}] ({e.spec.get('name')!r})"
        try:
            rid = _shape.recipe_id(e.spec)
            sig = _shape.shape_signature(e.spec)
        except _shape.RecipeError as exc:
            raise CustomAlertParityError(
                f"{where}: the exporter {'serves' if e.status == 'served' else 'accepts'} it as "
                f"{e.recipe_id!r}, but this compiler rejects it: {exc}") from exc
        if rid != e.recipe_id:
            raise CustomAlertParityError(
                f"{where}: the exporter's recipe_id is {e.recipe_id!r}, this compiler's is {rid!r} "
                f"— a rule built on {rid!r} would join no series the exporter serves")
        # recipe_id is injective (#1008 Part A); two distinct shapes on one id
        # is only reachable through a hash collision, and keeping one
        # representative would emit its selectors for the other.
        if rid in signatures and signatures[rid] != sig:
            raise CustomAlertConfigError(
                f"{where}: recipe_id {rid!r} collides between two distinct shapes "
                f"(residual hash collision — please report)")
        signatures[rid] = sig
        shapes.setdefault(rid, _shape_of(e.spec, rid))
        severities[rid].update(e.severities)
    result = []
    for rid in sorted(shapes):
        sh = shapes[rid]
        sh["severities"] = sorted(severities[rid])
        result.append(sh)
    return result, skipped


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


def lifecycle_notices(entries: Iterable[Entry]) -> List[str]:
    """Human-readable notices for non-active recipes in use (ADR-024 #6): one
    per (status, recipe) a compiled entry declares, naming its tenants.
    Deprecated / eol recipes keep compiling (no silent alert loss); the
    write-side eol rejection lives in tenant-api."""
    by_status: Dict[Tuple[str, str], set] = defaultdict(set)
    for e in entries:
        recipe = e.spec.get("recipe")
        if e.status not in COMPILED_STATUSES or recipe not in _shape.RECIPES:
            continue
        status = _shape.recipe_status(recipe)
        if status != "active":
            by_status[(status, recipe)].add(e.tenant)
    notices: List[str] = []
    for status, recipe in sorted(by_status):
        tail = ("migrate away — it still compiles." if status == "deprecated"
                else "existing declarations still compile, but new tenant-api "
                     "writes using it are rejected until SRE clears the status.")
        notices.append(f"recipe {recipe!r} is {status}: "
                       f"{_summarize_tenants(by_status[(status, recipe)])}; {tail}")
    return notices
