#!/usr/bin/env python3
"""threshold_govern.py — 閾值治理報告（唯讀，#656；ADR-027 §4 移除寫入路徑）。

把現成的「閾值推薦引擎」(``threshold_recommend.py``) 的結果過一道治理閘門，列出
**值得回頭調的腐敗閾值**（哪個租戶、哪個 key、現值、建議值、偏離幅度、信心等級）。
本工具**只讀**：不呼叫 tenant-api、不開 PR、不改任何檔案；要調的值由人改進租戶檔。

Run the recommendation ENGINE, gate its output on rot magnitude + confidence, and
report which thresholds are worth retuning. Read-only: it never calls tenant-api,
opens no PR and writes no file (ADR-027 §4 removed the former ``--apply`` write
path, its CronJob and its staleness alert). The operator applies a reported value
to the tenant's own file by hand.

設計重點：
  - **DETECT = recommender 的 delta%**：current vs P95 偏離幅度就是腐敗訊號。
  - **閘門**：只列 |delta| ≥ ``--min-delta-pct`` 且 confidence ∈ {HIGH, MEDIUM}
    （樣本不足不列，防噪音治理）。
  - **快照精神**：對 Prometheus 的查詢沿用 recommender（一次性 range 查詢）。

只治理租戶檔自己寫的 key（#2116）：歸屬取自 ``da-guard effective`` 的逐 key 來源；
繼承自 defaults／平台／profile 的 key 只列參考（``inherited``），不進計畫、不查
Prometheus。每個計畫帶租戶檔的路徑（``source_file``，相對 ``--config-dir``），
指出要改哪個檔。

另外列出的類別（都不進計畫）：沒量成的閾值（``not_measured``，#3133，報告不完整、
結束碼 2）、需人工 review 的（``force_manual``）、未分類的 lower-bound ``<``
（``ungoverned_lower_bound``）、by-design 不適用的（``not_applicable``）。

結束碼（dev-rules §13）：0 = 報告完整（不論有沒有腐敗閾值）；2 = 呼叫端錯誤（參數、
路徑、da-guard 或 observed-map 不可用）或有閾值沒量成（報告不完整）。本工具不產生 1。

領域邊界：本工具是 recommender 的**治理閘門層**，推薦邏輯/資料源完全沿用
``threshold_recommend``（Day-N，查 observed recording rule）。冷啟動粗估請用
``baseline_discovery``（Day-0，查 raw exporter）——⛔ 三者勿混用。

Usage:
  # 列出需要調的閾值（不寫入任何東西）
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090

  # 收緊閘門：只列偏離 ≥ 40% 的
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 \\
    --min-delta-pct 40

  # JSON 輸出（pipeline / 觀測）
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 --json
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

from _lib_python import (  # noqa: E402
    add_prometheus_arg, prometheus_url,
    detect_cli_lang,
    exit_on_yaml_file_error,
    parse_duration_seconds,
)
from _lib_tenant_values import exit_on_served_values_error  # noqa: E402  (#2116)
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping; #3276)
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, EXIT_OK, Report, partial_answer)
import _lib_input  # noqa: E402
import _lib_parse_cache  # noqa: E402

# Reuse the recommendation engine wholesale — threshold_govern is its gate
# layer, never a re-implementation (the engine owns percentiles / observed-map /
# direction-skip / confidence). Importing runs only its module-level setup; its
# main() is __main__-guarded.
import threshold_recommend as recommend  # noqa: E402

_LANG = detect_cli_lang()

# Governance defaults (tunable via flags).
DEFAULT_MIN_DELTA_PCT = 25.0   # only report meaningful rot (engine margin is 5%)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class PlannedChange:
    """One actionable per-key recommendation that cleared the governance gate."""

    key: str
    current_value: Any
    recommended: float
    new_value: str          # the conf.d-quoted value to set by hand (e.g. '"45"')
    delta_pct: float
    confidence: str
    p95: Optional[float]
    reason: str


@dataclass
class TenantPlan:
    """A tenant's set of governance-actionable changes."""

    tenant: str
    changes: list[PlannedChange] = field(default_factory=list)
    # The tenant's own file, relative to --config-dir (#2116); "" when unknown.
    source_file: str = ""


@dataclass
class UngovernedKey:
    """A threshold governance cannot act on — a bare/half-resolved lower-bound
    ``<`` still engine-skipped (#916 left it needs_review / not-supported).

    Surfaced so the un-classified ``<`` tail is an OBSERVABLE blind spot, not a
    silent coverage hole: these never reach the plan (``recommended is None`` fails
    the gate). After #916 the real map has none of these (every ``<`` key is
    classified percentile-lower or not-applicable); the field stays for a
    hand-edited / half-resolved entry.
    """

    tenant: str
    key: str
    reason: str


@dataclass
class NotApplicableKey:
    """A ``<`` threshold classified by-design not-applicable (#916 Item A —
    kafka broker-count / integer-count floors).

    An expected-value invariant or a topology floor: no percentile recommendation
    is ever meaningful, so it is reported as an INFO count (governance is complete
    for it), NOT a ⚠ blind spot and NOT a manual-review item.
    """

    tenant: str
    key: str
    reason: str


@dataclass
class ForceManualKey:
    """A key the recommender routed to manual review (``force_manual``): a
    lower-bound (percentile-lower) guardrail trip (#916 Item A — relaxation,
    floor would drop; an out-of-ratio-domain current/candidate; too thin a
    sample), an upper-bound key whose current threshold is 0 (#2997), or a
    recommender exception on any key. ``delta_pct`` is a miss-rate delta for
    a lower-bound trip and None otherwise.

    This is the floor-rot CORE signal — a floor drifting toward relaxation must
    never be invisible — so it gets its own manual-review section + summary count,
    distinct from the plan and the by-design N/A INFO.
    """

    tenant: str
    key: str
    guardrail_reason: str
    delta_pct: Optional[float]


@dataclass
class NotMeasuredKey:
    """A threshold whose Prometheus query was not answered (#3133): it has no
    measurement, so it is neither governed nor "within margin" — the run is
    partial (exit 2). ``error`` is the QueryError text."""

    tenant: str
    key: str
    error: str


@dataclass
class InheritedKey:
    """A threshold a tenant inherits from a defaults / platform / profile file
    (#2116): its value is not in the tenant's file, so retuning that file
    cannot change it and must not pin it. Listed for reference only (not
    queried; threshold-recommend lists it the same way)."""

    tenant: str
    key: str
    source_layer: str
    source_file: str


@dataclass
class GovernanceResult:
    """One run's full result — a named container (not a positional tuple) so a new
    surfaced category (#916 not_applicable / force_manual) is an added field, not a
    silently-shifted tuple position that a caller could unpack into the wrong var.
    """

    plans: list[TenantPlan]
    ungoverned: list[UngovernedKey]
    not_applicable: list[NotApplicableKey] = field(default_factory=list)
    force_manual: list[ForceManualKey] = field(default_factory=list)
    inherited: list[InheritedKey] = field(default_factory=list)
    not_measured: list[NotMeasuredKey] = field(default_factory=list)
    #: #3617: how many thresholds were queried — measured or not. A threshold
    #: within margin lands in no list above, so the answered part cannot be
    #: counted from them.
    queried: int = 0


# ---------------------------------------------------------------------------
# Governance gate
# ---------------------------------------------------------------------------
def is_governance_actionable(
    rec: "recommend.KeyRecommendation", min_delta_pct: float
) -> bool:
    """True iff this recommendation is strong enough to report as rotten.

    Stricter than the engine's ``_exportable`` (5% margin): a key is reported
    only when the threshold has drifted meaningfully (``|delta| >= min_delta_pct``)
    AND the recommendation rests on enough samples (HIGH/MEDIUM confidence). The
    confidence floor is the anti-noise guard — we never tell an operator to retune
    a threshold off a thin sample (防破窗). Since #2969 ``_exportable`` applies the
    same confidence floor — both call ``recommend.confidence_actionable`` — so at
    an equal margin the two agree; only the margin (5% vs ``min_delta_pct``)
    differs.

    Keys the engine skipped (unmapped / lower-bound ``<`` N/A / unsupported scope
    / no data → ``recommended is None``) are excluded for free, as are keys
    routed to manual review (``force_manual``: a lower-bound guardrail trip
    (#916), an upper-bound current of 0 (#2997), a recommender exception —
    explicit even though ``force_manual`` always implies ``recommended is None``).
    """
    return (
        rec.recommended is not None
        and not getattr(rec, "force_manual", False)
        and rec.delta_pct is not None
        and abs(rec.delta_pct) >= min_delta_pct
        and recommend.confidence_actionable(rec.confidence)
    )


def _quoted_value(rec: "recommend.KeyRecommendation") -> str:
    """Render a recommendation as the conf.d string literal to set (quoted)."""
    return '"' + recommend._format_threshold_value(rec.recommended) + '"'


def build_governance_plan(
    reports: list["recommend.TenantRecommendation"], min_delta_pct: float
) -> list[TenantPlan]:
    """Filter the engine's reports down to per-tenant actionable change sets.

    Only `report.keys` — the keys the tenant's own file writes — can enter a
    plan (#2116): an inherited key (`report.inherited`) is not in the tenant's
    file, and setting it there would pin the tenant."""
    plans: list[TenantPlan] = []
    for report in reports:
        changes = [
            PlannedChange(
                key=r.key,
                current_value=r.current_value,
                recommended=r.recommended,
                new_value=_quoted_value(r),
                delta_pct=r.delta_pct,
                confidence=r.confidence,
                p95=r.p95,
                reason=r.reason,
            )
            for r in report.keys
            if is_governance_actionable(r, min_delta_pct)
        ]
        if changes:
            changes.sort(key=lambda c: c.key)
            plans.append(TenantPlan(tenant=report.tenant, changes=changes,
                                    source_file=getattr(report, "source_file", "")))
    plans.sort(key=lambda p: p.tenant)
    return plans


def collect_inherited(
    reports: list["recommend.TenantRecommendation"],
) -> list[InheritedKey]:
    """The threshold keys each tenant inherits (#2116), for reference only."""
    out = [
        InheritedKey(report.tenant, r.key, r.source_layer, r.source_file)
        for report in reports for r in getattr(report, "inherited", [])
    ]
    out.sort(key=lambda u: (u.tenant, u.key))
    return out


# The recommender marks lower-bound ``<`` thresholds (hit-ratio / availability —
# where rot means LOWERING a floor) as skipped: a P95-upper recommendation would
# REDUCE protection, so the engine emits ``recommended=None`` and never queries
# them (threshold_recommend → _observed_map_lib.resolve_observed). Both engine
# skip-reason paths share this stable token. Matching it (plus the None guard)
# isolates the ``<`` blind spot from other skips (unmapped / no-data).
_LOWER_BOUND_SKIP_MARKER = "lower-bound (<)"


def collect_ungoverned_lower_bound(
    reports: list["recommend.TenantRecommendation"],
) -> list[UngovernedKey]:
    """Bare/half-resolved lower-bound ``<`` keys the engine skipped as not-supported.

    These fail the gate silently (``recommended is None``), so surfacing a count
    keeps any un-classified ``<`` tail observable instead of an invisible coverage
    hole. NOT in the plan — they need manual classification. After
    #916 the real map yields none (every ``<`` is percentile-lower or N/A); the
    detector stays for hand-edited entries.
    """
    out: list[UngovernedKey] = []
    for report in reports:
        for r in report.keys:
            if r.recommended is None and _LOWER_BOUND_SKIP_MARKER in (r.reason or ""):
                out.append(UngovernedKey(report.tenant, r.key, r.reason))
    out.sort(key=lambda u: (u.tenant, u.key))
    return out


# The by-design not-applicable marker build_map stamps into a N/A ``<`` entry's
# skip-reason. Bound to the lib constant (via the recommender's import) so a
# rename can't silently turn this detector into a no-op (mirrors the
# _LOWER_BOUND_SKIP_MARKER contract). Must NOT be a substring of the ``<``
# not-supported marker, else N/A keys mis-route into the ungoverned list.
_NOT_APPLICABLE_MARKER = recommend.observed_map_lib._NOT_APPLICABLE_MARKER


def collect_not_applicable(
    reports: list["recommend.TenantRecommendation"],
) -> list[NotApplicableKey]:
    """By-design not-applicable ``<`` keys → an INFO count (governance complete).

    These are expected-value invariants / topology floors: no percentile
    recommendation is meaningful, so they leave the ⚠ ungoverned list and are
    reported as an INFO by-design count instead.
    """
    out: list[NotApplicableKey] = []
    for report in reports:
        for r in report.keys:
            if r.recommended is None and _NOT_APPLICABLE_MARKER in (r.reason or ""):
                out.append(NotApplicableKey(report.tenant, r.key, r.reason))
    out.sort(key=lambda u: (u.tenant, u.key))
    return out


def collect_force_manual(
    reports: list["recommend.TenantRecommendation"],
) -> list[ForceManualKey]:
    """Keys the recommender routed to manual review (``force_manual``).

    Mostly lower-bound guardrail trips (#916: relaxation / out-of-domain / thin
    sample; the floor-rot core signal); also an upper-bound current of 0
    (#2997) and recommender exceptions (see ``ForceManualKey``). Surfaced as its own manual-review section + count so a floor
    drifting toward relaxation is never invisible in the governance run.
    """
    out: list[ForceManualKey] = []
    for report in reports:
        for r in report.keys:
            if getattr(r, "force_manual", False):
                out.append(ForceManualKey(
                    report.tenant, r.key,
                    r.guardrail_reason or r.reason, r.delta_pct,
                ))
    out.sort(key=lambda u: (u.tenant, u.key))
    return out


def collect_not_measured(
    reports: list["recommend.TenantRecommendation"],
) -> list[NotMeasuredKey]:
    """Keys whose query was not answered (#3133). They used to drop out of the
    run silently — a Prometheus that answered nothing read as "no thresholds
    need governance" at exit 0."""
    out = [NotMeasuredKey(report.tenant, r.key, r.query_error)
           for report in reports for r in report.keys + report.inherited if r.query_error]
    out.sort(key=lambda u: (u.tenant, u.key))
    return out


# ---------------------------------------------------------------------------
# The edit: Go applies it (ADR-036 §2 — only Go writes conf.d; #3206)
# ---------------------------------------------------------------------------
#: Staged copies of a --config-dir for `da-guard write` (`_stage`), one per
#: directory for the process; removed at exit.
_STAGES: dict[str, str] = {}





# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_text_report(
    plans: list[TenantPlan],
    ungoverned: Optional[list["UngovernedKey"]] = None,
    not_applicable: Optional[list["NotApplicableKey"]] = None,
    force_manual: Optional[list["ForceManualKey"]] = None,
    inherited: Optional[list["InheritedKey"]] = None,
    not_measured: Optional[list["NotMeasuredKey"]] = None,
) -> str:
    lines: list[str] = []
    lines.append("Threshold governance report — read-only (nothing is written)")
    lines.append("=" * 78)

    def _ungoverned_lines() -> list[str]:
        ung = ungoverned or []
        if not ung:
            return []
        head = (
            f"⚠ {len(ung)} 個閾值未治理（lower-bound `<` 未分類，需人工 review；#916）："
            if _LANG == "zh" else
            f"⚠ {len(ung)} threshold(s) ungoverned (unclassified lower-bound `<`, "
            "manual review — #916):"
        )
        return ["", "-" * 78, head] + [
            f"    {safe_label(u.tenant)} / {safe_label(u.key)}" for u in ung]

    def _force_manual_lines() -> list[str]:
        fm = force_manual or []
        if not fm:
            return []
        # Cause-agnostic wording: force_manual is a generic flag — a lower-bound
        # floor guardrail trip (#916), an upper-bound key whose current is 0
        # (#2997), or analyze_tenant's per-key except for ANY-direction key that
        # throws. Each body line carries its own guardrail_reason (the cause), so
        # the header names no cause and mislabels none.
        head = (
            f"⚠ {len(fm)} 個閾值需人工 review（不提供建議值，原因見各行）："
            if _LANG == "zh" else
            f"⚠ {len(fm)} threshold(s) need manual review "
            "(no value proposed — reason per line):"
        )
        body = []
        for k in fm:
            d = f" ({k.delta_pct:+.1f}% miss)" if k.delta_pct is not None else ""
            body.append(f"    {safe_label(k.tenant)} / {safe_label(k.key)}: "
                        f"{safe_label(k.guardrail_reason)}{d}")
        return ["", "-" * 78, head] + body

    def _not_applicable_lines() -> list[str]:
        na = not_applicable or []
        if not na:
            return []
        head = (
            f"ℹ {len(na)} 個 lower-bound `<` 閾值 by-design 不適用百分位推薦（治理完成，無需動作；#916）。"
            if _LANG == "zh" else
            f"ℹ {len(na)} lower-bound `<` threshold(s) by-design not-applicable to "
            "percentile recommendation (governance complete, no action — #916)."
        )
        return ["", "-" * 78, head]

    def _inherited_lines() -> list[str]:
        inh = inherited or []
        if not inh:
            return []
        n_tenants = len({k.tenant for k in inh})
        files = sorted({k.source_file for k in inh})
        shown = ", ".join(safe_label(f) for f in files[:5]) + (
            f" (+{len(files) - 5} more)" if len(files) > 5 else "")
        head = (
            f"ℹ {n_tenants} 個租戶的 {len(inh)} 個閾值繼承自平台／defaults／profile 檔"
            f"（{shown}），只列參考、不治理：要調的只有租戶自己的檔（#2116）。"
            if _LANG == "zh" else
            f"ℹ {len(inh)} threshold(s) across {n_tenants} tenant(s) are inherited from a "
            f"defaults / platform / profile file ({shown}) — reference only, not governed: "
            "only the tenant's own file is retuned (#2116)."
        )
        return ["", "-" * 78, head]

    def _not_measured_lines() -> list[str]:
        nm = not_measured or []
        if not nm:
            return []
        head = (
            f"✗ {len(nm)} 個閾值沒量成（Prometheus 查詢沒得到回答，#3133），報告不完整："
            if _LANG == "zh" else
            f"✗ {len(nm)} threshold(s) not measured (the Prometheus query was not "
            "answered — #3133); the report is partial:"
        )
        return ["", "-" * 78, head] + [
            f"  {safe_label(u.tenant)}/{safe_label(u.key)}: {safe_label(u.error[:80])}"
            for u in nm]

    def _deferred_note() -> str:
        # Fold the two review-worthy counts into Summary so a tail-scan catches
        # them even when the per-tenant output is long; N/A is complete (INFO), so
        # it is NOT folded into the bottom line (its detail line above suffices).
        n_ung = len(ungoverned or [])
        n_fm = len(force_manual or [])
        n_nm = len(not_measured or [])
        parts: list[str] = []
        if n_nm:
            parts.append(
                f" ✗ 另有 {n_nm} 個閾值沒量成，報告不完整。" if _LANG == "zh"
                else f" ✗ {n_nm} threshold(s) not measured; the report is partial.")
        if n_fm:
            parts.append(
                f" ⚠ 另有 {n_fm} 個閾值需人工 review。" if _LANG == "zh"
                else f" ⚠ {n_fm} threshold(s) also need manual review.")
        if n_ung:
            parts.append(
                f" ⚠ 另有 {n_ung} 個未分類 `<` 閾值未治理。" if _LANG == "zh"
                else f" ⚠ {n_ung} unclassified `<` threshold(s) also ungoverned.")
        return "".join(parts)

    def _deferred_block() -> list[str]:
        # All surfaced-but-not-planned categories, above the bottom line.
        return (_not_measured_lines() + _force_manual_lines() + _ungoverned_lines()
                + _not_applicable_lines() + _inherited_lines())

    if not plans:
        if not_measured:
            # #3133: "all within margin" is not what a run that measured
            # nothing (or not everything) found.
            lines.append(
                "量到的閾值中沒有需要治理的；有閾值沒量成（見下），報告不完整。"
                if _LANG == "zh" else
                "No measured threshold needs governance; some were not measured "
                "(below), the report is partial."
            )
        else:
            lines.append(
                "未發現需要治理的閾值（全部在閾值內或樣本不足）。"
                if _LANG == "zh" else
                "No thresholds need governance (all within margin or low-confidence)."
            )
        lines += _deferred_block()
        if not_measured:  # the bottom line a tail-scan reads
            lines.append(_deferred_note().strip())
        return "\n".join(lines)

    for plan in plans:
        where = f" — {safe_label(plan.source_file)}" if plan.source_file else ""
        lines.append(f"\nTenant: {safe_label(plan.tenant)} "
                     f"({len(plan.changes)} change(s)){where}")
        lines.append(f"  {'Key':<26s} {'Current':>10s} {'→':^3s} {'Recommend':>10s} {'Delta':>13s} {'Conf':<8s}")
        lines.append(f"  {'-' * 26} {'-' * 10} {'-' * 3} {'-' * 10} {'-' * 13} {'-' * 8}")
        for c in plan.changes:
            cur = str(c.current_value)
            rec = recommend._format_threshold_value(c.recommended)
            # Tag lower-bound (miss-rate space) deltas with " miss" so an operator
            # never reads a negative delta beside a RISING value as a mistake. The
            # engine stamps miss-rate into the reason; the plan carries that reason.
            miss = " miss" if recommend._MISS_RATE_MARKER in (c.reason or "") else ""
            delta = f"{c.delta_pct:+.1f}%{miss}"
            lines.append(
                f"  {c.key:<26s} {cur:>10s} {'→':^3s} {rec:>10s} "
                f"{delta:>13s} {c.confidence:<8s}"
            )

    # Blind-spot / manual-review detail goes ABOVE the bottom line so Summary
    # stays the last line a reader scanning the tail expects (CLI bottom-line
    # convention); the review counts are folded into Summary via _deferred_note().
    lines += _deferred_block()

    total_changes = sum(len(p.changes) for p in plans)
    lines.append("\n" + "=" * 78)
    lines.append(
        f"Summary: {len(plans)} tenant(s) / {total_changes} change(s) need retuning. "
        "Nothing was written: set the values in each tenant's file by hand."
        + _deferred_note()
    )
    return "\n".join(lines)



def json_document(
    plans: list[TenantPlan],
    ungoverned: Optional[list["UngovernedKey"]] = None,
    not_applicable: Optional[list["NotApplicableKey"]] = None,
    force_manual: Optional[list["ForceManualKey"]] = None,
    inherited: Optional[list["InheritedKey"]] = None,
    not_measured: Optional[list["NotMeasuredKey"]] = None,
) -> dict:
    """The --json document (run() encodes it, #3216)."""
    ung = ungoverned or []
    nm = not_measured or []
    na = not_applicable or []
    fm = force_manual or []
    inh = inherited or []
    out = {
        "tool": "threshold-govern",
        "plans": [asdict(p) for p in plans],
        "ungoverned_lower_bound": [asdict(u) for u in ung],
        "not_applicable": [asdict(u) for u in na],
        "force_manual": [asdict(u) for u in fm],
        "inherited": [asdict(u) for u in inh],
        "not_measured": [asdict(u) for u in nm],
        "summary": {
            "tenants_actionable": len(plans),
            "changes": sum(len(p.changes) for p in plans),
            "ungoverned_lower_bound": len(ung),
            "not_applicable": len(na),
            "force_manual": len(fm),
            "inherited": len(inh),
            "not_measured": len(nm),
        },
    }
    if nm:
        # #3133: as threshold-recommend marks its own report.
        out.update({"status": "partial", "reason": "prometheus_query_error"})
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> GovernanceResult:
    """Run the recommender and gate its output. Writes nothing.

    The surfaced-but-not-planned categories are always populated (all
    informational): ``ungoverned`` (un-classified ``<`` tail),
    ``not_applicable`` (by-design N/A INFO), ``force_manual`` (manual review:
    lower-bound guardrail trip, upper-bound current of 0, recommender
    exception), ``inherited`` and ``not_measured``.
    """
    reports = recommend.run_analysis(
        args.config_dir,
        prometheus_url=prometheus_url(args.prometheus),
        tenant_filter=args.tenant,
        lookback=args.lookback,
        min_samples=args.min_samples,
        dry_run=False,
    )
    return GovernanceResult(
        plans=build_governance_plan(reports, args.min_delta_pct),
        ungoverned=collect_ungoverned_lower_bound(reports),
        not_applicable=collect_not_applicable(reports),
        force_manual=collect_force_manual(reports),
        inherited=collect_inherited(reports),
        not_measured=collect_not_measured(reports),
        queried=sum(1 for r in reports for k in r.keys if k.queried),
    )


@exit_on_yaml_file_error  # a config file the load cannot read → rc 2, named
@exit_on_served_values_error  # #2116: da-guard missing or failing → rc 2, named
@recommend.observed_map_lib.exit_on_observed_map_error  # #3017: no usable observed-map → rc 2, named
@_lib_parse_cache.exit_on_parse_tree_error  # #3751: no usable parse-tree → rc 2
def main() -> "Report | None":
    parser = argparse.ArgumentParser(
        description=(
            "閾值治理報告 — 列出值得回頭調的腐敗閾值（唯讀，不寫入；#656）"
            if _LANG == "zh" else
            "Threshold governance report — list the rotten thresholds worth "
            "retuning (read-only, writes nothing; #656)"
        ),
    )
    # #3751: an empty conf.d is refused naming the flag (it was da-guard's
    # "no .yaml files found in <path>", rc 2, the flag unnamed).
    _lib_input.add_input(parser, "--config-dir", select=_lib_input.DA_GUARD_LISTS,
                         empty=_lib_input.EMPTY_REFUSED, required=True,
                         help="租戶配置目錄路徑（conf.d/）" if _LANG == "zh"
                         else "Path to tenant config directory (conf.d/)")
    # W1: canonical env fallback — `os.environ.get(k, default)` returned ""
    # for a present-but-empty $PROMETHEUS_URL; _lib_io.prometheus_url treats
    # "" as unset (#3528: the one env fallback; the dispatcher adds none).
    add_prometheus_arg(parser, help_text="Prometheus Query API URL")
    parser.add_argument("--tenant", default=None,
                        help="只分析指定租戶" if _LANG == "zh" else "Analyze only this tenant")
    parser.add_argument("--lookback", default="7d",
                        help=("回溯期間（預設 7d；下界/percentile-lower key 建議 14d——"
                              "7d 排除兩端 partial 後僅約 6 完整 UTC 日，對 scrape gap 脆弱）"
                              if _LANG == "zh" else
                              "Lookback period (default 7d; use 14d for lower-bound/"
                              "percentile-lower keys — 7d leaves only ~6 full UTC days "
                              "after dropping partial boundaries, fragile to scrape gaps)"))
    parser.add_argument("--min-samples", type=recommend.min_samples_arg, default=100,
                        help=("最低樣本數門檻：MEDIUM 與 HIGH 都至少要這麼多樣本"
                              "（預設 100，不得小於 100；HIGH 另需 ≥ 1000）" if _LANG == "zh"
                              else "Minimum sample count: both MEDIUM and HIGH need at "
                                   "least this many samples (default 100, may not be "
                                   "below 100; HIGH also needs >= 1000)"))
    parser.add_argument("--min-delta-pct", type=float, default=DEFAULT_MIN_DELTA_PCT,
                        help=("治理門檻：|delta%%| 須 ≥ 此值才列入報告（預設 25）。注意"
                              "上界為 value 空間、下界為 miss-rate 空間——同旋鈕敏感度不同"
                              "（0.95 floor 的 25%% miss ≈ 1.3%% value）"
                              if _LANG == "zh" else
                              "Governance gate: only report a key when |delta%%| >= this (default 25). "
                              "NB asymmetric: upper-bound is value space, lower-bound is MISS-RATE "
                              "space — same knob, different sensitivity (25%% miss on a 0.95 floor "
                              "≈ 1.3%% value)"))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON 格式輸出" if _LANG == "zh" else "Output as JSON")
    args = parser.parse_args()
    # One reader of --config-dir for the run: the one `declared_trees` judged.
    with _lib_parse_cache.session():
        return _main(args)


def _main(args: argparse.Namespace) -> "Report | None":
    # #3751: judged once — the path (#3276), a root da-guard cannot list, a
    # tree with no config file — each refusal naming --config-dir (a
    # CallerError: rc 2 through `run()`). `run` reads the tree at the path
    # judged here.
    _lib_parse_cache.declared_trees(args, "config_dir", lang=_LANG)

    if parse_duration_seconds(args.lookback) is None:
        msg = (f"無效的 lookback 值: {args.lookback}" if _LANG == "zh"
               else f"Invalid lookback value: {args.lookback}")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.min_delta_pct < 5.0:
        # Below the upper-bound engine's 5% VALUE no-change margin nothing is
        # exportable anyway — fail loud rather than silently produce an empty plan.
        # NB: for lower-bound keys --min-delta-pct is compared in MISS-RATE space
        # (the engine's own lower no-change margin is 10% miss), so this floor is a
        # necessary-not-sufficient lower bound; the two spaces are not the same
        # sensitivity (see --min-delta-pct help).
        msg = ("--min-delta-pct 不可低於上界引擎的 5%% value 雜訊邊界" if _LANG == "zh"
               else "--min-delta-pct cannot be below the upper-bound engine's 5%% value noise margin")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    result = run(args)

    if not args.json_output:
        print(format_text_report(
            result.plans, result.ungoverned, result.not_applicable,
            result.force_manual, result.inherited, not_measured=result.not_measured))

    # #3133: a threshold not measured makes the report partial — a caller error
    # (exit 2). A complete report is exit 0 whether or not it lists rot.
    if result.not_measured:
        first = result.not_measured[0]
        print(f"ERROR: Prometheus query failed for {len(result.not_measured)} threshold(s); "
              f"the report is partial (first: {safe_label(first.tenant)}/"
              f"{safe_label(first.key)}: {safe_label(first.error[:80])})", file=sys.stderr)
        status = EXIT_CALLER_ERROR
    else:
        status = EXIT_OK
    if args.json_output:
        # #3617: the thresholds not measured are named; the rest is the
        # partial answer — none measured is no answer (rc 2, stdout empty).
        return partial_answer(json_document(  # #3216: run() encodes it
            result.plans, result.ungoverned, result.not_applicable,
            result.force_manual, result.inherited, result.not_measured), status,
            unanswered=[f"{m.tenant}/{m.key}" for m in result.not_measured],
            of=result.queried)
    sys.exit(status)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
