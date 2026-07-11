#!/usr/bin/env python3
"""threshold_recommend.py — 閾值推薦引擎。

根據 Prometheus 歷史 metrics 的 P50/P95/P99 百分位數，結合 alert_quality Noise Score，
為每個 tenant 的每個 metric key 產生閾值推薦。

#719 資料源（重要）：本工具查每個閾值 key 在 rule-pack alert 中**實際比對**的
觀測 recording rule（透過 observed-map，`scripts/tools/ops/metric_observed_map.yaml`），
而非 `user_threshold`（那是已設定的閾值值 → 回音室；且 prod label 不符回傳空集合）。
無對映 / 下界(<) / 不支援 scope / needs_review 的 key 一律 fail-loud skip。

領域邊界（Day 0 vs Day N，#719）：
  本工具是 **Day N / 上線後精確微調** —— 查 normalized recording rule（同閾值單位）。
  **Day 0 / 冷啟動粗估**請用 `baseline_discovery.py`（查 raw exporter metric）。
  ⛔ 兩者勿合併（資料源 + 時空背景皆不同）。

Usage:
  # Recommend thresholds for all tenants
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090

  # Recommend for a specific tenant with custom lookback
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090 \
    --tenant db-a --lookback 14d

  # Dry-run: show PromQL queries without executing
  da-tools threshold-recommend --config-dir ./conf.d/ --dry-run

  # JSON output for pipeline integration
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090 --json

用法:
  # 推薦所有租戶閾值
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090

  # 指定租戶與自訂回溯期間
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090 \
    --tenant db-a --lookback 14d

  # 乾跑：僅顯示 PromQL 查詢，不實際執行
  da-tools threshold-recommend --config-dir ./conf.d/ --dry-run

  # JSON 輸出
  da-tools threshold-recommend --config-dir ./conf.d/ --prometheus http://localhost:9090 --json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, '..'))
from _lib_python import (  # noqa: E402
    VALID_RESERVED_KEYS,
    VALID_RESERVED_PREFIXES,
    detect_cli_lang,
    http_get_json,
    load_tenant_configs,
    parse_duration_seconds,
)
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402
import _observed_map_lib as observed_map_lib  # noqa: E402

_LANG = detect_cli_lang()

_HELP = {
    'description': {
        'zh': '閾值推薦引擎 — 基於歷史 P50/P95/P99 數據推薦最佳閾值',
        'en': 'Threshold Recommendation Engine — recommend optimal thresholds from historical P50/P95/P99 data',
    },
    'config_dir': {
        'zh': '租戶配置目錄路徑（conf.d/）',
        'en': 'Path to tenant config directory (conf.d/)',
    },
    'prometheus': {
        'zh': 'Prometheus Query API URL',
        'en': 'Prometheus Query API URL',
    },
    'tenant': {
        'zh': '只分析指定租戶（省略則分析全部）',
        'en': 'Analyze only this tenant (omit for all)',
    },
    'lookback': {
        'zh': '回溯期間（預設 7d；下界/percentile-lower key 建議 14d——需 ≥5 完整 UTC 日，7d 邊際薄）',
        'en': 'Lookback period (default: 7d; use 14d for lower-bound/percentile-lower keys — needs ≥5 full UTC days, 7d is marginal)',
    },
    'min_samples': {
        'zh': '最低樣本數門檻（預設 100）',
        'en': 'Minimum sample count threshold (default: 100)',
    },
    'dry_run': {
        'zh': '僅顯示 PromQL 查詢，不實際執行',
        'en': 'Show PromQL queries without executing',
    },
    'json_flag': {
        'zh': 'JSON 格式輸出',
        'en': 'Output as JSON',
    },
    'markdown': {
        'zh': 'Markdown 格式輸出',
        'en': 'Output as Markdown',
    },
}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"

# Percentile queries via Prometheus quantile_over_time
PERCENTILES = {
    "p50": 0.50,
    "p95": 0.95,
    "p99": 0.99,
}

# Sample count thresholds for confidence grading
SAMPLE_THRESHOLD_HIGH = 1000
SAMPLE_THRESHOLD_MEDIUM = 100

# ---------------------------------------------------------------------------
# #916 Item A — lower-bound (percentile-lower) engine tunables
# ---------------------------------------------------------------------------
# A lower-bound threshold (e.g. a hit-ratio / availability FLOOR in the ratio
# domain (0,1)) recommends a LOW percentile: we want the floor to sit just under
# routine dips so real drops still fire. The engine measures rot in COMPLEMENT
# (miss-rate) space — m = 1 - value — because a floor at 0.95 vs 0.97 is a 40%
# change in tolerated miss-rate, not the ~2% the raw values suggest.
LOWER_PERCENTILE = 0.05          # P5 of the observed floor series
LOWER_MARGIN = 0.10              # |rho-1| below this (tighten side) = no change
LOWER_CLAMP_FRACTION = 0.25      # don't tighten miss-rate below 25% of current
# Estimator-divergence gate: if the pooled-P5's miss-rate is >= K x the
# daily-median-P5's, a recurring trough is hiding in the pooled low tail that the
# daily median smooths over — auto-tightening would false-alarm on it every cycle,
# so defer to a human. K=1.5 is deliberately conservative: missing one genuine
# tighten only DEFERS it (cheap), whereas auto-tightening into a weekly trough is
# a recurring false page (expensive). Owner-tunable.
LOWER_DIVERGENCE_K = 1.5
MIN_POINTS_PER_DAY = 60          # a UTC day with fewer points is not a valid day
MIN_VALID_DAYS = 5               # need this many valid full days for an extreme pct
MIN_TOTAL_SAMPLES = 60           # and this many samples overall
# Stable reason token: formatters append a " miss" suffix to a lower-bound key's
# delta so an operator never misreads the sign/direction as value-space.
_MISS_RATE_MARKER = "miss-rate"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class KeyRecommendation:
    """Recommendation for a single metric key."""

    key: str
    current_value: Any
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    recommended: Optional[float] = None
    delta_pct: Optional[float] = None
    confidence: str = CONFIDENCE_LOW
    sample_count: int = 0
    reason: str = ""
    promql: str = ""
    # #916 Item A (lower-bound engine): a guardrail tripped so this key needs a
    # human (relaxation / out-of-domain / thin sample). recommended stays None;
    # _exportable / is_governance_actionable exclude it; govern surfaces it in a
    # manual-review section keyed on guardrail_reason.
    force_manual: bool = False
    guardrail_reason: str = ""


@dataclass
class TenantRecommendation:
    """Recommendation report for one tenant."""

    tenant: str
    keys: list[KeyRecommendation] = field(default_factory=list)
    total_keys: int = 0
    recommended_changes: int = 0


# ---------------------------------------------------------------------------
# Percentile calculation (pure Python, no numpy)
# ---------------------------------------------------------------------------
def percentile(values: list[float], q: float) -> float:
    """Calculate the q-th percentile of a sorted list (linear interpolation).

    Args:
        values: Sorted list of numeric values (must not be empty).
        q: Percentile as a fraction (0.0 to 1.0).

    Returns:
        Interpolated percentile value.
    """
    n = len(values)
    if n == 0:
        return 0.0
    if n == 1:
        return values[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return values[lo]
    frac = pos - lo
    return values[lo] * (1 - frac) + values[hi] * frac


def compute_percentiles(values: list[float]) -> dict[str, float]:
    """Compute P50, P95, P99 from a list of values.

    Args:
        values: Raw metric values (unsorted, may contain NaN).

    Returns:
        Dict with 'p50', 'p95', 'p99' keys.
    """
    clean = sorted(v for v in values if not math.isnan(v) and not math.isinf(v))
    if not clean:
        return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
    return {
        label: round(percentile(clean, q), 2)
        for label, q in PERCENTILES.items()
    }


# ---------------------------------------------------------------------------
# Confidence grading
# ---------------------------------------------------------------------------
def grade_confidence(sample_count: int, min_samples: int) -> str:
    """Grade confidence based on sample count.

    Args:
        sample_count: Number of data points.
        min_samples: User-configured minimum threshold.

    Returns:
        CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, or CONFIDENCE_LOW.
    """
    if sample_count >= SAMPLE_THRESHOLD_HIGH:
        return CONFIDENCE_HIGH
    if sample_count >= max(min_samples, SAMPLE_THRESHOLD_MEDIUM):
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------
def is_reserved_key(key: str) -> bool:
    """Check if a key is a platform reserved key (not a threshold metric).

    Args:
        key: Configuration key name.

    Returns:
        True if the key is reserved.
    """
    if key in VALID_RESERVED_KEYS:
        return True
    for prefix in VALID_RESERVED_PREFIXES:
        if key.startswith(prefix):
            return True
    return False


def recommend_threshold(
    key: str,
    current_value: Any,
    pcts: dict[str, float],
    sample_count: int,
    min_samples: int,
    noise_grade: Optional[str] = None,
) -> KeyRecommendation:
    """Generate a threshold recommendation for one metric key.

    Strategy:
      - Default: recommend P95 (covers 95% of observed values)
      - If noise_grade == BAD (too noisy): recommend P99 (relax threshold)
      - If noise_grade == GOOD and stale: recommend P95 (tighten threshold)
      - Delta < 5%: no change recommended (within noise margin)

    Args:
        key: Metric key name.
        current_value: Current threshold from config.
        pcts: Dict with p50, p95, p99 values.
        sample_count: Number of data points.
        min_samples: Minimum sample threshold for confidence.
        noise_grade: Optional noise grade from alert_quality.

    Returns:
        KeyRecommendation with recommended value and rationale.
    """
    confidence = grade_confidence(sample_count, min_samples)

    # Try to parse current value as float
    try:
        current_float = float(current_value)
    except (TypeError, ValueError):
        return KeyRecommendation(
            key=key,
            current_value=current_value,
            p50=pcts.get("p50"),
            p95=pcts.get("p95"),
            p99=pcts.get("p99"),
            confidence=confidence,
            sample_count=sample_count,
            reason="non-numeric current value, manual review needed",
        )

    # Select target percentile based on noise grade
    if noise_grade == "BAD":
        target = pcts.get("p99", pcts.get("p95", current_float))
        reason_prefix = "noisy alert (BAD noise grade) → relaxed to P99"
    else:
        target = pcts.get("p95", current_float)
        reason_prefix = "recommended at P95"

    # Round to same precision as current value
    if current_float != 0:
        # Detect integer vs float precision
        if isinstance(current_value, int) or (isinstance(current_value, str) and '.' not in str(current_value)):
            target = round(target)
        else:
            target = round(target, 2)

    # Calculate delta
    if current_float != 0:
        delta_pct = round(((target - current_float) / current_float) * 100, 1)
    else:
        delta_pct = 0.0 if target == 0 else 100.0

    # Skip if delta is within noise margin
    if abs(delta_pct) < 5.0:
        reason = "within 5% margin, no change needed"
    elif confidence == CONFIDENCE_LOW:
        reason = f"{reason_prefix} (low confidence — insufficient samples)"
    else:
        direction = "increase" if delta_pct > 0 else "decrease"
        reason = f"{reason_prefix} ({direction} {abs(delta_pct):.1f}%)"

    return KeyRecommendation(
        key=key,
        current_value=current_value,
        p50=pcts.get("p50"),
        p95=pcts.get("p95"),
        p99=pcts.get("p99"),
        recommended=target,
        delta_pct=delta_pct,
        confidence=confidence,
        sample_count=sample_count,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# #916 Item A — lower-bound (percentile-lower) recommendation engine
# ---------------------------------------------------------------------------
def _median_upper(values: list[float]) -> float:
    """Median with the UPPER-middle element on an even count.

    ``statistics.median`` averages the two middle values; here the estimand is a
    conservative floor and the daily-P5 series can be contaminated DOWNWARD by an
    outage echo, so on a tie we take the higher of the two middles (more robust
    against downward pollution). ``sorted[n // 2]`` is the upper-middle for even n
    and the exact median for odd n.
    """
    s = sorted(values)
    return s[len(s) // 2]


def _force_manual(
    key: str, current_value: Any, why: str, *,
    sample_count: int = 0, delta_pct: Optional[float] = None,
    p05: Optional[float] = None,
) -> "KeyRecommendation":
    """A lower-bound recommendation that a guardrail routed to manual review."""
    return KeyRecommendation(
        key=key, current_value=current_value, recommended=None,
        delta_pct=delta_pct, sample_count=sample_count, p95=p05,
        confidence=grade_confidence(sample_count, SAMPLE_THRESHOLD_MEDIUM),
        reason=f"lower-bound floor → manual review: {why}",
        force_manual=True, guardrail_reason=why,
    )


def recommend_threshold_lower(
    key: str,
    current_value: Any,
    samples_ts: list[tuple[float, float]],
    min_samples: int,
) -> KeyRecommendation:
    """Recommend a lower-bound (floor) threshold from a timestamped P5 series.

    Domain: a ratio-space floor in (0,1) (hit-ratio / availability). Guardrails
    run in order, ALL evaluated against the final 4-dp-floored target:

      guard 0 (domain, first): a non-numeric current (incl. the legal conf.d
        "disable") or a current / candidate outside the open interval (0,1) →
        force_manual (never divide-by-zero on current==1.0).
      estimator (anti-contamination): bucket by COMPLETE UTC day (drop the two
        partial boundary days); a day needs ≥MIN_POINTS_PER_DAY points to count;
        ``daily = median(valid days' daily-P5)`` (robust to a few polluted days)
        and ``pooled = P5(all samples)`` (sees the whole low tail);
        ``candidate = min(daily, pooled)``.
      precision: 4-dp ROUND_FLOOR (floor so rounding never loosens the floor).
      metric: complement (miss-rate) space — m_c=1-current, m_t=1-target,
        rho=m_t/m_c, delta_pct=(rho-1)*100.
      guard order (blocker): relaxation BEFORE margin, else a sub-10% loosen
        slips through as "no change"; divergence AFTER relaxation so a DEEP
        trough is claimed by relaxation and the SHALLOW (above-current) trough by
        divergence:
        1. sample gate → force_manual.
        2. relaxation (rho>1 → target below current) → force_manual, no exemption.
        3. divergence: (1-pooled) >= K*(1-daily) → force_manual (a recurring
           trough the daily median smooths over sits in the pooled low tail;
           auto-tightening would false-alarm on it each cycle). A transient
           outage (<5% of samples) leaves pooled ≈ daily so it does not trip.
        4. clamp: m_t < 0.25*m_c → target = 1 - 0.25*m_c, re-floor.
        5. margin (tighten side only): |rho-1| < 0.10 → recommended=None.
        6. tighten → auto-exportable.
    """
    # guard 0 — non-numeric current (e.g. "disable"): reuse the upper-bound early
    # exit, but as force_manual so it surfaces in the govern manual-review section.
    try:
        current_float = float(current_value)
    except (TypeError, ValueError):
        return _force_manual(
            key, current_value,
            "non-numeric current threshold (e.g. 'disable') — manual review",
        )
    total = len(samples_ts)
    confidence = grade_confidence(total, min_samples)

    # guard 0 — current must be a ratio strictly inside (0,1); current==1.0 would
    # divide by zero in miss-rate space and 95 means the units are wrong.
    if not (0.0 < current_float < 1.0):
        return _force_manual(
            key, current_value,
            "current threshold outside ratio domain (0,1) — check units",
            sample_count=total,
        )

    # estimator — bucket by COMPLETE UTC day.
    by_day: dict[Any, list[float]] = {}
    for ts, val in samples_ts:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        by_day.setdefault(day, []).append(val)
    boundary = {min(by_day), max(by_day)} if by_day else set()
    full_days = [d for d in by_day if d not in boundary]           # partials dropped
    valid_days = [d for d in full_days if len(by_day[d]) >= MIN_POINTS_PER_DAY]

    # guard 1 — sample gate (a distinct message when the LOOKBACK itself is short).
    if len(full_days) < MIN_VALID_DAYS:
        return _force_manual(
            key, current_value, "lookback < 5 full UTC days",
            sample_count=total,
        )
    if len(valid_days) < MIN_VALID_DAYS or total < MIN_TOTAL_SAMPLES:
        return _force_manual(
            key, current_value, "insufficient samples for extreme percentile",
            sample_count=total,
        )

    all_values = [v for _, v in samples_ts]
    daily = _median_upper([percentile(sorted(by_day[d]), LOWER_PERCENTILE)
                           for d in valid_days])
    pooled = percentile(sorted(all_values), LOWER_PERCENTILE)
    candidate = min(daily, pooled)

    # guard 0 (candidate) — a floor recommendation must itself stay in (0,1].
    if not (0.0 < candidate <= 1.0):
        return _force_manual(
            key, current_value,
            "computed floor outside ratio domain (0,1] — check units",
            sample_count=total, p05=round(candidate, 4),
        )

    def _floor4(x: float) -> float:
        return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_FLOOR))

    target = _floor4(candidate)
    m_c = 1.0 - current_float
    m_t = 1.0 - target
    rho = m_t / m_c
    delta_pct = round((rho - 1.0) * 100.0, 1)

    # guard 2 — relaxation (lowering the floor) has NO magnitude exemption: a
    # sub-10% loosen would otherwise ride the govern loop into an auto floor-drop.
    if rho > 1.0:
        return _force_manual(
            key, current_value,
            "would relax the floor (lower it) — miss-rate rises; manual review",
            sample_count=total, delta_pct=delta_pct, p05=target,
        )

    # guard 2.5 — estimator DIVERGENCE (real, not just an min()): the pooled-P5's
    # miss-rate materially exceeds the daily-median-P5's, i.e. a recurring trough
    # the daily median smooths over is hiding in the pooled low tail. Here
    # candidate>current (tighten side — a DEEP trough already fell to relaxation
    # above), so without this gate the engine would auto-tighten to a floor the
    # trough breaches every cycle (a weekly false page). Compared against `daily`
    # (NOT current), so it fires on the SHALLOW above-current trough the min()
    # alone silently tightened. A transient outage (<5% of samples) leaves
    # pooled ≈ daily → ratio ≈ 1 < K → does not trip.
    if (1.0 - pooled) >= LOWER_DIVERGENCE_K * (1.0 - daily):
        return _force_manual(
            key, current_value,
            "daily-P5 and pooled-P5 diverge (recurring trough) — manual review",
            sample_count=total, delta_pct=delta_pct, p05=target,
        )

    # guard 3 — clamp: never tighten tolerated miss-rate below 25% of current
    # (a P5 that collapses to ~0 miss usually means a quiet week, not a safe
    # floor at ~1.0). Re-floor because 1 - 0.25*m_c need not be 4-dp exact.
    if m_t < LOWER_CLAMP_FRACTION * m_c:
        target = _floor4(1.0 - LOWER_CLAMP_FRACTION * m_c)
        m_t = 1.0 - target
        rho = m_t / m_c
        delta_pct = round((rho - 1.0) * 100.0, 1)

    # guard 4 — within-margin (tighten side): recommended=None so _exportable
    # rejects it structurally (a reason string alone would still export).
    if abs(rho - 1.0) < LOWER_MARGIN:
        return KeyRecommendation(
            key=key, current_value=current_value, recommended=None,
            delta_pct=delta_pct, sample_count=total, p95=target,
            confidence=confidence,
            reason=f"within {int(LOWER_MARGIN*100)}% {_MISS_RATE_MARKER} margin, no change needed",
        )

    # guard 5 — tighten (raise the floor): auto-exportable.
    return KeyRecommendation(
        key=key, current_value=current_value, recommended=target,
        delta_pct=delta_pct, sample_count=total, p95=target,
        confidence=confidence,
        reason=(f"lower-bound P5 floor → tighten (raise floor); "
                f"{_MISS_RATE_MARKER} {delta_pct:+.1f}%"),
    )


# ---------------------------------------------------------------------------
# Prometheus query helpers
# ---------------------------------------------------------------------------
def build_metric_query(observed_series: str, tenant: str, lookback: str) -> str:
    """Build a PromQL range query for an OBSERVED-workload recording rule.

    #719: recommendations must come from the observed workload series that the
    threshold is actually compared against in the rule-pack alert (e.g.
    ``tenant:mysql_threads_connected:max``), NOT from ``user_threshold`` — that
    metric is the operator-CONFIGURED threshold value (collector.go: a gauge of
    ``ResolvedThreshold.Value``), so querying it gave an echo chamber
    (recommending future settings from past settings). It was also broken in
    prod: ``user_threshold`` has no ``key`` label (its labels are
    ``{tenant, metric, component, severity}``), so ``{key="..."}`` matched
    nothing and every key returned "no data points".

    The observed recording rule is, by construction, in the same unit/topology as
    the threshold (the alert compares them directly), so P95(observed) is a
    directly-usable threshold value — no unit conversion needed.

    Args:
        observed_series: Observed recording-rule series name (from the
            #719 observed-map), e.g. ``tenant:mysql_threads_connected:max``.
        tenant: Tenant identifier.
        lookback: Lookback duration (e.g., '7d').

    Returns:
        PromQL range-query string.
    """
    # Escape the tenant for a PromQL string label value (backslash first, then
    # double-quote) so a tenant id containing " or \ can't break out of the
    # selector or produce invalid PromQL.
    safe_tenant = tenant.replace("\\", "\\\\").replace('"', '\\"')
    return f'{observed_series}{{tenant="{safe_tenant}"}}[{lookback}]'


def query_prometheus_range(
    prometheus_url: str,
    promql: str,
    *,
    timeout: int = 30,
) -> tuple[list[float], Optional[str]]:
    """Execute a Prometheus instant query and extract sample values.

    For range vectors, Prometheus returns matrix results with arrays of
    [timestamp, value] pairs.

    Args:
        prometheus_url: Base Prometheus URL.
        promql: PromQL query string.
        timeout: HTTP timeout.

    Returns:
        (values_list, error_or_none)
    """
    url = f"{prometheus_url}/api/v1/query"
    params = urllib.parse.urlencode({"query": promql})
    full_url = f"{url}?{params}"

    data, err = http_get_json(full_url, timeout=timeout)
    if err:
        return [], err

    if data.get("status") != "success":
        return [], data.get("error", "Unknown Prometheus error")

    results = data.get("data", {}).get("result", [])
    values: list[float] = []

    for series in results:
        # Range vector → "values" array of [ts, val]
        for point in series.get("values", []):
            try:
                values.append(float(point[1]))
            except (IndexError, ValueError, TypeError):
                continue
        # Instant vector → single "value" [ts, val]
        val = series.get("value")
        if val and isinstance(val, list) and len(val) >= 2:
            try:
                values.append(float(val[1]))
            except (ValueError, TypeError):
                pass

    return values, None


def query_prometheus_range_ts(
    prometheus_url: str,
    promql: str,
    *,
    timeout: int = 30,
) -> tuple[list[tuple[float, float]], Optional[str]]:
    """Like ``query_prometheus_range`` but keeps the sample timestamps.

    The lower-bound engine (#916) buckets samples by UTC day to compute a
    daily-P5, so it needs ``(ts, value)`` pairs; the upper-bound path discards
    the timestamp and is left unchanged. Returns ``(pairs, error_or_none)``.

    NaN/Inf are filtered (parity with ``compute_percentiles``): a hit-ratio
    recording rule can emit ``NaN`` while idle (0/0), and an unfiltered NaN would
    sort to an arbitrary index and yield a silently-wrong percentile. A filtered
    series that ends up too thin then hits the engine's own sample gate.
    """
    url = f"{prometheus_url}/api/v1/query"
    params = urllib.parse.urlencode({"query": promql})
    full_url = f"{url}?{params}"

    data, err = http_get_json(full_url, timeout=timeout)
    if err:
        return [], err
    if data.get("status") != "success":
        return [], data.get("error", "Unknown Prometheus error")

    def _finite(ts_raw, v_raw) -> Optional[tuple[float, float]]:
        try:
            ts, v = float(ts_raw), float(v_raw)
        except (ValueError, TypeError):
            return None
        if math.isnan(v) or math.isinf(v):
            return None
        return (ts, v)

    results = data.get("data", {}).get("result", [])
    pairs: list[tuple[float, float]] = []
    for series in results:
        for point in series.get("values", []):
            try:
                p = _finite(point[0], point[1])
            except IndexError:
                continue
            if p is not None:
                pairs.append(p)
        val = series.get("value")
        if val and isinstance(val, list) and len(val) >= 2:
            p = _finite(val[0], val[1])
            if p is not None:
                pairs.append(p)
    return pairs, None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def analyze_tenant(
    tenant_name: str,
    tenant_config: dict[str, Any],
    *,
    prometheus_url: Optional[str] = None,
    lookback: str = "7d",
    min_samples: int = 100,
    dry_run: bool = False,
    observed_map: Optional[dict[str, Any]] = None,
) -> TenantRecommendation:
    """Analyze one tenant and generate threshold recommendations.

    #719: each threshold key is mapped (via the observed-map) to the OBSERVED
    workload recording rule the alert compares it against. Keys that are
    unmapped / lower-bound / unsupported-scope / needs-review are SKIPPED with an
    explicit reason (fail-loud) rather than producing a bogus or echo-chamber
    recommendation.

    Args:
        tenant_name: Tenant identifier.
        tenant_config: Tenant config dict from YAML.
        prometheus_url: Prometheus base URL (required unless dry_run).
        lookback: Lookback period for historical data.
        min_samples: Minimum sample count for confidence.
        dry_run: Only generate PromQL queries, don't execute.
        observed_map: conf.d-key -> observed-series map (loads default if None).

    Returns:
        TenantRecommendation with per-key results.
    """
    if observed_map is None:
        observed_map = observed_map_lib.load_observed_map()

    report = TenantRecommendation(tenant=tenant_name)

    # Extract metric keys (skip reserved keys)
    metric_keys = {
        k: v for k, v in tenant_config.items()
        if not is_reserved_key(k)
    }

    report.total_keys = len(metric_keys)

    for key, current_value in sorted(metric_keys.items()):
        # #719: resolve the observed-workload series this threshold is compared
        # against. fail-loud skip when there is no usable mapping.
        entry = observed_map.get(key)
        if entry is None:
            report.keys.append(KeyRecommendation(
                key=key,
                current_value=current_value,
                reason="no observed-load mapping for this key — not in observed-map (skipped)",
            ))
            continue
        observed_series, skip_reason = observed_map_lib.resolve_observed(entry)
        if skip_reason:
            report.keys.append(KeyRecommendation(
                key=key,
                current_value=current_value,
                reason=f"skipped: {skip_reason}",
            ))
            continue

        promql = build_metric_query(observed_series, tenant_name, lookback)

        if dry_run:
            rec = KeyRecommendation(
                key=key,
                current_value=current_value,
                reason="dry-run: query not executed",
                promql=promql,
            )
            report.keys.append(rec)
            continue

        # #916 Item A: route by comparison direction. A per-key try/except keeps
        # one tenant's bad value (e.g. a Decimal/parse blow-up) from sinking the
        # whole run — that key degrades to force_manual, the rest continue.
        direction = entry.get("direction")
        try:
            if direction == "<":
                # Lower-bound floor path: timestamped samples → daily-bucket P5
                # engine (never the upper-bound percentile logic).
                pairs, err = query_prometheus_range_ts(prometheus_url, promql)
                if err:
                    report.keys.append(KeyRecommendation(
                        key=key, current_value=current_value,
                        reason=f"query error: {err[:60]}", promql=promql,
                    ))
                    continue
                if not pairs:
                    report.keys.append(KeyRecommendation(
                        key=key, current_value=current_value,
                        confidence=CONFIDENCE_LOW, sample_count=0,
                        reason="no data points found", promql=promql,
                    ))
                    continue
                rec = recommend_threshold_lower(
                    key, current_value, pairs, min_samples
                )
            else:
                # Upper-bound path (unchanged).
                values, err = query_prometheus_range(prometheus_url, promql)
                if err:
                    report.keys.append(KeyRecommendation(
                        key=key, current_value=current_value,
                        reason=f"query error: {err[:60]}", promql=promql,
                    ))
                    continue
                if not values:
                    report.keys.append(KeyRecommendation(
                        key=key, current_value=current_value,
                        confidence=CONFIDENCE_LOW, sample_count=0,
                        reason="no data points found", promql=promql,
                    ))
                    continue
                pcts = compute_percentiles(values)
                rec = recommend_threshold(
                    key=key, current_value=current_value, pcts=pcts,
                    sample_count=len(values), min_samples=min_samples,
                )
        except Exception as exc:  # noqa: BLE001 — one bad value must not sink the run
            # Reason phrasing follows the key's own direction (an upper-bound key
            # blowing up must not be mislabelled "lower-bound floor").
            what = "lower-bound floor" if direction == "<" else "recommendation"
            report.keys.append(KeyRecommendation(
                key=key, current_value=current_value,
                reason=f"{what} → manual review: {str(exc)[:80]}",
                force_manual=True, guardrail_reason=str(exc)[:100], promql=promql,
            ))
            continue

        rec.promql = promql

        # An actionable recommendation (upper or lower) counts as a change; a
        # within-margin / force_manual / skipped key does not (_exportable is the
        # single source of truth — a lower within-margin has recommended=None).
        if _exportable(rec):
            report.recommended_changes += 1

        report.keys.append(rec)

    return report


def run_analysis(
    config_dir: str,
    *,
    prometheus_url: Optional[str] = None,
    tenant_filter: Optional[str] = None,
    lookback: str = "7d",
    min_samples: int = 100,
    dry_run: bool = False,
) -> list[TenantRecommendation]:
    """Run threshold analysis for all (or filtered) tenants.

    Args:
        config_dir: Path to tenant config directory.
        prometheus_url: Prometheus base URL.
        tenant_filter: If set, only analyze this tenant.
        lookback: Lookback period.
        min_samples: Minimum sample threshold.
        dry_run: Only generate queries.

    Returns:
        List of TenantRecommendation.
    """
    all_configs = load_tenant_configs(config_dir)

    if tenant_filter:
        if tenant_filter not in all_configs:
            return []
        all_configs = {tenant_filter: all_configs[tenant_filter]}

    # Load the observed-map once and share across tenants (#719).
    observed_map = observed_map_lib.load_observed_map()

    reports: list[TenantRecommendation] = []
    for tenant_name in sorted(all_configs):
        report = analyze_tenant(
            tenant_name,
            all_configs[tenant_name],
            prometheus_url=prometheus_url,
            lookback=lookback,
            min_samples=min_samples,
            dry_run=dry_run,
            observed_map=observed_map,
        )
        if report.keys:
            reports.append(report)

    return reports


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def format_text_report(reports: list[TenantRecommendation]) -> str:
    """Format reports as human-readable text table."""
    if not reports:
        msg = "未發現可分析的租戶 metric key。" if _LANG == 'zh' else "No analyzable tenant metric keys found."
        return msg

    lines: list[str] = []
    for report in reports:
        lines.append(f"\nTenant: {report.tenant} ({report.recommended_changes}/{report.total_keys} changes recommended)")
        lines.append(f"{'─' * 90}")
        header = f"  {'Key':<22s} {'Current':>8s} {'P95':>8s} {'Recommend':>10s} {'Delta':>8s} {'Confidence':<10s}"
        lines.append(header)
        lines.append(f"  {'─' * 22} {'─' * 8} {'─' * 8} {'─' * 10} {'─' * 8} {'─' * 10}")

        for r in report.keys:
            current = str(r.current_value) if r.current_value is not None else "—"
            p95 = f"{r.p95:.1f}" if r.p95 is not None else "—"
            rec = f"{r.recommended}" if r.recommended is not None else "—"
            delta = f"{r.delta_pct:+.1f}%" if r.delta_pct is not None else "—"
            lines.append(
                f"  {r.key:<22s} {current:>8s} {p95:>8s} {rec:>10s} {delta:>8s} {r.confidence:<10s}"
            )
            if r.reason and ("no change" not in r.reason):
                lines.append(f"    └─ {r.reason}")

    total_changes = sum(r.recommended_changes for r in reports)
    total_keys = sum(r.total_keys for r in reports)
    lines.append(f"\n{'=' * 90}")
    lines.append(f"  Total: {total_changes}/{total_keys} keys with recommended changes")
    lines.append(f"{'=' * 90}")

    return "\n".join(lines)


def format_json_report(reports: list[TenantRecommendation]) -> str:
    """Format reports as JSON string."""
    output = {
        "tool": "threshold-recommend",
        "tenants": [asdict(r) for r in reports],
        "summary": {
            "total_tenants": len(reports),
            "total_keys": sum(r.total_keys for r in reports),
            "recommended_changes": sum(r.recommended_changes for r in reports),
        },
    }
    return json.dumps(output, indent=2, ensure_ascii=False)


def format_markdown_report(reports: list[TenantRecommendation]) -> str:
    """Format reports as Markdown table."""
    if not reports:
        return "No recommendations generated.\n"

    lines: list[str] = ["# Threshold Recommendations\n"]
    for report in reports:
        lines.append(f"## Tenant: {report.tenant}\n")
        lines.append(f"| Key | Current | P95 | Recommend | Delta | Confidence | Reason |")
        lines.append(f"|-----|---------|-----|-----------|-------|------------|--------|")
        for r in report.keys:
            current = str(r.current_value) if r.current_value is not None else "—"
            p95 = f"{r.p95:.1f}" if r.p95 is not None else "—"
            rec = f"{r.recommended}" if r.recommended is not None else "—"
            delta = f"{r.delta_pct:+.1f}%" if r.delta_pct is not None else "—"
            lines.append(f"| {r.key} | {current} | {p95} | {rec} | {delta} | {r.confidence} | {r.reason} |")
        lines.append("")

    return "\n".join(lines)


def _format_threshold_value(value: float) -> str:
    """Render a recommended threshold as a conf.d string value.

    conf.d values are quoted strings (e.g. ``"70"``). Whole numbers render
    without a decimal point so the patch matches the prevailing integer style;
    fractional values keep up to 2 dp (recommend_threshold already rounded).
    """
    if value == int(value):
        return str(int(value))
    return f"{value:g}"


def _delta_str(r: KeyRecommendation) -> str:
    """Render a recommendation's delta, tagging miss-rate space for lower-bound.

    A lower-bound key's delta is a MISS-RATE change (complement space), so a bare
    ``+7.0%`` reads backwards to an operator expecting value space; the `` miss``
    suffix makes the axis explicit. The lower engine stamps ``miss-rate`` into the
    reason, which is the stable signal formatters key off.
    """
    if r.delta_pct is None:
        return "?"
    s = f"{r.delta_pct:+.1f}%"
    if _MISS_RATE_MARKER in (r.reason or ""):
        s += " miss"
    return s


def _skip_comment_body(r: KeyRecommendation) -> tuple[str, str]:
    """(label, detail) for a non-exportable key's transparency comment.

    A ``force_manual`` key (a lower-bound guardrail tripped — relaxation / thin
    sample / out-of-domain) is labelled ``manual`` and carries its
    ``guardrail_reason`` so a floor rotting toward relaxation is never invisible;
    every other non-exportable key stays ``skipped``.
    """
    if r.force_manual:
        detail = r.guardrail_reason or r.reason or "manual review required"
    else:
        detail = r.reason or "no recommendation"
    # Collapse embedded newlines/whitespace before this goes into a `#`-prefixed
    # export-patch comment line: guardrail_reason can be str(exc)[:100] and some
    # exception messages span lines — a raw \n would break out of the comment and
    # corrupt the exported --export-patch YAML fragment when an operator applies it.
    detail = " ".join(detail.split())
    return ("manual" if r.force_manual else "skipped"), detail


def _exportable(r: KeyRecommendation) -> bool:
    """True iff this key carries an actionable, applyable recommendation.

    #720 STAGE-1 fail-loud: only emit a patch line for keys with a real
    recommendation — i.e. a numeric ``recommended`` AND a delta past the 5%
    noise margin. Keys that were skipped (unmapped / lower-bound N/A / unsupported
    scope / no data — ``recommended is None``), within-margin (no change needed),
    or routed to manual review by a lower-bound guardrail (#916 ``force_manual``)
    are intentionally NOT patched, mirroring the analyze_tenant skip.
    """
    return (
        r.recommended is not None
        and not r.force_manual
        and r.delta_pct is not None
        and abs(r.delta_pct) >= 5.0
    )


def format_export_patch(reports: list[TenantRecommendation]) -> str:
    """Format reports as an applyable conf.d override fragment (#720 STAGE-1).

    Emits a ``tenants:``-rooted YAML block carrying ONLY the keys with an
    actionable recommendation (see ``_exportable``). The operator reviews it,
    merges/applies it into the matching ``conf.d/<tenant>.yaml``, and opens a
    PR — at which point the existing ``backtest.yaml`` CI posts the
    old-vs-new firing-count risk report (the STAGE-1 value basis). Skipped /
    within-margin keys are listed as comments so the output is self-explaining
    without re-running in another mode.

    T1 (advisory fragment, 0 new deps): the operator applies it; the tool does
    NOT edit conf.d in place (that heavier ruamel round-trip is deferred —
    #457 R0 §5 / #721).
    """
    exportable = [
        (rep, [r for r in rep.keys if _exportable(r)]) for rep in reports
    ]
    total = sum(len(ks) for _, ks in exportable)

    lines: list[str] = [
        "# threshold-recommend --export-patch (#720 STAGE-1)",
        "# Review, then merge each tenant block into the matching conf.d/<tenant>.yaml",
        "# and open a PR — backtest.yaml CI will post the old-vs-new firing-count risk report.",
        "# Only keys with an actionable recommendation (|delta| >= 5%, mapped, upper-bound",
        "# or an opted-in percentile-lower floor) appear; lower-bound deltas are miss-rate.",
    ]
    if total == 0:
        lines.append("# (no actionable recommendations)")
        # Still surface why every key was skipped — the transparency this mode
        # documents must not vanish just because nothing is actionable. These
        # are top-level comments (no `tenants:` block, so the output stays an
        # empty/None YAML doc that applies to nothing).
        for rep in reports:
            for r in sorted(rep.keys, key=lambda x: x.key):
                label, detail = _skip_comment_body(r)
                lines.append(f"# [{rep.tenant}] ({label}) {r.key}: {detail}")
        return "\n".join(lines) + "\n"

    lines.append("tenants:")
    for rep, ks in exportable:
        skipped = sorted(
            (r for r in rep.keys if not _exportable(r)), key=lambda x: x.key
        )
        if not ks:
            # tenant has only non-actionable keys → no YAML block, but keep the
            # per-key skip context as top-level comments (don't drop it).
            for r in skipped:
                label, detail = _skip_comment_body(r)
                lines.append(f"# [{rep.tenant}] ({label}) {r.key}: {detail}")
            continue
        lines.append(f"  {rep.tenant}:")
        for r in sorted(ks, key=lambda x: x.key):
            val = _format_threshold_value(r.recommended)
            # `cur` goes only into the trailing `#` comment. Safe to interpolate
            # raw: an exportable key has recommended != None, i.e.
            # recommend_threshold() float-parsed current_value, so it cannot
            # contain a newline that would break this single-line comment.
            # (Do NOT copy current_value into the skipped-key comments below —
            # skipped keys may carry an un-float-parseable / multiline value;
            # those comments use only the tool-generated reason string.)
            cur = r.current_value if r.current_value is not None else "?"
            delta = _delta_str(r)
            lines.append(
                f'    {r.key}: "{val}"   # was {cur}, {delta}, {r.confidence} — {r.reason}'
            )
        # surface this tenant's skipped keys as in-block comments
        for r in skipped:
            label, detail = _skip_comment_body(r)
            lines.append(f"    # ({label}) {r.key}: {detail}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """CLI entry point: threshold recommendation engine."""
    parser = argparse.ArgumentParser(
        description=_HELP['description'][_LANG],
    )
    parser.add_argument(
        "--config-dir",
        required=False,
        default=None,
        help=_HELP['config_dir'][_LANG],
    )
    parser.add_argument(
        "--generate-observed-map",
        action="store_true",
        help=(
            "重新從 rule-packs/*.yaml 產生 observed-map 草稿（#719）；"
            "merge 既有 map 保留人工 resolve 的 observed_series（#916）；"
            "needs_review 項目須人工 resolve" if _LANG == 'zh' else
            "Regenerate the observed-map draft from rule-packs/*.yaml (#719); "
            "merges over the existing map to preserve human-resolved observed_series "
            "(#916); needs_review entries require manual resolution"
        ),
    )
    parser.add_argument(
        "--prometheus",
        default=os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        help=_HELP['prometheus'][_LANG],
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help=_HELP['tenant'][_LANG],
    )
    parser.add_argument(
        "--lookback",
        default="7d",
        help=_HELP['lookback'][_LANG],
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=100,
        help=_HELP['min_samples'][_LANG],
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=_HELP['dry_run'][_LANG],
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help=_HELP['json_flag'][_LANG],
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help=_HELP['markdown'][_LANG],
    )
    parser.add_argument(
        "--export-patch",
        action="store_true",
        dest="export_patch",
        help=(
            "輸出可套用的 conf.d override 片段（#720 STAGE-1）；"
            "只含有實際建議的 key，operator 套用後自開 PR" if _LANG == 'zh' else
            "Output an applyable conf.d override fragment (#720 STAGE-1); "
            "only keys with an actionable recommendation, operator applies + opens a PR"
        ),
    )
    args = parser.parse_args()

    # #719: regenerate the observed-map and exit (does not need --config-dir).
    if args.generate_observed_map:
        summary = observed_map_lib.write_observed_map()
        if _LANG == 'zh':
            print(f"已產生 observed-map: {summary['path']}")
            print(f"  共 {summary['total']} 個 key："
                  f"{summary['clean']} clean / {summary['needs_review']} needs_review")
            print(f"  merge：preserved {summary.get('preserved', 0)} / "
                  f"demoted {summary.get('demoted', 0)} / dropped {summary.get('dropped', 0)}"
                  "（人工 resolve 保留 / 失效降級 / 已移除；細節見 stderr WARN）")
            print("  needs_review 項目須人工 resolve（挑 observed_series / 確認方向）後才會被推薦使用。")
        else:
            print(f"Wrote observed-map: {summary['path']}")
            print(f"  {summary['total']} keys: "
                  f"{summary['clean']} clean / {summary['needs_review']} needs_review")
            print(f"  merge: preserved {summary.get('preserved', 0)} / "
                  f"demoted {summary.get('demoted', 0)} / dropped {summary.get('dropped', 0)} "
                  "(see stderr WARN for details)")
            print("  needs_review entries require manual resolution before use.")
        return

    if not args.config_dir:
        msg = ("缺少 --config-dir（或用 --generate-observed-map）"
               if _LANG == 'zh' else
               "--config-dir is required (or use --generate-observed-map)")
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    if not Path(args.config_dir).is_dir():
        msg = f"配置目錄不存在: {args.config_dir}" if _LANG == 'zh' else f"Config directory not found: {args.config_dir}"
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    # Validate lookback
    lookback_secs = parse_duration_seconds(args.lookback)
    if lookback_secs is None:
        msg = f"無效的 lookback 值: {args.lookback}" if _LANG == 'zh' else f"Invalid lookback value: {args.lookback}"
        print(msg, file=sys.stderr)
        sys.exit(EXIT_CALLER_ERROR)

    reports = run_analysis(
        args.config_dir,
        prometheus_url=args.prometheus,
        tenant_filter=args.tenant,
        lookback=args.lookback,
        min_samples=args.min_samples,
        dry_run=args.dry_run,
    )

    if args.export_patch:
        print(format_export_patch(reports))
    elif args.json_output:
        print(format_json_report(reports))
    elif args.markdown:
        print(format_markdown_report(reports))
    else:
        print(format_text_report(reports))


if __name__ == "__main__":
    main()
