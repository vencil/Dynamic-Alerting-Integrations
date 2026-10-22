#!/usr/bin/env python3
"""threshold_govern.py — 閾值治理迴路（Renovate-for-thresholds，#656）。

把現成的「閾值推薦引擎」(``threshold_recommend.py``) 接成**主動治理迴路**：
定期跑推薦 → 過濾出值得介入的腐敗閾值 → 透過 tenant-api 為每個租戶開一個
**可一鍵批准的 proposed-PR**（reuse ADR-011 WritePR / 單寫者 ADR-023），把
「沒人主動回收的過鬆閾值」從「需要有人想起來」降到「批准一個 PR」。

Activate the existing recommendation ENGINE as an active governance loop (the
"DELIVER" rung of the PREVENT→DETECT→DELIVER ladder, #656): run the recommender,
gate on rot magnitude + confidence, then open one approvable per-tenant PR via
the tenant-api (single-writer, ADR-023) instead of a Slack nudge nobody acts on.

設計重點（與 issue #656 鎖定設計一致）：
  - **介入 = proposed-PR**：不自己 git push（違反單寫者）；POST 給 tenant-api
    （唯一寫者）開 per-tenant PR。天然 human-in-loop（owner 批 PR）。
  - **DETECT = recommender 的 delta%**：current vs P95 偏離幅度就是腐敗訊號。
  - **閘門**：只納 |delta| ≥ ``--min-delta-pct`` 且 confidence ∈ {HIGH, MEDIUM}
    （樣本不足不開 PR，防破窗 / 防噪音治理）。
  - **dedup**：tenant-api 的錯誤答覆依其錯誤碼在錯誤契約（``tenant-api-errors.json``，
    #3202）裡的 remedy 判讀（#3271）：``existing``（該租戶已有待審 PR）→ 視為已在處理、
    跳過，所以重跑不會洗出重複 PR；``refresh_retry``（base 動了）→ 重新 GET／合併／驗證後
    再 PUT 一次；其他 remedy、未知 remedy、契約沒列的碼 → error。
  - **通道隔離**：對 tenant-api 帶 ``X-DA-Write-Source: threshold-governance``
    header（PR-mode PUT 端消費）→ PR 走獨立 label / 標題 / 來源，不冒充
    tenant-manager UI、不污染告警平面。
  - **快照精神**：對 Prometheus 的查詢沿用 recommender（一次性 range 查詢），
    排程在離峰 + ``concurrencyPolicy: Forbid``，不持續 hammer prod。
  - **安全預設 dry-run**：不帶 ``--apply`` 只印出「會開哪些 PR」，**不發任何寫入**。

讀-改-寫的正確性（重要）：本工具 GET 當前完整租戶 YAML → 只 surgical 取代被推薦的
threshold 值行（保留註解 / 縮排 / 其餘行 byte-identical）→ 以 ``da-guard parse-tree``
（ADR-036 §2）讀改前、改後兩份文字，確認「只有目標 keys 變成推薦值」（其餘鍵、結構、每個
純量的原文／型別／值都不變；改前那份 parse-tree 讀不了、或租戶檔路徑沒有讀取端也拒絕）→
才 PUT。不通過的租戶記為 ``error``、不送 PUT；沒有可用的 da-guard 結束碼 2。產出的 PR diff 因此就是那幾行值的變化，乾淨可批准。
自 #3064 起 tenant-api 只寫「API 管理」的租戶：租戶檔只宣告它自己，body 整檔逐位元組
寫入。共用檔等其他形狀的租戶由 git 管理：GET 不給 ``raw_yaml``（本工具記為 error、
不寫入），PUT 回 409 ``TENANT_GIT_MANAGED``（remedy ``fix_in_repo``，不當成 dedup）。

只治理租戶檔自己寫的 key（#2116）：歸屬取自 ``da-guard effective`` 的逐 key 來源；
繼承自 defaults／平台／profile 的 key 只列參考（``inherited``），不進計畫、不查
Prometheus；寫回用檔案裡的拼法（舊別名照舊）。租戶檔在子目錄時 tenant-api 讀不到，
不送 GET、記 ``skipped_nested``，不計入斷路器與 ``--max-prs``。

領域邊界：本工具是 recommender 的**主動層**，推薦邏輯/資料源完全沿用
``threshold_recommend``（Day-N，查 observed recording rule）。冷啟動粗估請用
``baseline_discovery``（Day-0，查 raw exporter）——⛔ 三者勿混用。

Usage:
  # Dry-run（預設，安全）：印出會為哪些租戶開 PR，不發任何寫入
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090

  # 真正開 PR（--apply）：經 tenant-api 為每個租戶開 per-tenant governance PR
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 \\
    --apply --tenant-api-url http://tenant-api.tenant-api.svc.cluster.local:8080 \\
    --identity-email threshold-governance@platform.local \\
    --identity-groups threshold-governance

  # 收緊閘門 + 限制每次最多開 5 個 PR（防洪 / alert-fatigue budget）
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 \\
    --min-delta-pct 40 --max-prs 5 --apply --tenant-api-url http://tenant-api.tenant-api.svc.cluster.local:8080 \\
    --identity-groups threshold-governance

  # JSON 輸出（pipeline / 觀測）
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 --json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

from _lib_python import (  # noqa: E402
    add_prometheus_arg,
    detect_cli_lang,
    exit_on_yaml_file_error,
    http_get_json,
    non_negative_seconds,
    parse_duration_seconds,
    positive_seconds,
)
from _lib_compat import find_project_root  # noqa: E402
from _lib_tenant_values import exit_on_served_values_error  # noqa: E402  (#2116)
from _lib_parse_cache import exit_on_parse_tree_error  # noqa: E402  (ADR-036 §2, #2906)
# Aliased: the local format_json_report() below (domain report builder,
# exercised directly by tests) delegates its final dump to the shared helper.
from _lib_python import format_json_report as _dump_json  # noqa: E402
from _lib_io import safe_label  # noqa: E402  (#1538 output-layer escaping)
from _lib_prometheus import _validate_url_scheme  # noqa: E402
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION, arm_crash_envelope,
)

# Reuse the recommendation engine wholesale — threshold_govern is its active
# layer, never a re-implementation (the engine owns percentiles / observed-map /
# direction-skip / confidence). Importing runs only its module-level setup; its
# main() is __main__-guarded.
import threshold_recommend as recommend  # noqa: E402

_LANG = detect_cli_lang()

# The allowlisted X-DA-Write-Source value the tenant-api maps to the governance
# PR channel (distinct label/title/source). MUST match the Go const
# handler.WriteSourceThresholdGovernance.
WRITE_SOURCE = "threshold-governance"

# Governance defaults (tunable via flags).
DEFAULT_MIN_DELTA_PCT = 25.0   # only intervene past meaningful rot (engine margin is 5%)
DEFAULT_MAX_PRS = 10           # cap PRs per run — anti-flood / alert-fatigue budget
DEFAULT_THROTTLE_SECONDS = 2.0  # pause between tenant writes (don't stampede the writer)
DEFAULT_IDENTITY_EMAIL = "threshold-governance@platform.local"
# Circuit-break: stop attempting after this many CONSECUTIVE errors so a degraded
# write plane (503 / git contention) isn't hammered once per remaining tenant.
MAX_CONSECUTIVE_ERRORS = 5
# #656 dead-man's-switch: an --apply run whose failure share (errors + breaker
# skips) reaches this fraction of the attempted set is a SYSTEMIC write failure and
# exits non-zero (#3054: whether or not a PR was opened). < 1.0 so a single surviving
# already_pending can't mask a near-total outage (Gemini adversarial review); high
# enough that ordinary partial degradation (a few flaky tenants among many successes)
# does NOT trip it.
_SYSTEMIC_FAILURE_RATIO = 0.90


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class PlannedChange:
    """One actionable per-key recommendation that cleared the governance gate."""

    key: str
    current_value: Any
    recommended: float
    new_value: str          # conf.d-quoted string actually written (e.g. '"45"')
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
class TenantOutcome:
    """The result of attempting (or simulating) a governance PR for one tenant."""

    tenant: str
    # pr_opened | already_pending | no_changes | error | skipped |
    # skipped_breaker | skipped_nested.
    #   skipped: --max-prs reached this run (no request was sent)
    #   skipped_breaker (#3054): not attempted because the circuit breaker
    #     tripped after MAX_CONSECUTIVE_ERRORS consecutive errors
    #   skipped_nested (#2116): the tenant's file is in a sub-directory, which
    #     tenant-api's tenant read does not cover — no request was sent
    status: str
    keys: list[str] = field(default_factory=list)
    pr_url: str = ""
    pr_number: int = 0
    message: str = ""


@dataclass
class UngovernedKey:
    """A threshold governance cannot act on — a bare/half-resolved lower-bound
    ``<`` still engine-skipped (#916 left it needs_review / not-supported).

    Surfaced so the un-classified ``<`` tail is an OBSERVABLE blind spot, not a
    silent coverage hole: these never reach a PR (``recommended is None`` fails
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
    kafka active-controllers / broker-count / integer-count floors).

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
    distinct from the auto-PR plan and the by-design N/A INFO.
    """

    tenant: str
    key: str
    guardrail_reason: str
    delta_pct: Optional[float]


@dataclass
class InheritedKey:
    """A threshold a tenant inherits from a defaults / platform / profile file
    (#2116): its value is not in the tenant's file, so a governance PR on that
    file cannot change it and must not pin it. Listed for reference only (not
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
    outcomes: list[TenantOutcome]
    ungoverned: list[UngovernedKey]
    not_applicable: list[NotApplicableKey] = field(default_factory=list)
    force_manual: list[ForceManualKey] = field(default_factory=list)
    inherited: list[InheritedKey] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Governance gate
# ---------------------------------------------------------------------------
def is_governance_actionable(
    rec: "recommend.KeyRecommendation", min_delta_pct: float
) -> bool:
    """True iff this recommendation is strong enough to warrant a proposed-PR.

    Stricter than the engine's ``_exportable`` (5% margin): a governance PR fires
    only when the threshold has drifted meaningfully (``|delta| >= min_delta_pct``)
    AND the recommendation rests on enough samples (HIGH/MEDIUM confidence). The
    confidence floor is the anti-noise guard — we never nag an operator to retune
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
    """Render a recommendation as the conf.d string literal to write (quoted)."""
    return '"' + recommend._format_threshold_value(rec.recommended) + '"'


def build_governance_plan(
    reports: list["recommend.TenantRecommendation"], min_delta_pct: float
) -> list[TenantPlan]:
    """Filter the engine's reports down to per-tenant actionable change sets.

    Only `report.keys` — the keys the tenant's own file writes — can enter a
    plan (#2116): an inherited key (`report.inherited`) is not in the file the
    PR edits, and writing it there would pin the tenant."""
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


def _is_nested(plan: TenantPlan) -> bool:
    """The tenant's file sits in a sub-directory of --config-dir (#2116).
    tenant-api reads a tenant's file at the top level of its config dir only,
    so a GET for this tenant cannot return the file the change belongs in."""
    return "/" in plan.source_file


def _split_nested(plans: list[TenantPlan]) -> tuple[list[TenantPlan], list[TenantPlan]]:
    """(plans a PR can be opened for, plans whose tenant file is nested)."""
    return ([p for p in plans if not _is_nested(p)], [p for p in plans if _is_nested(p)])


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
    hole. NOT actionable here (no PR) — they need manual classification. After
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


# ---------------------------------------------------------------------------
# Surgical YAML editing (preserve comments + minimal diff)
# ---------------------------------------------------------------------------
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_inline_comment(line: str) -> tuple[str, str]:
    """Split a line into (code, trailing_comment) at the first ' #' that is
    OUTSIDE a quoted string.

    YAML requires whitespace before an inline ``#``. Scanning quote state means a
    ``#`` inside a quoted value (e.g. ``label: "a # b"``) is NOT mistaken for a
    comment — so the safety-net verify can't be fooled by such a value (threshold
    values are numeric/simple, but the editor must not corrupt a non-threshold
    sibling line either). The whitespace run before ``#`` is kept on the comment
    side so the code side ends exactly at the value.
    """
    in_single = in_double = False
    for i, c in enumerate(line):
        if c == "'" and not in_double:
            in_single = not in_single
        elif c == '"' and not in_single:
            in_double = not in_double
        elif (c == "#" and not in_single and not in_double
              and i > 0 and line[i - 1] in " \t"):
            j = i
            while j > 0 and line[j - 1] in " \t":
                j -= 1
            return line[:j], line[j:]
    return line, ""


def apply_threshold_changes(
    raw_yaml: str, tenant: str, new_values: dict[str, str]
) -> tuple[str, list[str]]:
    """Surgically replace threshold values in ``tenant``'s block of ``raw_yaml``.

    Only DIRECT scalar children of the tenant are touched; indentation, inline
    comments, and every other byte are preserved so the resulting PR diff is
    exactly the changed value lines. Returns ``(new_yaml, unapplied_keys)`` —
    keys not found as a direct child are returned (the caller fails loud rather
    than silently dropping a recommendation; a separate parse-verify step is the
    real correctness gate).
    """
    lines = raw_yaml.split("\n")
    unapplied = sorted(new_values)

    # 1. Locate the top-level `tenants:` mapping.
    t_idx = next(
        (i for i, ln in enumerate(lines) if re.match(r"^tenants:\s*(#.*)?$", ln)),
        None,
    )
    if t_idx is None:
        return raw_yaml, unapplied

    # 2. Find the tenant entry (shallowest indent under `tenants:`).
    tenant_idx = None
    tenant_indent = None
    j = t_idx + 1
    while j < len(lines):
        ln = lines[j]
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            ind = _indent(ln)
            if ind == 0:
                break  # back to top level → left the tenants: block
            if tenant_indent is None:
                tenant_indent = ind
            if ind == tenant_indent:
                key = stripped.split(":", 1)[0].strip()
                if key == tenant:
                    tenant_idx = j
                    break
        j += 1
    if tenant_idx is None:
        return raw_yaml, unapplied

    # 3. Tenant block = lines after the tenant key, indented deeper than it.
    block_end = tenant_idx + 1
    while block_end < len(lines):
        ln = lines[block_end]
        stripped = ln.strip()
        if stripped and not stripped.startswith("#") and _indent(ln) <= tenant_indent:
            break
        block_end += 1

    # 4. Direct-child indent = the first child key line's indent.
    child_indent = None
    for k in range(tenant_idx + 1, block_end):
        ln = lines[k]
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            child_indent = _indent(ln)
            break
    if child_indent is None:
        return raw_yaml, unapplied

    # 5. Replace each target key's value on its direct-child line.
    out = list(lines)
    remaining = dict(new_values)
    for k in range(tenant_idx + 1, block_end):
        if not remaining:
            break
        ln = out[k]
        if _indent(ln) != child_indent:
            continue
        code, comment = _split_inline_comment(ln)
        for key in list(remaining):
            # Capture a trailing CR so a CRLF file keeps its line ending: without
            # the (\r?) group, `.*` would swallow the \r on a comment-less line
            # and emit a lone LF, leaving a mixed-EOL file (spurious diff churn).
            m = re.match(r"^(\s*" + re.escape(key) + r"\s*:\s*)\S.*?(\r?)$", code)
            if m:
                out[k] = m.group(1) + remaining.pop(key) + m.group(2) + comment
                break

    return "\n".join(out), sorted(remaining)


def verify_only_changed(
    old_yaml: str, new_yaml: str, tenant: str, expected: dict[str, str],
    *, config_dir: Optional[str] = None, rel: str = "",
) -> Optional[str]:
    """Confirm the edit changes ONLY ``expected`` keys of ``tenant``, as
    ``da-guard parse-tree`` reads both texts (ADR-036 §2, #2906).

    Returns ``None`` if safe, else a human error string. This is the
    correctness gate: even if the surgical edit had a bug, a mismatch here
    aborts the write so a corrupted file is never PUT. ``expected`` maps key
    → conf.d literal (``'"45"'``, a double-quoted string).

    The old text is read by parse-tree as the file at *rel* of *config_dir*
    (the tenant's own file, ``<tenant>.yaml`` when unknown; a missing
    *config_dir* is an empty tree); the new text must read back as exactly
    that content with each expected key set to its string — every other key,
    the structure, and each scalar's source text, tag and value unchanged.
    An old text parse-tree cannot read (a duplicate key, a syntax error, …)
    or a path no reader reads is refused too. No usable da-guard raises
    ``_lib_parse_tree.ParseTreeError``.
    """
    import tempfile

    import _lib_parse_tree  # lazy: keeps `--dry-run` import-light
    import _lib_write_check

    if config_dir is None:
        with tempfile.TemporaryDirectory(prefix="threshold-govern-") as empty:
            return verify_only_changed(old_yaml, new_yaml, tenant, expected,
                                       config_dir=empty, rel=rel)
    rel = rel or f"{tenant}.yaml"
    root = config_dir
    want: dict[str, Any] = {}
    for key, literal in expected.items():
        text = literal.strip()
        if not (len(text) >= 2 and text[0] == text[-1] == '"'):
            return f"{key}: the literal {literal!r} is not a double-quoted string"
        want[key] = _lib_write_check.scalar(text[1:-1])
    try:
        old = _lib_write_check.original(root, rel, old_yaml.encode("utf-8"))
        tenants = old.get("tenants")
        block = (tenants.get(tenant) if isinstance(tenants, dict)
                 and not _lib_parse_tree.is_scalar(tenants) else None)
        if not isinstance(block, dict) or _lib_parse_tree.is_scalar(block):
            return f"tenant {tenant!r} has no mapping block in the live config"
        missing = sorted(set(want) - set(block))
        if missing:
            return f"keys not in the live config: {missing}"

        def _change(tree: Any) -> None:
            tree["tenants"][tenant].update(want)
        _lib_write_check.confirm(root, rel, new_yaml,
                                 _lib_write_check.with_change(old, _change))
    except _lib_write_check.WriteRefused as exc:
        return str(exc)
    return None


# ---------------------------------------------------------------------------
# tenant-api client (read-modify-write a per-tenant governance PR)
# ---------------------------------------------------------------------------
def _resolve_auth_token(args: argparse.Namespace) -> str:
    """Resolve the Bearer token: a literal (``--auth-token`` / ``DA_GOVERN_TOKEN``)
    wins, else a token FILE (``--auth-token-file`` / ``DA_GOVERN_TOKEN_FILE``) — a
    Kubernetes audience-bound projected SA token (ADR-027). kubelet rotates the
    file, so it is read at call time (this one-shot CLI reads it once per run).

    The Bearer here only feeds tenant-api's machine-identity AUDIT, which never
    gates the write; authz rides on the identity headers. So a token-file read
    error must NOT abort governance — it degrades to no Bearer (the audit records
    no_token) with a warning, mirroring the audit's own never-block contract.
    Returns "" when no token is configured or the file cannot be read.
    """
    literal = args.auth_token or os.environ.get("DA_GOVERN_TOKEN", "")
    if literal:
        return literal
    if args.auth_token_file:
        try:
            with open(args.auth_token_file, encoding="utf-8") as fh:
                return fh.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            # OSError = I/O failure; UnicodeDecodeError = a non-UTF-8/corrupt file
            # (NOT an OSError subclass). Both degrade to no Bearer — the fail-open
            # contract must not let a malformed token file abort governance (#962).
            print(f"warning: could not read --auth-token-file "
                  f"{args.auth_token_file!r}: {exc}; continuing without a Bearer "
                  f"(tenant-api machine-identity audit will record no_token)",
                  file=sys.stderr)
    return ""


def _auth_headers(args: argparse.Namespace) -> dict[str, str]:
    """Identity + audit headers for the tenant-api call.

    Direct in-cluster mode injects ``X-Forwarded-Email`` / ``X-Forwarded-Groups``
    (the tenant-api RBAC reads these — see rbac/middleware.go); a NetworkPolicy
    must restrict who can reach the API directly with injected identity. An
    oauth2-proxy-fronted deployment instead passes a Bearer and lets the proxy
    inject identity. The Bearer may ALSO be a Kubernetes projected SA token
    (``--auth-token-file``) that tenant-api audits (ADR-027 machine-identity);
    that audit is orthogonal to authz, so the identity headers and the Bearer
    may (and, for in-cluster governance, do) coexist.
    """
    headers: dict[str, str] = {"X-DA-Write-Source": WRITE_SOURCE}
    if args.identity_email:
        headers["X-Forwarded-Email"] = args.identity_email
    if args.identity_groups:
        headers["X-Forwarded-Groups"] = args.identity_groups
    token = _resolve_auth_token(args)
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _http_put_yaml(
    url: str, yaml_body: str, headers: dict[str, str], timeout: float
) -> tuple[int, Optional[dict], Optional[str]]:
    """PUT a raw-YAML body. Returns ``(status_code, parsed_json|None, error|None)``.

    The tenant-api PUT commits the body verbatim over the tenant's own file; it
    writes only an API-managed tenant (#3064: a git-managed one, e.g. in a file
    shared with other tenants, is a 409 TENANT_GIT_MANAGED). The body is sent as
    ``application/yaml`` (NOT JSON-wrapped). The status code and parsed body are
    surfaced so the caller can classify a refusal by its code's remedy
    (``classify_error_answer``).
    """
    scheme_err = _validate_url_scheme(url)
    if scheme_err:
        return 0, None, scheme_err
    req = urllib.request.Request(  # nosec B310 — scheme validated above
        url, data=yaml_body.encode("utf-8"), method="PUT"
    )
    req.add_header("Content-Type", "application/yaml")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {}), None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001 — body is best-effort diagnostics only
            pass
        parsed = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = {"raw": body[:300]}
        return exc.code, parsed, None
    except (urllib.error.URLError, OSError) as exc:
        return 0, None, str(exc)


# ---------------------------------------------------------------------------
# tenant-api error contract (#3202 / #3271)
# ---------------------------------------------------------------------------
# What a non-200 answer means is NOT decided here. tenant-api gives every error
# a code, and every code ONE remedy, in its internal/errcode; cmd/gen-error-contract
# writes that table to tenant-api-errors.json (drift-gated by `make
# error-contract-check`). This tool reads the code off the answer, the remedy off
# the contract, and acts on the remedy through _REMEDY_ACTIONS below — so a code
# tenant-api adds under an existing remedy is handled without touching this file.
_ERROR_CONTRACT_BASENAME = "tenant-api-errors.json"
# Where the generator writes it in a checkout (gen-error-contract's contractRel).
_ERROR_CONTRACT_REPO_DIR = ("tools", "portal", "src", "interactive", "tools",
                            "_common", "data")

# Remedy → what this tool does with a PUT answering it. ⛔ Every remedy the
# contract can carry is listed, deliberately, error ones included: the test
# test_every_contract_remedy_has_an_explicit_action compares this table with the
# contract's remedies, so a remedy tenant-api adds turns CI red until someone
# decides here what it means for a governance write. At run time a remedy missing
# from the table (or a code missing from the contract) is an error — fail-closed,
# never a silent dedup skip.
#   dedup — something already there stands in the way (an open PR/MR for this
#           tenant): a prior run's change is in flight → already_pending.
#   retry — what the PUT was prepared on changed: GET, re-merge, re-verify and PUT
#           once more; a second refusal is reported as an error.
#   error — recorded as this tenant's error (the next scheduled run tries again).
_REMEDY_ACTIONS: dict[str, str] = {
    "existing": "dedup",
    "refresh_retry": "retry",
    "retry_after": "error",
    "fix_request": "error",
    "fix_in_repo": "error",
    "choose_other": "error",
    "permission": "error",
    "operator": "error",
    "not_found": "error",
    "refresh": "error",
}

# Attempts per tenant for a refresh_retry answer: the first PUT plus one retry.
_MAX_PUT_ATTEMPTS = 2


class ErrorContractError(RuntimeError):
    """tenant-api-errors.json is missing or not the generator's shape."""


_ERROR_REMEDIES: Optional[dict[str, str]] = None


def _find_error_contract() -> Optional[Path]:
    """Locate tenant-api-errors.json in either shipping layout.

    Flat first: the da-tools image copies it beside this module (build.sh
    ``REPO_DATA_FILES``, paired by ``REQUIRED_DATA_FILES`` in
    check_build_completeness.py) and has no repo tree. Otherwise the
    generator's output path under THIS tree's root (``find_project_root``).
    """
    here = Path(__file__).resolve().parent
    flat = here / _ERROR_CONTRACT_BASENAME
    if flat.is_file():
        return flat
    root = find_project_root(here)
    if root is not None:
        candidate = root.joinpath(*_ERROR_CONTRACT_REPO_DIR, _ERROR_CONTRACT_BASENAME)
        if candidate.is_file():
            return candidate
    return None


def load_error_remedies(path: Optional[Path] = None) -> dict[str, str]:
    """``{code: remedy}`` from tenant-api's error contract. Fails closed.

    Raises ErrorContractError when the file is absent or is not
    ``{"codes": [{"code": str, "remedy": str, ...}, ...]}``.
    """
    if path is None:
        path = _find_error_contract()
    if path is None:
        raise ErrorContractError(
            f"{_ERROR_CONTRACT_BASENAME} not found beside this tool nor under this "
            f"tree's {'/'.join(_ERROR_CONTRACT_REPO_DIR)}/ — tenant-api answers "
            "cannot be classified")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ErrorContractError(f"cannot read {path}: {exc}") from exc
    codes = doc.get("codes") if isinstance(doc, dict) else None
    if not isinstance(codes, list) or not codes:
        raise ErrorContractError(f"{path}: no 'codes' list")
    remedies: dict[str, str] = {}
    for row in codes:
        code = row.get("code") if isinstance(row, dict) else None
        remedy = row.get("remedy") if isinstance(row, dict) else None
        if not isinstance(code, str) or not code or not isinstance(remedy, str) or not remedy:
            raise ErrorContractError(f"{path}: malformed row {row!r}")
        remedies[code] = remedy
    return remedies


def _error_remedies() -> dict[str, str]:
    """The contract's ``{code: remedy}``, loaded once per process."""
    global _ERROR_REMEDIES
    if _ERROR_REMEDIES is None:
        _ERROR_REMEDIES = load_error_remedies()
    return _ERROR_REMEDIES


def classify_error_answer(body: Any) -> tuple[str, str, str]:
    """``(action, code, remedy)`` for a non-200 tenant-api answer.

    *action* is one of ``_REMEDY_ACTIONS``' values. An answer with no code, a
    code the contract does not list, or a remedy the table does not handle is
    ``error`` — fail-closed.
    """
    code = body.get("code") if isinstance(body, dict) else None
    if not isinstance(code, str) or not code:
        return "error", "", ""
    remedy = _error_remedies().get(code, "")
    return _REMEDY_ACTIONS.get(remedy, "error"), code, remedy


def _error_answer_message(status: int, body: Any, code: str, remedy: str,
                          attempts: int) -> str:
    detail = ""
    if isinstance(body, dict):
        detail = body.get("error") or body.get("message") or json.dumps(body)[:200]
    head = f"HTTP {status}" + (f" {code}" if code else "")
    if code and not remedy:
        note = " (code not in tenant-api's error contract)"
    elif remedy and remedy not in _REMEDY_ACTIONS:
        note = f" (remedy {remedy!r} not handled by this tool)"
    elif remedy:
        note = f" (remedy {remedy})"
    else:
        note = ""
    if attempts > 1:
        note += f" after {attempts} attempts"
    return f"{head}: {detail}{note}"


def _prepare_put_body(plan: TenantPlan, args: argparse.Namespace, get_url: str,
                      headers: dict, keys: list[str]) -> "str | TenantOutcome":
    """GET the live tenant YAML, merge the changes, verify; the body to PUT.

    Returns a TenantOutcome (``error``) instead when any step refuses.
    """
    detail, err = http_get_json(get_url, timeout=args.timeout, headers=headers)
    if err:
        return TenantOutcome(plan.tenant, "error", keys, message=f"GET failed: {err}")
    raw_yaml = (detail or {}).get("raw_yaml")
    if not raw_yaml:
        return TenantOutcome(
            plan.tenant, "error", keys, message="GET returned no raw_yaml"
        )

    new_values = {c.key: c.new_value for c in plan.changes}
    merged, unapplied = apply_threshold_changes(raw_yaml, plan.tenant, new_values)
    if unapplied:
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=f"keys not found in live config: {unapplied}",
        )
    import _lib_parse_tree  # lazy, as in verify_only_changed
    try:
        verify_err = verify_only_changed(raw_yaml, merged, plan.tenant, new_values,
                                         config_dir=args.config_dir,
                                         rel=plan.source_file)
    except _lib_parse_tree.BinaryMissing:
        raise  # no da-guard at all: the whole run is rc 2 (main's decorator)
    except _lib_parse_tree.ParseTreeError as exc:
        # A da-guard that failed on THIS tenant (a timeout, the memory
        # limit, an unexpected exit): this tenant's error, the run goes on
        # and its summary still lists the PRs already opened.
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=f"refused to PUT (verify could not run): {exc}",
        )
    if verify_err:
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=f"refused to PUT (verify failed): {verify_err}",
        )
    return merged


def _classify_200(plan: TenantPlan, keys: list[str], body: Any) -> TenantOutcome:
    """Outcome of a 200 PUT answer: ``pr_opened``, ``no_changes`` or ``error``."""
    body = body or {}
    # The PR IS the human gate (#656). Only PR write-mode returns
    # status=pending_review + a pr_url; a tenant-api in the DEFAULT "direct"
    # write-mode returns status=ok having committed straight to the base
    # branch — which would silently bypass review AND kill the 409 dedup.
    # Refuse to claim a PR we didn't get, so a non-PR-mode target fails loud
    # on the first run instead of direct-committing tenant config for weeks.
    # A PR-mode API answers `no_changes` when the body it was handed is
    # already byte-identical to the branch base it just cut. That is a
    # healthy no-op, not a misconfigured deployment: this run planned the
    # change off the pod's on-disk copy, which the write path deliberately
    # does NOT treat as the base (the fresh origin base is re-resolved per
    # write), so the two can legitimately disagree once the change has
    # landed upstream. Recording it as an error would (a) tell the operator
    # to set a write-mode that is already set, and (b) feed the systemic-
    # failure ratio, failing a Job that in fact had nothing left to do.
    if body.get("status") == "no_changes":
        return TenantOutcome(
            plan.tenant, "no_changes", keys,
            message="tenant-api reports the proposed content already matches "
                    "its base — nothing to propose",
        )
    if body.get("status") != "pending_review" or not body.get("pr_url"):
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=(f"tenant-api is not in PR write-mode (status="
                     f"{body.get('status')!r}) — refusing to direct-commit; "
                     "set --write-mode pr-github/pr-gitlab on the API"),
        )
    return TenantOutcome(
        plan.tenant, "pr_opened", keys,
        pr_url=body["pr_url"], pr_number=body.get("pr_number", 0),
        message=body.get("message", ""),
    )


def open_governance_pr(
    plan: TenantPlan, args: argparse.Namespace
) -> TenantOutcome:
    """GET → surgical merge → verify → PUT a per-tenant governance PR.

    Maps the tenant-api response: 200 + ``pending_review`` → ``pr_opened``;
    200 + ``no_changes`` → ``no_changes`` (the API found the body already equal
    to its fresh base — nothing to propose, and no PR to open). Any other answer
    is classified by its code's remedy in tenant-api's error contract
    (``classify_error_answer``): remedy ``existing`` → ``already_pending`` (the
    dedup path — a prior run's PR is still open, leave it); ``refresh_retry`` →
    GET, merge, verify and PUT once more; anything else, and a second
    ``refresh_retry`` → ``error`` (recorded, not raised, so one bad tenant
    doesn't sink the run).
    """
    keys = [c.key for c in plan.changes]
    base = args.tenant_api_url.rstrip("/")
    enc = urllib.parse.quote(plan.tenant, safe="")
    get_url = f"{base}/api/v1/tenants/{enc}"
    headers = _auth_headers(args)

    attempt = 0
    while True:
        attempt += 1
        merged = _prepare_put_body(plan, args, get_url, headers, keys)
        if isinstance(merged, TenantOutcome):
            return merged
        status, body, put_err = _http_put_yaml(get_url, merged, headers, args.timeout)
        if put_err:
            return TenantOutcome(plan.tenant, "error", keys,
                                 message=f"PUT failed: {put_err}")
        if status == 200:
            return _classify_200(plan, keys, body)
        action, code, remedy = classify_error_answer(body)
        if action == "retry" and attempt < _MAX_PUT_ATTEMPTS:
            continue
        if action == "dedup":
            return TenantOutcome(
                plan.tenant, "already_pending", keys,
                pr_url=str(body.get("existing_pr_url") or ""),
                message=(f"tenant-api answered {code} (remedy {remedy}): an "
                         "existing PR/MR for this tenant is in the way — skipped"),
            )
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=_error_answer_message(status, body, code, remedy, attempt),
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_text_report(
    plans: list[TenantPlan], outcomes: list[TenantOutcome], applied: bool,
    ungoverned: Optional[list["UngovernedKey"]] = None,
    not_applicable: Optional[list["NotApplicableKey"]] = None,
    force_manual: Optional[list["ForceManualKey"]] = None,
    inherited: Optional[list["InheritedKey"]] = None,
    max_prs: Optional[int] = None,
) -> str:
    lines: list[str] = []
    mode = "APPLY" if applied else "DRY-RUN (no writes — pass --apply to open PRs)"
    lines.append(f"Threshold governance loop — {mode}")
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
            f"（{shown}），只列參考、不治理：PR 只改租戶自己的檔（#2116）。"
            if _LANG == "zh" else
            f"ℹ {len(inh)} threshold(s) across {n_tenants} tenant(s) are inherited from a "
            f"defaults / platform / profile file ({shown}) — reference only, not governed: "
            "a PR edits the tenant's own file only (#2116)."
        )
        return ["", "-" * 78, head]

    def _deferred_note() -> str:
        # Fold the two review-worthy counts into Summary so a tail-scan catches
        # them even when the per-tenant output is long; N/A is complete (INFO), so
        # it is NOT folded into the bottom line (its detail line above suffices).
        n_ung = len(ungoverned or [])
        n_fm = len(force_manual or [])
        parts: list[str] = []
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
        # All surfaced-but-no-auto-PR categories, above the bottom line.
        return (_force_manual_lines() + _ungoverned_lines() + _not_applicable_lines()
                + _inherited_lines())

    if not plans:
        lines.append(
            "未發現需要治理的閾值（全部在閾值內或樣本不足）。"
            if _LANG == "zh" else
            "No thresholds need governance (all within margin or low-confidence)."
        )
        lines += _deferred_block()
        return "\n".join(lines)

    for plan in plans:
        nested = (f" — {safe_label(plan.source_file)} is in a sub-directory: no PR via "
                  "tenant-api, change it by hand") if _is_nested(plan) else ""
        lines.append(f"\nTenant: {safe_label(plan.tenant)} "
                     f"({len(plan.changes)} change(s)){nested}")
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

    if applied and outcomes:
        lines.append("\n" + "-" * 78)
        lines.append("Outcomes:")
        for o in outcomes:
            tag = {
                "pr_opened": "✓ PR",
                "already_pending": "• skip (already open)",
                "no_changes": "• no-op (already at base)",
                "error": "✗ error",
                "skipped_breaker": "✗ skip (circuit breaker)",
                "skipped_nested": "• skip (file in a sub-directory)",
            }.get(o.status, o.status)
            detail = o.pr_url or o.message
            lines.append(f"  [{tag}] {safe_label(o.tenant)}: {safe_label(detail)}")

    # Blind-spot / manual-review detail goes ABOVE the bottom line so Summary
    # stays the last line a reader scanning the tail expects (CLI bottom-line
    # convention); the review counts are folded into Summary via _deferred_note().
    lines += _deferred_block()

    opened = sum(1 for o in outcomes if o.status == "pr_opened")
    pending = sum(1 for o in outcomes if o.status == "already_pending")
    nochange = sum(1 for o in outcomes if o.status == "no_changes")
    errors = sum(1 for o in outcomes if o.status == "error")
    capped = sum(1 for o in outcomes if o.status == "skipped")
    breaker = sum(1 for o in outcomes if o.status == "skipped_breaker")
    prable, nested = _split_nested(plans)
    nested_note = (f" {len(nested)} tenant(s) with a file in a sub-directory get no PR "
                   "(change by hand)." if nested else "")
    lines.append("\n" + "=" * 78)
    if applied:
        lines.append(
            f"Summary: {opened} PR(s) opened, {pending} already-pending (skipped), "
            f"{nochange} no-op, {errors} error(s), {capped} skipped (--max-prs), "
            f"{breaker} skipped (circuit breaker); {len(prable)} tenant(s) actionable."
            + nested_note + _deferred_note()
        )
    else:
        total_changes = sum(len(p.changes) for p in prable)
        if max_prs is not None and max_prs < len(prable):
            # #2929: --apply stops at the cap. "at most": a dry run does not ask
            # tenant-api, so it cannot foresee a 409 (already pending) or error.
            head = (f"Summary: {len(prable)} tenant(s) / {total_changes} change(s) "
                    f"actionable; with --max-prs {max_prs}, at most {max_prs} PR(s) "
                    "would be opened. ")
        else:
            head = (f"Summary: {len(prable)} tenant(s) / {total_changes} change(s) "
                    "would get a PR. ")
        lines.append(
            head + "Re-run with --apply to open them." + nested_note + _deferred_note()
        )
    return "\n".join(lines)


def format_json_report(
    plans: list[TenantPlan], outcomes: list[TenantOutcome], applied: bool,
    ungoverned: Optional[list["UngovernedKey"]] = None,
    not_applicable: Optional[list["NotApplicableKey"]] = None,
    force_manual: Optional[list["ForceManualKey"]] = None,
    inherited: Optional[list["InheritedKey"]] = None,
) -> str:
    ung = ungoverned or []
    na = not_applicable or []
    fm = force_manual or []
    inh = inherited or []
    prable, nested = _split_nested(plans)
    out = {
        "tool": "threshold-govern",
        "applied": applied,
        "plans": [asdict(p) for p in plans],
        "outcomes": [asdict(o) for o in outcomes],
        "ungoverned_lower_bound": [asdict(u) for u in ung],
        "not_applicable": [asdict(u) for u in na],
        "force_manual": [asdict(u) for u in fm],
        "inherited": [asdict(u) for u in inh],
        "summary": {
            # As the text Summary counts them (#2116): a tenant whose file is in
            # a sub-directory gets no PR, in a dry run or with --apply, so it is
            # counted under `skipped_nested`, not as actionable.
            "tenants_actionable": len(prable),
            "changes": sum(len(p.changes) for p in prable),
            "prs_opened": sum(1 for o in outcomes if o.status == "pr_opened"),
            "already_pending": sum(1 for o in outcomes if o.status == "already_pending"),
            "no_changes": sum(1 for o in outcomes if o.status == "no_changes"),
            "errors": sum(1 for o in outcomes if o.status == "error"),
            # --max-prs reached (no request sent) vs circuit breaker tripped
            # (#3054); the same split as outcomes[].status.
            "skipped": sum(1 for o in outcomes if o.status == "skipped"),
            "skipped_breaker": sum(1 for o in outcomes if o.status == "skipped_breaker"),
            "skipped_nested": len(nested),
            "ungoverned_lower_bound": len(ung),
            "not_applicable": len(na),
            "force_manual": len(fm),
            "inherited": len(inh),
        },
    }
    return _dump_json(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run(args: argparse.Namespace) -> GovernanceResult:
    """Run the recommender, gate, and (if --apply) open per-tenant PRs.

    Returns a ``GovernanceResult``. In dry-run, outcomes is empty (no writes). The
    three surfaced-but-not-auto-PR categories are always populated independent of
    --apply (all informational): ``ungoverned`` (un-classified ``<`` tail),
    ``not_applicable`` (by-design N/A INFO), and ``force_manual`` (manual review:
    lower-bound guardrail trip, upper-bound current of 0, recommender exception).
    """
    reports = recommend.run_analysis(
        args.config_dir,
        prometheus_url=args.prometheus,
        tenant_filter=args.tenant,
        lookback=args.lookback,
        min_samples=args.min_samples,
        dry_run=False,
    )
    plans = build_governance_plan(reports, args.min_delta_pct)
    ungoverned = collect_ungoverned_lower_bound(reports)
    not_applicable = collect_not_applicable(reports)
    force_manual = collect_force_manual(reports)
    inherited = collect_inherited(reports)

    outcomes: list[TenantOutcome] = []
    if not args.apply:
        return GovernanceResult(plans, outcomes, ungoverned, not_applicable, force_manual,
                                inherited)

    opened = 0
    attempted = 0
    consecutive_errors = 0
    for plan in plans:
        if _is_nested(plan):
            # #2116: no request is sent, so this is neither an error for the
            # circuit breaker nor a PR against --max-prs.
            outcomes.append(TenantOutcome(
                plan.tenant, "skipped_nested", [c.key for c in plan.changes],
                message=(f"tenant file {plan.source_file} is in a sub-directory; tenant-api "
                         "reads tenant files at the top level of its config dir only, so no "
                         "PR is opened — apply the change to that file by hand"),
            ))
            continue
        if opened >= args.max_prs:
            outcomes.append(TenantOutcome(
                plan.tenant, "skipped", [c.key for c in plan.changes],
                message=f"--max-prs={args.max_prs} reached this run",
            ))
            continue
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            outcomes.append(TenantOutcome(
                plan.tenant, "skipped_breaker", [c.key for c in plan.changes],
                message=(f"aborted after {consecutive_errors} consecutive errors "
                         "(tenant-api likely degraded)"),
            ))
            continue
        # Pace BEFORE each attempt except the first — paces every API round-trip
        # without an idle sleep after the final one.
        if attempted > 0 and args.throttle_seconds > 0:
            time.sleep(args.throttle_seconds)
        attempted += 1
        outcome = open_governance_pr(plan, args)
        outcomes.append(outcome)
        # Only a NEW PR counts against the per-run cap (dedup skips / errors don't
        # consume the alert-fatigue budget). Errors trip the circuit breaker;
        # a pr_opened OR a clean already_pending round-trip resets it.
        if outcome.status == "pr_opened":
            opened += 1
            consecutive_errors = 0
        elif outcome.status == "error":
            consecutive_errors += 1
        else:
            consecutive_errors = 0
    return GovernanceResult(plans, outcomes, ungoverned, not_applicable, force_manual,
                            inherited)


def _is_systemic_failure(outcomes: list[TenantOutcome], applied: bool) -> bool:
    """Whether an ``--apply`` run's writes failed overwhelmingly (#656, #3054).

    #656 dead-man's-switch honesty: a run where the write plane is broken still
    *executed*, so without a non-zero exit the process returns 0, K8s marks the Job
    Successful, and ``kube_cronjob_status_last_successful_time`` advances even though
    the writes failed — the staleness alert would stay green during exactly the
    silent degradation it exists to catch (did-it-run != did-it-work). Exiting
    non-zero makes it honest: the Job is marked Failed AND last_successful_time is not
    advanced, so ``ThresholdGovernanceStale`` can fire.

    Shares maintenance_scheduler's MECHANISM (non-zero exit -> Job Failed -> KSM signal
    stays honest) but is STRICTER than that sibling's any-error contract: a governance
    run legitimately has per-tenant errors amid successes, so a single flaky tenant
    must not fail the whole run.

    Trip condition: the attempted set is non-empty AND
    ``(errors + skipped_breaker) / attempted >= _SYSTEMIC_FAILURE_RATIO``.
      * attempted = every outcome that sent a write or was going to: ``pr_opened``,
        ``already_pending`` (409), ``no_changes``, ``error`` and ``skipped_breaker``.
        A ``skipped`` (--max-prs reached) or ``skipped_nested`` (#2116) outcome sent
        no request and would not have sent one this run, so it says nothing about
        the write plane and is left out of the attempted set altogether.
      * ``error`` is not only a write-plane fault (PUT failed / HTTP 5xx / not in
        PR write-mode): it also covers a failed read (``GET failed`` / ``GET
        returned no raw_yaml``) and the local checks that refuse to write
        (``keys not found in live config``, ``refused to PUT (verify failed)``).
        All of them count as failures here.
      * Breaker skips count as failures. The run's own circuit breaker caps
        consecutive errors at ``MAX_CONSECUTIVE_ERRORS`` then skips the rest, so a
        TOTAL outage shows up as ~5 errors + N breaker skips — a bare error ratio
        would dilute that to ~5% and miss it.
      * There is no "a PR was opened, so the run is healthy" short-circuit (#3054):
        one PR opened before the write plane died no longer hides the outage. Nor
        does one surviving ``already_pending`` (Gemini review). A genuinely
        half-degraded run and an all-already-pending run both stay exit 0; dry-run
        never fails — it issues no writes; an empty plan has nothing to judge.

    What the rc measures: it still depends on the plan — the same write-plane fault
    fails a run of 3 tenants sooner than a run of 300, and an empty plan is rc 0 no
    matter how the write plane is. But for a fixed plan it is monotone in write-plane
    health: turning any one successful write into an error never takes the rc from
    1 to 0. For a signal that does not depend on the plan, watch tenant-api itself:
    ``tenant_api_writes_total{result}`` and the ``TenantApiWriteErrorsHigh`` alert.
    """
    if not applied:
        return False
    attempted = [o for o in outcomes if o.status not in ("skipped", "skipped_nested")]
    if not attempted:
        return False
    failed = sum(1 for o in attempted if o.status in ("error", "skipped_breaker"))
    return failed / len(attempted) >= _SYSTEMIC_FAILURE_RATIO


@exit_on_yaml_file_error  # a config file the load cannot read → rc 2, named
@exit_on_served_values_error  # #2116: da-guard missing or failing → rc 2, named
@exit_on_parse_tree_error  # ADR-036 §2: the pre-PUT parse-tree check has no da-guard → rc 2
@recommend.observed_map_lib.exit_on_observed_map_error  # #3017: no usable observed-map → rc 2, named
def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "閾值治理迴路 — 把推薦引擎接成 proposed-PR（Renovate-for-thresholds，#656）"
            if _LANG == "zh" else
            "Threshold governance loop — turn recommendations into proposed-PRs (#656)"
        ),
    )
    parser.add_argument("--config-dir", required=True,
                        help="租戶配置目錄路徑（conf.d/）" if _LANG == "zh"
                        else "Path to tenant config directory (conf.d/)")
    # W1: canonical env fallback — `os.environ.get(k, default)` returned ""
    # for a present-but-empty $PROMETHEUS_URL; add_prometheus_arg's `or`
    # chain treats "" as unset (aligns with entrypoint.inject_prometheus_env).
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
                        help=("治理介入門檻：|delta%%| 須 ≥ 此值才開 PR（預設 25）。注意"
                              "上界為 value 空間、下界為 miss-rate 空間——同旋鈕敏感度不同"
                              "（0.95 floor 的 25%% miss ≈ 1.3%% value）"
                              if _LANG == "zh" else
                              "Governance gate: only open a PR when |delta%%| >= this (default 25). "
                              "NB asymmetric: upper-bound is value space, lower-bound is MISS-RATE "
                              "space — same knob, different sensitivity (25%% miss on a 0.95 floor "
                              "≈ 1.3%% value)"))
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS,
                        help=("每次最多開幾個 PR（防洪，預設 10；須 ≥ 1）" if _LANG == "zh"
                              else "Max PRs opened per run (anti-flood, default 10; "
                                   "must be >= 1)"))
    parser.add_argument("--apply", action="store_true",
                        help=("真正經 tenant-api 開 PR（預設僅 dry-run，不寫入）"
                              if _LANG == "zh" else
                              "Actually open PRs via tenant-api (default: dry-run, no writes)"))
    parser.add_argument("--tenant-api-url", default=os.environ.get("TENANT_API_URL"),
                        help="tenant-api base URL（--apply 必填）" if _LANG == "zh"
                        else "tenant-api base URL (required with --apply)")
    parser.add_argument("--identity-email", default=DEFAULT_IDENTITY_EMAIL,
                        help="X-Forwarded-Email（PR git author / 治理身分）" if _LANG == "zh"
                        else "X-Forwarded-Email (PR git author / governance identity)")
    parser.add_argument("--identity-groups", default=os.environ.get("DA_GOVERN_GROUPS"),
                        help=("X-Forwarded-Groups（須具 write 權限的 RBAC 群組；--apply 直連模式必填）"
                              if _LANG == "zh" else
                              "X-Forwarded-Groups (an RBAC group with write perm; required for direct --apply)"))
    parser.add_argument("--auth-token", default=None,
                        help=("Bearer token（oauth2-proxy 前置模式；亦可用 DA_GOVERN_TOKEN）"
                              if _LANG == "zh" else
                              "Bearer token (oauth2-proxy mode; or env DA_GOVERN_TOKEN)"))
    parser.add_argument("--auth-token-file", default=os.environ.get("DA_GOVERN_TOKEN_FILE"),
                        help=("Bearer token 檔路徑（K8s audience-bound projected SA token；"
                              "呼叫時讀取；亦可用 DA_GOVERN_TOKEN_FILE）"
                              if _LANG == "zh" else
                              "Path to a Bearer token file (K8s audience-bound projected SA "
                              "token, read at call time; or env DA_GOVERN_TOKEN_FILE)"))
    parser.add_argument("--throttle-seconds", type=non_negative_seconds, default=DEFAULT_THROTTLE_SECONDS,
                        help="開 PR 之間的間隔秒數（≥ 0，0 = 不等待，可帶小數；預設 2）" if _LANG == "zh"
                        else "Seconds to pause between opened PRs (>= 0, 0 = no wait, "
                        "fractions allowed; default 2)")
    parser.add_argument("--timeout", type=positive_seconds, default=30,
                        help=("tenant-api 的 HTTP 逾時秒數（> 0，可帶小數；預設 30；"
                              "不影響 Prometheus 查詢）" if _LANG == "zh" else
                              "HTTP timeout in seconds for tenant-api calls (> 0, "
                              "fractions allowed; default 30; Prometheus queries "
                              "are not affected)"))
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON 格式輸出" if _LANG == "zh" else "Output as JSON")
    args = parser.parse_args()
    if args.json_output:
        # #3094: a crash in --json mode still prints one envelope
        arm_crash_envelope(tool="threshold-govern")

    if not os.path.isdir(args.config_dir):
        msg = (f"配置目錄不存在: {args.config_dir}" if _LANG == "zh"
               else f"Config directory not found: {args.config_dir}")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

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

    if args.max_prs < 1:
        # #2929: 0 or less used to skip every tenant and still exit 0.
        msg = (f"--max-prs 必須 ≥ 1: {args.max_prs}" if _LANG == "zh"
               else f"--max-prs must be >= 1: {args.max_prs}")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.apply:
        if not args.tenant_api_url:
            msg = ("--apply 需要 --tenant-api-url" if _LANG == "zh"
                   else "--apply requires --tenant-api-url")
            print(msg, file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        if not args.identity_groups and not args.auth_token and not args.auth_token_file:
            msg = ("--apply 需要 --identity-groups（直連）或 --auth-token/--auth-token-file（oauth2-proxy）"
                   if _LANG == "zh" else
                   "--apply requires --identity-groups (direct) or "
                   "--auth-token/--auth-token-file (oauth2-proxy)")
            print(msg, file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        # #3271: tenant-api's answers are classified by its error contract.
        # Without it no 409 can be told apart, so refuse before any write.
        try:
            _error_remedies()
        except ErrorContractError as exc:
            print(f"threshold-govern: {exc}", file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    result = run(args)

    if args.json_output:
        print(format_json_report(
            result.plans, result.outcomes, args.apply, result.ungoverned,
            result.not_applicable, result.force_manual, result.inherited))
    else:
        print(format_text_report(
            result.plans, result.outcomes, args.apply, result.ungoverned,
            result.not_applicable, result.force_manual, result.inherited,
            max_prs=args.max_prs))

    # #656: surface a governance run whose writes overwhelmingly failed as a non-zero
    # exit (-> Job Failed + a frozen last_successful_time so ThresholdGovernanceStale
    # can fire). A clean / partly degraded / dry-run run stays exit 0. See
    # _is_systemic_failure for why the dead-man's-switch needs this to be honest.
    sys.exit(EXIT_VIOLATION if _is_systemic_failure(result.outcomes, args.apply) else EXIT_OK)


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
