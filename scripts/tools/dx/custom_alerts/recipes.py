"""The 6 core recipe PromQL emitters (ADR-024 Capability B, #741).

Each recipe compiles to ONE vectorised rule per shape (recipe_id), structured
exactly like the version-aware pilot (rule-pack-kubernetes.yaml:121-329):

  recording  custom:threshold:{id}   normalise threshold side (version→default)
  recording  custom:metric:{id}      recipe-specific metric side (not for absence)
  recording  custom:{id}:{sev}:core  per-severity exact-or-fallback join + maint suppress
  alert      Custom_{id}             metadata-enriched, bilingual, {{ $labels.name }} title

Invariants honoured (see plan / ADR-024):
  * vectorised on(tenant[,version]) — no per-tenant fan-out (O(M))
  * version graceful-join emitted unconditionally (absent version is safe)
  * aggregation uses explicit `by(tenant[,version])` keep-list (never bare sum())
  * `name` carried via group_left(name, mode) so on-call sees the tenant's title
  * alertname is the static shape slug; human display uses the `name` label
  * ratio guards division-by-zero with `(denominator > 0)` → empty vector, no +Inf
  * absence self-scopes off custom:threshold:{id} (only declaring tenants have it)
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from . import shape as _shape

# forecast cold-start data-sufficiency gate: a freshly-deployed / freshly-onboarded
# base series with too few samples in the lookback window yields wild slopes, so
# predict_linear is gated until at least this many samples exist (ADR-024
# §Forecast Recipe — a "enough data?" gate, NOT a current-value gate that would
# castrate the lead time).
_FORECAST_MIN_SAMPLES = 3

# forecast current-state sanity floor (RATIO MODE ONLY): a forecast must not page on
# a volume that currently has high headroom and merely dipped during a transient
# write burst — predict_linear's long lookback keeps the steep slope for hours after
# the burst ends, so a pure-slope alarm fires on an 80%-empty disk. Requiring the
# CURRENT headroom ratio < this band turns it into "predicted low AND currently low",
# filtering that FP class without castrating lead time (disk-fill thresholds sit well
# below it). Incidentally suppresses online-resize ratio distortion (a resize bumps
# headroom UP, past the band). Mirrors kube-prometheus-stack KubePersistentVolumeFillingUp's
# current-state gate. The tenant's OWN threshold (joined in the core) is untouched —
# this is an additional sanity gate, not a replacement.
# ⚠️ FOOTGUN (threshold-blind; tracked follow-up): the band is a FIXED ratio. A
# ratio-mode forecast threshold ≥ this band is partially neutered — it would only
# fire once CURRENT headroom drops below the band, silently losing the lead time
# between the tenant's threshold and 0.5. Disk-fill thresholds are inherently low
# (≤0.25) so this is a non-issue in practice; a follow-up adds a write/compile-time
# reject of ratio-mode threshold ≥ band so the constraint is LOUD, not silent.
_FORECAST_CURRENT_BAND = 0.5

# forecast horizons are single-unit Go durations (enum-validated upstream);
# parse to integer seconds for predict_linear's scalar arg + the [lookback] range.
_DUR_RE = re.compile(r"^(\d+)(h|m|s)$")


# normalise empty/absent version → "default" (reuses the version-aware idiom).
def _norm_version(expr: str) -> str:
    return f'label_replace({expr}, "version", "default", "version", "^$")'


def _gb_suffix(group_by) -> str:
    """', l1, l2' for the bounded group_by dims (or '') — appended inside a `by()`
    label list so the metric-side aggregation PRESERVES each dimension (per-PVC
    disk-fill: fire if ANY PVC crosses; ADR-024 §Addendum). The per-tenant threshold
    record and the on()/group_left join keys stay unchanged: the extra dimension
    rides the many-side of the join and reaches the alert label automatically."""
    return "".join(", " + g for g in (group_by or ()))


def _duration_to_seconds(d: str) -> int:
    m = _DUR_RE.match(str(d))
    if not m:
        raise _shape.RecipeError(f"cannot convert duration {d!r} to integer seconds")
    return int(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[m.group(2)]


def _forecast_records(rid: str, metric: str, sel: str, horizon: str,
                      capacity: str, gb=()) -> List[dict]:
    """forecast emission (ADR-024 §Forecast Recipe): predict (linear) a gauge/ratio
    crossing a threshold within `horizon`. Two records:

      custom:fcbase:{rid}   the base aggregate — ratio mode (capacity set):
                            avail/capacity headroom; raw mode: the gauge itself.
      custom:metric:{rid}   predict_linear over the base + a cold-start gate.

    Lookback is platform-derived = max(2·horizon, 1h) (NOT tenant-settable — an
    expert knob whose exposure is the biggest foot-gun; deriving it also makes
    horizon ≤ lookback hold by construction). All durations are emitted as integer
    seconds so the `[lookback]` range selector can never be a bad duration (e.g.
    `1.5h`). The standard _core_record then compares custom:metric {op} threshold.
    """
    h_s = _duration_to_seconds(horizon)
    lb_s = max(2 * h_s, 3600)            # max(2·horizon, 1h), integer seconds
    g = _gb_suffix(gb)                   # extra by() dims (e.g. per PVC), or ""
    base = f"custom:fcbase:{rid}"
    if capacity:  # ratio mode: headroom ratio (avail/capacity) falling to a floor
        _shape.validate_metric_name(capacity, "capacity_metric")
        base_inner = (
            f"sum by(tenant{g}) ({metric}{sel})\n"
            f"  /\n"
            f"(sum by(tenant{g}) ({capacity}{sel}) > 0)"   # >0 guard → no +Inf / div-by-zero
        )
    else:         # raw mode: a gauge crossing an absolute threshold
        base_inner = f"max by(tenant, version{g}) ({metric}{sel})"
    # predict_linear over the RECORDED base (predict_linear cannot range-select a
    # division/aggregation inline — hence the base recording rule). Bare `and`:
    # both operands derive from the same `base` series → identical label set, so
    # no on() needed; the gate drops tenants with < N samples (promtool-verified).
    if capacity:
        # ratio mode: `base` is a NON-NEGATIVE headroom ratio in [0,1].
        #  * clamp_min(…, 0): predict_linear extrapolates the ratio linearly and can
        #    overshoot below 0 — a "negative headroom" is physically meaningless and
        #    reads as gibberish to on-call. Clamping the floor leaves FIRING
        #    unchanged (anything < a positive threshold still fires) but keeps the
        #    surfaced value sane.
        #  * `{base} < _FORECAST_CURRENT_BAND`: current-state sanity floor (see the
        #    constant) — turns a pure-slope alarm into "predicted low AND currently
        #    low", killing the transient-write-burst false positive. The tenant's
        #    threshold (joined in the core) is UNTOUCHED; this is an extra gate.
        predict_inner = (
            f"clamp_min(predict_linear({base}[{lb_s}s], {h_s}), 0)\n"
            f"  and\n"
            f"{base} < {_FORECAST_CURRENT_BAND}\n"
            f"  and\n"
            f"count_over_time({base}[{lb_s}s]) > {_FORECAST_MIN_SAMPLES}"
        )
    else:
        # raw mode: an arbitrary gauge (may exceed 1 or be legitimately negative) — no
        # [0,1] clamp, no ratio band. A raw-mode anti-flap gate would have to be
        # threshold-relative; out of scope here (raw-mode forecast is rare).
        predict_inner = (
            f"predict_linear({base}[{lb_s}s], {h_s})\n"
            f"  and\n"
            f"count_over_time({base}[{lb_s}s]) > {_FORECAST_MIN_SAMPLES}"
        )
    return [
        {"record": base, "expr": base_inner},
        {"record": f"custom:metric:{rid}", "expr": _norm_version(predict_inner)},
    ]


def _threshold_record(rid: str, metric: str) -> dict:
    # Selector = {component="custom", metric=<metric>, recipe_id=<slug>} (label
    # form A, #741 S3a): the exporter emits user_threshold with these labels, so
    # the rule joins the real data. component+metric satisfy the #731 contract;
    # recipe_id disambiguates shapes sharing a metric (permitted only when
    # component="custom"). keep `name`+`mode` in by() so these PER-TENANT
    # attributes survive to group_left(name, mode) — `mode` MUST ride the data
    # plane (tenants sharing a shape may set page vs silent; a single vectorized
    # rule cannot bake a per-tenant mode, else S8 cannot route). keep `severity`
    # so the per-severity core selects its half. (tenant,recipe_id,severity) is
    # unique, so (name, mode) stays a singleton and the join is one-to-one.
    inner = (
        f'max by(tenant, version, severity, name, mode) '
        f'(user_threshold{{component="custom", metric="{metric}", recipe_id="{rid}"}})'
    )
    return {"record": f"custom:threshold:{rid}", "expr": _norm_version(inner)}


def _metric_record(rid: str, recipe: str, metric: str, sel: str,
                   window: str, quantile: str, denom: str, gb=()) -> dict:
    g = _gb_suffix(gb)  # extra by() dims (e.g. ", persistentvolumeclaim"), or ""
    if recipe == "threshold":
        inner = f"max by(tenant, version{g}) ({metric}{sel})"
    elif recipe == "rate":
        inner = f"sum by(tenant, version{g}) (rate({metric}{sel}[{window}]))"
    elif recipe == "ratio":
        # by(tenant) only (ratio is per-tenant aggregate, version-agnostic in MVP);
        # `(den > 0)` drops zero/negative denominators → empty vector, never +Inf.
        # group_by extends BOTH sides so the ratio is computed per-dimension.
        inner = (
            f"sum by(tenant{g}) (rate({metric}{sel}[{window}]))\n"
            f"  /\n"
            f"(sum by(tenant{g}) (rate({denom}{sel}[{window}])) > 0)"
        )
    elif recipe == "p99_latency":
        inner = (
            f"histogram_quantile({quantile},\n"
            f"  sum by(le, tenant, version{g}) (rate({metric}_bucket{sel}[{window}])))"
        )
    else:  # absence has no separate metric record
        raise _shape.RecipeError(f"_metric_record called for {recipe!r}")
    return {"record": f"custom:metric:{rid}", "expr": _norm_version(inner)}


def _eq_core_record(rid: str, sev: str, metric: str, sel: str) -> dict:
    """`==` any-match core (#810 + #819 adversarial fix).

    Ordered ops aggregate THEN compare (`max by(tenant)(metric) > thr` = worst
    case). Equality has no meaningful aggregate: `max(...) == code` masks a
    matching replica when another holds a larger code (max of 1236,1593 is 1593
    → 1593==1236 false → SILENT miss). So `==` compares the RAW per-series metric
    against the per-tenant threshold FIRST, then aggregates existence — any series
    equal to the configured code makes the tenant fire.

    The raw metric is INLINED (not a `custom:metric:{id}` recording rule) so we
    don't persist a per-pod high-cardinality intermediate; `max by(...)` after the
    compare collapses replicas back to one series per (tenant, version, name,
    mode). group_left(name, mode) stays many(replicas)-to-one(threshold) — the
    uniqueness guard keeps one threshold series per (tenant, version, severity).
    Version-aware exact-or-fallback mirrors the ordered-op core.
    """
    raw = _norm_version(f"{metric}{sel}")
    core = (
        f'(\n'
        f'  max by(tenant, version, name, mode) (\n'
        f'    {raw}\n'
        f'    == on(tenant, version) group_left(name, mode)\n'
        f'      custom:threshold:{rid}{{severity="{sev}"}}\n'
        f'  )\n'
        f'  or\n'
        f'  max by(tenant, version, name, mode) (\n'
        f'    (\n'
        f'      {raw}\n'
        f'      unless on(tenant, version)\n'
        f'        custom:threshold:{rid}{{severity="{sev}"}}\n'
        f'    )\n'
        f'    == on(tenant) group_left(name, mode)\n'
        f'      custom:threshold:{rid}{{version="default", severity="{sev}"}}\n'
        f'  )\n'
        f')'
    )
    expr = (
        f'{core}\n'
        f'unless on(tenant)\n'
        f'(user_state_filter{{filter="maintenance"}} == 1)'
    )
    return {"record": f"custom:{rid}:{sev}:core", "expr": expr}


def _core_record(rid: str, recipe: str, op: str, sev: str, metric: str,
                 sel: str, window: str) -> dict:
    if op == "==":
        # threshold-recipe-only (gate enforced upstream); any-match semantics.
        return _eq_core_record(rid, sev, metric, sel)
    if recipe == "absence":
        # self-scoped: only tenants with custom:threshold:{id} are candidates;
        # fire where the metric had no sample over the window.
        core = (
            f'(\n'
            f'  custom:threshold:{rid}{{severity="{sev}"}}\n'
            f'  unless on(tenant)\n'
            f'  count by(tenant) (count_over_time({metric}{sel}[{window}]) > 0)\n'
            f')'
        )
    else:
        # version-aware exact-or-fallback, per-severity (RHS is a per-(tenant,version)
        # singleton → joins are clean one-to-one / many-to-one). group_left(name, mode)
        # carries the tenant's title; fallback keeps the metric's REAL version.
        core = (
            f'(\n'
            f'  (\n'
            f'    custom:metric:{rid}\n'
            f'    {op} on(tenant, version) group_left(name, mode)\n'
            f'      custom:threshold:{rid}{{severity="{sev}"}}\n'
            f'  )\n'
            f'  or\n'
            f'  (\n'
            f'    (\n'
            f'      custom:metric:{rid}\n'
            f'      unless on(tenant, version)\n'
            f'        custom:threshold:{rid}{{severity="{sev}"}}\n'
            f'    )\n'
            f'    {op} on(tenant) group_left(name, mode)\n'
            f'      custom:threshold:{rid}{{version="default", severity="{sev}"}}\n'
            f'  )\n'
            f')'
        )
    expr = (
        f'{core}\n'
        f'unless on(tenant)\n'
        f'(user_state_filter{{filter="maintenance"}} == 1)'
    )
    return {"record": f"custom:{rid}:{sev}:core", "expr": expr}


def _alert_rule(rid: str, recipe: str, sev: str, metric: str, sel: str,
                for_: str, op: str = ">") -> dict:
    core = f"custom:{rid}:{sev}:core"
    # left-outer-join metadata enrichment (onboarding-vacuum safe, #709 pattern).
    expr = (
        f'(\n'
        f'  {core}\n'
        f'  * on(tenant) group_left(runbook_url, owner, tier)\n'
        f'    tenant_metadata_info\n'
        f')\n'
        f'or\n'
        f'(\n'
        f'  {core}\n'
        f'  unless on(tenant) tenant_metadata_info\n'
        f')'
    )
    sev_en = "CRITICAL " if sev == "critical" else ""
    sev_zh = "達臨界 " if sev == "critical" else ""
    desc_sel = sel if sel else ""
    # `==` is an exact status/error-code match (#810), not a threshold crossing —
    # say so, and print the code as an integer (codes are not decimals).
    if op == "==":
        desc_en = f'value {{{{ $value | printf "%.0f" }}}} matched the configured code'
        desc_zh = f'值 {{{{ $value | printf "%.0f" }}}} 等於設定代碼'
    else:
        desc_en = f'value {{{{ $value | printf "%.2f" }}}} crossed the configured threshold'
        desc_zh = f'值 {{{{ $value | printf "%.2f" }}}} 已越過設定閾值'
    return {
        "alert": f"Custom_{rid}",
        "expr": expr,
        "for": for_,
        "labels": {
            "severity": sev,
            "tenant": "{{ $labels.tenant }}",
            "recipe": recipe,
            # static routing discriminator (#741 S7/S8): no platform alert carries
            # `component`, so Alertmanager routes/groups the whole custom subtree on
            # an exact component="custom" match (vs coupling to the Custom_ alertname).
            "component": "custom",
            # per-tenant routing class (page|silent) carried from the data plane.
            # page → custom firehose receiver; silent → suppressed by the
            # CustomRecipeSilent sentinel + inhibit (ADR-003 pattern, NOT route-to-
            # null), so it stays a dashboard-only ALERTS series. `mode` drives the
            # sentinel; a single vectorized rule still serves mixed-mode tenants.
            "mode": "{{ $labels.mode }}",
        },
        "annotations": {
            "summary": f"{sev_en}Custom alert [{{{{ $labels.name }}}}] for {{{{ $labels.tenant }}}}",
            "summary_zh": f"{{{{ $labels.tenant }}}} 的自訂告警 [{{{{ $labels.name }}}}] {sev_zh}觸發",
            "description": f"{recipe} on {metric}{desc_sel}: {desc_en}",
            "description_zh": f"{metric}{desc_sel} 的 {recipe}: {desc_zh}",
            "runbook_url": "{{ $labels.runbook_url }}",
            "owner": "{{ $labels.owner }}",
            "tier": "{{ $labels.tier }}",
        },
    }


def emit_shape(shape: dict) -> Tuple[List[dict], List[dict]]:
    """Emit (recording_rules, alert_rules) for one shape.

    `shape` keys: recipe, metric, op, window, quantile, denominator_metric,
    horizon, capacity_metric, recipe_id, severities (sorted list), for, and the
    raw selector maps (`selectors`/`selectors_re`) for safe assembly.
    """
    rid = shape["recipe_id"]
    recipe = shape["recipe"]
    metric = shape["metric"]
    op = shape.get("op", ">")
    window = str(shape.get("window", ""))
    quantile = str(shape.get("quantile", "0.99"))
    denom = shape.get("denominator_metric", "")
    sel = _shape.assemble_selector(shape)
    for_ = str(shape.get("for", "1m"))
    severities = shape["severities"]
    # bounded extra aggregation dims (e.g. per PVC; ADR-024 §Addendum). Validated +
    # sorted; rejected for absence/== by recipe_id, so only the metric/forecast
    # records below need it (the standard core inherits it via custom:metric).
    gb = _shape._normalize_group_by(shape)

    recording: List[dict] = [_threshold_record(rid, metric)]
    if recipe == "forecast":
        # forecast emits TWO metric-side records (base aggregate + predict_linear);
        # the standard _core_record then compares custom:metric {op} threshold.
        recording.extend(_forecast_records(
            rid, metric, sel, str(shape.get("horizon", "")),
            shape.get("capacity_metric", ""), gb))
    elif recipe != "absence" and op != "==":
        # `==` inlines the raw metric in its any-match core (no maxed metric
        # record) — see _eq_core_record. absence has no metric record either.
        recording.append(_metric_record(rid, recipe, metric, sel, window, quantile, denom, gb))
    for sev in severities:
        recording.append(_core_record(rid, recipe, op, sev, metric, sel, window))

    alerts: List[dict] = [
        _alert_rule(rid, recipe, sev, metric, sel, for_, op) for sev in severities
    ]
    return recording, alerts
