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
  - **dedup**：tenant-api 對「該租戶已有 pending PR」回 409 → 視為已在處理、跳過，
    所以重跑不會洗出重複 PR。
  - **通道隔離**：對 tenant-api 帶 ``X-DA-Write-Source: threshold-governance``
    header（PR-mode PUT 端消費）→ PR 走獨立 label / 標題 / 來源，不冒充
    tenant-manager UI、不污染告警平面。
  - **快照精神**：對 Prometheus 的查詢沿用 recommender（一次性 range 查詢），
    排程在離峰 + ``concurrencyPolicy: Forbid``，不持續 hammer prod。
  - **安全預設 dry-run**：不帶 ``--apply`` 只印出「會開哪些 PR」，**不發任何寫入**。

讀-改-寫的正確性（重要）：tenant-api 的 PUT 是**整檔覆寫**（verbatim），所以本工具
GET 當前完整租戶 YAML → 只 surgical 取代被推薦的 threshold 值行（保留註解 / 縮排 /
其餘行 byte-identical）→ parse-before/after 驗證「只有目標 keys 變動」→ 才 PUT。
產出的 PR diff 因此就是那幾行值的變化，乾淨可批准。

領域邊界：本工具是 recommender 的**主動層**，推薦邏輯/資料源完全沿用
``threshold_recommend``（Day-N，查 observed recording rule）。冷啟動粗估請用
``baseline_discovery``（Day-0，查 raw exporter）——⛔ 三者勿混用。

Usage:
  # Dry-run（預設，安全）：印出會為哪些租戶開 PR，不發任何寫入
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090

  # 真正開 PR（--apply）：經 tenant-api 為每個租戶開 per-tenant governance PR
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 \\
    --apply --tenant-api-url http://tenant-api:8080 \\
    --identity-email threshold-governance@platform.local \\
    --identity-groups threshold-governance

  # 收緊閘門 + 限制每次最多開 5 個 PR（防洪 / alert-fatigue budget）
  da-tools threshold-govern --config-dir ./conf.d/ --prometheus http://prometheus:9090 \\
    --min-delta-pct 40 --max-prs 5 --apply --tenant-api-url http://tenant-api:8080 \\
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
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))

from _lib_python import (  # noqa: E402
    detect_cli_lang,
    http_get_json,
    parse_duration_seconds,
)
from _lib_prometheus import _validate_url_scheme  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402

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


@dataclass
class TenantOutcome:
    """The result of attempting (or simulating) a governance PR for one tenant."""

    tenant: str
    status: str             # planned | pr_opened | already_pending | error | skipped
    keys: list[str] = field(default_factory=list)
    pr_url: str = ""
    pr_number: int = 0
    message: str = ""


@dataclass
class UngovernedKey:
    """A threshold governance cannot act on — lower-bound ``<`` (engine-skipped).

    Surfaced so the deferred ``<`` support (#656) is an OBSERVABLE blind spot,
    not a silent coverage hole: these never reach a PR (``recommended is None``
    fails the gate), and ``<`` rot lowers a hit-ratio / availability floor =
    protection silently reduced.
    """

    tenant: str
    key: str
    reason: str


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
    a threshold off a thin sample (防破窗).

    Keys the engine skipped (unmapped / lower-bound ``<`` / unsupported scope /
    no data → ``recommended is None``) are excluded for free.
    """
    return (
        rec.recommended is not None
        and rec.delta_pct is not None
        and abs(rec.delta_pct) >= min_delta_pct
        and rec.confidence in (recommend.CONFIDENCE_HIGH, recommend.CONFIDENCE_MEDIUM)
    )


def _quoted_value(rec: "recommend.KeyRecommendation") -> str:
    """Render a recommendation as the conf.d string literal to write (quoted)."""
    return '"' + recommend._format_threshold_value(rec.recommended) + '"'


def build_governance_plan(
    reports: list["recommend.TenantRecommendation"], min_delta_pct: float
) -> list[TenantPlan]:
    """Filter the engine's reports down to per-tenant actionable change sets."""
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
            plans.append(TenantPlan(tenant=report.tenant, changes=changes))
    plans.sort(key=lambda p: p.tenant)
    return plans


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
    """Lower-bound ``<`` keys the engine skipped → the governance blind spot.

    These fail the gate silently (``recommended is None``), so surfacing a count
    keeps the deferred ``<`` support (#656) observable instead of an invisible
    coverage hole. NOT actionable here (no PR) — they need manual review.
    """
    out: list[UngovernedKey] = []
    for report in reports:
        for r in report.keys:
            if r.recommended is None and _LOWER_BOUND_SKIP_MARKER in (r.reason or ""):
                out.append(UngovernedKey(report.tenant, r.key, r.reason))
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
    old_yaml: str, new_yaml: str, tenant: str, expected: dict[str, str]
) -> Optional[str]:
    """Parse both docs and confirm ONLY ``expected`` keys changed for ``tenant``.

    Returns ``None`` if safe, else a human error string. This is the correctness
    gate: even if the surgical edit had a bug, a mismatch here aborts the write so
    a corrupted file is never PUT. ``expected`` maps key → conf.d literal (e.g.
    ``'"45"'``); the comparison is against the PARSED value (so ``"45"`` → ``45``
    matches whether quoted or not).

    yaml is imported lazily (the dry-run path needs none of this).
    """
    import yaml  # lazy: keeps `--dry-run` import-light and host-portable

    try:
        old_doc = yaml.safe_load(old_yaml) or {}
        new_doc = yaml.safe_load(new_yaml) or {}
    except yaml.YAMLError as exc:
        return f"post-edit YAML did not parse: {exc}"

    # Harden the safety net itself: a bug that dropped the `tenants:` wrapper or
    # the tenant section would otherwise resolve to {} on both sides and slip
    # through. Require the tenant to still be present after the edit.
    new_tenants = new_doc.get("tenants", {}) or {}
    if tenant not in new_tenants:
        return f"tenant {tenant!r} vanished from config after edit"
    old = (old_doc.get("tenants", {}) or {}).get(tenant, {}) or {}
    new = new_tenants.get(tenant, {}) or {}

    if set(old) != set(new):
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        return f"key set changed (added={added}, removed={removed})"

    def _norm(v: Any) -> str:
        return str(v).strip()

    expected_parsed = {}
    for key, literal in expected.items():
        # The literal is a quoted scalar; strip the quotes for comparison.
        expected_parsed[key] = literal.strip().strip('"')

    for key in old:
        if key in expected:
            if _norm(new[key]) != expected_parsed[key]:
                return (
                    f"{key}: expected {expected_parsed[key]!r}, "
                    f"got {new[key]!r} after edit"
                )
        else:
            if _norm(new[key]) != _norm(old[key]):
                return f"{key}: changed unexpectedly ({old[key]!r} → {new[key]!r})"
    return None


# ---------------------------------------------------------------------------
# tenant-api client (read-modify-write a per-tenant governance PR)
# ---------------------------------------------------------------------------
def _auth_headers(args: argparse.Namespace) -> dict[str, str]:
    """Identity headers for the tenant-api call.

    Direct in-cluster mode injects ``X-Forwarded-Email`` / ``X-Forwarded-Groups``
    (the tenant-api RBAC reads these — see rbac/middleware.go); a NetworkPolicy
    must restrict who can reach the API directly with injected identity. An
    oauth2-proxy-fronted deployment instead passes ``--auth-token`` (Bearer) and
    lets the proxy inject identity. Both may be set.
    """
    headers: dict[str, str] = {"X-DA-Write-Source": WRITE_SOURCE}
    if args.identity_email:
        headers["X-Forwarded-Email"] = args.identity_email
    if args.identity_groups:
        headers["X-Forwarded-Groups"] = args.identity_groups
    token = args.auth_token or os.environ.get("DA_GOVERN_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    return headers


def _http_put_yaml(
    url: str, yaml_body: str, headers: dict[str, str], timeout: int
) -> tuple[int, Optional[dict], Optional[str]]:
    """PUT a raw-YAML body. Returns ``(status_code, parsed_json|None, error|None)``.

    The tenant-api PUT commits the body verbatim as conf.d/{id}.yaml, so the body
    is sent as ``application/yaml`` (NOT JSON-wrapped). The status code is surfaced
    so the caller can treat 409 (a pending PR already exists) as a dedup-skip
    rather than an error.
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


# The tenant-api PR-mode "a PR for this tenant is already open" 409 carries this
# code (handler.CodePendingPR). A 409 WITHOUT it (e.g. direct-mode ErrConflict =
# git HEAD moved during the write) is a transient error, NOT a dedup signal — so
# don't silently skip it as "already pending".
_PENDING_PR_CODE = "PENDING_PR_EXISTS"


def _is_pending_pr(body: Any) -> bool:
    """True iff a 409 body is the PR-mode pending-PR signal (vs a git conflict)."""
    if not isinstance(body, dict):
        return False
    return body.get("code") == _PENDING_PR_CODE or body.get("error") == "pending_pr_exists"


def open_governance_pr(
    plan: TenantPlan, args: argparse.Namespace
) -> TenantOutcome:
    """GET → surgical merge → verify → PUT a per-tenant governance PR.

    Maps the tenant-api response: 200 → ``pr_opened``; 409 → ``already_pending``
    (the dedup path — a prior run's PR is still open, leave it); anything else →
    ``error`` (recorded, not raised, so one bad tenant doesn't sink the run).
    """
    keys = [c.key for c in plan.changes]
    base = args.tenant_api_url.rstrip("/")
    enc = urllib.parse.quote(plan.tenant, safe="")
    get_url = f"{base}/api/v1/tenants/{enc}"
    headers = _auth_headers(args)

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
    verify_err = verify_only_changed(raw_yaml, merged, plan.tenant, new_values)
    if verify_err:
        return TenantOutcome(
            plan.tenant, "error", keys,
            message=f"refused to PUT (verify failed): {verify_err}",
        )

    status, body, put_err = _http_put_yaml(get_url, merged, headers, args.timeout)
    if put_err:
        return TenantOutcome(plan.tenant, "error", keys, message=f"PUT failed: {put_err}")
    if status == 200:
        body = body or {}
        # The PR IS the human gate (#656). Only PR write-mode returns
        # status=pending_review + a pr_url; a tenant-api in the DEFAULT "direct"
        # write-mode returns status=ok having committed straight to the base
        # branch — which would silently bypass review AND kill the 409 dedup.
        # Refuse to claim a PR we didn't get, so a non-PR-mode target fails loud
        # on the first run instead of direct-committing tenant config for weeks.
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
    if status == 409 and _is_pending_pr(body):
        body = body or {}
        extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
        return TenantOutcome(
            plan.tenant, "already_pending", keys,
            pr_url=(extra.get("existing_pr_url") or body.get("existing_pr_url") or ""),
            message="a governance PR for this tenant is already open — skipped",
        )
    msg = ""
    if isinstance(body, dict):
        msg = body.get("error") or body.get("message") or json.dumps(body)[:200]
    return TenantOutcome(plan.tenant, "error", keys, message=f"HTTP {status}: {msg}")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def format_text_report(
    plans: list[TenantPlan], outcomes: list[TenantOutcome], applied: bool,
    ungoverned: Optional[list["UngovernedKey"]] = None,
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
            f"⚠ {len(ung)} 個閾值未治理（lower-bound `<`，需人工 review；#656 DETECT `<` 暫緩）："
            if _LANG == "zh" else
            f"⚠ {len(ung)} threshold(s) ungoverned (lower-bound `<`, manual review — "
            "#656 DETECT `<` deferred):"
        )
        return ["", "-" * 78, head] + [f"    {u.tenant} / {u.key}" for u in ung]

    if not plans:
        lines.append(
            "未發現需要治理的閾值（全部在閾值內或樣本不足）。"
            if _LANG == "zh" else
            "No thresholds need governance (all within margin or low-confidence)."
        )
        lines += _ungoverned_lines()
        return "\n".join(lines)

    for plan in plans:
        lines.append(f"\nTenant: {plan.tenant} ({len(plan.changes)} change(s))")
        lines.append(f"  {'Key':<26s} {'Current':>10s} {'→':^3s} {'Recommend':>10s} {'Delta':>9s} {'Conf':<8s}")
        lines.append(f"  {'-' * 26} {'-' * 10} {'-' * 3} {'-' * 10} {'-' * 9} {'-' * 8}")
        for c in plan.changes:
            cur = str(c.current_value)
            rec = recommend._format_threshold_value(c.recommended)
            lines.append(
                f"  {c.key:<26s} {cur:>10s} {'→':^3s} {rec:>10s} "
                f"{c.delta_pct:>+8.1f}% {c.confidence:<8s}"
            )

    if applied and outcomes:
        lines.append("\n" + "-" * 78)
        lines.append("Outcomes:")
        for o in outcomes:
            tag = {
                "pr_opened": "✓ PR",
                "already_pending": "• skip (already open)",
                "error": "✗ error",
            }.get(o.status, o.status)
            detail = o.pr_url or o.message
            lines.append(f"  [{tag}] {o.tenant}: {detail}")

    opened = sum(1 for o in outcomes if o.status == "pr_opened")
    pending = sum(1 for o in outcomes if o.status == "already_pending")
    errors = sum(1 for o in outcomes if o.status == "error")
    lines.append("\n" + "=" * 78)
    if applied:
        lines.append(
            f"Summary: {opened} PR(s) opened, {pending} already-pending (skipped), "
            f"{errors} error(s); {len(plans)} tenant(s) actionable."
        )
    else:
        total_changes = sum(len(p.changes) for p in plans)
        lines.append(
            f"Summary: {len(plans)} tenant(s) / {total_changes} change(s) would get a PR. "
            "Re-run with --apply to open them."
        )
    lines += _ungoverned_lines()
    return "\n".join(lines)


def format_json_report(
    plans: list[TenantPlan], outcomes: list[TenantOutcome], applied: bool,
    ungoverned: Optional[list["UngovernedKey"]] = None,
) -> str:
    ung = ungoverned or []
    out = {
        "tool": "threshold-govern",
        "applied": applied,
        "plans": [asdict(p) for p in plans],
        "outcomes": [asdict(o) for o in outcomes],
        "ungoverned_lower_bound": [asdict(u) for u in ung],
        "summary": {
            "tenants_actionable": len(plans),
            "changes": sum(len(p.changes) for p in plans),
            "prs_opened": sum(1 for o in outcomes if o.status == "pr_opened"),
            "already_pending": sum(1 for o in outcomes if o.status == "already_pending"),
            "errors": sum(1 for o in outcomes if o.status == "error"),
            "ungoverned_lower_bound": len(ung),
        },
    }
    return json.dumps(out, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run(
    args: argparse.Namespace,
) -> tuple[list[TenantPlan], list[TenantOutcome], list[UngovernedKey]]:
    """Run the recommender, gate, and (if --apply) open per-tenant PRs.

    Returns ``(plans, outcomes, ungoverned)``. In dry-run, outcomes is empty (no
    writes). ``ungoverned`` is the lower-bound ``<`` blind spot — always surfaced,
    independent of --apply, since it is informational (no PR is ever opened).
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

    outcomes: list[TenantOutcome] = []
    if not args.apply:
        return plans, outcomes, ungoverned

    opened = 0
    attempted = 0
    consecutive_errors = 0
    for plan in plans:
        if opened >= args.max_prs:
            outcomes.append(TenantOutcome(
                plan.tenant, "skipped", [c.key for c in plan.changes],
                message=f"--max-prs={args.max_prs} reached this run",
            ))
            continue
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            outcomes.append(TenantOutcome(
                plan.tenant, "skipped", [c.key for c in plan.changes],
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
    return plans, outcomes, ungoverned


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
    parser.add_argument("--prometheus",
                        default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
                        help="Prometheus Query API URL")
    parser.add_argument("--tenant", default=None,
                        help="只分析指定租戶" if _LANG == "zh" else "Analyze only this tenant")
    parser.add_argument("--lookback", default="7d",
                        help="回溯期間（預設 7d）" if _LANG == "zh" else "Lookback period (default 7d)")
    parser.add_argument("--min-samples", type=int, default=100,
                        help="最低樣本數門檻（預設 100）" if _LANG == "zh"
                        else "Minimum sample count (default 100)")
    parser.add_argument("--min-delta-pct", type=float, default=DEFAULT_MIN_DELTA_PCT,
                        help=("治理介入門檻：|delta%%| 須 ≥ 此值才開 PR（預設 25）"
                              if _LANG == "zh" else
                              "Governance gate: only open a PR when |delta%%| >= this (default 25)"))
    parser.add_argument("--max-prs", type=int, default=DEFAULT_MAX_PRS,
                        help=("每次最多開幾個 PR（防洪，預設 10）" if _LANG == "zh"
                              else "Max PRs opened per run (anti-flood, default 10)"))
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
    parser.add_argument("--throttle-seconds", type=float, default=DEFAULT_THROTTLE_SECONDS,
                        help="開 PR 之間的間隔秒數（預設 2）" if _LANG == "zh"
                        else "Seconds to pause between opened PRs (default 2)")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="JSON 格式輸出" if _LANG == "zh" else "Output as JSON")
    args = parser.parse_args()

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
        # Below the engine's own no-change margin nothing is exportable anyway —
        # fail loud rather than silently produce an empty plan.
        msg = ("--min-delta-pct 不可低於引擎的 5%% 雜訊邊界" if _LANG == "zh"
               else "--min-delta-pct cannot be below the engine's 5%% noise margin")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if args.apply:
        if not args.tenant_api_url:
            msg = ("--apply 需要 --tenant-api-url" if _LANG == "zh"
                   else "--apply requires --tenant-api-url")
            print(msg, file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)
        if not args.identity_groups and not args.auth_token:
            msg = ("--apply 需要 --identity-groups（直連）或 --auth-token（oauth2-proxy）"
                   if _LANG == "zh" else
                   "--apply requires --identity-groups (direct) or --auth-token (oauth2-proxy)")
            print(msg, file=sys.stderr)
            sys.exit(EXIT_CALLER_ERROR)

    plans, outcomes, ungoverned = run(args)

    if args.json_output:
        print(format_json_report(plans, outcomes, args.apply, ungoverned))
    else:
        print(format_text_report(plans, outcomes, args.apply, ungoverned))


if __name__ == "__main__":
    main()
