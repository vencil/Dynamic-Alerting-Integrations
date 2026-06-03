"""The 5 core recipe PromQL emitters (ADR-024 Capability B, #741 S2).

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

from typing import Dict, List, Tuple

from . import shape as _shape


# normalise empty/absent version → "default" (reuses the version-aware idiom).
def _norm_version(expr: str) -> str:
    return f'label_replace({expr}, "version", "default", "version", "^$")'


def _threshold_record(rid: str) -> dict:
    # keep `name` + `mode` in by() so these PER-TENANT attributes survive to
    # group_left(name, mode); keep `severity` so the per-severity core selects
    # its half. `mode` MUST ride the data plane: tenants sharing a shape may set
    # different modes (page vs silent), and a single vectorized rule cannot bake
    # a per-tenant mode — without this label S8 routing cannot tell a silent
    # tenant's alert from a paging one (they share the rule). (tenant,recipe_id,
    # severity) is unique, so (name, mode) stays a per-(tenant,version,severity)
    # singleton and the join remains one-to-one.
    inner = f'max by(tenant, version, severity, name, mode) (user_threshold{{recipe_id="{rid}"}})'
    return {"record": f"custom:threshold:{rid}", "expr": _norm_version(inner)}


def _metric_record(rid: str, recipe: str, metric: str, sel: str,
                   window: str, quantile: str, denom: str) -> dict:
    if recipe == "threshold":
        inner = f"max by(tenant, version) ({metric}{sel})"
    elif recipe == "rate":
        inner = f"sum by(tenant, version) (rate({metric}{sel}[{window}]))"
    elif recipe == "ratio":
        # by(tenant) only (ratio is per-tenant aggregate, version-agnostic in MVP);
        # `(den > 0)` drops zero/negative denominators → empty vector, never +Inf.
        inner = (
            f"sum by(tenant) (rate({metric}{sel}[{window}]))\n"
            f"  /\n"
            f"(sum by(tenant) (rate({denom}{sel}[{window}])) > 0)"
        )
    elif recipe == "p99_latency":
        inner = (
            f"histogram_quantile({quantile},\n"
            f"  sum by(le, tenant, version) (rate({metric}_bucket{sel}[{window}])))"
        )
    else:  # absence has no separate metric record
        raise _shape.RecipeError(f"_metric_record called for {recipe!r}")
    return {"record": f"custom:metric:{rid}", "expr": _norm_version(inner)}


def _core_record(rid: str, recipe: str, op: str, sev: str, metric: str,
                 sel: str, window: str) -> dict:
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
                for_: str) -> dict:
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
    return {
        "alert": f"Custom_{rid}",
        "expr": expr,
        "for": for_,
        "labels": {
            "severity": sev,
            "tenant": "{{ $labels.tenant }}",
            "recipe": recipe,
            # per-tenant routing class (page|silent) carried from the data plane
            # so S8 can route silent→null / page→pager WITHOUT forking the rule.
            "mode": "{{ $labels.mode }}",
        },
        "annotations": {
            "summary": f"{sev_en}Custom alert [{{{{ $labels.name }}}}] for {{{{ $labels.tenant }}}}",
            "summary_zh": f"{{{{ $labels.tenant }}}} 的自訂告警 [{{{{ $labels.name }}}}] {sev_zh}觸發",
            "description": (
                f"{recipe} on {metric}{desc_sel}: "
                f'value {{{{ $value | printf "%.2f" }}}} crossed the configured threshold'
            ),
            "description_zh": (
                f"{metric}{desc_sel} 的 {recipe}: "
                f'值 {{{{ $value | printf "%.2f" }}}} 已越過設定閾值'
            ),
            "runbook_url": "{{ $labels.runbook_url }}",
            "owner": "{{ $labels.owner }}",
            "tier": "{{ $labels.tier }}",
        },
    }


def emit_shape(shape: dict) -> Tuple[List[dict], List[dict]]:
    """Emit (recording_rules, alert_rules) for one shape.

    `shape` keys: recipe, metric, op, window, quantile, denominator_metric,
    recipe_id, severities (sorted list), for, and the raw selector maps
    (`selectors`/`selectors_re`) for safe assembly.
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

    recording: List[dict] = [_threshold_record(rid)]
    if recipe != "absence":
        recording.append(_metric_record(rid, recipe, metric, sel, window, quantile, denom))
    for sev in severities:
        recording.append(_core_record(rid, recipe, op, sev, metric, sel, window))

    alerts: List[dict] = [
        _alert_rule(rid, recipe, sev, metric, sel, for_) for sev in severities
    ]
    return recording, alerts
