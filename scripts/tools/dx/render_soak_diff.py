#!/usr/bin/env python3
"""
render_soak_diff.py — v2.8.0 readiness harness: chaos soak result renderer.

Consumes the output directory of `run_chaos_soak.py` and produces a
markdown report comparing first-sample vs last-sample for each tracked
metric, plus a monotonic-drift verdict per metric.

The verdict per metric:
    [PASS] flat or wandering — no single-direction trend over the run
    [WARN] mild upward drift (5-20% rise from first to last)
    [FAIL] strong upward drift (>20% rise) — likely leak

Usage
-----
    python3 scripts/tools/dx/render_soak_diff.py --input-dir .build/v2.8.0-soak

    # Or write to file:
    python3 scripts/tools/dx/render_soak_diff.py \\
        --input-dir .build/v2.8.0-soak \\
        --output report.md

Exit codes
----------
    0   No FAIL verdicts (release-ready signal)
    1   At least one FAIL verdict OR caller error (missing input)
    2   At least one WARN but no FAIL
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

# How much rise across the run counts as drift?
WARN_THRESHOLD_PCT = 5.0
FAIL_THRESHOLD_PCT = 20.0

# Default warmup window — exclude this fraction of run from drift baseline.
# Go programs go through ~30-60s of cold-start arena allocation that
# inflates first-sample heap stats; without a warmup skip, every short
# soak FAILs trivially.
DEFAULT_WARMUP_PCT = 0.10  # 10%
MIN_WARMUP_SEC = 30
MAX_WARMUP_SEC = 5 * 60

# Some metrics are *expected* to grow — exclude them from drift verdict
# (they still appear in the report for visibility):
MONOTONIC_BY_DESIGN = {
    "process_cpu_seconds_total",       # CPU counter only goes up
    "go_gc_duration_seconds_count",    # GC count only goes up
}


def load_csv(csv_path: Path) -> tuple[list[str], list[list[str]]]:
    if not csv_path.exists():
        return [], []
    with open(csv_path, encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def parse_float(v: str) -> float | None:
    if v == "" or v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def metric_summary(values: list[float]) -> dict:
    """First / last / min / max / mean."""
    if not values:
        return {"first": None, "last": None, "min": None, "max": None, "mean": None}
    return {
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": sum(values) / len(values),
    }


def drift_pct(first: float | None, last: float | None) -> float | None:
    if first is None or last is None or first == 0:
        return None
    return ((last - first) / first) * 100.0


def verdict_for(metric: str, summary: dict) -> str:
    if metric in MONOTONIC_BY_DESIGN:
        return "[N/A]"
    pct = drift_pct(summary.get("first"), summary.get("last"))
    if pct is None:
        return "[N/A]"
    if pct >= FAIL_THRESHOLD_PCT:
        return "[FAIL]"
    if pct >= WARN_THRESHOLD_PCT:
        return "[WARN]"
    return "[PASS]"


def fmt_value(metric: str, v: float | None) -> str:
    if v is None:
        return "-"
    if "memory" in metric or metric.endswith("_bytes"):
        # Render bytes in MiB
        return f"{v / (1024 * 1024):.1f} MiB"
    if "duration" in metric or "seconds" in metric:
        return f"{v:.2f}"
    return f"{v:.0f}"


def compute_warmup_sec(duration_min: int, override_sec: int | None) -> int:
    if override_sec is not None:
        return max(0, override_sec)
    pct_window = int(duration_min * 60 * DEFAULT_WARMUP_PCT)
    return max(MIN_WARMUP_SEC, min(MAX_WARMUP_SEC, pct_window))


def render(input_dir: Path, warmup_sec_override: int | None = None) -> tuple[str, int]:
    """Returns (markdown report, exit code)."""
    run_config_path = input_dir / "run-config.json"
    csv_path = input_dir / "metrics-timeseries.csv"

    if not run_config_path.exists() or not csv_path.exists():
        return f"Error: missing run-config.json or metrics-timeseries.csv in {input_dir}\n", 1

    with open(run_config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    header, rows = load_csv(csv_path)
    if not rows:
        return f"Error: empty timeseries in {csv_path}\n", 1

    warmup_sec = compute_warmup_sec(cfg.get("duration_min", 0), warmup_sec_override)

    # Find metric columns (first 3 are timestamp / elapsed / reload_count)
    metric_cols = header[3:]

    # Per-metric series — only include samples after warmup window
    series: dict[str, list[float]] = {m: [] for m in metric_cols}
    skipped_warmup = 0
    for row in rows:
        if len(row) < len(header):
            continue
        elapsed = parse_float(row[1])
        if elapsed is not None and elapsed < warmup_sec:
            skipped_warmup += 1
            continue
        for idx, m in enumerate(metric_cols):
            v = parse_float(row[3 + idx])
            if v is not None:
                series[m].append(v)

    # Build report
    lines: list[str] = []
    lines.append(f"# v2.8.0 Readiness Soak Report")
    lines.append("")
    lines.append(f"- **Target**: `{cfg['target_url']}`")
    lines.append(f"- **Duration**: {cfg['duration_min']} min "
                 f"({'completed' if cfg['ended_at_utc'] else 'incomplete'})")
    lines.append(f"- **Reload interval**: {cfg['reload_interval_sec']}s "
                 f"({cfg.get('reload_count', 0)} reloads triggered)")
    lines.append(f"- **Metric polls**: {cfg['metrics_poll_sec']}s "
                 f"({cfg.get('poll_count', 0)} samples)")
    lines.append(f"- **Started (UTC)**: {cfg['started_at_utc']}")
    lines.append(f"- **Ended (UTC)**: {cfg['ended_at_utc']}")
    lines.append(f"- **Warmup skip**: first {warmup_sec}s excluded from drift baseline "
                 f"(skipped {skipped_warmup} of {len(rows)} samples)")
    lines.append("")
    lines.append("## Drift verdicts")
    lines.append("")
    lines.append(
        f"Drift = (last - first) / first × 100%, computed across post-warmup samples only. "
        f"WARN ≥ {WARN_THRESHOLD_PCT}%, FAIL ≥ {FAIL_THRESHOLD_PCT}%. "
        f"Counters that monotonically grow by design (CPU seconds, GC count) "
        f"are reported but never get a verdict."
    )
    lines.append("")
    lines.append("| Metric | First | Last | Min | Max | Mean | Drift % | Verdict |")
    lines.append("|---|---|---|---|---|---|---|---|")

    fail_count = 0
    warn_count = 0
    for m in metric_cols:
        s = metric_summary(series[m])
        pct = drift_pct(s["first"], s["last"])
        v = verdict_for(m, s)
        if v == "[FAIL]":
            fail_count += 1
        elif v == "[WARN]":
            warn_count += 1
        pct_str = f"{pct:+.1f}%" if pct is not None else "-"
        lines.append(
            f"| `{m}` | {fmt_value(m, s['first'])} | {fmt_value(m, s['last'])} | "
            f"{fmt_value(m, s['min'])} | {fmt_value(m, s['max'])} | "
            f"{fmt_value(m, s['mean'])} | {pct_str} | {v} |"
        )

    lines.append("")
    lines.append("## Summary")
    lines.append("")
    if fail_count > 0:
        lines.append(f"**❌ FAIL** — {fail_count} metric(s) show strong upward drift "
                     f"(≥ {FAIL_THRESHOLD_PCT}%). Investigate before tagging release.")
        exit_code = 1
    elif warn_count > 0:
        lines.append(f"**⚠️ WARN** — {warn_count} metric(s) show mild upward drift "
                     f"(≥ {WARN_THRESHOLD_PCT}%). Re-run longer to confirm; not a blocker.")
        exit_code = 2
    else:
        lines.append(f"**✅ PASS** — no drift signal under "
                     f"{cfg.get('reload_count', 0)} reloads over {cfg['duration_min']} min.")
        exit_code = 0

    return "\n".join(lines) + "\n", exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--input-dir", required=True,
                        help="Output directory from run_chaos_soak.py")
    parser.add_argument("--output", default=None,
                        help="Optional output file (default: stdout)")
    parser.add_argument("--warmup-sec", type=int, default=None,
                        help=f"Override warmup-skip window in seconds "
                             f"(default: 10%% of run, clamped {MIN_WARMUP_SEC}-{MAX_WARMUP_SEC}s; "
                             f"set 0 to disable)")
    args = parser.parse_args()

    report, exit_code = render(Path(args.input_dir), warmup_sec_override=args.warmup_sec)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"[info] report written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(report)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
