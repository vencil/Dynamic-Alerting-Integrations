#!/usr/bin/env python3
"""
cardinality_forecasting.py — 基數預測工具（§5.8）。

基於 Prometheus 時序資料（``scrape_series_added``、``tenant_threshold_*``），
使用線性回歸預測 per-tenant 基數增長趨勢。在觸頂前 N 天發出預警。

主要功能：
  - 查詢 Prometheus 取得 per-tenant 基數時序
  - 線性回歸擬合成長趨勢
  - 預測觸頂日期（預設上限 500）
  - 文字報告 / JSON / Markdown 輸出
  - CI gate（`--ci` + `--warn-days`）

用法：
  da-tools cardinality-forecast --prometheus http://prometheus:9090
  da-tools cardinality-forecast --prometheus http://prometheus:9090 --json
  da-tools cardinality-forecast --prometheus http://prometheus:9090 --ci --warn-days 7
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Repo-layout import compatibility (stripped in Docker build)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from _lib_python import (
        detect_cli_lang,
        http_get_json,
        parse_duration_seconds,
    )
except ImportError:
    from scripts.tools._lib_python import (  # type: ignore[no-redef]
        detect_cli_lang,
        http_get_json,
        parse_duration_seconds,
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_CARDINALITY_LIMIT = 500
DEFAULT_WARN_DAYS = 7
DEFAULT_LOOKBACK = "30d"
DEFAULT_STEP = "1h"
SECONDS_PER_DAY = 86400


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class TenantForecast:
    """單一 tenant 的基數預測結果。"""
    tenant: str
    current_cardinality: int
    cardinality_limit: int
    slope_per_day: float
    intercept: float
    r_squared: float
    days_to_limit: Optional[float]
    predicted_date: Optional[str]
    trend: str  # "growing", "stable", "declining"
    risk_level: str  # "critical", "warning", "safe"
    data_points: int


@dataclass
class ForecastReport:
    """整體預測報告。"""
    tenants: list[TenantForecast] = field(default_factory=list)
    generated_at: str = ""
    lookback_days: int = 30
    cardinality_limit: int = DEFAULT_CARDINALITY_LIMIT
    warn_days: int = DEFAULT_WARN_DAYS

    @property
    def critical_count(self) -> int:
        return sum(1 for t in self.tenants if t.risk_level == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for t in self.tenants if t.risk_level == "warning")

    @property
    def safe_count(self) -> int:
        return sum(1 for t in self.tenants if t.risk_level == "safe")


# ---------------------------------------------------------------------------
# Linear regression (pure Python — no numpy dependency)
# ---------------------------------------------------------------------------
def linear_regression(
    xs: list[float], ys: list[float]
) -> tuple[float, float, float]:
    """簡單線性回歸 y = slope * x + intercept。

    Args:
        xs: x 值清單（時間戳或天數）。
        ys: y 值清單（基數）。

    Returns:
        (slope, intercept, r_squared)。
    """
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0

    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)

    denom = n * sum_x2 - sum_x * sum_x
    if abs(denom) < 1e-10:
        return 0.0, sum_y / n, 0.0

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    # R-squared
    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))

    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return slope, intercept, r_squared


# ---------------------------------------------------------------------------
# Trend & risk classification
# ---------------------------------------------------------------------------
def classify_trend(slope_per_day: float) -> str:
    """分類基數成長趨勢。"""
    if slope_per_day > 0.5:
        return "growing"
    elif slope_per_day < -0.5:
        return "declining"
    return "stable"


def classify_risk(
    current: int,
    days_to_limit: Optional[float],
    warn_days: int,
    limit: int,
) -> str:
    """分類風險等級。

    - critical: 已超限 或 預計在 warn_days 內觸頂
    - warning: 預計在 warn_days*3 內觸頂 或 已達 80% 容量
    - safe: 其他
    """
    if current >= limit:
        return "critical"
    if days_to_limit is not None and days_to_limit <= warn_days:
        return "critical"
    if days_to_limit is not None and days_to_limit <= warn_days * 3:
        return "warning"
    if current >= limit * 0.8:
        return "warning"
    return "safe"


def compute_days_to_limit(
    current: float, slope_per_day: float, limit: int
) -> Optional[float]:
    """計算觸頂剩餘天數。

    Returns:
        天數（正值），或 None（slope <= 0 或已超限）。
    """
    if current >= limit:
        return 0.0
    if slope_per_day <= 0:
        return None  # Not growing
    remaining = limit - current
    days = remaining / slope_per_day
    return round(days, 1)


# ---------------------------------------------------------------------------
# Prometheus queries
# ---------------------------------------------------------------------------
def query_cardinality_range(
    prometheus_url: str,
    lookback: str = DEFAULT_LOOKBACK,
    step: str = DEFAULT_STEP,
) -> dict[str, list[tuple[float, float]]]:
    """查詢 per-tenant 基數時序。

    使用 ``count by (tenant)({__name__=~"tenant_threshold_.*"})``
    取得每個 tenant 的活躍 time series 數量。

    Args:
        prometheus_url: Prometheus 端點。
        lookback: 回看時間範圍（如 ``30d``）。
        step: 查詢步長（如 ``1h``）。

    Returns:
        {tenant: [(timestamp, cardinality), ...]}。
    """
    lookback_secs = parse_duration_seconds(lookback) or 30 * SECONDS_PER_DAY
    end = time.time()
    start = end - lookback_secs

    query = 'count by (tenant)({__name__=~"tenant_threshold_.*"})'
    url = (f"{prometheus_url}/api/v1/query_range?"
           f"query={query}&start={start}&end={end}&step={step}")

    data, err = http_get_json(url)
    if err or not data or data.get("status") != "success":
        return {}

    results: dict[str, list[tuple[float, float]]] = {}
    for series in data.get("data", {}).get("result", []):
        tenant = series.get("metric", {}).get("tenant", "unknown")
        values = [
            (float(ts), float(val))
            for ts, val in series.get("values", [])
        ]
        if values:
            results[tenant] = values

    return results


def query_scrape_series_added(
    prometheus_url: str,
) -> dict[str, float]:
    """查詢 scrape_series_added per job/tenant。

    補充資料：若 tenant_threshold_* 不可用，
    可用 scrape_series_added 作為基數成長的替代指標。

    Returns:
        {tenant: current_scrape_series_added}。
    """
    query = 'sum by (tenant)(scrape_series_added)'
    url = f"{prometheus_url}/api/v1/query?query={query}"

    data, err = http_get_json(url)
    if err or not data or data.get("status") != "success":
        return {}

    results: dict[str, float] = {}
    for series in data.get("data", {}).get("result", []):
        tenant = series.get("metric", {}).get("tenant", "unknown")
        value = series.get("value", [None, "0"])
        if len(value) >= 2:
            try:
                results[tenant] = float(value[1])
            except (ValueError, TypeError):
                pass

    return results


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
def analyze_tenant(
    tenant: str,
    time_series: list[tuple[float, float]],
    limit: int = DEFAULT_CARDINALITY_LIMIT,
    warn_days: int = DEFAULT_WARN_DAYS,
) -> TenantForecast:
    """分析單一 tenant 的基數趨勢。

    將 timestamp 轉為「距第一筆資料的天數」做線性回歸。

    Args:
        tenant: tenant 名稱。
        time_series: [(timestamp, cardinality), ...]。
        limit: 基數上限。
        warn_days: 預警天數。

    Returns:
        TenantForecast。
    """
    if not time_series:
        return TenantForecast(
            tenant=tenant, current_cardinality=0, cardinality_limit=limit,
            slope_per_day=0, intercept=0, r_squared=0,
            days_to_limit=None, predicted_date=None,
            trend="stable", risk_level="safe", data_points=0,
        )

    # Convert timestamps to days from start
    t0 = time_series[0][0]
    xs = [(ts - t0) / SECONDS_PER_DAY for ts, _ in time_series]
    ys = [val for _, val in time_series]

    slope, intercept, r_squared = linear_regression(xs, ys)
    current = int(round(ys[-1]))
    slope_per_day = slope  # Already in units/day

    trend = classify_trend(slope_per_day)
    days_to_limit = compute_days_to_limit(current, slope_per_day, limit)
    risk = classify_risk(current, days_to_limit, warn_days, limit)

    # Predict date
    predicted_date = None
    if days_to_limit is not None and days_to_limit > 0:
        predicted_ts = time.time() + days_to_limit * SECONDS_PER_DAY
        predicted_date = time.strftime("%Y-%m-%d", time.localtime(predicted_ts))

    return TenantForecast(
        tenant=tenant,
        current_cardinality=current,
        cardinality_limit=limit,
        slope_per_day=round(slope_per_day, 2),
        intercept=round(intercept, 1),
        r_squared=round(r_squared, 3),
        days_to_limit=days_to_limit,
        predicted_date=predicted_date,
        trend=trend,
        risk_level=risk,
        data_points=len(time_series),
    )


def generate_forecast(
    cardinality_data: dict[str, list[tuple[float, float]]],
    limit: int = DEFAULT_CARDINALITY_LIMIT,
    warn_days: int = DEFAULT_WARN_DAYS,
    lookback_days: int = 30,
    tenant_filter: Optional[str] = None,
) -> ForecastReport:
    """對所有 tenant 產生預測報告。"""
    report = ForecastReport(
        generated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        lookback_days=lookback_days,
        cardinality_limit=limit,
        warn_days=warn_days,
    )

    for tenant in sorted(cardinality_data):
        if tenant_filter and tenant != tenant_filter:
            continue
        forecast = analyze_tenant(
            tenant, cardinality_data[tenant], limit, warn_days
        )
        report.tenants.append(forecast)

    return report


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_text_report(report: ForecastReport, lang: str = "en") -> str:
    """產生純文字報告。"""
    lines: list[str] = []

    if lang == "zh":
        lines.append("═══ 基數預測報告 ═══")
        lines.append(f"產生時間: {report.generated_at}")
        lines.append(f"回看天數: {report.lookback_days} | "
                     f"基數上限: {report.cardinality_limit} | "
                     f"預警天數: {report.warn_days}")
        lines.append(f"危急: {report.critical_count} | "
                     f"警告: {report.warning_count} | "
                     f"安全: {report.safe_count}")
    else:
        lines.append("═══ Cardinality Forecast Report ═══")
        lines.append(f"Generated: {report.generated_at}")
        lines.append(f"Lookback: {report.lookback_days}d | "
                     f"Limit: {report.cardinality_limit} | "
                     f"Warn: {report.warn_days}d")
        lines.append(f"Critical: {report.critical_count} | "
                     f"Warning: {report.warning_count} | "
                     f"Safe: {report.safe_count}")

    if not report.tenants:
        lines.append("")
        lines.append("No tenant data available." if lang == "en"
                     else "無租戶資料。")
        return "\n".join(lines)

    lines.append("")

    risk_icons = {"critical": "🔴", "warning": "🟡", "safe": "🟢"}

    for t in report.tenants:
        icon = risk_icons.get(t.risk_level, "⚪")
        lines.append(f"{icon} [{t.tenant}]")
        if lang == "zh":
            lines.append(f"  目前基數: {t.current_cardinality}/{t.cardinality_limit} "
                         f"({t.current_cardinality * 100 // t.cardinality_limit}%)")
            lines.append(f"  趨勢: {t.trend} ({t.slope_per_day:+.1f}/天) | "
                         f"R²: {t.r_squared:.3f} | "
                         f"資料點: {t.data_points}")
            if t.days_to_limit is not None:
                if t.days_to_limit == 0:
                    lines.append("  ⚠ 已觸頂！")
                else:
                    lines.append(f"  預計觸頂: {t.days_to_limit:.0f} 天後"
                                 f" ({t.predicted_date})")
            else:
                lines.append("  預計觸頂: 目前趨勢下不會觸頂")
        else:
            lines.append(f"  Current: {t.current_cardinality}/{t.cardinality_limit} "
                         f"({t.current_cardinality * 100 // t.cardinality_limit}%)")
            lines.append(f"  Trend: {t.trend} ({t.slope_per_day:+.1f}/day) | "
                         f"R²: {t.r_squared:.3f} | "
                         f"Points: {t.data_points}")
            if t.days_to_limit is not None:
                if t.days_to_limit == 0:
                    lines.append("  ⚠ Limit reached!")
                else:
                    lines.append(f"  ETA to limit: {t.days_to_limit:.0f} days"
                                 f" ({t.predicted_date})")
            else:
                lines.append("  ETA to limit: not projected to reach")
        lines.append("")

    return "\n".join(lines)


def generate_json_report(report: ForecastReport) -> dict:
    """產生 JSON 格式報告。"""
    return {
        "generated_at": report.generated_at,
        "lookback_days": report.lookback_days,
        "cardinality_limit": report.cardinality_limit,
        "warn_days": report.warn_days,
        "summary": {
            "critical": report.critical_count,
            "warning": report.warning_count,
            "safe": report.safe_count,
            "total": len(report.tenants),
        },
        "tenants": [
            {
                "tenant": t.tenant,
                "current_cardinality": t.current_cardinality,
                "cardinality_limit": t.cardinality_limit,
                "slope_per_day": t.slope_per_day,
                "r_squared": t.r_squared,
                "days_to_limit": t.days_to_limit,
                "predicted_date": t.predicted_date,
                "trend": t.trend,
                "risk_level": t.risk_level,
                "data_points": t.data_points,
            }
            for t in report.tenants
        ],
    }


def generate_markdown(report: ForecastReport) -> str:
    """產生 Markdown 格式報告。"""
    lines: list[str] = []
    lines.append("# Cardinality Forecast Report")
    lines.append("")
    lines.append(f"**Generated:** {report.generated_at}  ")
    lines.append(f"**Lookback:** {report.lookback_days} days | "
                 f"**Limit:** {report.cardinality_limit} | "
                 f"**Warn:** {report.warn_days} days")
    lines.append("")

    risk_icons = {"critical": "🔴", "warning": "🟡", "safe": "🟢"}

    lines.append("| Tenant | Current | Trend | Slope/Day | R² | "
                 "Days to Limit | Risk |")
    lines.append("|--------|---------|-------|-----------|-----|"
                 "---------------|------|")

    for t in report.tenants:
        icon = risk_icons.get(t.risk_level, "⚪")
        dtl = f"{t.days_to_limit:.0f}" if t.days_to_limit is not None else "∞"
        lines.append(
            f"| {t.tenant} | {t.current_cardinality}/{t.cardinality_limit} | "
            f"{t.trend} | {t.slope_per_day:+.1f} | {t.r_squared:.3f} | "
            f"{dtl} | {icon} {t.risk_level} |"
        )

    lines.append("")
    lines.append(f"**Summary:** {report.critical_count} critical, "
                 f"{report.warning_count} warning, {report.safe_count} safe")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser(lang: str = "en") -> argparse.ArgumentParser:
    """建構 CLI 解析器。"""
    if lang == "zh":
        parser = argparse.ArgumentParser(
            description="基數預測工具 — 分析 per-tenant time series 成長趨勢，預測觸頂時間。",
        )
        parser.add_argument("--prometheus", required=True,
                            help="Prometheus 端點 URL")
        parser.add_argument("--lookback", default=DEFAULT_LOOKBACK,
                            help=f"回看時間範圍（預設: {DEFAULT_LOOKBACK}）")
        parser.add_argument("--limit", type=int, default=DEFAULT_CARDINALITY_LIMIT,
                            help=f"基數上限（預設: {DEFAULT_CARDINALITY_LIMIT}）")
        parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS,
                            help=f"預警天數（預設: {DEFAULT_WARN_DAYS}）")
        parser.add_argument("--tenant", help="僅分析指定 tenant")
        parser.add_argument("--json", action="store_true", dest="json_output",
                            help="輸出 JSON 格式")
        parser.add_argument("--markdown", action="store_true",
                            help="輸出 Markdown 格式")
        parser.add_argument("--ci", action="store_true",
                            help="CI 模式：有 critical 風險時 exit 1")
    else:
        parser = argparse.ArgumentParser(
            description="Cardinality forecasting — analyze per-tenant time series growth and predict limit breach.",
        )
        parser.add_argument("--prometheus", required=True,
                            help="Prometheus endpoint URL")
        parser.add_argument("--lookback", default=DEFAULT_LOOKBACK,
                            help=f"Lookback period (default: {DEFAULT_LOOKBACK})")
        parser.add_argument("--limit", type=int, default=DEFAULT_CARDINALITY_LIMIT,
                            help=f"Cardinality limit (default: {DEFAULT_CARDINALITY_LIMIT})")
        parser.add_argument("--warn-days", type=int, default=DEFAULT_WARN_DAYS,
                            help=f"Warning days before limit (default: {DEFAULT_WARN_DAYS})")
        parser.add_argument("--tenant", help="Analyze specific tenant only")
        parser.add_argument("--json", action="store_true", dest="json_output",
                            help="Output in JSON format")
        parser.add_argument("--markdown", action="store_true",
                            help="Output in Markdown format")
        parser.add_argument("--ci", action="store_true",
                            help="CI mode: exit 1 if any critical risk found")

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI 進入點。"""
    lang = detect_cli_lang()
    parser = build_parser(lang)
    args = parser.parse_args(argv)

    # Parse lookback
    lookback_secs = parse_duration_seconds(args.lookback) or 30 * SECONDS_PER_DAY
    lookback_days = lookback_secs // SECONDS_PER_DAY

    # Query Prometheus
    cardinality_data = query_cardinality_range(
        args.prometheus, args.lookback
    )

    if not cardinality_data:
        if lang == "zh":
            print("未取得基數資料。請確認 Prometheus 端點和 tenant_threshold_* 指標。",
                  file=sys.stderr)
        else:
            print("No cardinality data retrieved. Check Prometheus endpoint "
                  "and tenant_threshold_* metrics.", file=sys.stderr)
        return 1

    # Generate forecast
    report = generate_forecast(
        cardinality_data,
        limit=args.limit,
        warn_days=args.warn_days,
        lookback_days=lookback_days,
        tenant_filter=args.tenant,
    )

    # Output
    if args.json_output:
        print(json.dumps(generate_json_report(report), indent=2,
                         ensure_ascii=False))
    elif args.markdown:
        print(generate_markdown(report))
    else:
        print(generate_text_report(report, lang))

    # CI exit code
    if args.ci and report.critical_count > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
