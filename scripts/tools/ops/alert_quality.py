#!/usr/bin/env python3
"""alert_quality.py — 警報品質評估工具。

分析 Alertmanager 歷史紀錄，對每個 alertname × tenant 組合計算品質指標：

1. **Noise Score** — 單位時間內 firing 次數（反覆震盪偵測）
2. **Stale Score** — 長期未 fire 的警報（閾值可能已失去意義）
3. **Resolution Latency** — firing → resolved 平均時間
4. **Suppression Ratio** — 被 inhibit / silence 壓掉的比例

產出 per-tenant JSON 報告，可嵌入 Grafana dashboard 或作為 CI gate。

用法:
    da-tools alert-quality --prometheus http://localhost:9090 --period 30d
    da-tools alert-quality --prometheus http://localhost:9090 --period 7d --json
    da-tools alert-quality --alertmanager http://localhost:9093 --period 30d --tenant db-a
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from urllib.parse import quote

# ---------------------------------------------------------------------------
# Imports from shared library
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from _lib_python import (  # noqa: E402
    detect_cli_lang,
    http_get_json,
    parse_duration_seconds,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# 品質等級閾值
NOISE_THRESHOLDS = {"HIGH": 20, "MEDIUM": 10}  # firings / period
STALE_DAYS = 14  # 超過此天數未 fire 視為 stale
FLAPPING_RESOLUTION_SECS = 300  # <5min = flapping
SUPPRESSION_WARN_RATIO = 0.5  # 超過 50% 被壓掉

# 品質評級
GRADE_GOOD = "GOOD"
GRADE_WARN = "WARN"
GRADE_BAD = "BAD"

# Tenant 名稱白名單 pattern（僅允許字母、數字、底線、連字號）
_TENANT_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]+$')


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class AlertQualityMetrics:
    """單一 alertname × tenant 的品質指標。"""

    alertname: str
    tenant: str
    # Noise: 震盪偵測
    fire_count: int = 0
    noise_grade: str = GRADE_GOOD
    # Stale: 閾值失效偵測
    last_fired_ts: float = 0.0
    days_since_last_fire: float = 0.0
    stale_grade: str = GRADE_GOOD
    # Resolution latency
    avg_resolution_secs: float = 0.0
    resolution_grade: str = GRADE_GOOD
    # Suppression ratio
    total_alerts: int = 0
    suppressed_count: int = 0
    suppression_ratio: float = 0.0
    suppression_grade: str = GRADE_GOOD
    # Overall
    overall_grade: str = GRADE_GOOD


@dataclass
class TenantQualityReport:
    """單一 tenant 的品質報告。"""

    tenant: str
    period_days: int = 0
    total_alertnames: int = 0
    good_count: int = 0
    warn_count: int = 0
    bad_count: int = 0
    score: float = 100.0  # 0-100 綜合分數
    alerts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class QualityReport:
    """完整品質評估報告。"""

    timestamp: str = ""
    period: str = ""
    tenants: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core computation engine
# ---------------------------------------------------------------------------
def query_prometheus_alerts(
    prom_url: str,
    metric: str,
    period_seconds: int,
    *,
    tenant: Optional[str] = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    """查詢 Prometheus ALERTS metric 歷史資料。

    Args:
        prom_url: Prometheus base URL。
        metric: Metric 名稱（ALERTS 或 ALERTS_FOR_STATE）。
        period_seconds: 回溯秒數。
        tenant: 可選，篩選特定 tenant。
        timeout: HTTP timeout。

    Returns:
        Prometheus range query 結果清單。
    """
    end_ts = time.time()
    start_ts = end_ts - period_seconds
    step = max(60, period_seconds // 1000)  # 自適應 step，最小 60s

    label_filter = '{alertstate="firing"}'
    if tenant:
        if not _TENANT_NAME_RE.match(tenant):
            return []
        label_filter = f'{{alertstate="firing",tenant="{tenant}"}}'

    query = f"{metric}{label_filter}"
    url = (
        f"{prom_url}/api/v1/query_range"
        f"?query={query}"
        f"&start={start_ts:.0f}"
        f"&end={end_ts:.0f}"
        f"&step={step}"
    )

    data, err = http_get_json(url, timeout=timeout)
    if err or not data:
        return []
    if data.get("status") != "success":
        return []
    return data.get("data", {}).get("result", [])


def query_alertmanager_alerts(
    am_url: str,
    *,
    tenant: Optional[str] = None,
    state: str = "",
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """查詢 Alertmanager 當前告警。

    Args:
        am_url: Alertmanager base URL。
        tenant: 可選，篩選特定 tenant。
        state: 可選，篩選狀態（active, suppressed, unprocessed）。
        timeout: HTTP timeout。

    Returns:
        Alert 清單。
    """
    params = []
    if state:
        params.append(f"filter=alertstate%3D{quote(state)}")
    if tenant:
        if not _TENANT_NAME_RE.match(tenant):
            return []
        params.append(f"filter=tenant%3D{quote(tenant)}")

    url = f"{am_url}/api/v2/alerts"
    if params:
        url += "?" + "&".join(params)

    data, err = http_get_json(url, timeout=timeout)
    if err or not data:
        return []
    if isinstance(data, list):
        return data
    return []


def query_alertmanager_silences(
    am_url: str,
    *,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """查詢 Alertmanager 活躍的 silence 規則。"""
    url = f"{am_url}/api/v2/silences"
    data, err = http_get_json(url, timeout=timeout)
    if err or not data:
        return []
    if isinstance(data, list):
        return [s for s in data if s.get("status", {}).get("state") == "active"]
    return []


def compute_noise_score(fire_count: int, period_days: int) -> tuple[int, str]:
    """計算 Noise Score（震盪分數）。

    Args:
        fire_count: 期間內 firing 次數。
        period_days: 觀察天數。

    Returns:
        (normalized_fire_count, grade) — 標準化到 30 天的 fire 次數 + 評級。
    """
    if period_days <= 0:
        return fire_count, GRADE_GOOD

    # 標準化到 30 天
    normalized = int(fire_count * 30 / period_days)

    if normalized >= NOISE_THRESHOLDS["HIGH"]:
        return normalized, GRADE_BAD
    if normalized >= NOISE_THRESHOLDS["MEDIUM"]:
        return normalized, GRADE_WARN
    return normalized, GRADE_GOOD


def compute_stale_score(
    last_fired_ts: float,
    now: float,
    period_days: int,
) -> tuple[float, str]:
    """計算 Stale Score（閾值失效分數）。

    Args:
        last_fired_ts: 最後一次 firing 的 UNIX timestamp。
        now: 當前 UNIX timestamp。
        period_days: 觀察期間天數。

    Returns:
        (days_since_last_fire, grade)。
    """
    if last_fired_ts <= 0:
        # 從未 fire 過（在觀察期間內）
        return float(period_days), GRADE_WARN

    days_since = (now - last_fired_ts) / 86400
    if days_since >= STALE_DAYS:
        return days_since, GRADE_WARN
    return days_since, GRADE_GOOD


def compute_resolution_latency(
    durations: list[float],
) -> tuple[float, str]:
    """計算 Resolution Latency（解決延遲）。

    Args:
        durations: firing → resolved 的秒數清單。

    Returns:
        (avg_seconds, grade) — 平均秒數 + 評級。
    """
    if not durations:
        return 0.0, GRADE_GOOD

    avg = sum(durations) / len(durations)

    # 太短 = flapping（<5min），太長 = 無人處理（>24h）
    if avg < FLAPPING_RESOLUTION_SECS:
        return avg, GRADE_BAD  # flapping
    if avg > 86400:
        return avg, GRADE_WARN  # 超過 24h 無人處理
    return avg, GRADE_GOOD


def compute_suppression_ratio(
    total: int,
    suppressed: int,
) -> tuple[float, str]:
    """計算 Suppression Ratio（壓制比例）。

    Args:
        total: 總告警次數。
        suppressed: 被 inhibit/silence 壓掉的次數。

    Returns:
        (ratio, grade) — 比例 + 評級。
    """
    if total <= 0:
        return 0.0, GRADE_GOOD

    ratio = suppressed / total
    if ratio >= SUPPRESSION_WARN_RATIO:
        return ratio, GRADE_WARN
    return ratio, GRADE_GOOD


def compute_overall_grade(metrics: AlertQualityMetrics) -> str:
    """綜合四項指標決定 overall grade。

    任一項 BAD → overall BAD。
    兩項以上 WARN → overall BAD。
    一項 WARN → overall WARN。
    全 GOOD → overall GOOD。
    """
    grades = [
        metrics.noise_grade,
        metrics.stale_grade,
        metrics.resolution_grade,
        metrics.suppression_grade,
    ]

    bad_count = grades.count(GRADE_BAD)
    warn_count = grades.count(GRADE_WARN)

    if bad_count > 0:
        return GRADE_BAD
    if warn_count >= 2:
        return GRADE_BAD
    if warn_count == 1:
        return GRADE_WARN
    return GRADE_GOOD


def compute_tenant_score(alerts: list[AlertQualityMetrics]) -> float:
    """計算 tenant 綜合分數（0-100）。

    GOOD=100, WARN=50, BAD=0，取所有 alertname 的平均。
    """
    if not alerts:
        return 100.0

    score_map = {GRADE_GOOD: 100, GRADE_WARN: 50, GRADE_BAD: 0}
    total = sum(score_map.get(a.overall_grade, 50) for a in alerts)
    return round(total / len(alerts), 1)


# ---------------------------------------------------------------------------
# Prometheus-based analysis (main path)
# ---------------------------------------------------------------------------
def analyze_from_prometheus(
    prom_url: str,
    period_seconds: int,
    *,
    tenant: Optional[str] = None,
    am_url: Optional[str] = None,
) -> list[AlertQualityMetrics]:
    """從 Prometheus ALERTS metric 分析品質。

    Args:
        prom_url: Prometheus URL。
        period_seconds: 回溯秒數。
        tenant: 可選，篩選特定 tenant。
        am_url: 可選 Alertmanager URL（取得 suppression 資料）。

    Returns:
        每個 alertname × tenant 的品質指標清單。
    """
    now = time.time()
    period_days = period_seconds // 86400 or 1

    # 查詢 ALERTS metric
    results = query_prometheus_alerts(
        prom_url, "ALERTS", period_seconds, tenant=tenant,
    )

    # 按 alertname × tenant 聚合
    alert_data: dict[tuple[str, str], dict[str, Any]] = {}
    for series in results:
        labels = series.get("metric", {})
        aname = labels.get("alertname", "unknown")
        tname = labels.get("tenant", "unknown")
        key = (aname, tname)

        if key not in alert_data:
            alert_data[key] = {
                "fire_transitions": 0,
                "last_fired_ts": 0.0,
                "durations": [],
                "total": 0,
            }

        values = series.get("values", [])
        if not values:
            continue

        # 計算 fire transitions（0→1 轉換次數）
        prev_val = 0
        fire_start = 0.0
        for ts, val_str in values:
            try:
                val = int(float(val_str))
            except (ValueError, TypeError):
                continue

            ts_f = float(ts)
            alert_data[key]["total"] += 1

            if val == 1 and prev_val == 0:
                # 新的 firing 事件
                alert_data[key]["fire_transitions"] += 1
                fire_start = ts_f
            elif val == 0 and prev_val == 1 and fire_start > 0:
                # Resolved
                duration = ts_f - fire_start
                if duration > 0:
                    alert_data[key]["durations"].append(duration)
                fire_start = 0.0

            if val == 1 and ts_f > alert_data[key]["last_fired_ts"]:
                alert_data[key]["last_fired_ts"] = ts_f

            prev_val = val

    # 查詢 suppression 資料（從 Alertmanager）
    suppressed_counts: dict[tuple[str, str], int] = {}
    if am_url:
        suppressed_alerts = query_alertmanager_alerts(
            am_url, tenant=tenant, state="suppressed",
        )
        for alert in suppressed_alerts:
            labels = alert.get("labels", {})
            aname = labels.get("alertname", "unknown")
            tname = labels.get("tenant", "unknown")
            key = (aname, tname)
            suppressed_counts[key] = suppressed_counts.get(key, 0) + 1

    # 組裝指標
    metrics_list: list[AlertQualityMetrics] = []
    for (aname, tname), data in sorted(alert_data.items()):
        m = AlertQualityMetrics(alertname=aname, tenant=tname)

        # Noise
        m.fire_count, m.noise_grade = compute_noise_score(
            data["fire_transitions"], period_days,
        )

        # Stale
        m.last_fired_ts = data["last_fired_ts"]
        m.days_since_last_fire, m.stale_grade = compute_stale_score(
            data["last_fired_ts"], now, period_days,
        )

        # Resolution latency
        m.avg_resolution_secs, m.resolution_grade = compute_resolution_latency(
            data["durations"],
        )

        # Suppression
        key = (aname, tname)
        m.total_alerts = data["total"]
        m.suppressed_count = suppressed_counts.get(key, 0)
        m.suppression_ratio, m.suppression_grade = compute_suppression_ratio(
            m.total_alerts, m.suppressed_count,
        )

        # Overall
        m.overall_grade = compute_overall_grade(m)
        metrics_list.append(m)

    return metrics_list


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_report(
    metrics: list[AlertQualityMetrics],
    period: str,
) -> QualityReport:
    """從指標清單生成完整報告。

    Args:
        metrics: AlertQualityMetrics 清單。
        period: 期間字串（如 "30d"）。

    Returns:
        QualityReport 物件。
    """
    report = QualityReport(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        period=period,
    )

    # 按 tenant 分組
    tenant_map: dict[str, list[AlertQualityMetrics]] = {}
    for m in metrics:
        tenant_map.setdefault(m.tenant, []).append(m)

    total_good = 0
    total_warn = 0
    total_bad = 0

    for tname in sorted(tenant_map):
        alerts = tenant_map[tname]
        t_report = TenantQualityReport(tenant=tname)
        t_report.period_days = parse_duration_seconds(period) // 86400 if parse_duration_seconds(period) else 0
        t_report.total_alertnames = len(alerts)
        t_report.good_count = sum(1 for a in alerts if a.overall_grade == GRADE_GOOD)
        t_report.warn_count = sum(1 for a in alerts if a.overall_grade == GRADE_WARN)
        t_report.bad_count = sum(1 for a in alerts if a.overall_grade == GRADE_BAD)
        t_report.score = compute_tenant_score(alerts)
        t_report.alerts = [asdict(a) for a in alerts]

        total_good += t_report.good_count
        total_warn += t_report.warn_count
        total_bad += t_report.bad_count

        report.tenants.append(asdict(t_report))

    report.summary = {
        "total_tenants": len(tenant_map),
        "total_alertnames": len(metrics),
        "good": total_good,
        "warn": total_warn,
        "bad": total_bad,
        "overall_score": round(
            sum(t["score"] for t in report.tenants) / max(len(report.tenants), 1), 1,
        ),
    }

    return report


def print_text_report(report: QualityReport) -> None:
    """印出人類可讀的文字報告。"""
    lang = detect_cli_lang()

    if lang == "zh":
        print("=" * 60)
        print(f"  警報品質評估報告")
        print(f"  期間: {report.period}  |  時間: {report.timestamp}")
        print("=" * 60)
    else:
        print("=" * 60)
        print(f"  Alert Quality Report")
        print(f"  Period: {report.period}  |  Generated: {report.timestamp}")
        print("=" * 60)

    summary = report.summary
    print()
    grade_label = "評級" if lang == "zh" else "Grade"
    print(f"  {grade_label}: {summary.get('good', 0)} GOOD / "
          f"{summary.get('warn', 0)} WARN / "
          f"{summary.get('bad', 0)} BAD")
    score_label = "綜合分數" if lang == "zh" else "Overall Score"
    print(f"  {score_label}: {summary.get('overall_score', 0)}/100")
    print()

    for t in report.tenants:
        print("-" * 60)
        tenant_label = "租戶" if lang == "zh" else "Tenant"
        print(f"  {tenant_label}: {t['tenant']}  "
              f"(Score: {t['score']}/100, "
              f"{t['good_count']}G/{t['warn_count']}W/{t['bad_count']}B)")
        print()

        # 只顯示 WARN/BAD 的詳細資訊（GOOD 的省略）
        problem_alerts = [
            a for a in t["alerts"]
            if a["overall_grade"] != GRADE_GOOD
        ]
        if not problem_alerts:
            ok_label = "所有警報品質良好" if lang == "zh" else "All alerts in good standing"
            print(f"    ✓ {ok_label}")
            continue

        for a in problem_alerts:
            print(f"    [{a['overall_grade']}] {a['alertname']}")
            issues: list[str] = []
            if a["noise_grade"] != GRADE_GOOD:
                noise_label = "震盪" if lang == "zh" else "Noise"
                issues.append(f"{noise_label}: {a['fire_count']} fires/30d")
            if a["stale_grade"] != GRADE_GOOD:
                stale_label = "閒置" if lang == "zh" else "Stale"
                issues.append(f"{stale_label}: {a['days_since_last_fire']:.0f}d")
            if a["resolution_grade"] != GRADE_GOOD:
                if a["avg_resolution_secs"] < FLAPPING_RESOLUTION_SECS:
                    res_label = "抖動" if lang == "zh" else "Flapping"
                else:
                    res_label = "解決延遲" if lang == "zh" else "Slow resolution"
                issues.append(f"{res_label}: {a['avg_resolution_secs']:.0f}s")
            if a["suppression_grade"] != GRADE_GOOD:
                supp_label = "壓制率" if lang == "zh" else "Suppressed"
                issues.append(f"{supp_label}: {a['suppression_ratio']:.0%}")
            if issues:
                print(f"           {' | '.join(issues)}")
        print()

    print("=" * 60)


def generate_markdown(report: QualityReport) -> str:
    """生成 Markdown 格式報告。"""
    lines: list[str] = []
    lines.append(f"## Alert Quality Report — {report.period}")
    lines.append("")
    lines.append(f"Generated: {report.timestamp}")
    lines.append("")

    s = report.summary
    lines.append(f"**Overall Score: {s.get('overall_score', 0)}/100** "
                 f"({s.get('good', 0)} GOOD / {s.get('warn', 0)} WARN / {s.get('bad', 0)} BAD)")
    lines.append("")

    lines.append("| Tenant | Score | GOOD | WARN | BAD |")
    lines.append("|--------|-------|------|------|-----|")
    for t in report.tenants:
        lines.append(
            f"| {t['tenant']} | {t['score']} | "
            f"{t['good_count']} | {t['warn_count']} | {t['bad_count']} |"
        )
    lines.append("")

    # 詳細問題清單
    for t in report.tenants:
        problems = [a for a in t["alerts"] if a["overall_grade"] != GRADE_GOOD]
        if not problems:
            continue
        lines.append(f"### {t['tenant']}")
        lines.append("")
        lines.append("| Alert | Grade | Noise | Stale | Resolution | Suppressed |")
        lines.append("|-------|-------|-------|-------|------------|------------|")
        for a in problems:
            lines.append(
                f"| `{a['alertname']}` | {a['overall_grade']} | "
                f"{a['fire_count']}/30d | {a['days_since_last_fire']:.0f}d | "
                f"{a['avg_resolution_secs']:.0f}s | {a['suppression_ratio']:.0%} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    lang = detect_cli_lang()

    if lang == "zh":
        parser = argparse.ArgumentParser(
            description="警報品質評估 — 分析 Alertmanager 歷史以識別問題警報",
        )
        parser.add_argument("--prometheus", required=True, help="Prometheus URL")
        parser.add_argument("--alertmanager", help="Alertmanager URL（取得 suppression 資料）")
        parser.add_argument("--period", default="30d", help="分析期間（預設: 30d）")
        parser.add_argument("--tenant", help="篩選特定租戶")
        parser.add_argument("--json", action="store_true", dest="json_output", help="JSON 格式輸出")
        parser.add_argument("--markdown", action="store_true", help="Markdown 格式輸出")
        parser.add_argument("--ci", action="store_true", help="CI 模式: BAD 時 exit code 1")
        parser.add_argument("--min-score", type=float, default=0, help="CI 最低分數門檻（預設: 0）")
    else:
        parser = argparse.ArgumentParser(
            description="Alert Quality Scoring — analyze Alertmanager history to identify problem alerts",
        )
        parser.add_argument("--prometheus", required=True, help="Prometheus URL")
        parser.add_argument("--alertmanager", help="Alertmanager URL (for suppression data)")
        parser.add_argument("--period", default="30d", help="Analysis period (default: 30d)")
        parser.add_argument("--tenant", help="Filter to specific tenant")
        parser.add_argument("--json", action="store_true", dest="json_output", help="JSON output")
        parser.add_argument("--markdown", action="store_true", help="Markdown output")
        parser.add_argument("--ci", action="store_true", help="CI mode: exit 1 if any BAD alert")
        parser.add_argument("--min-score", type=float, default=0, help="CI minimum score threshold (default: 0)")

    return parser


def main() -> None:
    """CLI 進入點。"""
    parser = build_parser()
    args = parser.parse_args()

    period_secs = parse_duration_seconds(args.period)
    if not period_secs:
        print(f"Error: invalid period '{args.period}'", file=sys.stderr)
        sys.exit(1)

    if args.tenant and not _TENANT_NAME_RE.match(args.tenant):
        print(f"Error: invalid tenant name '{args.tenant}' "
              "(only alphanumeric, underscore, hyphen allowed)",
              file=sys.stderr)
        sys.exit(1)

    # 分析
    metrics = analyze_from_prometheus(
        args.prometheus,
        period_secs,
        tenant=args.tenant,
        am_url=args.alertmanager,
    )

    # 產生報告
    report = generate_report(metrics, args.period)

    # 輸出
    if args.json_output:
        print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    elif args.markdown:
        print(generate_markdown(report))
    else:
        print_text_report(report)

    # CI gate
    if args.ci:
        score = report.summary.get("overall_score", 100)
        bad_count = report.summary.get("bad", 0)
        if bad_count > 0 or score < args.min_score:
            sys.exit(1)


if __name__ == "__main__":
    main()
