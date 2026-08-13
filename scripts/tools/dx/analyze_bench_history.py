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
# Applied when a frozen row has been UNJUDGEABLE for several consecutive nights
# — the bench stopped reporting, or the host class it was frozen on never came
# back. Such an issue is a permanent wedge: it can never satisfy the recovery
# check, and (deliberately) is never auto-closed either, because "silently
# closed on no evidence" is the exact failure the frozen anchor exists to stop.
# So it is made LOUD instead: the label plus the body's "Held" table say which
# benches, for how many nights, and why — and a human decides. See
# UNVERIFIABLE_DISCLOSE_AT.
UNVERIFIABLE_LABEL = "perf-trend:unverifiable"
# Consecutive unjudgeable nights before the label goes on. 3 = the same span the
# sustained rule needs, i.e. "a full detection window has gone by without the
# watchdog being able to say anything about this bench".
UNVERIFIABLE_DISCLOSE_AT = 3

# Marker row kinds. `sustained` / `creep` are FINDINGS (the bench is above its
# floor tonight); `held` is a bench that is NOT flagged tonight but has not yet
# proved recovery against its frozen baseline, so its row must survive (#1396
# follow-up A — walking only tonight's findings evicted those rows and handed
# back the free auto-close the frozen anchor was introduced to prevent).
KIND_HELD = "held"
FLAGGED_KINDS = frozenset({"sustained", "creep"})

# Three-state verdict for one night (see `analyze_trend`). "no findings" is NOT
# one state but two, and conflating them is what let a night on which NOTHING
# could be evaluated close a still-open regression issue.
STATUS_FINDINGS = "FINDINGS"          # ≥1 bench above its floor → open/update
STATUS_CLEAR = "CLEAR"                # ≥1 bench evaluated, none above floor
STATUS_INCONCLUSIVE = "INCONCLUSIVE"  # nothing evaluable → never fire, NEVER close

# How tonight's host class relates to the window (meta["stratification"]).
STRATA_ON = "on"                      # tonight's class known → same-class windows
STRATA_LEGACY = "legacy-unstratified"  # NO night in the window has a class at all
STRATA_TONIGHT_UNKNOWN = "tonight-unknown"  # window is classified, tonight is not

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


@dataclass
class HeldRow:
    """A bench on the marker that is NOT flagged tonight and has NOT proved recovery.

    ``code`` is why it has not: ``above`` (measured, still over the frozen
    baseline), ``absent`` / ``not-comparable`` (could not be measured at all —
    these accumulate into ``streak``), or ``pending`` (no evidence was available
    this run). Only a row with no reason at all may be retired from the marker.
    """

    bench: str
    anchor_ns: float
    cpu_model: str | None
    code: str
    reason: str
    streak: int = 0
    # The kind written back to the marker. `held` on any night that could judge
    # the bench and did not flag it; on a night that judged NOTHING the prior
    # kind is preserved instead, because downgrading a `sustained` row to `held`
    # would be a state change made on no evidence — and would then read as a
    # sustained→(nothing) transition next night, posting "Recovered (no longer
    # flagged)" for a bench nobody measured.
    kind: str = KIND_HELD


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

    Three, not two, host-class situations (follow-up E)
    --------------------------------------------------
    Deciding "stratified?" from ``nights[0]`` alone conflated two opposite cases.
    A window where NOTHING carries a `cpu:` header is genuinely unstratifiable and
    keeps the legacy behaviour (including ``min_settled = 2``). A window where the
    other nights ARE labelled and only TONIGHT failed to parse is a different
    animal: falling back there silently re-enabled cross-host comparison for that
    night *and* dropped the settled-night requirement from 3 to 2 — the exact
    configuration measured to re-fire the #1396 window. One unreadable header
    could therefore flip a CLEAR night into a freshly-filed 30.8% "sustained"
    issue. Tonight-unknown is now INCONCLUSIVE: not judged, never fired.
    """
    today_cpu = nights[0].cpu_model if nights else None
    if not any(n.cpu_model is not None for n in nights):
        stratification = STRATA_LEGACY
    elif today_cpu is None:
        stratification = STRATA_TONIGHT_UNKNOWN
    else:
        stratification = STRATA_ON
    stratified = stratification == STRATA_ON
    if stratification == STRATA_ON:
        series = [n for n in nights if n.cpu_model == today_cpu]
        min_settled = MIN_SETTLED_SAME_CLASS
    elif stratification == STRATA_LEGACY:
        series = list(nights)
        min_settled = 2
    else:
        # Tonight cannot be placed in any stratum → there is no population to
        # judge it against. Empty series ⇒ no bench evaluated ⇒ INCONCLUSIVE.
        series = []
        min_settled = MIN_SETTLED_SAME_CLASS

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

    # Evidence for the CLOSE path (follow-up H). Fire looks at `recent_k` nights;
    # close used to look at ONE (tonight), so recovery was judged on a ~√3-noisier
    # statistic than detection — a single lucky night could retire a real
    # regression. Same population and the same all-present alignment rule as the
    # fire path above, so this costs nothing: it is the recent window's median.
    recent_medians: dict[str, float] = {}
    recent_nights = series[:recent_k]
    if len(recent_nights) == recent_k:
        for bench in {b for n in recent_nights for b in n.medians}:
            vals = [n.medians.get(bench) for n in recent_nights]
            if all(v is not None for v in vals):
                recent_medians[bench] = statistics.median(vals)

    meta = {
        "canary_cv": canary_cv,
        "floor_pct": floor * 100,
        "creep_floor_pct": creep_floor * 100,
        "n_nights": len(nights),
        "recent_k": recent_k,
        "status": status,
        "stratified": stratified,
        "stratification": stratification,
        "today_cpu_model": today_cpu,
        # Which night these numbers are FROM. Without it an issue body refreshed
        # on a night that judged nothing reads as if it were current (follow-up C).
        "today_night": nights[0].created_at if nights else None,
        "today_run_id": nights[0].run_id if nights else None,
        "cpu_class_counts": _cpu_class_counts(nights),
        "n_class_nights": len(series),
        "min_settled": min_settled,
        "evaluated_benches": sorted(evaluated),
        "inconclusive_benches": sorted(inconclusive),
        "recent_medians": recent_medians,
    }
    return findings, meta


def _signed_pct(pct: float) -> str:
    """Signed percent, e.g. '+5.4%' / '-1.2%'. Avoids the '+-1.2%' double-sign a
    hard-coded '+' prefix produced for below-anchor findings (issue #702)."""
    if math.isnan(pct):
        return "—"
    return f"{pct:+.1f}%"


def _safe_md(text: str | None) -> str:
    """Neutralise a free-form string before it is interpolated into the issue body.

    The body carries the hidden state marker that is parsed back next night, so
    anything free-form written into the body AHEAD of that marker is a state-
    injection surface. The runner's `cpu:` header is free-form and attacker-
    adjacent enough to matter: a header containing a fake marker was measured to
    hijack the parse outright — the real marker sat at offset 1773, the regex
    matched the fake at 596, and ``_parse_frozen_anchors`` returned ``{}``, i.e.
    every frozen baseline gone and the next CLEAR night free to auto-close a
    still-regressed issue.

    Two independent fixes, both applied on purpose: parsing takes the LAST match
    (see ``_marker_match``) and the comment delimiters are escaped here. Escaping
    rather than deleting keeps the string readable in the rendered body; the
    backtick swap keeps it inside its markdown code span.
    """
    return (str(text if text is not None else "")
            .replace("<!--", "&lt;!--").replace("-->", "--&gt;").replace("`", "'"))


def _render_host_class_lines(meta: dict) -> list[str]:
    """The #1396 one-minute triage block: which machine ran tonight, and what the
    window is actually made of. Without this an operator cannot tell a real
    regression from a runner-pool reshuffle without re-downloading 14 artifacts."""
    counts = meta.get("cpu_class_counts") or {}
    comp = ", ".join(
        f"`{_safe_md(model)}` ×{n}"
        for model, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ) or "—"
    host = _safe_md(meta.get("today_cpu_model")) or None
    if meta.get("stratification") == STRATA_TONIGHT_UNKNOWN:
        return [
            "- Host class tonight: **unreadable** — tonight's artifact carries no `cpu:` "
            f"header (or it failed to parse) while the rest of the window does. "
            f"Window composition: {comp}.",
            "- Same-class stratification: **cannot be applied to tonight.** Falling back to "
            "an unstratified verdict here would compare tonight against whatever hardware "
            "the window happens to hold *and* drop the settled-night minimum — the exact "
            "configuration that re-fires #1396 — so tonight is INCONCLUSIVE instead: "
            "nothing is judged and nothing is closed.",
        ]
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


def _render_tonight_line(meta: dict) -> list[str]:
    """WHICH NIGHT the numbers below are from.

    The body is refreshed in place every night, including nights that judged
    nothing at all (follow-up C). Without a datestamp the reader cannot tell a
    current table from a stale one, and "the issue still shows +30%" reads as
    tonight's measurement when it may be a week old."""
    night = meta.get("today_night")
    if not night:
        return []
    run = meta.get("today_run_id")
    return [f"Newest night in this window: **{night}**"
            + (f" (run `{run}`)." if run else ".")]


def _render_held_lines(held: list[HeldRow]) -> list[str]:
    """The zombie disclosure (follow-up F).

    A frozen row that cannot be judged — the bench vanished from the recent
    window, or its host class stopped appearing — is a permanent wedge: the
    recovery check can never pass, so the issue can never auto-close. The chosen
    behaviour is deliberately conservative: do NOT close it, make it VISIBLE.
    For a watchdog, stuck-but-legible beats closed-on-no-evidence; only a human
    can tell "the bench was renamed" from "the bench got so slow it timed out",
    and exactly one of those must not be silently forgiven."""
    if not held:
        return []
    lines = ["", "### Held — awaiting proof of recovery", "",
             "| Bench | Frozen baseline | Frozen host class | Why this issue stays open "
             "| Unjudgeable nights |", "|---|---:|---|---|---:|"]
    for h in held:
        lines.append(
            f"| `{h.bench}` | {format_ns(h.anchor_ns)} "
            f"| `{_safe_md(h.cpu_model) if h.cpu_model else 'unknown'}` "
            f"| {h.reason} | {h.streak or '—'} |"
        )
    stuck = [h for h in held if h.streak >= UNVERIFIABLE_DISCLOSE_AT]
    if stuck:
        lines += [
            "",
            f"⚠️ **Not verifiable for ≥ {UNVERIFIABLE_DISCLOSE_AT} consecutive nights** "
            + ", ".join(f"`{h.bench}` ({h.streak} nights)" for h in stuck)
            + f". The watchdog will not close this on its own — it has nothing to close it "
            f"ON — and it is not claiming the regression is gone either. A human decides: "
            f"if the benchmark was renamed or retired, close this issue; if its host class "
            f"simply stopped appearing in the pool, widen `--trend-limit` or wait. Labelled "
            f"`{UNVERIFIABLE_LABEL}`.",
        ]
    return lines


def _render_verdict_lines(findings: list[TrendFinding], meta: dict) -> list[str]:
    """Tonight's verdict, spelled out. FINDINGS renders the table; the other two
    states say in words why there is no table — because an empty table under a
    heading that says "regression" is exactly how an INCONCLUSIVE night used to
    look like a recovered one."""
    status = meta.get("status") or (STATUS_FINDINGS if findings else STATUS_CLEAR)
    if status == STATUS_INCONCLUSIVE:
        host = _safe_md(meta.get("today_cpu_model")) or "unknown"
        return [
            "",
            f"### ⚠️ Not evaluated tonight (INCONCLUSIVE) — host class `{host}`, "
            f"only **{meta.get('n_class_nights', '?')}** of "
            f"**{meta.get('n_nights', '?')}** window nights on it "
            f"(need ≥ {meta.get('recent_k', '?')} recent + "
            f"{meta.get('min_settled', MIN_SETTLED_SAME_CLASS)} settled same-class nights "
            "per bench)",
            "",
            "No benchmark could be measured against a same-class anchor tonight, so nothing "
            "is flagged and — the point of the three-state verdict — **nothing is closed "
            "either**. Silence from a detector that never ran is not evidence of recovery.",
        ]
    if not findings:
        return [
            "",
            "### ✅ Nothing above its floor tonight",
            "",
            "Benchmarks WERE evaluated and none is above its floor, but this issue is not "
            "closed: see the held table below for what still has to be proved.",
        ]
    return [
        "",
        "| Bench | Rule | Today | today vs anchor | recent-median vs anchor |",
        "|---|---|---:|---:|---:|",
    ] + [
        f"| `{f.bench}` | {f.kind} | {format_ns(f.today_ns)} "
        f"| {_signed_pct(f.pct_vs_anchor)} | {_signed_pct(f.pct_typical_vs_anchor)} |"
        for f in findings
    ]


def render_trend_issue_body(findings: list[TrendFinding], meta: dict,
                            frozen: dict[str, tuple[float, str | None]] | None = None,
                            rows: list[list] | None = None,
                            held: list[HeldRow] | None = None) -> str:
    """Render the issue body.

    ``rows``/``held`` are the next marker state and the held-row disclosure as
    computed by ``_ledger`` (the caller owns them because retiring a row needs
    tonight's evidence). When they are omitted, ``frozen`` carries the anchors
    already frozen by a previous night so they are preserved verbatim — the
    frozen anchor must never re-baseline onto the drifted level."""
    lines = [
        "## Nightly bench trend regression",
        "",
        f"Detected across the last **{meta['n_nights']}** nightly `bench-record` runs.",
        *_render_tonight_line(meta),
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
    lines += _render_verdict_lines(findings, meta)
    lines += _render_held_lines(held or [])
    if rows is None:
        rows = _frozen_state(findings, frozen or {}, meta.get("today_cpu_model"))
    lines += [
        "",
        "_Auto-filed by `analyze_bench_history.py --trend-watch`. The watchdog updates this "
        "issue **in place** each night (no comment spam) and only comments when the set of "
        "flagged benchmarks changes; it auto-closes only when the recent-window median falls "
        "back below the FROZEN baseline captured when the issue was filed, measured on the "
        "same host class (closed loop) — and never on a night that could evaluate nothing. "
        "Single-night blips are filtered by the multi-night window; movement below the canary "
        "noise floor is ignored._",
        "",
        _render_state_marker(rows),
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
# [bench, kind, frozen_anchor_ns, frozen_cpu_model, unverifiable_streak?] — the
# anchor that was in force the FIRST night the bench fired, the host class it was
# measured on, and (5th element, omitted when 0) how many consecutive nights the
# row has been unjudgeable. v1 markers on already-open issues MUST keep parsing
# (migration), and do: the version is not pinned in the regex and row length is
# checked per row. A v1 row simply carries no frozen anchor, so that bench keeps
# the pre-#1396 close behaviour instead of being wedged open forever.
#
# The version is CAPTURED (not just matched) because "which close path applies"
# is a property of the marker's own declared version, not of a row-shape guess:
# an empty v2 payload `[]` (every row retired) has the same shape as an empty v1
# payload but the opposite meaning.
_STATE_MARKER_RE = re.compile(r"<!--\s*perf-trend-state v(\d+)\s*(\[.*\])\s*-->")
_STATE_MARKER_VERSION = "v2"

# Marker health. The close path MUST distinguish these: only a marker we could
# actually read may authorise an auto-close (follow-up B).
MARKER_OK = "ok"              # v2+, every row parsed
MARKER_LEGACY_V1 = "v1"       # pre-#1396 marker → documented legacy close path
MARKER_ABSENT = "absent"      # no marker at all (hand-edited body / foreign issue)
MARKER_DAMAGED = "damaged"    # marker present but (partly) unreadable


def _finding_state(findings: list[TrendFinding]) -> list[list[str]]:
    """Canonical, order-independent state: sorted [bench, kind] pairs."""
    return sorted([f.bench, f.kind] for f in findings)


def _flagged_only(state: list[list[str]]) -> list[list[str]]:
    """Keep only rows that mean "flagged tonight".

    The marker also carries `held` rows now (follow-up A). They are bookkeeping —
    "not yet proved recovered" — not findings, so they must not reach the
    transition comment (where a held row would read as a benchmark that was
    flagged and then "Recovered (no longer flagged)", which is the opposite of
    what it means) nor the recovering-label rule."""
    return [[b, k] for b, k in state if k in FLAGGED_KINDS]


def _frozen_state(findings: list[TrendFinding],
                  prior_frozen: dict[str, tuple[float, str | None]],
                  today_cpu: str | None,
                  held: list[HeldRow] | None = None) -> list[list]:
    """Marker rows: [bench, kind, frozen_anchor_ns, frozen_cpu_model, streak?].

    A bench already present in ``prior_frozen`` KEEPS its original anchor — that
    is the whole point. Re-freezing onto tonight's sliding anchor would re-create
    the bug this fixes: the anchor creeps up to the regressed level, the finding
    silently clears, and the issue closes itself claiming recovery.

    ``held`` is the set of frozen benches that are NOT firing tonight and have not
    proved recovery; ``_ledger`` computes it from tonight's evidence. Passing it
    explicitly is what makes the marker a LEDGER instead of a snapshot: walking
    only ``findings`` (as this did until the follow-up) evicted the frozen anchor
    of any bench that merely dropped out of tonight's finding set, and an evicted
    anchor cannot block anything — two benches recovering on different nights was
    enough to hand back the false auto-close. When ``held`` is omitted the safe
    default applies: every prior frozen row is carried forward untouched.
    """
    rows: list[list] = []
    flagged = {f.bench for f in findings}
    for f in findings:
        anchor, cpu = prior_frozen.get(f.bench, (None, None))
        if anchor is None:
            anchor, cpu = f.anchor_ns, today_cpu
        rows.append([f.bench, f.kind, anchor, cpu])
    if held is None:
        held = [HeldRow(b, a, c, "pending", "", 0)
                for b, (a, c) in prior_frozen.items() if b not in flagged]
    for h in held:
        if h.bench in flagged:
            continue
        row: list = [h.bench, h.kind, h.anchor_ns, h.cpu_model]
        if h.streak:
            row.append(int(h.streak))
        rows.append(row)
    return sorted(rows, key=lambda r: (r[0], r[1]))


def _render_state_marker(state: list[list]) -> str:
    return (f"<!-- perf-trend-state {_STATE_MARKER_VERSION} "
            f"{json.dumps(state, separators=(',', ':'))} -->")


def _marker_match(body: str | None):
    """The LAST marker in the body, not the first.

    ``re.search`` took the first, and the body prints the runner's free-form
    `cpu:` string ABOVE the marker — so a `cpu:` header containing a lookalike
    marker hijacked the parse (measured: real marker at offset 1773, regex match
    at 596, frozen anchors back as ``{}``, close path free to fire). The watchdog
    always appends its own marker last, so last-match is the authoritative one.
    ``_safe_md`` neuters the delimiters on the way in; this is the other half."""
    if not body:
        return None
    matches = list(_STATE_MARKER_RE.finditer(body))
    return matches[-1] if matches else None


def _state_marker_rows(body: str | None) -> tuple[str, list]:
    """(health, surviving raw rows) — v1 2-tuples or v2 4/5-tuples.

    Fail-OPEN was the bug (follow-up B): one malformed row made the whole marker
    unreadable, ``_parse_frozen_anchors`` returned ``{}``, and ``{}`` was read
    downstream as "this issue has no frozen baseline" → the legacy close path →
    a still-regressed issue auto-closed. Measured on
    ``[["BenchmarkA","sustained",1000.0,"…"],["oops"]]``: a +50% regression
    closed the ticket. Hand-deleting the marker did the same thing.

    So a bad row is SKIPPED, not fatal, and the health of the marker is reported
    separately, because "unreadable" and "readable and empty" must not lead to
    the same decision.
    """
    m = _marker_match(body)
    if m is None:
        return MARKER_ABSENT, []
    try:
        data = json.loads(m.group(2))
    except ValueError:
        return MARKER_DAMAGED, []
    if not isinstance(data, list):
        return MARKER_DAMAGED, []
    rows, damaged = [], False
    for row in data:
        # len 3 = a v2 row missing its host class: not a v1 row, not a usable v2
        # row. Treat as damage rather than silently dropping the anchor.
        if not isinstance(row, list) or len(row) < 2 or len(row) == 3:
            damaged = True
            continue
        rows.append(row)
    if damaged:
        return MARKER_DAMAGED, rows
    try:
        version = int(m.group(1))
    except ValueError:
        version = 0
    return (MARKER_LEGACY_V1 if version <= 1 else MARKER_OK), rows


def _parse_state_marker(body: str | None) -> list[list[str]] | None:
    """Recover the prior flagged-set from an issue body, or None if
    absent/unparseable (a legacy issue filed before this marker existed → caller
    treats as no-change so the migration run is silent).

    Version-agnostic on purpose: only [bench, kind] is needed for the transition
    comment, and both v1 and v2 rows start with exactly that pair.
    """
    health, rows = _state_marker_rows(body)
    if health == MARKER_ABSENT or (health == MARKER_DAMAGED and not rows):
        return None
    return sorted([str(r[0]), str(r[1])] for r in rows)


def _parse_frozen_anchors(body: str | None) -> dict[str, tuple[float, str | None]]:
    """bench → (frozen_anchor_ns, frozen_cpu_model) from a v2 marker.

    Empty dict for a v1 / absent / unparseable marker — i.e. "no frozen baseline
    is known". ⚠️ That is NOT by itself permission to close: the caller must
    consult ``_state_marker_rows``' health first, because only the v1 case means
    "pre-#1396 issue, legacy close path"; absent/damaged mean "we do not know
    what this issue was watching" and must fail closed (follow-up B).
    """
    _health, rows = _state_marker_rows(body)
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


def _parse_held_streaks(body: str | None) -> dict[str, int]:
    """bench → consecutive nights the row could not be judged (5th row element)."""
    _health, rows = _state_marker_rows(body)
    out: dict[str, int] = {}
    for row in rows:
        if len(row) < 5:
            continue
        try:
            n = int(row[4])
        except (TypeError, ValueError):
            continue
        if n > 0:
            out[str(row[0])] = n
    return out


# Block codes that mean "nothing was compared", as opposed to "compared, still
# regressed". Only these accumulate toward the unverifiable disclosure, because
# only these can persist forever regardless of how perf actually behaves.
_UNVERIFIABLE_CODES = frozenset({"absent", "not-comparable"})


def _recovery_block(bench: str, anchor: float, cpu: str | None,
                    recent_medians: dict[str, float], today_cpu: str | None,
                    floor: float) -> tuple[str, str] | None:
    """(code, message) for one frozen row, or None when recovery is PROVEN.

    ``code`` ∈ {"not-comparable", "absent", "above"}.

    Same host class is a PRECONDITION, not a nicety, and "unknown" is not a
    match (follow-up D). The old check skipped the comparison entirely when the
    frozen host class was ``None`` and compared the raw numbers instead — which
    means an issue frozen while the class was unknown could be closed by a night
    on a completely different, faster machine, while ``_close_comment`` printed
    "measured on the same host class". Unknown ≠ same. An unverified claim about
    the host class must not be made, and a comparison that rests on one must not
    be trusted.
    """
    if cpu is None or today_cpu is None or cpu != today_cpu:
        return ("not-comparable",
                f"`{bench}`: frozen baseline was measured on "
                f"`{_safe_md(cpu) if cpu else 'unknown'}`, tonight's window is "
                f"`{_safe_md(today_cpu) if today_cpu else 'unknown'}` — not comparable")
    value = recent_medians.get(bench)
    if value is None:
        return ("absent",
                f"`{bench}`: absent from the recent same-class window — nothing to compare")
    if value >= anchor * (1 + floor):
        return ("above",
                f"`{bench}`: {format_ns(value)} still ≥ frozen baseline "
                f"{format_ns(anchor)} × (1 + {floor:.1%})")
    return None


def _recovery_blockers(frozen: dict[str, tuple[float, str | None]],
                       recent_medians: dict[str, float],
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
    anchor frozen when the bench first fired, and only against nights measured
    on the same host class — cross-class numbers are not comparable, so they are
    not evidence of anything.

    ``recent_medians`` is the RECENT WINDOW's median per bench, not tonight alone
    (follow-up H): firing needs `recent_k` nights to agree, so letting a single
    night retire the issue made recovery the softer of the two tests in the same
    tool. ``analyze_trend`` publishes it in ``meta`` off the same stratified
    series, so the symmetry is free.

    The ``>=`` is deliberate: a value sitting EXACTLY on
    ``anchor × (1 + floor)`` has not come back below the floor, so it blocks.
    """
    blockers: list[str] = []
    for bench, (anchor, cpu) in sorted(frozen.items()):
        blocked = _recovery_block(bench, anchor, cpu, recent_medians, today_cpu, floor)
        if blocked is not None:
            blockers.append(blocked[1])
    return blockers


def _ledger(body: str | None, findings: list[TrendFinding], meta: dict,
            *, allow_retire: bool) -> tuple[list[list], list[HeldRow]]:
    """Next marker state + held-row disclosure for one open issue.

    The marker is a LEDGER OF BENCHES NOT YET PROVEN RECOVERED (follow-up A), so
    a row leaves it in exactly one way: that bench passes ``_recovery_block``.
    Falling out of tonight's finding set is not proof of anything — the sliding
    anchor drifting up onto the regression is the single most likely reason for
    it, which is precisely the failure the frozen anchor exists to catch.

    ``allow_retire`` is False on an INCONCLUSIVE night: nothing was evaluated, so
    nothing may be retired, and the v1 rows of a pre-#1396 marker are carried
    forward verbatim rather than migrated on no evidence.
    """
    prior_frozen = _parse_frozen_anchors(body)
    prior_streaks = _parse_held_streaks(body)
    _health, prior_rows = _state_marker_rows(body)
    prior_kinds = {str(r[0]): str(r[1]) for r in prior_rows}
    today_cpu = meta.get("today_cpu_model")
    recent = meta.get("recent_medians") or {}
    floor = meta.get("floor_pct", 0.0) / 100.0
    flagged = {f.bench for f in findings}

    held: list[HeldRow] = []
    for bench, (anchor, cpu) in sorted(prior_frozen.items()):
        if bench in flagged:
            continue          # still firing → carried as a finding row, not held
        blocked = _recovery_block(bench, anchor, cpu, recent, today_cpu, floor)
        if blocked is None and allow_retire:
            continue          # proven recovered → the row retires
        code, reason = blocked if blocked else (
            "pending", f"`{bench}`: below the frozen baseline, awaiting an evaluable night")
        streak = prior_streaks.get(bench, 0) + 1 if code in _UNVERIFIABLE_CODES else 0
        prior_kind = prior_kinds.get(bench, KIND_HELD)
        kind = KIND_HELD if allow_retire or prior_kind not in FLAGGED_KINDS else prior_kind
        held.append(HeldRow(bench, anchor, cpu, code, reason, streak, kind))

    # v1 rows (no frozen anchor at all) keep their documented pre-#1396 semantics:
    # they are retired by any night that could judge, and survive a night that
    # could not. Rewriting them away on an INCONCLUSIVE night would lose the prior
    # flagged set and make the next real transition comment announce every bench
    # as "newly flagged".
    carry: list[list] = []
    if not allow_retire:
        carry = [list(r) for r in prior_rows
                 if len(r) == 2 and str(r[0]) not in flagged
                 and str(r[0]) not in prior_frozen]

    rows = _frozen_state(findings, prior_frozen, today_cpu, held=held) + carry
    rows.sort(key=lambda r: (str(r[0]), str(r[1])))
    return rows, held


def _close_comment(frozen: dict[str, tuple[float, str | None]],
                   recent_medians: dict[str, float],
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

    And the same-host-class clause is itself conditional (follow-up D). It used
    to be printed unconditionally, including on the path where the frozen class
    was unknown and therefore never compared — a sentence asserting an
    equivalence the tool had not established. An unverified claim is not written
    at all.
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
    verified_class = today_cpu is not None and all(c == today_cpu for _a, c in frozen.values())
    where = (f", measured on the same host class (`{_safe_md(today_cpu)}`)"
             if verified_class else "")
    detail = ", ".join(
        f"`{b}` {format_ns(recent_medians[b])} < {format_ns(a)} × (1 + {floor:.1%})"
        for b, (a, _c) in sorted(frozen.items()) if b in recent_medians
    )
    return (
        "✅ Auto-closing (closed loop). The recent-window median — the same nights the "
        "sustained rule reads, not a single lucky night — is back below the **frozen "
        f"baseline** captured when this issue was filed{where}: {detail}.\n\n"
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
    prior_had_sustained = any(k == "sustained" for _, k in _flagged_only(prior_state))
    recovering = _is_recovering(findings) and (prior_had_sustained or labelled)
    if recovering and not labelled:
        return "add"
    if not recovering and labelled:
        return "remove"
    return None


def _state_transition_comment(prior: list[list[str]], current: list[list[str]]) -> str | None:
    """Markdown summary of how the flagged-benchmark set changed since the prior
    run, or None when nothing changed (→ body refreshed silently, no comment).

    Only `sustained` / `creep` rows count as "flagged": a `held` row is a bench
    the watchdog is still waiting on, and letting one through here would post
    "**Recovered (no longer flagged):** `X`" about a benchmark whose whole reason
    for being on the marker is that recovery has NOT been shown."""
    prior, current = _flagged_only(prior), _flagged_only(current)
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


def _warn(msg: str) -> None:
    """Emit a GitHub Actions warning annotation (and a plain stderr line locally).

    A nightly watchdog that cannot evaluate anything used to be indistinguishable
    from a nightly watchdog that found nothing wrong: green tick, exit 0, no
    annotation, no summary, issue untouched (follow-up C). An injected permanent
    +30% regression was measured to produce exactly that — a silent green run.
    Silence is the one thing a watchdog may never do."""
    print(f"::warning::{msg}", file=sys.stderr)


def _step_summary(markdown: str) -> None:
    """Append a line to the job's step summary, when running under Actions.

    The annotation above is easy to miss in a run with many steps; the summary is
    the page a human actually opens. Both, or the failure mode is "nobody found
    out". No-op locally / in tests unless GITHUB_STEP_SUMMARY points somewhere."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(markdown.rstrip("\n") + "\n")
    except OSError as exc:                                   # pragma: no cover - env issue
        print(f"  ⚠️  could not write step summary: {exc}", file=sys.stderr)


def _sync_label(num: int, label: str, want: bool, current_labels: list[str],
                args, gh_available: bool, *, color: str, description: str) -> str | None:
    """Add/remove `label` on issue #num only when its presence has to change."""
    has = label in current_labels
    if want == has:
        return None
    action = "add" if want else "remove"
    print(f"→ {'[dry-run] would ' if args.dry_run else ''}{action} "
          f"`{label}` label", file=sys.stderr)
    if args.dry_run or not gh_available:
        return action
    if want:
        subprocess.run(
            ["gh", "label", "create", label, "--repo", REPO, "--color", color,
             "--description", description, "--force"],
            capture_output=True, text=True, check=False, timeout=60,
        )
        _gh_write(["issue", "edit", str(num), "--repo", REPO, "--add-label", label])
    else:
        _gh_write(["issue", "edit", str(num), "--repo", REPO, "--remove-label", label])
    return action


def _sync_unverifiable_label(issue: dict, want: bool, args, gh_available: bool) -> str | None:
    return _sync_label(
        issue["number"], UNVERIFIABLE_LABEL, want,
        [l.get("name") for l in (issue.get("labels") or [])], args, gh_available,
        color="D93F0B",
        description="perf-trend: recovery cannot be verified — needs a human",
    )


def _marker_health_warning(issue: dict, health: str) -> str | None:
    """The ::warning:: text for an unusable marker, or None when it is usable."""
    if health == MARKER_ABSENT:
        return (f"perf-trend issue #{issue['number']} carries NO state marker (hand-edited "
                "body, or an issue labelled `perf-trend` by a human). The watchdog does not "
                "know what it was watching, so it will NOT auto-close it — close it by hand "
                "once perf is confirmed good.")
    if health == MARKER_DAMAGED:
        return (f"perf-trend issue #{issue['number']} has a DAMAGED state marker (unparseable "
                "rows). Surviving rows are still honoured, but the watchdog will NOT "
                "auto-close it — a marker it could not fully read is not evidence of "
                "recovery. Fix or clear the marker by hand.")
    return None


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
            # sliding anchor. The ledger additionally KEEPS the rows of benches
            # that stopped firing but have not proved recovery (follow-up A).
            prior_body = open_issues[0].get("body") if open_issues else None
            health, _rows = _state_marker_rows(prior_body)
            if open_issues and health == MARKER_DAMAGED:
                _warn(_marker_health_warning(open_issues[0], health))
            rows, held = _ledger(prior_body, findings, meta, allow_retire=True)
            body = render_trend_issue_body(findings, meta, rows=rows, held=held)
            print(body)
            _step_summary(
                f"⚠️ **perf-trend watchdog: {len(findings)} benchmark(s) above the floor** — "
                + ", ".join(f"`{f.bench}` ({f.kind})" for f in findings)
            )
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
                # Held rows that nothing can judge are disclosed, never closed.
                _sync_unverifiable_label(
                    open_issues[0],
                    any(h.streak >= UNVERIFIABLE_DISCLOSE_AT for h in held),
                    args, gh_available)
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
            detail = (f"nothing evaluable tonight: only {meta.get('n_class_nights')} of "
                      f"{meta.get('n_nights')} window nights ran on tonight's host class "
                      f"({host}); need ≥ {args.recent_nights} recent + "
                      f"{meta.get('min_settled')} settled same-class nights per bench")
            print(f"⚠️  INCONCLUSIVE — {detail}.")
            # An INCONCLUSIVE night is NOT a clean bill of health, and until this
            # follow-up it looked exactly like one from the outside: no
            # annotation, no summary, nothing on the issue. A real +30% permanent
            # regression on a night with too few same-class peers produced a
            # green, silent run. Now it says so in all three places a human
            # looks (follow-up C).
            _warn(f"perf-trend watchdog INCONCLUSIVE — {detail}. Tonight's numbers were NOT "
                  "checked for regressions; this is not a passing perf verdict.")
            _step_summary(
                f"⚠️ **perf-trend watchdog: INCONCLUSIVE** — {detail}. "
                f"Newest night in the window: {meta.get('today_night') or 'unknown'}."
            )
            for issue in open_issues:
                num = issue["number"]
                print(f"→ NOT closing perf-trend issue #{num} — tonight is "
                      "INCONCLUSIVE, not recovered.", file=sys.stderr)
                health, _rows = _state_marker_rows(issue.get("body"))
                warning = _marker_health_warning(issue, health)
                if warning:
                    # Never overwrite a marker we could not read: rewriting would
                    # replace unreadable state with a fresh EMPTY one, which the
                    # next CLEAR night would happily close on.
                    _warn(warning)
                    _sync_unverifiable_label(issue, True, args, gh_available)
                    continue
                # Refresh the body so the ticket says WHICH night it is showing
                # and that tonight was not one of them. Nothing may be retired
                # here: allow_retire=False (no evidence was produced tonight).
                rows, held = _ledger(issue.get("body"), [], meta, allow_retire=False)
                body = render_trend_issue_body([], meta, rows=rows, held=held)
                print(f"→ {'[dry-run] would update' if args.dry_run else 'updating'} body of "
                      f"perf-trend issue #{num} (INCONCLUSIVE — not evaluated tonight)",
                      file=sys.stderr)
                if not args.dry_run and gh_available:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body])
                _sync_unverifiable_label(
                    issue, any(h.streak >= UNVERIFIABLE_DISCLOSE_AT for h in held),
                    args, gh_available)
            return EXIT_OK

        # CLEAR = benches WERE judged and none is above its floor. Close EVERY
        # open perf-trend issue (not just [0]) so stragglers never linger, and
        # CLOSE BEFORE commenting so a transient comment failure can't leave a
        # recovered issue open — but only for issues whose FROZEN baseline
        # confirms the recovery.
        print("✅ No sustained nightly bench regression.")
        floor = meta["floor_pct"] / 100.0
        # The recent-window median, not tonight alone — symmetric with fire (H).
        recent = meta.get("recent_medians") or {}
        today_cpu = meta.get("today_cpu_model")
        for issue in open_issues:
            num = issue["number"]
            health, _rows = _state_marker_rows(issue.get("body"))
            warning = _marker_health_warning(issue, health)
            if warning:
                # FAIL CLOSED (follow-up B). "I could not read the state" is not
                # "there was no state": auto-closing here is exactly how a
                # hand-deleted or half-corrupt marker turned a +50% regression
                # into a closed ticket. The body is deliberately NOT rewritten —
                # planting a fresh empty marker over unreadable state would make
                # the next CLEAR night close it after all.
                print(f"→ NOT closing perf-trend issue #{num} — its state marker is "
                      f"{'absent' if health == MARKER_ABSENT else 'damaged'}.",
                      file=sys.stderr)
                _warn(warning)
                _step_summary(f"⚠️ **perf-trend watchdog: issue #{num} not auto-closed** — "
                              f"state marker {'absent' if health == MARKER_ABSENT else 'damaged'}; "
                              "close it by hand once perf is confirmed good.")
                _sync_unverifiable_label(issue, True, args, gh_available)
                continue
            frozen = _parse_frozen_anchors(issue.get("body"))
            blockers = _recovery_blockers(frozen, recent, today_cpu, floor)
            if blockers:
                print(f"→ NOT closing perf-trend issue #{num} — the frozen baseline does not "
                      "confirm recovery: " + "; ".join(blockers), file=sys.stderr)
                # Refresh the body so the held table (and the count of nights a
                # row has been unjudgeable) is visible on the ticket itself.
                rows, held = _ledger(issue.get("body"), [], meta, allow_retire=True)
                body = render_trend_issue_body([], meta, rows=rows, held=held)
                print(f"→ {'[dry-run] would update' if args.dry_run else 'updating'} body of "
                      f"perf-trend issue #{num} (still open — held rows)", file=sys.stderr)
                if not args.dry_run and gh_available:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body])
                stuck = [h for h in held if h.streak >= UNVERIFIABLE_DISCLOSE_AT]
                if stuck:
                    _step_summary(
                        f"⚠️ **perf-trend watchdog: issue #{num} cannot be verified** — "
                        + ", ".join(f"`{h.bench}` ({h.streak} nights)" for h in stuck)
                        + ". Needs a human."
                    )
                _sync_unverifiable_label(issue, bool(stuck), args, gh_available)
                continue
            print(f"→ {'[dry-run] would close' if args.dry_run else 'closing'} "
                  f"recovered perf-trend issue #{num}", file=sys.stderr)
            cur_labels = [l.get("name") for l in (issue.get("labels") or [])]
            drop_recovering = RECOVERING_LABEL in cur_labels
            if drop_recovering:
                print(f"→ {'[dry-run] would ' if args.dry_run else ''}remove stale "
                      f"`{RECOVERING_LABEL}` before closing #{num}", file=sys.stderr)
            close_comment = _close_comment(frozen, recent, today_cpu, floor)
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
