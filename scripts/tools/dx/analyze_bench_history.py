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
#   R2 creep — the recent window's typical night sits ≥ creep_floor above the
#       BEST night in the whole window. Catches "+2%/night for a week" where no
#       single night-vs-prior step ever crosses a floor, yet the cumulative
#       drift from the best observed night is large.
#
# Both rules use the recent *window* (not just tonight), so neither fires on a
# lone bad night. The floor is the max of a fixed minimum and a multiple of the
# control canary's own night-to-night CV — movement smaller than the runner's
# intrinsic noise (as measured by BenchmarkControlCanaryCPU) is never alerted.
# ─────────────────────────────────────────────────────────────────────────────

CANARY_BENCH = "BenchmarkControlCanaryCPU"
PERF_TREND_LABEL = "perf-trend"


@dataclass
class NightRecord:
    """One nightly run reduced to a per-bench median ns/op."""

    run_id: int
    created_at: str
    medians: dict[str, float] = field(default_factory=dict)


def _cv(values: list[float]) -> float:
    """Coefficient of variation; 0.0 if < 2 points or mean 0 (treated as no signal)."""
    vals = [v for v in values if not math.isnan(v)]
    if len(vals) < 2:
        return 0.0
    mean = statistics.mean(vals)
    if mean == 0:
        return 0.0
    return statistics.stdev(vals) / mean


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
        ))
    return nights


def night_records_from_fixture(path: Path) -> list[NightRecord]:
    """Load pre-reduced nightly medians from a JSON fixture (offline testing).

    Format: a JSON list of {"run_id", "createdAt", "benches": {name: median_ns}}.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    nights = [
        NightRecord(run_id=int(d["run_id"]), created_at=d["createdAt"],
                    medians={k: float(v) for k, v in d["benches"].items()})
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
    best_ns: float
    pct_vs_anchor: float
    pct_vs_best: float


def analyze_trend(
    nights: list[NightRecord],
    recent_k: int,
    min_floor_pct: float,
    canary_floor_mult: float,
    creep_floor_pct: float,
) -> tuple[list[TrendFinding], dict]:
    """Apply R1 (sustained) + R2 (creep) to newest-first nightly medians.

    Returns (findings, meta). `meta` carries the computed floors + canary CV for
    transparency in the rendered issue body.
    """
    canary_series = [n.medians[CANARY_BENCH] for n in nights if CANARY_BENCH in n.medians]
    canary_cv = _cv(canary_series)
    # Floors as fractions. The canary raises the floor above the fixed minimum so
    # we never alert below the runner's own measured noise — but its contribution
    # is CAPPED: a genuinely noisy runner must not be able to inflate the floor so
    # high that real sustained regressions are silenced (the watchdog's worst
    # failure mode — failing toward silence exactly when the runner is noisy).
    CANARY_FLOOR_CAP = 0.10  # ≤ 10 percentage points of canary-driven floor
    canary_contrib = min(canary_floor_mult * canary_cv, CANARY_FLOOR_CAP)
    floor = max(min_floor_pct / 100.0, canary_contrib)
    creep_floor = max(creep_floor_pct / 100.0, canary_contrib)

    findings: list[TrendFinding] = []
    benches = {b for n in nights for b in n.medians} - {CANARY_BENCH}
    for bench in sorted(benches):
        # Align to CALENDAR nights (newest-first), NOT "nights that happen to
        # contain this bench". Positional slicing of a gap-filtered series would
        # let a bench that STOPPED reporting — the classic symptom of a perf
        # timeout/crash — collapse so an old night masquerades as "today" and a
        # real spike hides in the baseline window. Require the bench present in
        # ALL recent_k newest nights (so `today` is genuinely tonight) and in ≥2
        # older nights (for a settled anchor).
        recent = [n.medians.get(bench) for n in nights[:recent_k]]
        if any(v is None for v in recent):
            continue
        baseline_vals = [n.medians[bench] for n in nights[recent_k:] if bench in n.medians]
        if len(baseline_vals) < 2:
            continue
        present_vals = [n.medians[bench] for n in nights if bench in n.medians]
        anchor = statistics.median(baseline_vals)
        best = min(present_vals)
        today = recent[0]
        recent_typical = statistics.median(recent)

        sustained = anchor > 0 and all(x >= anchor * (1 + floor) for x in recent)
        creep = best > 0 and recent_typical >= best * (1 + creep_floor)
        if sustained or creep:
            findings.append(TrendFinding(
                bench=bench,
                kind="sustained" if sustained else "creep",
                today_ns=today,
                anchor_ns=anchor,
                best_ns=best,
                pct_vs_anchor=(today / anchor - 1) * 100 if anchor else float("nan"),
                pct_vs_best=(today / best - 1) * 100 if best else float("nan"),
            ))
    meta = {
        "canary_cv": canary_cv,
        "floor_pct": floor * 100,
        "creep_floor_pct": creep_floor * 100,
        "n_nights": len(nights),
        "recent_k": recent_k,
    }
    return findings, meta


def render_trend_issue_body(findings: list[TrendFinding], meta: dict) -> str:
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
        f"- Sustained rule = all {meta['recent_k']} most-recent nights above an anchored "
        "(settled-window-median) baseline; creep rule = recent typical night above the best "
        "night in the window.",
        "",
        "| Bench | Rule | Today | vs anchor | vs best-night |",
        "|---|---|---:|---:|---:|",
    ]
    for f in findings:
        lines.append(
            f"| `{f.bench}` | {f.kind} | {format_ns(f.today_ns)} "
            f"| +{f.pct_vs_anchor:.1f}% | +{f.pct_vs_best:.1f}% |"
        )
    lines += [
        "",
        "_Auto-filed by `analyze_bench_history.py --trend-watch`. This issue auto-closes "
        "when nightly perf returns below the floor (closed loop). Single-night blips are "
        "filtered by the multi-night window; movement below the canary noise floor is ignored._",
    ]
    return "\n".join(lines)


def _list_open_trend_issues() -> list[dict]:
    out = _gh([
        "issue", "list", "--repo", REPO, "--label", PERF_TREND_LABEL,
        "--state", "open", "--json", "number,title",
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
            return 2
        if args.cache_dir:
            cache_dir, cleanup = args.cache_dir, False
            cache_dir.mkdir(parents=True, exist_ok=True)
        else:
            cache_dir, cleanup = Path(tempfile.mkdtemp(prefix="bench-trend-")), True
        try:
            nights = night_records_from_gh(args.workflow, args.trend_limit, cache_dir)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        if len(nights) < args.recent_nights + 2:
            print(f"→ only {len(nights)} usable nights — need ≥ {args.recent_nights + 2}; "
                  "skipping trend verdict (not enough history yet).", file=sys.stderr)
            return 0

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
            open_issues = ([{"number": args.fixture_open_issue, "title": "(simulated)"}]
                           if args.fixture_open_issue else [])
        else:
            open_issues = _list_open_trend_issues() if gh_available else []

        if findings:
            body = render_trend_issue_body(findings, meta)
            print(body)
            if open_issues:
                num = open_issues[0]["number"]
                print(f"→ {'[dry-run] would update' if args.dry_run else 'updating'} "
                      f"existing perf-trend issue #{num}", file=sys.stderr)
                if not args.dry_run and gh_available:
                    _gh_write(["issue", "comment", str(num), "--repo", REPO, "--body", body])
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
            return 0

        # No regression → recovered/closed-loop. Close EVERY open perf-trend issue
        # (not just [0]) so stragglers never linger, and CLOSE BEFORE commenting so
        # a transient comment failure can't leave a recovered issue open.
        print("✅ No sustained nightly bench regression.")
        for issue in open_issues:
            num = issue["number"]
            print(f"→ {'[dry-run] would close' if args.dry_run else 'closing'} "
                  f"recovered perf-trend issue #{num}", file=sys.stderr)
            if not args.dry_run and gh_available:
                _gh_write(["issue", "close", str(num), "--repo", REPO])
                _gh_write(["issue", "comment", str(num), "--repo", REPO, "--body",
                           "✅ Nightly perf has returned below the floor across the recent window "
                           "— auto-closing (closed loop). A new issue is filed if it regresses again."])
        return 0
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
                        help="Creep-rule floor %% vs the best night in the window (default: 10.0).")
    parser.add_argument("--assignee", default=REPO.split("/")[0],
                        help=f"Issue assignee for --trend-watch (default: {REPO.split('/')[0]}).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print intended issue actions without calling gh writes.")
    parser.add_argument("--fixture-json", type=Path, default=None,
                        help="Offline test: read nightly medians from a JSON fixture "
                             "instead of gh (implies no gh writes).")
    parser.add_argument("--fixture-open-issue", type=int, default=None,
                        help="With --fixture-json, simulate a pre-existing open perf-trend "
                             "issue number (tests the update/close closed-loop offline).")
    args = parser.parse_args()

    if args.trend_watch:
        return run_trend_watch(args)

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
        try:
            runs = list_recent_runs(args.workflow, args.limit)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
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
