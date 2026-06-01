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
        'zh': '回溯期間（預設 7d）',
        'en': 'Lookback period (default: 7d)',
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

        # Query Prometheus
        values, err = query_prometheus_range(prometheus_url, promql)

        if err:
            rec = KeyRecommendation(
                key=key,
                current_value=current_value,
                reason=f"query error: {err[:60]}",
                promql=promql,
            )
            report.keys.append(rec)
            continue

        if not values:
            rec = KeyRecommendation(
                key=key,
                current_value=current_value,
                confidence=CONFIDENCE_LOW,
                sample_count=0,
                reason="no data points found",
                promql=promql,
            )
            report.keys.append(rec)
            continue

        # Compute percentiles
        pcts = compute_percentiles(values)

        # Generate recommendation
        rec = recommend_threshold(
            key=key,
            current_value=current_value,
            pcts=pcts,
            sample_count=len(values),
            min_samples=min_samples,
        )
        rec.promql = promql

        if rec.delta_pct is not None and abs(rec.delta_pct) >= 5.0:
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
            "needs_review 項目須人工 resolve" if _LANG == 'zh' else
            "Regenerate the observed-map draft from rule-packs/*.yaml (#719); "
            "needs_review entries require manual resolution"
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
    args = parser.parse_args()

    # #719: regenerate the observed-map and exit (does not need --config-dir).
    if args.generate_observed_map:
        summary = observed_map_lib.write_observed_map()
        if _LANG == 'zh':
            print(f"已產生 observed-map: {summary['path']}")
            print(f"  共 {summary['total']} 個 key："
                  f"{summary['clean']} clean / {summary['needs_review']} needs_review")
            print("  needs_review 項目須人工 resolve（挑 observed_series / 確認方向）後才會被推薦使用。")
        else:
            print(f"Wrote observed-map: {summary['path']}")
            print(f"  {summary['total']} keys: "
                  f"{summary['clean']} clean / {summary['needs_review']} needs_review")
            print("  needs_review entries require manual resolution before use.")
        return

    if not args.config_dir:
        msg = ("缺少 --config-dir（或用 --generate-observed-map）"
               if _LANG == 'zh' else
               "--config-dir is required (or use --generate-observed-map)")
        print(msg, file=sys.stderr)
        sys.exit(1)

    if not Path(args.config_dir).is_dir():
        msg = f"配置目錄不存在: {args.config_dir}" if _LANG == 'zh' else f"Config directory not found: {args.config_dir}"
        print(msg, file=sys.stderr)
        sys.exit(1)

    # Validate lookback
    lookback_secs = parse_duration_seconds(args.lookback)
    if lookback_secs is None:
        msg = f"無效的 lookback 值: {args.lookback}" if _LANG == 'zh' else f"Invalid lookback value: {args.lookback}"
        print(msg, file=sys.stderr)
        sys.exit(1)

    reports = run_analysis(
        args.config_dir,
        prometheus_url=args.prometheus,
        tenant_filter=args.tenant,
        lookback=args.lookback,
        min_samples=args.min_samples,
        dry_run=args.dry_run,
    )

    if args.json_output:
        print(format_json_report(reports))
    elif args.markdown:
        print(format_markdown_report(reports))
    else:
        print(format_text_report(reports))


if __name__ == "__main__":
    main()
