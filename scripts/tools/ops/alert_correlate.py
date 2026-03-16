#!/usr/bin/env python3
"""alert_correlate.py — 告警關聯分析引擎（離線 CLI 模式）。

分析 Alertmanager 歷史告警，以時間窗口聚合跨 tenant 事件，計算關聯分數，
推斷 root cause 候選。

用法:
    da-tools alert-correlate --prometheus http://localhost:9090 --window 5m
    da-tools alert-correlate --input alerts.json --window 5m --json
    da-tools alert-correlate --prometheus http://localhost:9090 --min-score 0.5 --ci
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import List, Optional

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
DEFAULT_WINDOW_SECS = 300  # 5 minutes
MIN_CORRELATION_SCORE = 0.3
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S"

_HELP = {
    "zh": {
        "desc": "告警關聯分析：時間窗口聚合 + 跨 tenant 關聯評分 + root cause 推斷",
        "window": "時間窗口大小 (預設 5m)",
        "min_score": "最低關聯分數閾值 (預設 0.3)",
        "input": "從 JSON 檔案讀取 alerts (替代 Prometheus/Alertmanager API)",
        "lookback": "回顧期間 (預設 24h)",
    },
    "en": {
        "desc": "Alert correlation analysis: time-window clustering + cross-tenant scoring + root cause inference",
        "window": "Time window size (default 5m)",
        "min_score": "Minimum correlation score threshold (default 0.3)",
        "input": "Read alerts from JSON file (instead of Prometheus/Alertmanager API)",
        "lookback": "Lookback period (default 24h)",
    },
}


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------
@dataclass
class AlertEvent:
    """A single alert firing event."""

    alertname: str
    tenant: str = ""
    severity: str = "warning"
    namespace: str = ""
    starts_at: float = 0.0  # unix timestamp
    ends_at: float = 0.0
    labels: dict = field(default_factory=dict)

    @classmethod
    def from_alertmanager(cls, raw: dict) -> "AlertEvent":
        """Parse from Alertmanager /api/v2/alerts format."""
        labels = raw.get("labels", {})
        starts_at = _parse_iso_timestamp(raw.get("startsAt", ""))
        ends_at = _parse_iso_timestamp(raw.get("endsAt", ""))
        return cls(
            alertname=labels.get("alertname", "unknown"),
            tenant=labels.get("tenant", labels.get("namespace", "")),
            severity=labels.get("severity", "warning"),
            namespace=labels.get("namespace", ""),
            starts_at=starts_at,
            ends_at=ends_at,
            labels=labels,
        )


@dataclass
class CorrelationCluster:
    """A group of correlated alerts."""

    cluster_id: int
    window_start: float
    window_end: float
    alerts: List[AlertEvent] = field(default_factory=list)
    root_cause: Optional[AlertEvent] = None
    correlation_scores: dict = field(default_factory=dict)

    @property
    def tenant_count(self) -> int:
        return len(set(a.tenant for a in self.alerts))

    @property
    def alert_count(self) -> int:
        return len(self.alerts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _parse_iso_timestamp(ts_str: str) -> float:
    """Parse ISO 8601 timestamp to unix epoch. Returns 0.0 on failure."""
    if not ts_str:
        return 0.0
    try:
        # Handle various ISO formats
        clean = re.sub(r"\.\d+", "", ts_str)  # strip fractional seconds
        clean = re.sub(r"Z$", "+00:00", clean)
        if "+" not in clean and "-" not in clean[10:]:
            clean += "+00:00"
        dt = datetime.fromisoformat(clean)
        return dt.timestamp()
    except (ValueError, OSError):
        return 0.0


def _time_overlap(a_start: float, a_end: float,
                  b_start: float, b_end: float) -> float:
    """Compute overlap ratio between two time ranges [0.0, 1.0].

    Returns the fraction of the shorter interval that overlaps.
    """
    if a_start == 0 or b_start == 0:
        return 0.0
    # Use current time as end if still firing (end=0)
    now = datetime.now(timezone.utc).timestamp()
    a_end = a_end if a_end > a_start else now
    b_end = b_end if b_end > b_start else now

    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    overlap = max(0.0, overlap_end - overlap_start)

    shorter = min(a_end - a_start, b_end - b_start)
    if shorter <= 0:
        return 0.0
    return min(1.0, overlap / shorter)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def load_alerts_from_json(path: str) -> List[AlertEvent]:
    """Load alerts from a JSON file.

    Supports both raw array format and Alertmanager API response format.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    # Handle Alertmanager API wrapper
    if isinstance(data, dict) and "data" in data:
        alerts_raw = data["data"]
    elif isinstance(data, list):
        alerts_raw = data
    else:
        return []

    return [AlertEvent.from_alertmanager(a) for a in alerts_raw]


def load_alerts_from_alertmanager(prom_url: str,
                                  lookback_secs: int = 86400
                                  ) -> List[AlertEvent]:
    """Load alerts from Alertmanager /api/v2/alerts endpoint.

    Falls back to Prometheus /api/v1/alerts if Alertmanager is not separate.
    """
    # Try Alertmanager API first (common port 9093)
    am_url = prom_url.replace(":9090", ":9093")
    data, err = http_get_json(f"{am_url}/api/v2/alerts")
    if not err and isinstance(data, list):
        return [AlertEvent.from_alertmanager(a) for a in data]

    # Fallback to Prometheus alerts API
    data, err = http_get_json(f"{prom_url}/api/v1/alerts")
    if err:
        print(f"WARN: Cannot fetch alerts: {err}", file=sys.stderr)
        return []

    if data.get("status") != "success":
        return []

    alerts_raw = data.get("data", {}).get("alerts", [])
    return [AlertEvent.from_alertmanager(a) for a in alerts_raw]


def time_window_cluster(alerts: List[AlertEvent],
                        window_secs: int = DEFAULT_WINDOW_SECS
                        ) -> List[CorrelationCluster]:
    """Cluster alerts by overlapping time windows.

    Alerts that start within `window_secs` of each other are grouped together.
    Uses a greedy approach: sort by start time, extend window as new alerts join.
    """
    if not alerts:
        return []

    sorted_alerts = sorted(alerts, key=lambda a: a.starts_at)
    clusters: List[CorrelationCluster] = []
    cluster_id = 0

    current = CorrelationCluster(
        cluster_id=cluster_id,
        window_start=sorted_alerts[0].starts_at,
        window_end=sorted_alerts[0].starts_at + window_secs,
        alerts=[sorted_alerts[0]],
    )

    for alert in sorted_alerts[1:]:
        if alert.starts_at <= current.window_end:
            # Extend the window
            current.alerts.append(alert)
            current.window_end = max(
                current.window_end,
                alert.starts_at + window_secs,
            )
        else:
            # Start a new cluster
            if len(current.alerts) > 1:
                clusters.append(current)
            cluster_id += 1
            current = CorrelationCluster(
                cluster_id=cluster_id,
                window_start=alert.starts_at,
                window_end=alert.starts_at + window_secs,
                alerts=[alert],
            )

    # Don't forget the last cluster
    if len(current.alerts) > 1:
        clusters.append(current)

    return clusters


def compute_correlation_score(a: AlertEvent, b: AlertEvent) -> float:
    """Compute correlation score between two alerts [0.0, 1.0].

    Factors:
    - Time overlap (40% weight)
    - Same namespace (30% weight)
    - Alert name prefix match (20% weight)
    - Same severity (10% weight)
    """
    score = 0.0

    # Time overlap (40%)
    overlap = _time_overlap(a.starts_at, a.ends_at, b.starts_at, b.ends_at)
    score += 0.4 * overlap

    # Namespace match (30%)
    if a.namespace and b.namespace and a.namespace == b.namespace:
        score += 0.3

    # Alert name prefix (20%) — e.g., "MariaDB*" alerts correlate
    a_prefix = a.alertname.split("_")[0] if "_" in a.alertname else a.alertname[:6]
    b_prefix = b.alertname.split("_")[0] if "_" in b.alertname else b.alertname[:6]
    if a_prefix == b_prefix and len(a_prefix) >= 3:
        score += 0.2

    # Same severity (10%)
    if a.severity == b.severity:
        score += 0.1

    return round(min(1.0, score), 3)


def score_cluster(cluster: CorrelationCluster,
                  min_score: float = MIN_CORRELATION_SCORE) -> None:
    """Compute pairwise correlation scores within a cluster.

    Populates cluster.correlation_scores with {(i,j): score} for pairs
    exceeding min_score.
    """
    alerts = cluster.alerts
    scores = {}
    for i in range(len(alerts)):
        for j in range(i + 1, len(alerts)):
            s = compute_correlation_score(alerts[i], alerts[j])
            if s >= min_score:
                scores[f"{i}-{j}"] = s
    cluster.correlation_scores = scores


def infer_root_cause(cluster: CorrelationCluster) -> None:
    """Infer the most likely root cause alert in a cluster.

    Heuristic: the alert with the earliest start time among the highest
    severity level is the root cause candidate.
    """
    if not cluster.alerts:
        return

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    candidates = sorted(
        cluster.alerts,
        key=lambda a: (
            severity_order.get(a.severity, 99),
            a.starts_at,
        ),
    )
    cluster.root_cause = candidates[0]


def analyze_alerts(alerts: List[AlertEvent],
                   window_secs: int = DEFAULT_WINDOW_SECS,
                   min_score: float = MIN_CORRELATION_SCORE
                   ) -> List[CorrelationCluster]:
    """Full analysis pipeline: cluster → score → root cause."""
    clusters = time_window_cluster(alerts, window_secs)
    for cluster in clusters:
        score_cluster(cluster, min_score)
        infer_root_cause(cluster)
    return clusters


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------
def build_report(clusters: List[CorrelationCluster],
                 total_alerts: int) -> dict:
    """Build structured JSON report."""
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "total_alerts": total_alerts,
        "cluster_count": len(clusters),
        "clusters": [],
    }

    for c in clusters:
        root = None
        if c.root_cause:
            root = {
                "alertname": c.root_cause.alertname,
                "tenant": c.root_cause.tenant,
                "severity": c.root_cause.severity,
            }
        report["clusters"].append({
            "cluster_id": c.cluster_id,
            "alert_count": c.alert_count,
            "tenant_count": c.tenant_count,
            "root_cause": root,
            "alerts": [
                {"alertname": a.alertname, "tenant": a.tenant,
                 "severity": a.severity}
                for a in c.alerts
            ],
            "avg_correlation": (
                round(sum(c.correlation_scores.values())
                      / len(c.correlation_scores), 3)
                if c.correlation_scores else 0.0
            ),
        })

    return report


def format_text_report(report: dict) -> str:
    """Format human-readable text report."""
    lines = []
    lines.append("=" * 60)
    lines.append("  Alert Correlation Report")
    lines.append("=" * 60)
    lines.append(f"  Total alerts analyzed: {report['total_alerts']}")
    lines.append(f"  Correlation clusters:  {report['cluster_count']}")
    lines.append("")

    if not report["clusters"]:
        lines.append("  No correlated alert clusters found.")
        return "\n".join(lines)

    for c in report["clusters"]:
        lines.append(f"  Cluster #{c['cluster_id']} "
                     f"({c['alert_count']} alerts, "
                     f"{c['tenant_count']} tenants, "
                     f"avg score: {c['avg_correlation']:.2f})")
        if c["root_cause"]:
            rc = c["root_cause"]
            lines.append(f"    Root Cause: [{rc['severity']}] "
                         f"{rc['alertname']} (tenant: {rc['tenant']})")
        for a in c["alerts"]:
            marker = " ◄" if (c["root_cause"]
                              and a["alertname"] == c["root_cause"]["alertname"]
                              and a["tenant"] == c["root_cause"]["tenant"]) else ""
            lines.append(f"    - [{a['severity']}] {a['alertname']} "
                         f"(tenant: {a['tenant']}){marker}")
        lines.append("")

    return "\n".join(lines)


def format_json_report(report: dict) -> str:
    """Format JSON report."""
    return json.dumps(report, indent=2, ensure_ascii=False)


def format_markdown_report(report: dict) -> str:
    """Format Markdown report."""
    lines = []
    lines.append("# Alert Correlation Report")
    lines.append("")
    lines.append(f"- **Total alerts**: {report['total_alerts']}")
    lines.append(f"- **Clusters found**: {report['cluster_count']}")
    lines.append("")

    for c in report["clusters"]:
        lines.append(f"## Cluster #{c['cluster_id']}")
        lines.append("")
        lines.append(f"| Property | Value |")
        lines.append(f"|----------|-------|")
        lines.append(f"| Alerts | {c['alert_count']} |")
        lines.append(f"| Tenants | {c['tenant_count']} |")
        lines.append(f"| Avg Score | {c['avg_correlation']:.2f} |")
        if c["root_cause"]:
            rc = c["root_cause"]
            lines.append(f"| Root Cause | [{rc['severity']}] "
                         f"{rc['alertname']} |")
        lines.append("")
        lines.append("| Severity | Alert | Tenant |")
        lines.append("|----------|-------|--------|")
        for a in c["alerts"]:
            lines.append(f"| {a['severity']} | {a['alertname']} "
                         f"| {a['tenant']} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    lang = detect_cli_lang()
    h = _HELP.get(lang, _HELP["en"])

    parser = argparse.ArgumentParser(
        description=h["desc"],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Examples:
              %(prog)s --prometheus http://localhost:9090 --window 5m
              %(prog)s --input alerts.json --window 5m --json
              %(prog)s --prometheus http://localhost:9090 --min-score 0.5 --ci
        """),
    )
    parser.add_argument("--prometheus", default=None,
                        help="Prometheus URL (default: $PROMETHEUS_URL)")
    parser.add_argument("--input", "-i", default=None,
                        help=h["input"])
    parser.add_argument("--window", default="5m",
                        help=h["window"])
    parser.add_argument("--lookback", default="24h",
                        help=h["lookback"])
    parser.add_argument("--min-score", type=float, default=MIN_CORRELATION_SCORE,
                        help=h["min_score"])
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--markdown", action="store_true",
                        help="Output as Markdown")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 if critical clusters found")
    return parser


def main():
    """CLI entry point: 告警關聯分析引擎。"""
    parser = build_parser()
    args = parser.parse_args()

    window_secs = parse_duration_seconds(args.window)
    if window_secs is None or window_secs <= 0:
        print("ERROR: Invalid --window value", file=sys.stderr)
        sys.exit(1)

    # Load alerts
    if args.input:
        alerts = load_alerts_from_json(args.input)
    else:
        prom_url = (args.prometheus
                    or os.environ.get("PROMETHEUS_URL",
                                      "http://localhost:9090"))
        alerts = load_alerts_from_alertmanager(prom_url)

    if not alerts:
        print("WARN: No alerts found to analyze", file=sys.stderr)

    # Analyze
    clusters = analyze_alerts(alerts, window_secs, args.min_score)
    report = build_report(clusters, len(alerts))

    # Output
    if args.json:
        print(format_json_report(report))
    elif args.markdown:
        print(format_markdown_report(report))
    else:
        print(format_text_report(report))

    # CI gate: exit 1 if any cluster has critical root cause
    if args.ci:
        critical = any(
            c.get("root_cause", {}).get("severity") == "critical"
            for c in report["clusters"]
        )
        if critical:
            sys.exit(1)


if __name__ == "__main__":
    main()
