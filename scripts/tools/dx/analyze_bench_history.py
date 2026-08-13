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
Per benchmark, computed across **per-run medians** (not raw samples — see
``BenchStats`` docstring for methodology):

  - cross-run ``CV ≤ 25%``               (stddev / mean of the per-run medians)
  - cross-run ``max_ns / min_ns ≤ 1.30`` (max/min of the per-run medians)

Across the run window:
  - ``≥ 26 of N runs succeeded`` (default N=28; tolerates ~2 GitHub Actions
    outages in 4 weeks)

Within-run CV (variance among the 6 samples *inside* one run) is reported
as a separate column for diagnostics but does **not** affect the verdict —
median-of-samples per run absorbs within-run jitter, which is the entire
point of the median-of-5 framing in issue #60 §Phase 2.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Two sys.path inserts: parent (`scripts/tools/`) for the repo layout
# where _lib_compat.py lives one directory up, and self-dir for the
# Docker flat layout where every file sits in /app/. analyze_bench_history
# is NOT bundled into the Docker image (dev-only tool), so only the
# parent insert is functionally required; the self-dir insert is kept
# for parity with sibling ops/ tools that do get bundled.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402

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

# Suite header emitted by `go test -bench`, e.g.
#   "cpu: AMD EPYC 7763 64-Core Processor"
# `bench_filter.go` has always RETAINED this line in the artifact (see its
# `retainPrefixes`); until #1396 nothing ever read it. The nightly runner pool is
# heterogeneous (Intel Xeon / AMD EPYC 7763 / AMD EPYC 9V74 observed in a single
# 30-night window, with IO/CPU ratios 11.8 / 19.1 / 25.3 — completely separated),
# so this string is the stratification key for the trend watchdog.
_CPU_RE = re.compile(r"^cpu:\s*(\S.*?)\s*$")


@dataclass
class RunSample:
    """One ns/op observation from one bench iteration in one run."""

    run_id: int
    bench: str
    ns_per_op: float


@dataclass
class BenchStats:
    """Aggregate stats for one benchmark across the run window.

    Stats methodology
    -----------------
    The gate question is: *does this benchmark's typical-night latency move
    between nights?* That requires **cross-run variance**, not within-run
    jitter.

    1. Group samples by ``run_id`` → compute median per run (e.g., 6
       samples per run becomes 1 representative number).
    2. Variance / CV / max-min are computed across the **per-run medians**,
       not raw samples.
    3. Within-run jitter is reported separately as ``within_run_cv_mean``
       (mean of per-run CVs) for transparency / outlier diagnosis.

    This matches issue #60 §Phase 2's "3× of median-of-5" framing: median
    smooths within-run jitter, cross-run CV is the regression signal.
    """

    bench: str
    samples: list[float] = field(default_factory=list)
    runs: set[int] = field(default_factory=set)
    # Map run_id -> list of samples in that run, populated by aggregate()
    samples_by_run: dict[int, list[float]] = field(default_factory=dict)

    @property
    def n_samples(self) -> int:
        return len(self.samples)

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    @property
    def per_run_medians(self) -> list[float]:
        """One representative ns/op per run."""
        return [statistics.median(s) for s in self.samples_by_run.values() if s]

    @property
    def median(self) -> float:
        """Median of per-run medians (typical-night latency over the window)."""
        m = self.per_run_medians
        return statistics.median(m) if m else math.nan

    @property
    def cv(self) -> float:
        """Cross-run coefficient of variation: stddev(per-run medians) / mean(...).

        Returns NaN if fewer than 2 runs (variance undefined).
        """
        m = self.per_run_medians
        if len(m) < 2:
            return math.nan
        mean = statistics.mean(m)
        if mean == 0:
            return math.nan
        return statistics.stdev(m) / mean

    @property
    def max_min_ratio(self) -> float:
        """Cross-run max/min ratio of per-run medians."""
        m = self.per_run_medians
        if not m:
            return math.nan
        lo = min(m)
        if lo == 0:
            return math.nan
        return max(m) / lo

    @property
    def within_run_cv_mean(self) -> float:
        """Mean of per-run within-run CVs. High value = bench is jittery in any single run."""
        cvs = []
        for run_samples in self.samples_by_run.values():
            if len(run_samples) >= 2:
                mean = statistics.mean(run_samples)
                if mean > 0:
                    cvs.append(statistics.stdev(run_samples) / mean)
        return statistics.mean(cvs) if cvs else math.nan

    def verdict(self) -> tuple[str, list[str]]:
        """Returns (GO|NO-GO|INSUFFICIENT, reason_list).

        Verdict is based on cross-run CV + max/min, NOT within-run noise.
        Within-run noise is informational only.
        """
        reasons = []
        if self.n_runs < 2:
            return "INSUFFICIENT", [f"only {self.n_runs} run(s) — need ≥ 2 for cross-run variance"]
        if self.cv > CV_THRESHOLD:
            reasons.append(f"cross-run CV={self.cv:.1%} > {CV_THRESHOLD:.0%}")
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
        timeout=120,
    )
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, ["gh", *cmd], proc.stdout, proc.stderr
        )
    return proc.stdout


def list_recent_runs(workflow: str, limit: int) -> list[dict]:
    """List the N most recent successful workflow runs.

    Raises ``RuntimeError`` with a friendly message if ``gh`` is unauthenticated
    or the workflow doesn't exist.
    """
    try:
        out = _gh([
            "run", "list",
            "--workflow", workflow,
            "--repo", REPO,
            "--limit", str(limit),
            "--status", "success",
            "--json", "databaseId,createdAt,headSha,conclusion",
        ])
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        if "authentication" in stderr.lower() or "gh auth login" in stderr:
            raise RuntimeError(
                "gh is not authenticated. Run `gh auth login` first."
            ) from exc
        raise RuntimeError(
            f"`gh run list --workflow {workflow}` failed: {stderr or 'no stderr'}"
        ) from exc
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


def parse_cpu_model(path: Path) -> str | None:
    """Return the runner's CPU model string from the artifact's `cpu:` header.

    The RAW string is the classification key — deliberately NOT normalised into
    "Xeon" / "EPYC" families. Two Xeon SKUs (e.g. `Platinum 8370C` vs
    `PLATINUM 8573C`) are different machines with different latency levels;
    folding them together would re-create the very cross-host anchor that made
    #1396 a guaranteed false positive. Exact string equality, or nothing.

    Returns None when the artifact predates the header being read (legacy runs)
    or the line is absent — callers must degrade loudly, never silently.
    """
    with path.open(encoding="utf-8") as f:
        for line in f:
            m = _CPU_RE.match(line)
            if m:
                return m.group(1)
    return None


def aggregate(samples: Iterable[RunSample]) -> dict[str, BenchStats]:
    by_bench: dict[str, BenchStats] = {}
    for s in samples:
        if s.bench not in by_bench:
            by_bench[s.bench] = BenchStats(bench=s.bench)
        bs = by_bench[s.bench]
        bs.samples.append(s.ns_per_op)
        bs.runs.add(s.run_id)
        bs.samples_by_run.setdefault(s.run_id, []).append(s.ns_per_op)
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
    # Cross-run CV is the gate signal; within-run CV is informational
    # (high within-run CV alone does NOT fail the gate, but worth flagging).
    lines.append(
        "| Bench | Runs | Samples | Median | Cross-run CV | max/min | Within-run CV | Verdict | Reason |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---|---|")

    summary = {"GO": 0, "NO-GO": 0, "INSUFFICIENT": 0}
    for name in sorted(stats):
        s = stats[name]
        verdict, reasons = s.verdict()
        summary[verdict] += 1
        emoji = {"GO": "✅", "NO-GO": "❌", "INSUFFICIENT": "⚠️"}[verdict]
        cv_str = f"{s.cv:.1%}" if not math.isnan(s.cv) else "—"
        ratio_str = f"{s.max_min_ratio:.2f}×" if not math.isnan(s.max_min_ratio) else "—"
        within_cv_str = (
            f"{s.within_run_cv_mean:.1%}" if not math.isnan(s.within_run_cv_mean) else "—"
        )
        reason_str = "; ".join(reasons) if reasons else ""
        lines.append(
            f"| `{s.bench}` | {s.n_runs} | {s.n_samples} | {format_ns(s.median)} "
            f"| {cv_str} | {ratio_str} | {within_cv_str} | {emoji} {verdict} | {reason_str} |"
        )

    lines.append("")
    lines.append(
        f"**Summary**: {summary['GO']} GO, {summary['NO-GO']} NO-GO, "
        f"{summary['INSUFFICIENT']} INSUFFICIENT"
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Trend watchdog (--trend-watch) — nightly sustained-regression detection.
#
# Distinct from the GO/NO-GO variance gate above (which asks "is this bench
# stable enough to gate on?"). The watchdog asks "has main's nightly perf
# actually DRIFTED?" and, if so, opens/updates a `perf-trend` GitHub issue —
# and auto-closes it when perf recovers (closed loop). Two detection rules so a
# single-night blip never fires and a slow multi-night creep never hides:
#
#   R1 sustained — ALL of the most recent K nights sit ≥ floor above an
#       ANCHORED baseline (median of the older, settled nights). Anchoring to
#       the settled window (not "vs yesterday") is what makes creep visible.
#   R2 creep — the recent window's MEDIAN sits ≥ creep_floor above the SAME
#       settled-window-median anchor used by sustained. This tolerates one noisy
#       recent night (which sustained's all() would let hide a real step-change)
#       while staying robust to a lone anomalously-fast night — a raw-`min`
#       baseline let one fast night pin the floor so a flat series crept forever
#       and the closed-loop issue never closed (#702). (A 14-night window can't
#       accumulate a multi-week +X%/night creep past the floor anyway, so the
#       only honest signal here is recent-typical vs settled-typical.)
#
# Both rules use the recent *window* (not just tonight), so neither fires on a
# lone bad night. The floor is the max of a fixed minimum and a multiple of the
# control canary's own night-to-night CV — movement smaller than the runner's
# intrinsic noise (as measured by BenchmarkControlCanaryCPU) is never alerted.
#
# Three things about the POPULATION and the CLOSED LOOP were wrong until #1396:
#
#   1. Host class (B) — GitHub's hosted runners are heterogeneous, and the `cpu:`
#      header that says which machine ran was parsed by nobody. It is now read,
#      carried on NightRecord, and disclosed in the issue body so a false alarm
#      is a one-minute read instead of a re-download of 14 artifacts.
#   2. Stratification + three states (C) — both windows are drawn from tonight's
#      host class only, and a night that cannot be judged is INCONCLUSIVE, which
#      is neither "regressed" nor "recovered". Previously `findings == []` closed
#      every open issue, so a night with no evaluable data announced recovery.
#   3. Frozen anchor (D) — the sliding anchor is right for DETECTION and fatal
#      for RECOVERY: a permanent regression ages into the settled window, the
#      anchor rises to meet it, the finding vanishes, and the issue self-closed
#      claiming perf "has returned below the floor". Replaying the real 30-night
#      series with a +20% permanent step: 184 of 259 issues (71%) closed
#      themselves while still fully regressed, median 6 nights after filing — and
#      that is a LOWER bound, since a self-close falling past the end of the
#      series counts here as "never closed". Closing now requires beating the
#      anchor frozen the night the bench first fired, on the same host class;
#      the same replay then yields 0 of 236.
#
# The FIRE arithmetic itself is untouched by all three.
# ─────────────────────────────────────────────────────────────────────────────

CANARY_BENCH = "BenchmarkControlCanaryCPU"
PERF_TREND_LABEL = "perf-trend"
# Applied when every current finding is `creep` (the sustained regression has
# cleared but a softer creep remains) so subscribers can tell "still degraded"
# from "on the mend" at a glance. Removed again the moment any `sustained`
# finding reappears or the issue closes.
RECOVERING_LABEL = "perf-trend:recovering"

# Three-state verdict for one night (see `analyze_trend`). "no findings" is NOT
# one state but two, and conflating them is what let a night on which NOTHING
# could be evaluated close a still-open regression issue.
STATUS_FINDINGS = "FINDINGS"          # ≥1 bench above its floor → open/update
STATUS_CLEAR = "CLEAR"                # ≥1 bench evaluated, none above floor
STATUS_INCONCLUSIVE = "INCONCLUSIVE"  # nothing evaluable → never fire, NEVER close

# Same-class settled nights required before a bench may be judged at all, once
# host-class stratification is active. Empirically calibrated, not guessed: on
# the real 30-night series a 2-night same-class anchor still produced 19
# false-positive bench-nights (the whole 2026-08-12 / #1396 window fires again,
# because a 2-sample median on a 12.7–19.0%-CV bench is not a central tendency);
# at 3 the same replay yields ZERO false positives, for 0.4 pp of detection power
# at δ=20% (83.5% → 83.1%). The unstratified fallback keeps its historical
# threshold of 2 so legacy behaviour is bit-for-bit unchanged.
MIN_SETTLED_SAME_CLASS = 3


@dataclass
class NightRecord:
    """One nightly run reduced to a per-bench median ns/op.

    ``cpu_model`` is the runner's raw `cpu:` string (None for legacy artifacts /
    parse failure). It is the stratification key: nights measured on a different
    CPU model are a different population, not a trend.
    """

    run_id: int
    created_at: str
    medians: dict[str, float] = field(default_factory=dict)
    cpu_model: str | None = None


def _cv(values: list[float]) -> float:
    """Coefficient of variation; 0.0 if < 2 points or mean 0 (treated as no signal)."""
    vals = [v for v in values if not math.isnan(v)]
    if len(vals) < 2:
        return 0.0
    mean = statistics.mean(vals)
    if mean == 0:
        return 0.0
    return statistics.stdev(vals) / mean


def _cpu_class_counts(nights: list[NightRecord]) -> dict[str, int]:
    """How many nights of the window came from each host class.

    ``None`` (unknown class) is reported under the literal key "unknown" so a
    partially-labelled window is visible rather than silently merged.
    """
    counts: dict[str, int] = {}
    for n in nights:
        key = n.cpu_model if n.cpu_model is not None else "unknown"
        counts[key] = counts.get(key, 0) + 1
    return counts


def night_records_from_gh(workflow: str, limit: int, cache_dir: Path) -> list[NightRecord]:
    """Fetch the last `limit` nightly runs and reduce each to per-bench medians."""
    runs = list_recent_runs(workflow, limit)
    # gh returns newest-first, but sort explicitly so the series is deterministic.
    runs.sort(key=lambda r: r["createdAt"], reverse=True)
    nights: list[NightRecord] = []
    for run in runs:
        run_id = run["databaseId"]
        txt = download_artifact(run_id, cache_dir)
        if txt is None:
            continue
        by_bench: dict[str, list[float]] = {}
        for s in parse_bench_file(txt, run_id):
            by_bench.setdefault(s.bench, []).append(s.ns_per_op)
        if not by_bench:
            continue
        nights.append(NightRecord(
            run_id=run_id,
            created_at=run["createdAt"],
            medians={b: statistics.median(v) for b, v in by_bench.items()},
            cpu_model=parse_cpu_model(txt),
        ))
    return nights


def night_records_from_fixture(path: Path) -> list[NightRecord]:
    """Load pre-reduced nightly medians from a JSON fixture (offline testing).

    Format: a JSON list of
    {"run_id", "createdAt", "benches": {name: median_ns}, "cpu_model": str?}.
    ``cpu_model`` is optional — absent means None, i.e. the unstratified
    fallback (same as a legacy artifact with no `cpu:` header).
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    nights = [
        NightRecord(run_id=int(d["run_id"]), created_at=d["createdAt"],
                    medians={k: float(v) for k, v in d["benches"].items()},
                    cpu_model=(str(d["cpu_model"])
                               if d.get("cpu_model") is not None else None))
        for d in data
    ]
    nights.sort(key=lambda n: n.created_at, reverse=True)
    return nights


@dataclass
class TrendFinding:
    bench: str
    kind: str          # "sustained" | "creep"
    today_ns: float
    anchor_ns: float
    recent_typical_ns: float
    pct_vs_anchor: float
    pct_typical_vs_anchor: float


def analyze_trend(
    nights: list[NightRecord],
    recent_k: int,
    min_floor_pct: float,
    canary_floor_mult: float,
    creep_floor_pct: float,
) -> tuple[list[TrendFinding], dict]:
    """Apply R1 (sustained) + R2 (creep) to newest-first nightly medians.

    Returns (findings, meta). `meta` carries the computed floors + canary CV for
    transparency in the rendered issue body, plus the host-class stratification
    facts and the three-state ``status`` (see STATUS_* below).

    Host-class stratification (#1396)
    ---------------------------------
    GitHub's hosted runner pool is heterogeneous. Over one real 30-night window
    the same workflow landed on Intel Xeon 8370C, Xeon 8573C, AMD EPYC 7763 and
    AMD EPYC 9V74 — with IO/CPU ratios of 11.8 / 19.1 / 25.3, i.e. completely
    separated populations. A "sliding anchor" built from a mixed-host settled
    window therefore compares CPUs, not commits: it fired on 3 benches whose real
    night-to-night CV *within* a host class was 12.7–19.0%, while the sustained
    floor was pinned at 10% by ``CANARY_FLOOR_CAP`` — a guaranteed, repeating
    false positive that no threshold tweak can fix.

    So BOTH windows are drawn from tonight's host class only: `recent` = the
    newest ``recent_k`` same-class nights, `baseline` = the same-class nights
    behind them. The *fire* arithmetic below is byte-for-byte unchanged; only the
    population it runs on is corrected.
    """
    today_cpu = nights[0].cpu_model if nights else None
    stratified = today_cpu is not None
    # Unknown host class (legacy artifact / `cpu:` parse failure) → fall back to
    # the historical unstratified behaviour, and say so loudly in the body.
    series = [n for n in nights if n.cpu_model == today_cpu] if stratified else list(nights)
    min_settled = MIN_SETTLED_SAME_CLASS if stratified else 2

    canary_series = [n.medians[CANARY_BENCH] for n in series if CANARY_BENCH in n.medians]
    canary_cv = _cv(canary_series)
    # Floors as fractions. The canary raises the floor above the fixed minimum so
    # we never alert below the runner's own measured noise — but its contribution
    # is CAPPED: a genuinely noisy runner must not be able to inflate the floor so
    # high that real sustained regressions are silenced (the watchdog's worst
    # failure mode — failing toward silence exactly when the runner is noisy).
    CANARY_FLOOR_CAP = 0.10  # ≤ 10 pp of canary-driven floor for the sustained rule
    # The creep rule is the NOISE-PRONE one (recent-median vs the settled anchor):
    # a noisy runner widens that gap, so on a noisy runner the creep floor must
    # RISE. Its cap is therefore higher than the sustained cap. Sharing the
    # sustained cap made the canary term a NO-OP for creep — the cap equalled the
    # 10% creep default, so max(0.10, ≤0.10) ≡ 0.10 — letting runner noise leak
    # straight into spurious creep findings (issue #702).
    CREEP_CANARY_FLOOR_CAP = 0.20
    canary_contrib = min(canary_floor_mult * canary_cv, CANARY_FLOOR_CAP)
    creep_canary_contrib = min(canary_floor_mult * canary_cv, CREEP_CANARY_FLOOR_CAP)
    floor = max(min_floor_pct / 100.0, canary_contrib)
    creep_floor = max(creep_floor_pct / 100.0, creep_canary_contrib)

    findings: list[TrendFinding] = []
    inconclusive: list[str] = []
    evaluated: list[str] = []
    benches = {b for n in series for b in n.medians} - {CANARY_BENCH}
    for bench in sorted(benches):
        # Align to CALENDAR nights (newest-first), NOT "nights that happen to
        # contain this bench". Positional slicing of a gap-filtered series would
        # let a bench that STOPPED reporting — the classic symptom of a perf
        # timeout/crash — collapse so an old night masquerades as "today" and a
        # real spike hides in the baseline window. Require the bench present in
        # ALL recent_k newest same-class nights (so `today` is genuinely tonight)
        # and in ≥ min_settled older same-class nights (for a settled anchor).
        recent = [n.medians.get(bench) for n in series[:recent_k]]
        if len(recent) < recent_k or any(v is None for v in recent):
            inconclusive.append(bench)
            continue
        baseline_vals = [n.medians[bench] for n in series[recent_k:] if bench in n.medians]
        if len(baseline_vals) < min_settled:
            inconclusive.append(bench)
            continue
        evaluated.append(bench)
        anchor = statistics.median(baseline_vals)
        today = recent[0]
        recent_typical = statistics.median(recent)

        # Both rules anchor to the SETTLED-window median — a robust central
        # tendency, immune to the lone anomalously-fast night that a raw-`min`
        # low-watermark let pin the creep baseline forever (the #702 trap: a
        # single fast night → every later night reads as "+X% vs best" → creep
        # fires perpetually → the closed-loop issue never closes). In a 14-night
        # window a multi-week thermal creep can't accumulate past the floor
        # anyway, so the only honest signal here is "recent typical vs settled
        # typical". sustained = ALL recent nights up (consistent); creep = the
        # recent MEDIAN up (tolerates one noisy night that would hide a real
        # step-change from sustained's all()).
        sustained = anchor > 0 and all(x >= anchor * (1 + floor) for x in recent)
        creep = anchor > 0 and recent_typical >= anchor * (1 + creep_floor)
        if sustained or creep:
            findings.append(TrendFinding(
                bench=bench,
                kind="sustained" if sustained else "creep",
                today_ns=today,
                anchor_ns=anchor,
                recent_typical_ns=recent_typical,
                pct_vs_anchor=(today / anchor - 1) * 100 if anchor else float("nan"),
                pct_typical_vs_anchor=(recent_typical / anchor - 1) * 100 if anchor else float("nan"),
            ))
    # Three-state verdict. The critical distinction is CLEAR vs INCONCLUSIVE:
    # "no findings" used to mean "recovered → close every open issue", which is a
    # lie whenever nothing could be evaluated at all (too few same-class nights).
    # Only CLEAR — at least one bench actually measured, none above its floor —
    # may ever close an issue.
    if findings:
        status = STATUS_FINDINGS
    elif evaluated:
        status = STATUS_CLEAR
    else:
        status = STATUS_INCONCLUSIVE
    meta = {
        "canary_cv": canary_cv,
        "floor_pct": floor * 100,
        "creep_floor_pct": creep_floor * 100,
        "n_nights": len(nights),
        "recent_k": recent_k,
        "status": status,
        "stratified": stratified,
        "today_cpu_model": today_cpu,
        "cpu_class_counts": _cpu_class_counts(nights),
        "n_class_nights": len(series),
        "min_settled": min_settled,
        "evaluated_benches": sorted(evaluated),
        "inconclusive_benches": sorted(inconclusive),
    }
    return findings, meta


def _signed_pct(pct: float) -> str:
    """Signed percent, e.g. '+5.4%' / '-1.2%'. Avoids the '+-1.2%' double-sign a
    hard-coded '+' prefix produced for below-anchor findings (issue #702)."""
    if math.isnan(pct):
        return "—"
    return f"{pct:+.1f}%"


def _render_host_class_lines(meta: dict) -> list[str]:
    """The #1396 one-minute triage block: which machine ran tonight, and what the
    window is actually made of. Without this an operator cannot tell a real
    regression from a runner-pool reshuffle without re-downloading 14 artifacts."""
    counts = meta.get("cpu_class_counts") or {}
    comp = ", ".join(
        f"`{model}` ×{n}"
        for model, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ) or "—"
    host = meta.get("today_cpu_model")
    if meta.get("stratified") and host:
        return [
            f"- Host class tonight: **`{host}`** — "
            f"**{meta.get('n_class_nights', '?')}** of the "
            f"**{meta.get('n_nights', '?')}** window nights ran on it. "
            f"Window composition: {comp}.",
            f"- Same-class stratification: **ON** — both the recent window and the anchor "
            f"use only `{host}` nights (min {meta.get('min_settled', MIN_SETTLED_SAME_CLASS)} "
            "same-class settled nights, else the bench is INCONCLUSIVE and never fires). "
            "A mixed-host anchor compares CPUs, not commits (#1396).",
        ]
    return [
        "- Host class tonight: **unknown** — the artifact carries no `cpu:` header, or it "
        f"failed to parse. Window composition: {comp}.",
        "- Same-class stratification: **OFF — this verdict is NOT stratified.** The anchor "
        "may mix runner hardware, so a finding here can be a host-class artefact rather than "
        "a real regression. Check the `cpu:` line of the linked runs before acting (#1396).",
    ]


def render_trend_issue_body(findings: list[TrendFinding], meta: dict,
                            frozen: dict[str, tuple[float, str | None]] | None = None) -> str:
    """Render the issue body. ``frozen`` carries the anchors already frozen by a
    previous night (parsed from the open issue's marker) so they are preserved
    verbatim — the frozen anchor must never re-baseline onto the drifted level."""
    lines = [
        "## Nightly bench trend regression",
        "",
        f"Detected across the last **{meta['n_nights']}** nightly `bench-record` runs.",
        "",
        f"- Effective floor: **{meta['floor_pct']:.1f}%** "
        f"(max of fixed minimum and canary-noise-scaled); "
        f"creep floor: **{meta['creep_floor_pct']:.1f}%**.",
        f"- Control-canary night-to-night CV: **{meta['canary_cv']:.2%}** "
        f"(`{CANARY_BENCH}`; movement below the floor is indistinguishable from runner noise).",
        f"- Sustained rule = all {meta['recent_k']} most-recent nights above the anchored "
        "(settled-window-median) baseline; creep rule = the recent-window MEDIAN above that "
        "same anchor (catches a step-change that a single noisy night hides from `sustained`).",
    ]
    lines += _render_host_class_lines(meta)
    skipped = meta.get("inconclusive_benches") or []
    if skipped:
        lines.append(
            f"- INCONCLUSIVE (not judged tonight — too few same-class nights): "
            + ", ".join(f"`{b}`" for b in skipped)
        )
    lines += [
        "",
        "| Bench | Rule | Today | today vs anchor | recent-median vs anchor |",
        "|---|---|---:|---:|---:|",
    ]
    for f in findings:
        lines.append(
            f"| `{f.bench}` | {f.kind} | {format_ns(f.today_ns)} "
            f"| {_signed_pct(f.pct_vs_anchor)} | {_signed_pct(f.pct_typical_vs_anchor)} |"
        )
    lines += [
        "",
        "_Auto-filed by `analyze_bench_history.py --trend-watch`. The watchdog updates this "
        "issue **in place** each night (no comment spam) and only comments when the set of "
        "flagged benchmarks changes; it auto-closes only when tonight's numbers fall back "
        "below the FROZEN baseline captured when the issue was filed, measured on the same "
        "host class (closed loop). Single-night blips are filtered by the multi-night window; "
        "movement below the canary noise floor is ignored._",
        "",
        _render_state_marker(
            _frozen_state(findings, frozen or {}, meta.get("today_cpu_model"))
        ),
    ]
    return "\n".join(lines)


# ── Stateful issue lifecycle (update-in-place + transition-only comments) ──────
# The watchdog is stateless across nightly runs, so the PRIOR state is persisted
# in the issue body as a hidden HTML-comment marker and parsed back next night.
# This lets us refresh the body every night (always current) yet comment ONLY on
# a real transition — killing the daily comment spam that made #702 unreadable.
# Greedy within the marker's own line (no DOTALL) so a nested JSON array
# `[["x","y"]]` is captured whole, up to the last `]` before `-->`. Greedy is
# safe here because the payload is json.dumps of rows whose free-form component
# is only the runner's `cpu:` string (e.g. "AMD EPYC 7763 64-Core Processor") —
# it contains no `-->`, quote or unbalanced bracket that could let the match
# overrun, and any payload that somehow did would fail json.loads and degrade to
# "no prior state" (silent, never a false recovery).
#
# v1 → v2 (#1396). v1 rows are [bench, kind]; v2 rows are
# [bench, kind, frozen_anchor_ns, frozen_cpu_model] — the anchor that was in
# force the FIRST night the bench fired, plus the host class it was measured on.
# v1 markers on already-open issues MUST keep parsing (migration), and do: the
# version is not pinned in the regex and row length is checked per row. A v1 row
# simply carries no frozen anchor, so that bench keeps the pre-#1396 close
# behaviour instead of being wedged open forever.
_STATE_MARKER_RE = re.compile(r"<!--\s*perf-trend-state v\d+\s*(\[.*\])\s*-->")
_STATE_MARKER_VERSION = "v2"


def _finding_state(findings: list[TrendFinding]) -> list[list[str]]:
    """Canonical, order-independent state: sorted [bench, kind] pairs."""
    return sorted([f.bench, f.kind] for f in findings)


def _frozen_state(findings: list[TrendFinding],
                  prior_frozen: dict[str, tuple[float, str | None]],
                  today_cpu: str | None) -> list[list]:
    """v2 rows: [bench, kind, frozen_anchor_ns, frozen_cpu_model].

    A bench already present in ``prior_frozen`` KEEPS its original anchor — that
    is the whole point. Re-freezing onto tonight's sliding anchor would re-create
    the bug this fixes: the anchor creeps up to the regressed level, the finding
    silently clears, and the issue closes itself claiming recovery.
    """
    rows: list[list] = []
    for f in findings:
        anchor, cpu = prior_frozen.get(f.bench, (None, None))
        if anchor is None:
            anchor, cpu = f.anchor_ns, today_cpu
        rows.append([f.bench, f.kind, anchor, cpu])
    return sorted(rows, key=lambda r: (r[0], r[1]))


def _render_state_marker(state: list[list]) -> str:
    return (f"<!-- perf-trend-state {_STATE_MARKER_VERSION} "
            f"{json.dumps(state, separators=(',', ':'))} -->")


def _state_marker_rows(body: str | None) -> list | None:
    """Raw marker rows (v1 2-tuples or v2 4-tuples), or None if absent/garbage."""
    if not body:
        return None
    m = _STATE_MARKER_RE.search(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    for row in data:
        if not isinstance(row, list) or len(row) < 2:
            return None
    return data


def _parse_state_marker(body: str | None) -> list[list[str]] | None:
    """Recover the prior flagged-set from an issue body, or None if
    absent/unparseable (a legacy issue filed before this marker existed → caller
    treats as no-change so the migration run is silent).

    Version-agnostic on purpose: only [bench, kind] is needed for the transition
    comment, and both v1 and v2 rows start with exactly that pair.
    """
    rows = _state_marker_rows(body)
    if rows is None:
        return None
    return sorted([str(r[0]), str(r[1])] for r in rows)


def _parse_frozen_anchors(body: str | None) -> dict[str, tuple[float, str | None]]:
    """bench → (frozen_anchor_ns, frozen_cpu_model) from a v2 marker.

    Empty dict for a v1 / absent / unparseable marker — i.e. "no frozen baseline
    is known", which the close path reads as "fall back to the pre-#1396
    behaviour for this issue" rather than "wedge it open".
    """
    rows = _state_marker_rows(body) or []
    out: dict[str, tuple[float, str | None]] = {}
    for row in rows:
        if len(row) < 4:
            continue
        try:
            anchor = float(row[2])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(anchor) or anchor <= 0:
            continue
        cpu = str(row[3]) if row[3] is not None else None
        out[str(row[0])] = (anchor, cpu)
    return out


def _recovery_blockers(frozen: dict[str, tuple[float, str | None]],
                       tonight: dict[str, float],
                       today_cpu: str | None,
                       floor: float) -> list[str]:
    """Reasons NOT to close a perf-trend issue. Empty list = safe to close.

    Why a frozen baseline is required (#1396, the Critical half)
    -----------------------------------------------------------
    The fire path anchors on the SLIDING settled-window median, which is correct
    for detection but fatal for recovery: once a permanent regression has aged
    into the settled window, the anchor rises to meet it, the finding evaporates,
    and the watchdog closed the issue announcing "perf has returned below the
    floor". Replaying the real 30-night series with a +20% permanent step, that
    happened to 184 of 259 opened issues (71%, median 6 nights, and a lower bound
    — a self-close past the end of the series counts as "never closed"); with
    this check the same replay closes 0 of 236. Recovery is judged ONLY against the
    anchor frozen when the bench first fired, and only against a night measured
    on the same host class — cross-class numbers are not comparable, so they are
    not evidence of anything.
    """
    blockers: list[str] = []
    for bench, (anchor, cpu) in sorted(frozen.items()):
        if cpu is not None and cpu != today_cpu:
            blockers.append(
                f"`{bench}`: frozen baseline was measured on `{cpu}`, tonight ran on "
                f"`{today_cpu or 'unknown'}` — not comparable"
            )
            continue
        value = tonight.get(bench)
        if value is None:
            blockers.append(f"`{bench}`: absent from tonight's run — nothing to compare")
            continue
        if value >= anchor * (1 + floor):
            blockers.append(
                f"`{bench}`: {format_ns(value)} still ≥ frozen baseline "
                f"{format_ns(anchor)} × (1 + {floor:.1%})"
            )
    return blockers


def _close_comment(frozen: dict[str, tuple[float, str | None]],
                   tonight: dict[str, float],
                   today_cpu: str | None,
                   floor: float) -> str:
    """The auto-close comment. Deliberately narrow: it may only claim what the
    watchdog actually measured.

    The old text — "Nightly perf has returned below the floor across the recent
    window" — was false in the common case. "The floor" was the SLIDING anchor,
    which by then had drifted up onto the regressed level, so the sentence
    asserted a recovery that never happened. What is now said instead is exactly
    what was compared: these benchmarks, against the baseline frozen when the
    issue was filed, on this host class.
    """
    if not frozen:
        # v1 / legacy marker: no frozen baseline exists, so only the weaker
        # sliding-anchor statement is available — and it is labelled as such.
        return (
            "✅ Auto-closing (closed loop). This issue predates the frozen-baseline marker "
            "(#1396), so recovery could only be judged against the *sliding* settled-window "
            "anchor: no benchmark is currently above its floor. That anchor drifts with the "
            "data, so this is weaker evidence than a frozen-baseline close. A new issue is "
            "filed if it regresses again."
        )
    detail = ", ".join(
        f"`{b}` {format_ns(tonight[b])} < {format_ns(a)} × (1 + {floor:.1%})"
        for b, (a, _c) in sorted(frozen.items()) if b in tonight
    )
    return (
        "✅ Auto-closing (closed loop). Tonight's medians are back below the **frozen "
        "baseline** captured when this issue was filed, measured on the same host class "
        f"(`{today_cpu or 'unknown'}`): {detail}.\n\n"
        "_Scope: this compares those benchmarks against that frozen baseline. It is not a "
        "claim about any other benchmark, host class, or the overall health of nightly perf. "
        "A new issue is filed if it regresses again._"
    )


def _is_recovering(findings: list[TrendFinding]) -> bool:
    """True when there are findings but every one is `creep` — the sustained
    regression has cleared and only the softer creep signal remains."""
    return bool(findings) and all(f.kind == "creep" for f in findings)


def _recovering_label_change(findings: list[TrendFinding], prior_state: list[list[str]],
                             current_labels: list[str]) -> str | None:
    """'add' / 'remove' / None for the recovering label.

    The label means "the sustained regression has cleared, only creep remains",
    so it is added only on a sustained→creep transition (the PRIOR state carried a
    sustained finding) and PERSISTS through subsequent creep-only nights (already
    labelled), and is removed the moment any sustained returns. A creep-from-start
    issue that was never sustained therefore never acquires it. `current_labels`
    gates the gh call so we never fire a redundant add/remove."""
    labelled = RECOVERING_LABEL in current_labels
    prior_had_sustained = any(k == "sustained" for _, k in prior_state)
    recovering = _is_recovering(findings) and (prior_had_sustained or labelled)
    if recovering and not labelled:
        return "add"
    if not recovering and labelled:
        return "remove"
    return None


def _state_transition_comment(prior: list[list[str]], current: list[list[str]]) -> str | None:
    """Markdown summary of how the flagged-benchmark set changed since the prior
    run, or None when nothing changed (→ body refreshed silently, no comment)."""
    if prior == current:
        return None
    prior_kind = {b: k for b, k in prior}
    cur_kind = {b: k for b, k in current}
    newly = sorted(b for b in cur_kind if b not in prior_kind)
    cleared = sorted(b for b in prior_kind if b not in cur_kind)
    escalated = sorted(b for b in cur_kind
                       if b in prior_kind and prior_kind[b] != cur_kind[b] == "sustained")
    eased = sorted(b for b in cur_kind
                   if b in prior_kind and prior_kind[b] != cur_kind[b] == "creep")
    if not (newly or cleared or escalated or eased):
        return None  # defensive: states differ only in an unexpected way
    parts = ["## Perf-trend update", ""]
    if newly:
        parts.append("**Newly flagged:** " + ", ".join(f"`{b}`" for b in newly))
    if escalated:
        parts.append("**Escalated to sustained:** " + ", ".join(f"`{b}`" for b in escalated))
    if eased:
        parts.append("**Eased to creep:** " + ", ".join(f"`{b}`" for b in eased))
    if cleared:
        parts.append("**Recovered (no longer flagged):** " + ", ".join(f"`{b}`" for b in cleared))
    parts += ["", "_See the issue body for the full current table. Posted only on a "
              "change in the flagged set — the body is refreshed silently every night._"]
    return "\n".join(parts)


def _list_open_trend_issues() -> list[dict]:
    out = _gh([
        "issue", "list", "--repo", REPO, "--label", PERF_TREND_LABEL,
        "--state", "open", "--json", "number,title,body,labels",
    ])
    return json.loads(out) if out.strip() else []


def _gh_write(cmd: list[str]) -> bool:
    """Best-effort gh write — NEVER raises. A transient API blip on an issue
    comment/create/close must not red the whole nightly run or, worse, abort a
    close path before the issue is actually closed. Returns True on success."""
    try:
        _gh(cmd)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  ⚠️  gh {' '.join(cmd[:2])} failed (non-fatal): {exc}", file=sys.stderr)
        return False


def run_trend_watch(args) -> int:
    """Nightly trend watchdog: open/update/close a perf-trend issue. Returns exit code."""
    # Source the nightly series — fixture (offline test) or live gh artifacts.
    if args.fixture_json:
        nights = night_records_from_fixture(args.fixture_json)
        cache_dir = None
        cleanup = False
    else:
        if not shutil.which("gh"):
            print("error: gh CLI not found in PATH", file=sys.stderr)
            return EXIT_CALLER_ERROR
        if args.cache_dir:
            cache_dir, cleanup = args.cache_dir, False
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            cache_dir, cleanup = Path(tempfile.mkdtemp(prefix="bench-trend-")), True
        try:
            nights = night_records_from_gh(args.workflow, args.trend_limit, cache_dir)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR

    try:
        if len(nights) < args.recent_nights + 2:
            print(f"→ only {len(nights)} usable nights — need ≥ {args.recent_nights + 2}; "
                  "skipping trend verdict (not enough history yet).", file=sys.stderr)
            return EXIT_OK

        findings, meta = analyze_trend(
            nights, args.recent_nights, args.min_floor_pct,
            args.canary_floor_mult, args.creep_floor_pct,
        )

        # Reads are safe in dry-run; only writes are gated. With a fixture and no
        # gh, skip issue I/O entirely and just print the verdict. In fixture mode
        # --fixture-open-issue simulates a pre-existing open issue so the
        # update/close (closed-loop) branches are testable offline.
        gh_available = bool(args.fixture_json is None and shutil.which("gh"))
        if args.fixture_json is not None:
            open_issues = ([{"number": args.fixture_open_issue, "title": "(simulated)",
                             "body": getattr(args, "fixture_open_body", None) or "",
                             "labels": [{"name": n} for n in
                                        (getattr(args, "fixture_open_labels", None) or [])]}]
                           if args.fixture_open_issue else [])
        else:
            open_issues = _list_open_trend_issues() if gh_available else []

        if findings:
            # Frozen anchors are carried over from the OPEN issue's marker so the
            # baseline this regression will eventually be judged against is the
            # one in force the night it was first filed — never tonight's drifted
            # sliding anchor.
            frozen = _parse_frozen_anchors(open_issues[0].get("body")) if open_issues else {}
            body = render_trend_issue_body(findings, meta, frozen=frozen)
            print(body)
            current_state = _finding_state(findings)
            if open_issues:
                num = open_issues[0]["number"]
                # Persisted prior state lives in the issue body; a legacy body
                # (no marker) is treated as no-change so the migration run is
                # silent — it just refreshes the body and plants the marker.
                prior_state = _parse_state_marker(open_issues[0].get("body"))
                if prior_state is None:
                    prior_state = current_state
                transition = _state_transition_comment(prior_state, current_state)
                cur_labels = [l.get("name") for l in (open_issues[0].get("labels") or [])]
                label_change = _recovering_label_change(findings, prior_state, cur_labels)
                verb = "[dry-run] would update" if args.dry_run else "updating"
                note = "state changed → will comment" if transition else "no state change → body only"
                print(f"→ {verb} body of existing perf-trend issue #{num} ({note})",
                      file=sys.stderr)
                if label_change:
                    print(f"→ {'[dry-run] would ' if args.dry_run else ''}{label_change} "
                          f"`{RECOVERING_LABEL}` label", file=sys.stderr)
                if not args.dry_run and gh_available:
                    # Update-in-place: the body always reflects the current table
                    # + state marker; we never append the table as a comment.
                    #
                    # ORDER MATTERS — body (with the advanced state marker) is
                    # written BEFORE the transition comment ON PURPOSE. These are
                    # two best-effort calls with no transaction, so on a partial
                    # failure we want to fail toward SILENCE, not spam: if the
                    # comment call fails the marker is already advanced, so next
                    # night sees no transition and simply skips the (lost) comment
                    # — the body still shows the correct current state. Reversing
                    # the order would re-fire the same comment whenever the body
                    # edit failed, resurrecting the exact comment-spam this change
                    # exists to kill. Do not reorder.
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body])
                    if label_change == "add":
                        subprocess.run(
                            ["gh", "label", "create", RECOVERING_LABEL, "--repo", REPO,
                             "--color", "C2E0C6", "--description",
                             "perf-trend: sustained cleared, creep remains", "--force"],
                            capture_output=True, text=True, check=False, timeout=60,
                        )
                        _gh_write(["issue", "edit", str(num), "--repo", REPO,
                                   "--add-label", RECOVERING_LABEL])
                    elif label_change == "remove":
                        _gh_write(["issue", "edit", str(num), "--repo", REPO,
                                   "--remove-label", RECOVERING_LABEL])
                    # Comment ONLY on a real transition of the flagged set.
                    if transition:
                        _gh_write(["issue", "comment", str(num), "--repo", REPO,
                                   "--body", transition])
            else:
                assignee_note = f" (assignee: {args.assignee})" if args.assignee else ""
                print(f"→ {'[dry-run] would open' if args.dry_run else 'opening'} "
                      f"new perf-trend issue{assignee_note}", file=sys.stderr)
                if not args.dry_run and gh_available:
                    # Ensure the label exists (idempotent).
                    subprocess.run(
                        ["gh", "label", "create", PERF_TREND_LABEL, "--repo", REPO,
                         "--color", "FBCA04", "--description",
                         "Nightly bench trend regression (auto-filed)", "--force"],
                        capture_output=True, text=True, check=False, timeout=60,
                    )
                    create = ["issue", "create", "--repo", REPO,
                              "--title", "Nightly bench trend regression detected",
                              "--label", PERF_TREND_LABEL, "--body", body]
                    # Assign when requested, but NEVER let an unresolvable login
                    # block the alert: the default assignee is the repo OWNER,
                    # which `gh issue create --assignee` rejects ("Could not
                    # resolve to a User") if the owner is a GitHub Org rather than
                    # a User. Try with the assignee; on failure, file unassigned
                    # (the `perf-trend` label still drives notification).
                    filed = False
                    if args.assignee:
                        filed = _gh_write(create + ["--assignee", args.assignee])
                        if not filed:
                            print(f"::warning::could not assign '{args.assignee}' "
                                  "(org name? invalid login?) — filing perf-trend issue "
                                  "unassigned; rely on the label for notification.",
                                  file=sys.stderr)
                    if not filed:
                        _gh_write(create)
            return EXIT_OK

        # ── No findings. That is TWO different situations, not one. ──────────
        # INCONCLUSIVE = nothing could be judged tonight (too few same-class
        # nights). Silence from a detector that never ran is not evidence of
        # recovery, so it must never fire AND never close.
        if meta["status"] == STATUS_INCONCLUSIVE:
            host = meta.get("today_cpu_model") or "unknown"
            print(f"⚠️  INCONCLUSIVE — nothing evaluable tonight: only "
                  f"{meta.get('n_class_nights')} of {meta.get('n_nights')} window nights ran "
                  f"on tonight's host class ({host}); "
                  f"need ≥ {args.recent_nights} recent + {meta.get('min_settled')} settled "
                  "same-class nights per bench.")
            for issue in open_issues:
                print(f"→ NOT closing perf-trend issue #{issue['number']} — tonight is "
                      "INCONCLUSIVE, not recovered.", file=sys.stderr)
            return EXIT_OK

        # CLEAR = benches WERE judged and none is above its floor. Close EVERY
        # open perf-trend issue (not just [0]) so stragglers never linger, and
        # CLOSE BEFORE commenting so a transient comment failure can't leave a
        # recovered issue open — but only for issues whose FROZEN baseline
        # confirms the recovery.
        print("✅ No sustained nightly bench regression.")
        floor = meta["floor_pct"] / 100.0
        tonight = nights[0].medians
        today_cpu = meta.get("today_cpu_model")
        for issue in open_issues:
            num = issue["number"]
            frozen = _parse_frozen_anchors(issue.get("body"))
            blockers = _recovery_blockers(frozen, tonight, today_cpu, floor)
            if blockers:
                print(f"→ NOT closing perf-trend issue #{num} — the frozen baseline does not "
                      "confirm recovery: " + "; ".join(blockers), file=sys.stderr)
                continue
            print(f"→ {'[dry-run] would close' if args.dry_run else 'closing'} "
                  f"recovered perf-trend issue #{num}", file=sys.stderr)
            cur_labels = [l.get("name") for l in (issue.get("labels") or [])]
            drop_recovering = RECOVERING_LABEL in cur_labels
            if drop_recovering:
                print(f"→ {'[dry-run] would ' if args.dry_run else ''}remove stale "
                      f"`{RECOVERING_LABEL}` before closing #{num}", file=sys.stderr)
            close_comment = _close_comment(frozen, tonight, today_cpu, floor)
            if not args.dry_run and gh_available:
                # Strip the recovering label first so a closed issue never retains it.
                if drop_recovering:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO,
                               "--remove-label", RECOVERING_LABEL])
                _gh_write(["issue", "close", str(num), "--repo", REPO])
                _gh_write(["issue", "comment", str(num), "--repo", REPO, "--body",
                           close_comment])
        return EXIT_OK
    finally:
        if cache_dir is not None and cleanup:
            shutil.rmtree(cache_dir, ignore_errors=True)


def main() -> int:
    try_utf8_stdout()
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
    # ── Trend watchdog mode ──────────────────────────────────────────────────
    parser.add_argument("--trend-watch", action="store_true",
                        help="Nightly trend mode: open/update/close a perf-trend issue on "
                             "sustained regression (instead of the variance GO/NO-GO table).")
    parser.add_argument("--trend-limit", type=int, default=14,
                        help="Nights of history to pull for --trend-watch (default: 14).")
    parser.add_argument("--recent-nights", type=int, default=3,
                        help="K most-recent nights that must all regress for the sustained "
                             "rule (default: 3).")
    parser.add_argument("--min-floor-pct", type=float, default=5.0,
                        help="Minimum regression floor %% for --trend-watch (default: 5.0).")
    parser.add_argument("--canary-floor-mult", type=float, default=3.0,
                        help="Floor is max(min-floor, mult × canary night-to-night CV) (default: 3).")
    parser.add_argument("--creep-floor-pct", type=float, default=10.0,
                        help="Creep-rule floor %% for recent-median vs settled anchor (default: 10.0).")
    parser.add_argument("--assignee", default=REPO.split("/")[0],
                        help=f"Issue assignee for --trend-watch (default: {REPO.split('/')[0]}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended issue actions without calling gh writes.")
    parser.add_argument("--fixture-json", type=Path, default=None,
                        help="Offline test: read nightly medians from a JSON fixture "
                             "instead of gh (implies no gh writes). Each entry accepts an "
                             "optional \"cpu_model\" (host class); omitting it exercises the "
                             "unstratified fallback.")
    parser.add_argument("--fixture-open-issue", type=int, default=None,
                        help="With --fixture-json, simulate a pre-existing open perf-trend "
                             "issue number (tests the update/close closed-loop offline).")
    parser.add_argument("--fixture-open-body", default=None,
                        help="With --fixture-open-issue, the simulated issue's existing body "
                             "(carries the prior-state marker; tests transition-only comments).")
    parser.add_argument("--fixture-open-labels", nargs="*", default=None,
                        help="With --fixture-open-issue, the simulated issue's existing labels "
                             "(tests recovering-label add/remove lifecycle offline).")
    args = parser.parse_args()

    if args.trend_watch:
        return run_trend_watch(args)

    # Verify gh CLI is available
    if not shutil.which("gh"):
        print("error: gh CLI not found in PATH", file=sys.stderr)
        return EXIT_CALLER_ERROR

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
        try:
            runs = list_recent_runs(args.workflow, args.limit)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_CALLER_ERROR
        if not runs:
            print("error: no successful runs found", file=sys.stderr)
            return EXIT_CALLER_ERROR

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
            return EXIT_CALLER_ERROR

        stats = aggregate(all_samples)
        print(render_markdown_table(stats, n_runs_total=len(runs), n_runs_succeeded=succeeded))

        if args.no_gate:
            return EXIT_OK

        # CI mode: exit non-zero on NO-GO
        verdicts = [s.verdict()[0] for s in stats.values()]
        if any(v == "NO-GO" for v in verdicts):
            return EXIT_VIOLATION if args.ci else EXIT_OK
        if all(v == "INSUFFICIENT" for v in verdicts):
            return EXIT_CALLER_ERROR if args.ci else EXIT_OK
        return EXIT_OK

    finally:
        if cleanup:
            shutil.rmtree(cache_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
