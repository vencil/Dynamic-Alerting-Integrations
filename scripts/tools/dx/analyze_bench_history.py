#!/usr/bin/env python3
"""analyze_bench_history.py — Aggregate bench-record nightly history into per-benchmark stats.

Purpose
-------
Phase 2 readiness gate (issue #67) requires empirical variance analysis over a
sliding 4-week window of nightly ``bench-record`` workflow runs (issue #60
Phase 1, PR #65). This tool downloads recent run artifacts, parses
``bench-baseline.txt`` from each, groups samples per benchmark across runs,
and outputs per-benchmark statistics with a GO/NO-GO verdict against the
gate thresholds defined in #67.

Outputs a Markdown table; exit code reflects gate verdict (0 = all GO,
1 = any NO-GO, 2 = insufficient data / fetch error).

Threshold gate (per #67)
-----------------------
Per benchmark:
  - ``CV ≤ 25%``               (coefficient of variation)
  - ``max_ns / min_ns ≤ 1.30`` (window max-min ratio)

Across the run window:
  - ``≥ 26 of N runs succeeded`` (default N=28; tolerates ~2 GitHub Actions
    outages in 4 weeks)

Usage
-----
::

    # Default: latest 28 runs of bench-record.yaml
    python3 scripts/tools/dx/analyze_bench_history.py

    # Custom window
    python3 scripts/tools/dx/analyze_bench_history.py --limit 14

    # Single run sanity check (no aggregation)
    python3 scripts/tools/dx/analyze_bench_history.py --limit 1 --no-gate

    # CI mode — exit 1 on NO-GO (for #67 review automation)
    python3 scripts/tools/dx/analyze_bench_history.py --ci

    # Cache artifacts locally (skip re-download)
    python3 scripts/tools/dx/analyze_bench_history.py --cache-dir /tmp/bench-cache

Dependencies
------------
- ``gh`` CLI (authenticated; same as ``pr_preflight.py``).
- Python ≥ 3.9 stdlib only — no pandas. Keeps tool runnable in Dev Container
  + Cowork VM + CI runner without extra install.

See also
--------
- issue #60 — 3-phase pre-tag bench gate rollout (informational ↔ hard gate)
- issue #67 — Phase 2 readiness review (this tool's primary consumer)
- ``.github/workflows/bench-record.yaml`` — produces the artifacts this parses
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO = "vencil/Dynamic-Alerting-Integrations"
WORKFLOW_FILE = "bench-record.yaml"
ARTIFACT_FILE = "bench-baseline.txt"

# Gate thresholds (issue #67 §Acceptance gate)
CV_THRESHOLD = 0.25       # coefficient of variation
RATIO_THRESHOLD = 1.30    # max/min
MIN_RUN_RELIABILITY = 26  # of N=28 default

# Bench line: name-vCPU<TAB>iters<TAB>ns/op<TAB>...
# e.g. "BenchmarkScanDirHierarchical_1000-4   93   35422664 ns/op   ..."
_BENCH_RE = re.compile(
    r"^(Benchmark[A-Za-z0-9_]+)-\d+\s+\d+\s+(\d+(?:\.\d+)?)\s+ns/op\b"
)


@dataclass
class RunSample:
    """One ns/op observation from one bench iteration in one run."""

    run_id: int
    bench: str
    ns_per_op: float


@dataclass
class BenchStats:
    """Aggregate stats for one benchmark across the run window."""

    bench: str
    samples: list[float] = field(default_factory=list)
    runs: set[int] = field(default_factory=set)

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else math.nan

    @property
    def cv(self) -> float:
        """Coefficient of variation = stddev / mean."""
        if len(self.samples) < 2:
            return math.nan
        m = statistics.mean(self.samples)
        if m == 0:
            return math.nan
        return statistics.stdev(self.samples) / m

    @property
    def max_min_ratio(self) -> float:
        if not self.samples:
            return math.nan
        lo = min(self.samples)
        if lo == 0:
            return math.nan
        return max(self.samples) / lo

    def verdict(self) -> tuple[str, list[str]]:
        """Returns (GO|NO-GO|INSUFFICIENT, reason_list)."""
        reasons = []
        if self.n_runs < 2:
            return "INSUFFICIENT", [f"only {self.n_runs} run(s) — need ≥ 2 for variance"]
        if self.cv > CV_THRESHOLD:
            reasons.append(f"CV={self.cv:.1%} > {CV_THRESHOLD:.0%}")
        if self.max_min_ratio > RATIO_THRESHOLD:
            reasons.append(f"max/min={self.max_min_ratio:.2f}× > {RATIO_THRESHOLD}×")
        return ("NO-GO" if reasons else "GO", reasons)


def _gh(cmd: list[str], capture: bool = True) -> str:
    """Run a `gh` command; return stdout. Raises CalledProcessError on non-zero."""
    proc = subprocess.run(
        ["gh", *cmd],
        capture_output=capture,
        text=True,
        check=False,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["gh", *cmd], proc.stdout, proc.stderr
        )
    return proc.stdout


def list_recent_runs(workflow: str, limit: int) -> list[dict]:
    """List the N most recent successful workflow runs."""
    out = _gh([
        "run", "list",
        "--workflow", workflow,
        "--repo", REPO,
        "--limit", str(limit),
        "--status", "success",
        "--json", "databaseId,createdAt,headSha,conclusion",
    ])
    return json.loads(out)


def download_artifact(run_id: int, dest_dir: Path) -> Path | None:
    """Download the run's bench-baseline artifact zip; return path to bench-baseline.txt or None."""
    target = dest_dir / f"run-{run_id}"
    txt = target / ARTIFACT_FILE
    if txt.exists():
        return txt  # cached
    target.mkdir(parents=True, exist_ok=True)
    artifact_name = f"bench-baseline-{run_id}"
    try:
        _gh([
            "run", "download", str(run_id),
            "--repo", REPO,
            "--name", artifact_name,
            "--dir", str(target),
        ])
    except subprocess.CalledProcessError as exc:
        print(f"  ⚠️  run {run_id}: download failed — {exc.stderr.strip()}", file=sys.stderr)
        return None
    if not txt.exists():
        print(f"  ⚠️  run {run_id}: artifact missing {ARTIFACT_FILE}", file=sys.stderr)
        return None
    return txt


def parse_bench_file(path: Path, run_id: int) -> Iterable[RunSample]:
    """Yield one RunSample per ns/op observation line."""
    with path.open(encoding="utf-8") as f:
        for line in f:
            m = _BENCH_RE.match(line)
            if m:
                yield RunSample(run_id=run_id, bench=m.group(1), ns_per_op=float(m.group(2)))


def aggregate(samples: Iterable[RunSample]) -> dict[str, BenchStats]:
    by_bench: dict[str, BenchStats] = {}
    for s in samples:
        if s.bench not in by_bench:
            by_bench[s.bench] = BenchStats(bench=s.bench)
        by_bench[s.bench].samples.append(s.ns_per_op)
        by_bench[s.bench].runs.add(s.run_id)
    return by_bench


def format_ns(ns: float) -> str:
    """Human-friendly latency: ns / µs / ms / s."""
    if math.isnan(ns):
        return "—"
    if ns < 1_000:
        return f"{ns:.0f} ns"
    if ns < 1_000_000:
        return f"{ns / 1_000:.1f} µs"
    if ns < 1_000_000_000:
        return f"{ns / 1_000_000:.1f} ms"
    return f"{ns / 1_000_000_000:.2f} s"


def render_markdown_table(
    stats: dict[str, BenchStats],
    n_runs_total: int,
    n_runs_succeeded: int,
) -> str:
    lines = []
    lines.append(
        f"## Bench history analysis — {n_runs_succeeded}/{n_runs_total} runs"
    )
    lines.append("")
    lines.append(
        f"Gate thresholds (issue #67): CV ≤ {CV_THRESHOLD:.0%}, "
        f"max/min ≤ {RATIO_THRESHOLD}×, ≥ {MIN_RUN_RELIABILITY}/{n_runs_total} runs."
    )
    lines.append("")

    # Run reliability gate
    reliability_ok = n_runs_succeeded >= min(MIN_RUN_RELIABILITY, n_runs_total)
    lines.append(
        f"- Run reliability: **{'✅' if reliability_ok else '❌'} "
        f"{n_runs_succeeded}/{n_runs_total}**"
        + (" (below threshold)" if not reliability_ok else "")
    )
    lines.append("")

    # Per-bench table
    lines.append("| Bench | Runs | Samples | Median | CV | max/min | Verdict | Reason |")
    lines.append("|---|---:|---:|---:|---:|---:|---|---|")

    summary = {"GO": 0, "NO-GO": 0, "INSUFFICIENT": 0}
    for name in sorted(stats):
        s = stats[name]
        verdict, reasons = s.verdict()
        summary[verdict] += 1
        emoji = {"GO": "✅", "NO-GO": "❌", "INSUFFICIENT": "⚠️"}[verdict]
        cv_str = f"{s.cv:.1%}" if not math.isnan(s.cv) else "—"
        ratio_str = f"{s.max_min_ratio:.2f}×" if not math.isnan(s.max_min_ratio) else "—"
        reason_str = "; ".join(reasons) if reasons else ""
        lines.append(
            f"| `{s.bench}` | {s.n_runs} | {s.n_samples} | {format_ns(s.median)} "
            f"| {cv_str} | {ratio_str} | {emoji} {verdict} | {reason_str} |"
        )

    lines.append("")
    lines.append(
        f"**Summary**: {summary['GO']} GO, {summary['NO-GO']} NO-GO, "
        f"{summary['INSUFFICIENT']} INSUFFICIENT"
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=28,
                        help="Number of recent successful runs to analyze (default: 28).")
    parser.add_argument("--workflow", default=WORKFLOW_FILE,
                        help=f"Workflow file (default: {WORKFLOW_FILE}).")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="Persist downloaded artifacts here for re-runs (default: tempdir).")
    parser.add_argument("--ci", action="store_true",
                        help="CI mode: exit 1 on any NO-GO, exit 2 on insufficient data.")
    parser.add_argument("--no-gate", action="store_true",
                        help="Show stats without verdict (single-run sanity check).")
    args = parser.parse_args()

    # Verify gh CLI is available
    if not shutil.which("gh"):
        print("error: gh CLI not found in PATH", file=sys.stderr)
        return 2

    # Cache dir setup
    if args.cache_dir:
        cache_dir = args.cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        cache_dir = Path(tempfile.mkdtemp(prefix="bench-history-"))
        cleanup = True

    try:
        print(f"→ listing last {args.limit} successful runs of {args.workflow}…",
              file=sys.stderr)
        runs = list_recent_runs(args.workflow, args.limit)
        if not runs:
            print("error: no successful runs found", file=sys.stderr)
            return 2

        print(f"→ downloading {len(runs)} artifacts (cache: {cache_dir})…",
              file=sys.stderr)
        all_samples: list[RunSample] = []
        succeeded = 0
        for run in runs:
            run_id = run["databaseId"]
            txt = download_artifact(run_id, cache_dir)
            if txt is None:
                continue
            samples = list(parse_bench_file(txt, run_id))
            if not samples:
                print(f"  ⚠️  run {run_id}: parsed 0 bench rows from {txt.name}",
                      file=sys.stderr)
                continue
            all_samples.extend(samples)
            succeeded += 1
            print(f"  ✓ run {run_id}: {len(samples)} samples", file=sys.stderr)

        if not all_samples:
            print("error: no usable samples across the run window", file=sys.stderr)
            return 2

        stats = aggregate(all_samples)
        print(render_markdown_table(stats, n_runs_total=len(runs), n_runs_succeeded=succeeded))

        if args.no_gate:
            return 0

        # CI mode: exit non-zero on NO-GO
        verdicts = [s.verdict()[0] for s in stats.values()]
        if any(v == "NO-GO" for v in verdicts):
            return 1 if args.ci else 0
        if all(v == "INSUFFICIENT" for v in verdicts):
            return 2 if args.ci else 0
        return 0

    finally:
        if cleanup:
            shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
