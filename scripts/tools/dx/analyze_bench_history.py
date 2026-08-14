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
# Two things about the POPULATION the watchdog judges were wrong until #1396:
#
#   1. Host class (B) — GitHub's hosted runners are heterogeneous, and the `cpu:`
#      header that says which machine ran was parsed by nobody. It is now read,
#      carried on NightRecord, and disclosed in the issue body so a false alarm
#      is a one-minute read instead of a re-download of 14 artifacts.
#   2. Stratification + three states (C) — both windows are drawn from tonight's
#      host class only, and a night that cannot be judged is INCONCLUSIVE, which
#      is neither "regressed" nor "recovered". Previously `findings == []` closed
#      every open issue, so a night with no evaluable data announced recovery.
#
# The FIRE arithmetic itself is untouched by both.
#
# The three-state verdict is a per-NIGHT gate, and closing is a per-BENCH claim.
# A night on which A stopped reporting and B was clean is `status=CLEAR,
# evaluated=[B], inconclusive=[A]` — and it closed A's issue, announced A
# "Recovered (no longer flagged)" and could even label the issue `recovering`,
# on zero measurements of A. Every judgement about a tracked bench is therefore
# now gated on that bench appearing in `evaluated_benches` tonight: the close
# guard reads the issue's own marker (`_unverified_benches`), the marker carries
# unmeasured rows forward instead of dropping them (`_carry_forward_state`), and
# the body says visibly which benches it is still tracking but did not measure.
#
# ⚠️ WHAT THAT GUARD COSTS — measured, and larger than first written down. The
# trigger is a benchmark RENAME, not "a benchmark permanently removed": the
# stratification key and the marker both compare bench names with string
# equality, so `BenchmarkFoo` → `BenchmarkFooV2` is indistinguishable from
# `BenchmarkFoo` disappearing. Renaming a benchmark is an ordinary refactor.
# And the blast radius is not one ticket: the FINDINGS path opens a NEW issue
# only when `open_issues` is empty and updates only `open_issues[0]`, so one
# wedged issue absorbs every later regression in the repo into itself — it keeps
# accumulating carried-forward rows while no new ticket can be filed. The
# `_held_open_detail` phrasing exists so the nightly warning names the wedged
# bench and how many nights it has been silent; the fix (bench identity that
# survives a rename) is structural and is #1396's remaining follow-up.
#
# ⚠️ KNOWN AND DELIBERATELY NOT FIXED HERE — the recovery/close side still reads
# the SLIDING anchor. A permanent regression ages into the settled window, the
# anchor rises to meet it, the finding evaporates, and a CLEAR night closes the
# issue. Replaying the real 30-night series with a +20% permanent step, that
# still retires 160 of the 236 issues a real regression opened (68%, median 5
# nights; the pre-#1396 unstratified code scored 184 of 259 / 71% / 6 nights, so
# B+C move this barely at all — they were never meant to). A frozen per-bench
# baseline was prototyped for exactly this and REVERTED before merge: it needs a
# state lifecycle (ledger semantics, marker health, unjudgeable-forever rows)
# that the prototype got wrong in six independent places, and its fail-closed
# guards were provably never executed by any test. Detection (B+C) ships alone;
# the close path is issue #1396's remaining follow-up and is being redesigned.
# Until then the close COMMENT says what it actually measured, and says that the
# anchor it measured against drifts.
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

# How tonight's host class relates to the window (meta["stratification"]).
STRATA_ON = "on"                      # tonight's class known → same-class windows
STRATA_LEGACY = "legacy-unstratified"  # NO night in the window has a class at all
STRATA_TONIGHT_UNKNOWN = "tonight-unknown"  # window is classified, tonight is not

# WHY a bench was not judged tonight (meta["inconclusive_reasons"][bench]).
# The two causes are opposite problems and used to be reported with one
# sentence — "too few same-class nights" — which is a MISDIAGNOSIS for the
# first one and, on an all-one-class window, self-contradicting prose ("only 14
# of 14 window nights ran on tonight's host class"). A bench that STOPS
# REPORTING is the classic perf-timeout/crash symptom; telling an operator to
# wait for more same-class nights sends them to the wrong place entirely.
REASON_NO_RECENT = "no-recent"       # absent from ≥1 of the K newest same-class nights
REASON_THIN_ANCHOR = "thin-anchor"   # < min_settled same-class settled nights behind it
# A third cause exists only for benches named in an ISSUE'S MARKER: a bench that
# is in NO night of the window is never iterated by `analyze_trend`, so it lands
# in neither list and carries neither reason code. Reporting it through
# `_inconclusive_causes` fell through to "no benchmark appeared in tonight's
# same-class window at all" — printed, on a CLEAR night, right next to a verdict
# produced by benchmarks that plainly did appear. Renaming a benchmark is the
# ordinary way to reach this state, and nothing ever leaves it.
REASON_ABSENT = "absent-from-window"

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
    inconclusive_reasons: dict[str, str] = {}
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
            inconclusive_reasons[bench] = REASON_NO_RECENT
            continue
        baseline_vals = [n.medians[bench] for n in series[recent_k:] if bench in n.medians]
        if len(baseline_vals) < min_settled:
            inconclusive.append(bench)
            inconclusive_reasons[bench] = REASON_THIN_ANCHOR
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
        # How many same-class nights actually carried the canary. `_cv` returns
        # 0.0 for BOTH "measured, perfectly stable" and "never measured", and
        # the body printed the latter as `0.00%` — a positive assertion that the
        # runner has zero night-to-night jitter, made from zero samples. The
        # count is what lets the renderer tell the two apart.
        "canary_n": len(canary_series),
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
        "inconclusive_reasons": dict(sorted(inconclusive_reasons.items())),
    }
    return findings, meta


def _cause_clause(cause: str, meta: dict, benches: list[str]) -> str:
    """The prose for ONE cause, applied to the benches that share it.

    Split out of ``_inconclusive_causes`` so the per-issue variant
    (``_unverified_causes``) says the same sentences about the same causes
    rather than paraphrasing them a second time."""
    names = ", ".join(f"`{b}`" for b in benches)
    if cause == REASON_NO_RECENT:
        return (f"did not report on all {meta.get('recent_k', '?')} newest same-class nights "
                f"(a benchmark that STOPS reporting is the classic perf-timeout/crash symptom, "
                f"not a thin window): {names}")
    if cause == REASON_THIN_ANCHOR:
        return (f"only {meta.get('n_class_nights', '?')} of {meta.get('n_nights', '?')} window "
                f"nights ran on tonight's host class, so fewer than "
                f"{meta.get('min_settled', MIN_SETTLED_SAME_CLASS)} settled same-class nights "
                f"are behind them; need ≥ {meta.get('recent_k', '?')} recent + "
                f"{meta.get('min_settled', MIN_SETTLED_SAME_CLASS)} settled same-class nights "
                f"per bench: {names}")
    return (f"did not appear in ANY of the {meta.get('n_nights', '?')} nights in tonight's "
            f"window, so tonight neither judged nor skipped "
            f"{'it' if len(benches) == 1 else 'them'} — a RENAMED or deleted benchmark "
            f"reaches this state and never leaves it: {names}")


def _inconclusive_causes(meta: dict) -> list[str]:
    """One clause per DISTINCT reason a bench IN TONIGHT'S WINDOW was not judged.

    ``analyze_trend`` already separates the two `continue` branches; until this
    helper existed both collapsed into a single sentence blaming a thin
    same-class window. On a window where every night IS tonight's class that
    printed "only 14 of 14 window nights ran on tonight's host class" — a
    self-contradiction that points the reader away from the actual cause, which
    is a benchmark that stopped reporting.

    Scope note: this answers "what did TONIGHT fail to judge?". "Why is THIS
    ISSUE's bench unverified?" is a different question with a third possible
    answer — see ``_unverified_causes``."""
    if meta.get("stratification") == STRATA_TONIGHT_UNKNOWN:
        return ["tonight's `cpu:` header is unreadable while the rest of the window is "
                "labelled, so there is no same-class population to judge anything against"]
    by_cause: dict[str, list[str]] = {}
    for bench, cause in sorted((meta.get("inconclusive_reasons") or {}).items()):
        by_cause.setdefault(cause, []).append(bench)
    out = [_cause_clause(c, meta, by_cause[c])
           for c in (REASON_NO_RECENT, REASON_THIN_ANCHOR) if c in by_cause]
    if not out:
        out.append("no benchmark appeared in tonight's same-class window at all")
    return out


def _unverified_causes(meta: dict, unverified: Iterable[str]) -> list[str]:
    """Why each bench THIS ISSUE tracks went unverified tonight.

    ``_inconclusive_causes`` is the wrong question here and produced a
    self-contradicting answer: a bench named only in the issue's marker can be
    absent from the window ENTIRELY, in which case there is no reason code for
    it, and the fallback clause ("no benchmark appeared in tonight's same-class
    window at all") was printed on a CLEAR night — i.e. beside a verdict that
    exists precisely because other benchmarks DID appear. The third cause is the
    actionable one: the bench was renamed or removed, and no future night can
    retire this issue."""
    if meta.get("stratification") == STRATA_TONIGHT_UNKNOWN:
        return _inconclusive_causes(meta)
    reasons = meta.get("inconclusive_reasons") or {}
    by_cause: dict[str, list[str]] = {}
    for bench in sorted(unverified):
        by_cause.setdefault(reasons.get(bench, REASON_ABSENT), []).append(bench)
    return [_cause_clause(c, meta, by_cause[c])
            for c in (REASON_NO_RECENT, REASON_THIN_ANCHOR, REASON_ABSENT) if c in by_cause]


def _nights_since_seen(nights: list[NightRecord], bench: str) -> int:
    """How many of the NEWEST window nights do not carry `bench` (0 = tonight has it).

    Derived from the window that is already in hand — no new marker field, no
    new state. It is what turns "issue #42 is held open" into "held open because
    `BenchmarkX` last reported 5 nights ago", which is the difference between an
    operator filing a rename fix and an operator ignoring a recurring warning."""
    for i, night in enumerate(nights):
        if bench in night.medians:
            return i
    return len(nights)


def _held_open_detail(nights: list[NightRecord], bench: str) -> str:
    """One phrase saying HOW LONG this bench has been unmeasurable."""
    missing = _nights_since_seen(nights, bench)
    if missing == 0:
        return f"`{bench}` reported tonight but could not be judged"
    if missing >= len(nights):
        return f"`{bench}` has not reported in ANY of the last {len(nights)} nights"
    return f"`{bench}` last reported {missing} night{'' if missing == 1 else 's'} ago"


def _unverified_benches(state: list[list[str]] | None, meta: dict) -> list[str]:
    """Benches an issue is tracking that tonight did NOT evaluate.

    The three-state verdict got the *night* granularity right — a night that
    evaluated nothing never closes anything — and stopped there. Closing is a
    PER-BENCH claim: an issue filed for `BenchmarkA` is closed on the evidence
    that `BenchmarkA` recovered, and a night where A stopped reporting while B
    was clean is `status=CLEAR` with `evaluated=[B]`, `inconclusive=[A]`. That
    night used to close A's issue while the same body's marker still said A was
    sustained. Membership in ``evaluated_benches`` (rather than absence from
    ``inconclusive_benches``) is deliberate: a bench that vanished from the
    window entirely is in neither list, and it is no more verified than one that
    was skipped."""
    if not state:
        return []
    evaluated = set(meta.get("evaluated_benches") or [])
    return sorted({str(b) for b, _k in state} - evaluated)


def _tracked_rows(state: list[list[str]] | None,
                  unverified: Iterable[str]) -> list[list[str]]:
    """The prior-state rows for benches tonight could not verify, as [bench, kind]."""
    unseen = set(unverified)
    return sorted([str(b), str(k)] for b, k in (state or []) if str(b) in unseen)


def _carry_forward_state(current: list[list[str]],
                         tracked: list[list[str]]) -> list[list[str]]:
    """Tonight's flagged set PLUS the rows tonight could not measure.

    The marker is the watchdog's only memory. Overwriting it with tonight's
    findings drops a bench that merely went unmeasurable, and the ledger then
    reads "this issue tracks nothing about A" — which is exactly what lets a
    later CLEAR night close A's issue without ever having measured A. Carrying
    the row forward keeps the close guard armed until A is genuinely judged, and
    it is also why no "Recovered" comment can be produced for A: prior and
    current agree about A, so there is no transition to announce."""
    have = {str(b) for b, _k in current}
    return sorted([[str(b), str(k)] for b, k in current]
                  + [[b, k] for b, k in tracked if b not in have])


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
    injection surface. Disclosing the runner's `cpu:` header (B) opened exactly
    that surface: the header is free-form, and one containing a lookalike marker
    was measured to hijack the parse outright — the real marker sat at offset
    1773 and the regex matched the fake at 596, so the watchdog read a
    prior-state the artifact had authored. From there the flagged-set diff is
    whatever the fake says: a bench can be announced "Recovered (no longer
    flagged)" without a single number moving.

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


def _render_verdict_lines(findings: list[TrendFinding], meta: dict,
                          tracked: list[list[str]] | None = None) -> list[str]:
    """Tonight's verdict, spelled out. FINDINGS renders the table; the other two
    states say in words why there is no table — because an empty table under a
    heading that says "regression" is exactly how an INCONCLUSIVE night used to
    look like a recovered one.

    ``tracked`` is the set of benches THIS ISSUE was filed for that tonight could
    not verify. It exists because "no finding tonight" and "this issue's subject
    recovered" are different statements, and the body used to make the second one
    while its own hidden marker still said `sustained`."""
    status = meta.get("status") or (STATUS_FINDINGS if findings else STATUS_CLEAR)
    still = ", ".join(f"`{b}` ({k})" for b, k in (tracked or []))
    if status == STATUS_INCONCLUSIVE:
        host = _safe_md(meta.get("today_cpu_model")) or "unknown"
        lines = [
            "",
            f"### ⚠️ Not evaluated tonight (INCONCLUSIVE) — host class `{host}`",
            "",
        ] + [f"- {c}" for c in _inconclusive_causes(meta)] + [
            "",
            "No benchmark could be measured against a same-class anchor tonight, so no NEW "
            "finding was produced and — the point of the three-state verdict — **nothing is "
            "closed either**. Silence from a detector that never ran is not evidence of "
            "recovery.",
        ]
        if still:
            lines += [
                "",
                f"**This issue is still tracking {still}.** That is the state the last night "
                "which COULD judge it left behind, not a measurement from tonight — tonight "
                "says nothing about it either way.",
            ]
        return lines
    if not findings:
        lines = [
            "",
            "### ✅ Nothing above its floor tonight (CLEAR)",
            "",
            "Benchmarks WERE evaluated on tonight's host class and none is above its floor.",
        ]
        if still:
            lines += [
                "",
                f"⚠️ **But {still} — what this issue was filed for — is NOT among the "
                "benchmarks evaluated tonight.** A clean verdict on the OTHER benchmarks is "
                "not evidence about this one, so the issue stays open.",
            ]
        return lines
    return [
        "",
        "| Bench | Rule | Today | today vs anchor | recent-median vs anchor |",
        "|---|---|---:|---:|---:|",
    ] + [
        f"| `{f.bench}` | {f.kind} | {format_ns(f.today_ns)} "
        f"| {_signed_pct(f.pct_vs_anchor)} | {_signed_pct(f.pct_typical_vs_anchor)} |"
        for f in findings
    ]


def _render_floor_lines(meta: dict) -> list[str]:
    """The floor + canary disclosure.

    ``canary_n < 2`` means the canary was never measured on tonight's class, and
    ``_cv`` reports that as 0.0 — the same value as a runner with perfectly
    reproducible timings. Printing "**0.00%**" for it asserted a measurement that
    does not exist, and the floor line credited the canary for a number it never
    contributed (with no samples the floor is the fixed minimum, always)."""
    measured = (meta.get("canary_n") or 0) >= 2
    floor_src = ("max of fixed minimum and canary-noise-scaled" if measured
                 else "fixed minimum only — no canary measurement to raise it")
    canary_line = (
        f"- Control-canary night-to-night CV: **{meta['canary_cv']:.2%}** "
        f"(`{CANARY_BENCH}`; movement below the floor is indistinguishable from runner noise)."
        if measured else
        f"- Control-canary night-to-night CV: **not measured** — `{CANARY_BENCH}` appears on "
        f"{meta.get('canary_n', 0)} of tonight's same-class nights (needs ≥ 2 for a CV), so "
        "the floors below carry NO canary component. This is not a measurement of zero "
        "jitter; it is the absence of a measurement."
    )
    return [
        f"- Effective floor: **{meta['floor_pct']:.1f}%** ({floor_src}); "
        f"creep floor: **{meta['creep_floor_pct']:.1f}%**.",
        canary_line,
    ]


def render_trend_issue_body(findings: list[TrendFinding], meta: dict,
                            state: list[list[str]] | None = None,
                            tracked: list[list[str]] | None = None) -> str:
    """Render the issue body (table + host-class disclosure + hidden state marker).

    ``state`` overrides what goes into the hidden marker; it defaults to tonight's
    findings. The INCONCLUSIVE path overrides it because it refreshes the body on
    a night that measured NOTHING: writing tonight's empty finding set there would
    erase the prior flagged set on no evidence, and the next real transition
    comment would then announce every bench as "newly flagged". The FINDINGS and
    held-open-CLEAR paths override it to carry forward the rows tonight could not
    measure, for the same reason at per-bench granularity.

    ``tracked`` are those same unmeasurable rows, rendered VISIBLY — the marker is
    a hidden comment, and a body that silently keeps state while its prose says
    "nothing is flagged" is how a reader ends up trusting the wrong half."""
    n_class = meta.get("n_class_nights")
    span = (f"Detected across the last **{n_class}** nights that ran on tonight's host class "
            f"(of **{meta['n_nights']}** in the `bench-record` window)."
            if meta.get("stratified") and n_class is not None else
            f"Detected across the last **{meta['n_nights']}** nightly `bench-record` runs "
            "(no host-class stratification — see below).")
    lines = [
        "## Nightly bench trend regression",
        "",
        span,
        *_render_tonight_line(meta),
        "",
        *_render_floor_lines(meta),
        f"- Sustained rule = all {meta['recent_k']} most-recent nights above the anchored "
        "(settled-window-median) baseline; creep rule = the recent-window MEDIAN above that "
        "same anchor (catches a step-change that a single noisy night hides from `sustained`).",
    ]
    lines += _render_host_class_lines(meta)
    skipped = meta.get("inconclusive_benches") or []
    if skipped:
        lines.append(
            "- INCONCLUSIVE (not judged tonight): "
            + ", ".join(f"`{b}`" for b in skipped)
        )
        if meta.get("inconclusive_reasons"):
            lines += [f"  - {c}" for c in _inconclusive_causes(meta)]
    if tracked:
        lines.append(
            "- ⚠️ STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT: "
            + ", ".join(f"`{b}` ({k})" for b, k in tracked)
            + " — tonight could not measure "
            + ("it" if len(tracked) == 1 else "them")
            + ", so the state above is the last judged night's, and this issue is NOT closed."
        )
    lines += _render_verdict_lines(findings, meta, tracked)
    lines += [
        "",
        "_Auto-filed by `analyze_bench_history.py --trend-watch`. The watchdog updates this "
        "issue **in place** each night (no comment spam) and only comments when the set of "
        "flagged benchmarks changes; it auto-closes when no benchmark is above its floor on "
        "an evaluable night (closed loop) — never on a night that could evaluate nothing, and "
        "never while ANY benchmark this issue is tracking went unmeasured. Single-night blips "
        "are filtered by the multi-night window; movement below the canary noise floor is "
        "ignored._",
        "",
        _render_state_marker(_finding_state(findings) if state is None else state),
    ]
    return "\n".join(lines)


# ── Stateful issue lifecycle (update-in-place + transition-only comments) ──────
# The watchdog is stateless across nightly runs, so the PRIOR state is persisted
# in the issue body as a hidden HTML-comment marker and parsed back next night.
# This lets us refresh the body every night (always current) yet comment ONLY on
# a real transition — killing the daily comment spam that made #702 unreadable.
# Greedy within the marker's own line (no DOTALL) so a nested JSON array
# `[["x","y"]]` is captured whole, up to the last `]` before `-->`. Greedy is
# safe here because the payload is json.dumps of [[bench, kind]] where bench is
# constrained by _BENCH_RE to [A-Za-z0-9_] and kind ∈ {creep, sustained} — so it
# can never contain `-->`, an extra quote, or an unbalanced bracket that would let
# the match overrun (a free-form payload would need base64; this one doesn't).
#
# ⚠️ The BODY, however, now carries a free-form string: #1396 discloses the
# runner's raw `cpu:` header, and it is printed ABOVE this marker. That is what
# makes `_safe_md` (escape on the way out) and `_marker_match` (take the LAST
# match on the way in) load-bearing rather than cosmetic — see their docstrings.
_STATE_MARKER_RE = re.compile(r"<!--\s*perf-trend-state v1\s*(\[.*\])\s*-->")


def _finding_state(findings: list[TrendFinding]) -> list[list[str]]:
    """Canonical, order-independent state: sorted [bench, kind] pairs."""
    return sorted([f.bench, f.kind] for f in findings)


def _render_state_marker(state: list[list[str]]) -> str:
    return f"<!-- perf-trend-state v1 {json.dumps(state, separators=(',', ':'))} -->"


def _marker_match(body: str | None):
    """The LAST marker in the body, not the first.

    ``re.search`` took the first, and the body prints the runner's free-form
    `cpu:` string ABOVE the marker (that disclosure is #1396's B half) — so a
    `cpu:` header containing a lookalike marker hijacked the parse: measured, the
    real marker sat at offset 1773 and the regex matched the fake at 596. The
    prior flagged-set then comes from the artifact rather than from the watchdog,
    which is enough to post a fabricated "Recovered (no longer flagged)". The
    watchdog always appends its own marker last, so last-match is the
    authoritative one. ``_safe_md`` neuters the delimiters on the way out; this
    is the other half."""
    if not body:
        return None
    matches = list(_STATE_MARKER_RE.finditer(body))
    return matches[-1] if matches else None


def _parse_state_marker(body: str | None) -> list[list[str]] | None:
    """Recover the prior state from an issue body, or None if absent/unparseable
    (a legacy issue filed before this marker existed → caller treats as no-change
    so the migration run is silent)."""
    m = _marker_match(body)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return sorted([str(b), str(k)] for b, k in data)
    except Exception as exc:  # noqa: BLE001 — see below; degrade, never red the run
        # Catch-all ON PURPOSE. The narrow `(ValueError, TypeError)` covered the
        # payloads this code writes, not the payloads it READS: the body is
        # human-editable, and a hand-pasted deeply-nested JSON array makes
        # `json.loads` raise RecursionError (a RuntimeError, not a ValueError),
        # which escaped to `main` and exited 1 — measured: every nightly run red
        # until someone edits the issue body by hand. An unreadable marker is a
        # missing marker; the caller already treats None as "no prior state".
        print(f"  ⚠️  perf-trend state marker unreadable ({type(exc).__name__}) — "
              "treating this issue as having no prior state.", file=sys.stderr)
        return None


def _is_recovering(findings: list[TrendFinding]) -> bool:
    """True when there are findings but every one is `creep` — the sustained
    regression has cleared and only the softer creep signal remains."""
    return bool(findings) and all(f.kind == "creep" for f in findings)


def _recovering_label_change(findings: list[TrendFinding], prior_state: list[list[str]],
                             current_labels: list[str],
                             unverified: Iterable[str] = ()) -> str | None:
    """'add' / 'remove' / None for the recovering label.

    The label means "the sustained regression has cleared, only creep remains",
    so it is added only on a sustained→creep transition (the PRIOR state carried a
    sustained finding) and PERSISTS through subsequent creep-only nights (already
    labelled), and is removed the moment any sustained returns. A creep-from-start
    issue that was never sustained therefore never acquires it. `current_labels`
    gates the gh call so we never fire a redundant add/remove.

    ``unverified`` — benches tonight could not measure — blocks the ADD. "Only
    creep remains" is a claim about the sustained bench, and if that bench simply
    stopped reporting the claim has no evidence behind it: the label would repeat,
    in the notification layer, exactly the false "on the mend" the comment layer
    was fixed not to say. REMOVAL is left alone on purpose — it is driven by a
    sustained finding that WAS measured tonight, which is real evidence.

    The block is scoped to the benches the CLAIM is about — the ones the prior
    state called `sustained`. Blocking on the whole prior set made an unrelated
    row veto the label: an issue tracking a `creep` bench that went unmeasured
    could never acquire `recovering` even when the sustained bench it was filed
    for was measured tonight and had genuinely eased. That is a different false
    statement (silence about real progress), not the one this guard exists for."""
    labelled = RECOVERING_LABEL in current_labels
    prior_had_sustained = any(k == "sustained" for _, k in prior_state)
    recovering = _is_recovering(findings) and (prior_had_sustained or labelled)
    if recovering and not labelled:
        blocked = {str(b) for b, k in prior_state if k == "sustained"} & set(unverified)
        return None if blocked else "add"
    if not recovering and labelled:
        return "remove"
    return None


def _state_transition_comment(prior: list[list[str]],
                              current: list[list[str]],
                              unverified: Iterable[str] = ()) -> str | None:
    """Markdown summary of how the flagged-benchmark set changed since the prior
    run, or None when nothing changed (→ body refreshed silently, no comment).

    ``unverified`` names the benches tonight could not measure. Dropping out of
    the flagged set has two possible meanings — "measured, and it is back under
    its floor" and "not measured at all" — and only the first one is a recovery.
    The production caller carries unverified rows forward in the marker, so they
    never reach `cleared` in the first place; this subtraction is the second lock
    on the same door, for any caller that hands over a raw finding set."""
    if prior == current:
        return None
    unseen = set(unverified)
    prior_kind = {b: k for b, k in prior}
    cur_kind = {b: k for b, k in current}
    newly = sorted(b for b in cur_kind if b not in prior_kind)
    dropped = [b for b in prior_kind if b not in cur_kind]
    cleared = sorted(b for b in dropped if b not in unseen)
    unknown = sorted(b for b in dropped if b in unseen)
    escalated = sorted(b for b in cur_kind
                       if b in prior_kind and prior_kind[b] != cur_kind[b] == "sustained")
    eased = sorted(b for b in cur_kind
                   if b in prior_kind and prior_kind[b] != cur_kind[b] == "creep")
    if not (newly or cleared or unknown or escalated or eased):
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
    if unknown:
        parts.append("**Not measured tonight (state unknown, NOT recovered):** "
                     + ", ".join(f"`{b}`" for b in unknown))
    parts += ["", "_See the issue body for the full current table. Posted only on a "
              "change in the flagged set — the body is refreshed silently every night._"]
    return "\n".join(parts)


def _list_open_trend_issues() -> list[dict]:
    out = _gh([
        "issue", "list", "--repo", REPO, "--label", PERF_TREND_LABEL,
        "--state", "open", "--json", "number,title,body,labels",
    ])
    return json.loads(out) if out.strip() else []


def _gh_write(cmd: list[str], what: str | None = None) -> bool:
    """Best-effort gh write — NEVER raises. A transient API blip on an issue
    comment/create/close must not red the whole nightly run or, worse, abort a
    close path before the issue is actually closed. Returns True on success.

    ``what`` describes the write in operator terms. Without it the failure was a
    single stderr line inside a job that still ends green — the update and close
    paths could fail EVERY gh call (stale body, unclosed issue, missing label,
    lost transition comment) and the run's outward face stayed identical to a
    clean night. Callers that pass ``what`` get the failure into the two places a
    human actually looks. `_file_new_issue` deliberately passes nothing: a failed
    ASSIGNED create is an expected step on the way to the unassigned retry, and it
    does its own reporting once the outcome is known."""
    try:
        _gh(cmd)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"  ⚠️  gh {' '.join(cmd[:2])} failed (non-fatal): {exc}", file=sys.stderr)
        if what:
            _warn(f"perf-trend watchdog: {what} FAILED ({type(exc).__name__}). The issue "
                  "does not reflect tonight's run.")
            _step_summary(f"⚠️ **perf-trend watchdog: {what} failed** — the issue does not "
                          "reflect tonight's run.")
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


def _file_new_issue(body: str, assignee: str | None, n_findings: int) -> bool:
    """File the perf-trend issue; return whether one actually exists afterwards.

    Assign when requested, but NEVER let an unresolvable login block the alert:
    the default assignee is the repo OWNER, which `gh issue create --assignee`
    rejects ("Could not resolve to a User") if the owner is a GitHub Org rather
    than a User. Try with the assignee; on failure, retry unassigned (the
    `perf-trend` label still drives notification).

    ⚠️ The "filed unassigned" annotation is emitted AFTER the retry, never
    before. It used to fire the moment the ASSIGNED create failed — asserting the
    issue had been filed unassigned one line before the only call that could make
    that true. When the retry also failed, the nightly run ended with no issue,
    exit 0, and its single annotation claiming a ticket existed. Total failure is
    now its own, louder message: no ticket, nobody notified.
    """
    create = ["issue", "create", "--repo", REPO,
              "--title", "Nightly bench trend regression detected",
              "--label", PERF_TREND_LABEL, "--body", body]
    filed = False
    assign_failed = False
    if assignee:
        filed = _gh_write(create + ["--assignee", assignee])
        assign_failed = not filed
    if not filed:
        filed = _gh_write(create)
        if filed and assign_failed:
            _warn(f"could not assign '{assignee}' (org name? invalid login?) — the "
                  "perf-trend issue was filed UNASSIGNED; rely on the label for "
                  "notification.")
    if not filed:
        detail = (f"{n_findings} benchmark(s) above the floor but NO issue could be "
                  "filed — every `gh issue create` attempt failed")
        _warn(f"perf-trend watchdog: {detail}. Nobody has been notified; the findings "
              "exist only in this job's log and step summary.")
        _step_summary(f"⚠️ **perf-trend watchdog: issue creation FAILED** — {detail}.")
    return filed


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
            # Same class of failure as INCONCLUSIVE, and it used to be even more
            # silent: a stderr line nobody reads, exit 0, no annotation, no
            # summary. "The watchdog could not run" and "the watchdog found
            # nothing" looked identical from the outside.
            detail = (f"only {len(nights)} usable nights — need ≥ {args.recent_nights + 2}; "
                      "no trend verdict was computed (not enough history yet)")
            print(f"⚠️  {detail}.", file=sys.stderr)
            _warn(f"perf-trend watchdog did NOT run — {detail}. Tonight's numbers were not "
                  "checked for regressions; this is not a passing perf verdict.")
            _step_summary(
                f"⚠️ **perf-trend watchdog: not evaluated** — {detail}."
            )
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

        # An unstratified verdict is a KNOWN-false-positive-prone mode (#1396):
        # the anchor may mix runner hardware, so a finding can be a runner-pool
        # reshuffle. The body says so; the JOB did not, so a nightly that quietly
        # degraded to the legacy path looked exactly like a stratified one.
        if meta.get("stratification") == STRATA_LEGACY:
            _warn("perf-trend watchdog is running UNSTRATIFIED — no night in the window "
                  "carries a `cpu:` header, so the anchor may mix runner hardware and a "
                  "finding here can be a host-class artefact rather than a regression "
                  "(#1396). Check the `cpu:` line of the linked runs before acting.")
            _step_summary("⚠️ **perf-trend watchdog: UNSTRATIFIED verdict** — no `cpu:` header "
                          "in the window; findings may be host-class artefacts (#1396).")

        if findings:
            current_state = _finding_state(findings)
            # What THIS issue was filed for that tonight could not measure. The
            # marker is the only memory of it, and overwriting it with tonight's
            # findings is what disarms the close guard on a later CLEAR night.
            prior_state = tracked = None
            state_for_body = current_state
            if open_issues:
                prior_state = _parse_state_marker(open_issues[0].get("body"))
                tracked = _tracked_rows(prior_state, _unverified_benches(prior_state, meta))
                state_for_body = _carry_forward_state(current_state, tracked)
            body = render_trend_issue_body(findings, meta, state=state_for_body,
                                           tracked=tracked)
            print(body)
            _step_summary(
                f"⚠️ **perf-trend watchdog: {len(findings)} benchmark(s) above the floor** — "
                + ", ".join(f"`{f.bench}` ({f.kind})" for f in findings)
            )
            if open_issues:
                num = open_issues[0]["number"]
                # Persisted prior state lives in the issue body; a legacy body
                # (no marker) is treated as no-change so the migration run is
                # silent — it just refreshes the body and plants the marker.
                if prior_state is None:
                    prior_state = state_for_body
                unverified = _unverified_benches(prior_state, meta)
                transition = _state_transition_comment(prior_state, state_for_body,
                                                       unverified)
                cur_labels = [l.get("name") for l in (open_issues[0].get("labels") or [])]
                label_change = _recovering_label_change(findings, prior_state, cur_labels,
                                                        unverified)
                if tracked:
                    print("→ carrying forward (not measured tonight, NOT recovered): "
                          + ", ".join(f"{b} ({k})" for b, k in tracked), file=sys.stderr)
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
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body],
                              what=f"body refresh of issue #{num}")
                    if label_change == "add":
                        subprocess.run(
                            ["gh", "label", "create", RECOVERING_LABEL, "--repo", REPO,
                             "--color", "C2E0C6", "--description",
                             "perf-trend: sustained cleared, creep remains", "--force"],
                            capture_output=True, text=True, check=False, timeout=60,
                        )
                        _gh_write(["issue", "edit", str(num), "--repo", REPO,
                                   "--add-label", RECOVERING_LABEL],
                                  what=f"adding `{RECOVERING_LABEL}` to issue #{num}")
                    elif label_change == "remove":
                        _gh_write(["issue", "edit", str(num), "--repo", REPO,
                                   "--remove-label", RECOVERING_LABEL],
                                  what=f"removing `{RECOVERING_LABEL}` from issue #{num}")
                    # Comment ONLY on a real transition of the flagged set.
                    if transition:
                        _gh_write(["issue", "comment", str(num), "--repo", REPO,
                                   "--body", transition],
                                  what=f"transition comment on issue #{num}")
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
                    _file_new_issue(body, args.assignee, len(findings))
            return EXIT_OK

        # ── No findings. That is TWO different situations, not one. ──────────
        # INCONCLUSIVE = nothing could be judged tonight (too few same-class
        # nights). Silence from a detector that never ran is not evidence of
        # recovery, so it must never fire AND never close.
        if meta["status"] == STATUS_INCONCLUSIVE:
            # `_safe_md` because the host string is FREE-FORM artifact content and
            # this detail is interpolated into a `::warning::` annotation and into
            # the markdown step summary — the same injection surface the issue
            # body was hardened against, reached by a different route.
            host = _safe_md(meta.get("today_cpu_model")) or "unknown"
            # Causes, not one blanket sentence: "too few same-class nights" is a
            # misdiagnosis for a bench that stopped reporting, and on an
            # all-one-class window it printed "only 14 of 14 window nights".
            detail = (f"nothing evaluable tonight on host class {host} — "
                      + "; ".join(_inconclusive_causes(meta)))
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
                # Refresh the body so the ticket says WHICH night it is showing
                # and that tonight was not one of them. The marker is rewritten
                # from the PRIOR state, not from tonight's empty finding set:
                # tonight produced no evidence, so it may not rewrite the flagged
                # set (which would make the next real transition announce every
                # bench as newly flagged). The same prior state is ALSO rendered
                # visibly — a body that says "nothing is flagged" while its own
                # hidden marker says `sustained` contradicts itself, and the
                # reader has no way to know which half to believe.
                prior_state = _parse_state_marker(issue.get("body")) or []
                body = render_trend_issue_body(
                    [], meta, state=prior_state,
                    tracked=_tracked_rows(prior_state,
                                          _unverified_benches(prior_state, meta)))
                print(f"→ {'[dry-run] would update' if args.dry_run else 'updating'} body of "
                      f"perf-trend issue #{num} (INCONCLUSIVE — not evaluated tonight)",
                      file=sys.stderr)
                if not args.dry_run and gh_available:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body],
                              what=f"INCONCLUSIVE body refresh of issue #{num}")
            return EXIT_OK

        # CLEAR = benches WERE judged on tonight's host class and none is above
        # its floor. Close EVERY open perf-trend issue (not just [0]) so
        # stragglers never linger, and CLOSE BEFORE commenting so a transient
        # comment failure can't leave a recovered issue open.
        #
        # ⚠️ The evidence here is the SLIDING settled-window anchor, so this
        # close is weaker than it looks and the comment says so rather than
        # claiming a recovery the watchdog cannot observe. Hardening it (a
        # per-bench baseline frozen when the issue was filed) is #1396's open
        # follow-up; a first attempt was reverted for getting the surrounding
        # state lifecycle wrong.
        print("✅ No sustained nightly bench regression.")
        for issue in open_issues:
            num = issue["number"]
            # ── PER-BENCH close guard ────────────────────────────────────────
            # CLEAR is a statement about the benches that were EVALUATED. This
            # issue was filed for particular ones, and the marker remembers
            # which. If any of them is not in tonight's evaluated set — it
            # stopped reporting, or its stratum went thin — then tonight has no
            # evidence about the thing this issue is about, and closing it is
            # the same error the three-state verdict fixed at night granularity,
            # committed one level down. A body whose own marker says `sustained`
            # must never be closed by a night that did not measure it.
            #
            # Unreadable / absent marker → unchanged behaviour (close): a legacy
            # or hand-filed issue carries no per-bench claim to check, and
            # inventing a marker-health mechanism here is a separate design.
            prior_state = _parse_state_marker(issue.get("body"))
            unverified = _unverified_benches(prior_state, meta)
            if unverified:
                names = ", ".join(f"`{b}`" for b in unverified)
                it = "it" if len(unverified) == 1 else "them"
                # WHICH bench is stuck and for HOW LONG. Without it the same
                # warning repeats every night with no way to tell a bench that
                # is down for one night from one that was renamed a month ago —
                # and only the second one needs a human.
                stuck = "; ".join(_held_open_detail(nights, b) for b in unverified)
                detail = (f"issue #{num} is tracking {names}, and tonight did NOT evaluate "
                          f"{it} ({stuck}) — "
                          + "; ".join(_unverified_causes(meta, unverified))
                          + f" — a CLEAR verdict on the OTHER benchmarks is not evidence "
                            f"about {it}, so the issue stays open")
                print(f"→ NOT closing perf-trend issue #{num} — tonight could not verify "
                      f"{names}.", file=sys.stderr)
                _warn(f"perf-trend watchdog: {detail}.")
                _step_summary(f"⚠️ **perf-trend watchdog: issue #{num} held open** — {detail}.")
                tracked = _tracked_rows(prior_state, unverified)
                # ⚠️ `tracked`, NOT `prior_state`. This path is a CLEAR night:
                # every prior row NOT in `tracked` was measured tonight and was
                # under its floor, so writing the prior ledger back verbatim
                # re-asserts a flag the night just disproved — and the ledger
                # then never shrinks. Two alternating benches keep the issue
                # open forever (stratification alone produces that parity), and
                # a stale row makes the NEXT night's transition comment wrong in
                # whichever direction the stale kind happens to point: a genuine
                # new `sustained` reads as "no change" (subscribers get nothing)
                # and a genuine new `creep` reads as "Eased to creep", i.e. good
                # news, for a bench that just started regressing. Tonight's
                # findings are empty here, so this is the same rule the FINDINGS
                # path applies — `_carry_forward_state(current, tracked)` with an
                # empty `current`.
                body = render_trend_issue_body([], meta, state=tracked, tracked=tracked)
                print(f"→ {'[dry-run] would update' if args.dry_run else 'updating'} body of "
                      f"perf-trend issue #{num} (CLEAR, but its benches were not verified)",
                      file=sys.stderr)
                if not args.dry_run and gh_available:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body],
                              what=f"held-open body refresh of issue #{num}")
                continue
            print(f"→ {'[dry-run] would close' if args.dry_run else 'closing'} "
                  f"recovered perf-trend issue #{num}", file=sys.stderr)
            cur_labels = [l.get("name") for l in (issue.get("labels") or [])]
            drop_recovering = RECOVERING_LABEL in cur_labels
            if drop_recovering:
                print(f"→ {'[dry-run] would ' if args.dry_run else ''}remove stale "
                      f"`{RECOVERING_LABEL}` before closing #{num}", file=sys.stderr)
            # The body is refreshed on the way out too. Nothing else refreshes it
            # on a CLEAR night, so the closing comment used to land beside the
            # table of the LAST night that found something — a "+30.8% sustained"
            # table sitting directly above "✅ Auto-closing", which reads as if
            # the watchdog closed an issue it still had findings for.
            #
            # The marker keeps the PRE-CLOSE ledger instead of being blanked.
            # Blanking it is only correct for an issue that stays closed: the
            # auto-close comment tells the reader to reopen if the regression
            # returns, and a reopened issue carrying `[]` has no per-bench claim
            # left, so the very next CLEAR night closes it again in silence — the
            # close guard reads a marker that says the issue is about nothing.
            # Keeping the ledger is strictly more information (the issue records
            # what it was closed ON), and it re-arms the guard for the reopen.
            # The cost is that a reopened issue waits for ALL of those benches to
            # be measured again before it may close — which is the fail-closed
            # semantics this guard is for.
            body = render_trend_issue_body([], meta, state=prior_state or [])
            print(f"→ {'[dry-run] would refresh' if args.dry_run else 'refreshing'} body of "
                  f"#{num} to tonight's CLEAR verdict before closing", file=sys.stderr)
            if not args.dry_run and gh_available:
                # Strip the recovering label first so a closed issue never retains it.
                if drop_recovering:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO,
                               "--remove-label", RECOVERING_LABEL],
                              what=f"removing stale `{RECOVERING_LABEL}` from issue #{num}")
                # Close BEFORE commenting so a transient comment failure cannot
                # leave a recovered issue open — and gate the comment on the
                # close actually succeeding. It used to be unconditional: a
                # failed close left the issue OPEN with "✅ Auto-closing" as its
                # newest comment, which is a lie about the state of the ticket
                # printed onto the ticket itself.
                closed = _gh_write(["issue", "close", str(num), "--repo", REPO],
                                   what=f"closing recovered issue #{num}")
                if closed:
                    _gh_write(["issue", "edit", str(num), "--repo", REPO, "--body", body],
                              what=f"CLEAR body refresh of issue #{num}")
                    _gh_write(["issue", "comment", str(num), "--repo", REPO, "--body",
                               "✅ Auto-closing (closed loop): no benchmark is above its floor "
                               "on tonight's evaluable, same-host-class window. _Scope: that "
                               "verdict is measured against the SLIDING settled-window anchor, "
                               "which drifts with the data — it is not proof that perf returned "
                               "to the level this issue was filed at. A new issue is filed if it "
                               "regresses again._"],
                              what=f"auto-close comment on issue #{num}")
                else:
                    print(f"  ⚠️  issue #{num} was NOT closed — skipping the auto-close "
                          "comment (it would claim a close that did not happen).",
                          file=sys.stderr)
        return EXIT_OK
    finally:
        if cache_dir is not None and cleanup:
            shutil.rmtree(cache_dir, ignore_errors=True)


def _at_least(minimum: int):
    """argparse type: an int with a lower bound, rejected as a usage error.

    ``--recent-nights 0`` walked all the way into the detector, where the recent
    slice `series[:0]` is empty, every `len(recent) < 0` guard is vacuously False,
    and `recent[0]` raised IndexError — a traceback and exit 1 out of a nightly
    watchdog, from a flag value argparse could have rejected at the door."""
    def parse(raw: str) -> int:
        value = int(raw)
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be >= {minimum} (got {value})")
        return value
    parse.__name__ = f"int>={minimum}"
    return parse


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=_at_least(1), default=28,
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
    parser.add_argument("--trend-limit", type=_at_least(1), default=14,
                        help="Nights of history to pull for --trend-watch (default: 14).")
    parser.add_argument("--recent-nights", type=_at_least(1), default=3,
                        help="K most-recent nights that must all regress for the sustained "
                             "rule (must be >= 1; default: 3).")
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
