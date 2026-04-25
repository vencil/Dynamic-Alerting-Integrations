#!/usr/bin/env python3
"""Aggregate per-run JSONs from the v2.8.0 B-1 Phase 2 e2e harness.

Reads `bench-results/per-run-*.json` (output of driver.py PR-2), computes:

  * Fire and resolve P50/P95/P99 of e2e_ms
  * Bootstrap 95% confidence intervals (1000 resamples, percentile of
    percentile per design §8.5)
  * Stage A/B/C/D percentiles
  * Stage C histogram (quantization noise dominates per design §5.4 —
    percentile alone is misleading)
  * gate_status determination per design §6.5 calibration gate model

Output:
  * `bench-results/e2e-{ISO8601}.json` aggregate file
  * Last line of stdout = single-line JSON summary (per A-15 convention,
    design §7.2)

CLI:
  python3 aggregate.py [--results-dir DIR] [--baseline-glob PATTERN]
                       [--gate-threshold-pct N] [--bootstrap N]
                       [--seed S]

`--baseline-glob` lets a customer-anon aggregator point at the most-recent
synthetic-v2 aggregate JSON for ±30% comparison; absent, gate_status is
derived from `fixture_kind` alone (synthetic-v* → pending; customer-anon
without baseline → pending with warning).
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_BOOTSTRAP_N = 1000
DEFAULT_GATE_THRESHOLD_PCT = 30  # design §6.5 placeholder for v2.8.0
STAGE_C_HISTOGRAM_BUCKETS = [0, 1000, 2000, 3000, 4000, 5000, 6000, 7500, 10000, 15000]


# ---------------------------------------------------------------------------
# Per-run JSON loading
# ---------------------------------------------------------------------------


def load_per_run_files(results_dir: Path) -> list[dict]:
    """Return parsed per-run JSONs sorted by run_id, excluding warm_up runs."""
    files = sorted(results_dir.glob("per-run-*.json"))
    if not files:
        raise FileNotFoundError(
            f"No per-run-*.json files found in {results_dir}; did the driver run?"
        )
    runs: list[dict] = []
    for fp in files:
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"WARN: skipping {fp.name}: {e}", file=sys.stderr)
            continue
        if data.get("warm_up", False):
            continue
        runs.append(data)
    if not runs:
        raise ValueError(
            f"No non-warm-up runs found in {results_dir}; "
            f"all {len(files)} files were warm_up=true or unreadable"
        )
    return runs


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (statistics.quantiles supports this
    via method='exclusive' but only at fixed quantiles; we want arbitrary
    p in [0, 100]). Returns 0 for empty input.
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(sorted_vals[int(k)])
    return float(sorted_vals[f]) + (float(sorted_vals[c]) - float(sorted_vals[f])) * (k - f)


def bootstrap_ci(
    values: list[float], p: float, n_resamples: int, ci_pct: float, rng: random.Random
) -> tuple[float, float]:
    """Bootstrap 95% CI for the p-th percentile (per design §8.5).

    Resamples with replacement n_resamples times; returns (lower, upper)
    bounds at the (100-ci_pct)/2 and (100+ci_pct)/2 percentiles of the
    bootstrap distribution.

    Returns (0.0, 0.0) for empty input.
    """
    if not values:
        return 0.0, 0.0
    n = len(values)
    bootstrap_estimates: list[float] = []
    for _ in range(n_resamples):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        bootstrap_estimates.append(percentile(sample, p))
    lower = percentile(bootstrap_estimates, (100 - ci_pct) / 2)
    upper = percentile(bootstrap_estimates, (100 + ci_pct) / 2)
    return lower, upper


def histogram(values: list[float], buckets: list[float]) -> list[dict]:
    """Bucketize values; returns [{le: bucket, count: N, ...}] cumulative
    Prometheus-style. Last entry uses +Inf for unbounded.
    """
    sorted_buckets = sorted(buckets) + [float("inf")]
    out = []
    for b in sorted_buckets:
        count = sum(1 for v in values if v <= b)
        out.append({"le": b if b != float("inf") else "+Inf", "count": count})
    return out


# ---------------------------------------------------------------------------
# Phase aggregation
# ---------------------------------------------------------------------------


def aggregate_phase(runs: list[dict], phase: str, rng: random.Random, n_resamples: int) -> dict:
    """Aggregate fire or resolve phase across runs. Skips runs missing
    the phase or with negative e2e_ms (sentinel for failed measurement).
    """
    e2e_values: list[float] = []
    stage_values: dict[str, list[float]] = {"A": [], "B": [], "C": [], "D": []}

    for r in runs:
        phase_data = r.get(phase, {})
        e2e = phase_data.get("e2e_ms", -1)
        if e2e < 0:
            continue  # measurement failed (anchor missing)
        e2e_values.append(float(e2e))
        for stage, lst in stage_values.items():
            v = phase_data.get("stage_ms", {}).get(stage, -1)
            if v >= 0:  # stage was measured (not skipped/failed)
                lst.append(float(v))

    n_valid = len(e2e_values)
    if n_valid == 0:
        return {
            "n_valid": 0,
            "skipped": True,
            "reason": "all e2e_ms < 0 (measurement failed for every run)",
        }

    p50 = percentile(e2e_values, 50)
    p95 = percentile(e2e_values, 95)
    p99 = percentile(e2e_values, 99)
    p50_lo, p50_hi = bootstrap_ci(e2e_values, 50, n_resamples, 95, rng)
    p95_lo, p95_hi = bootstrap_ci(e2e_values, 95, n_resamples, 95, rng)

    return {
        "n_valid": n_valid,
        "e2e_ms": {
            "p50": p50,
            "p95": p95,
            "p99": p99,
            "p50_ci95": [p50_lo, p50_hi],
            "p95_ci95": [p95_lo, p95_hi],
            "ci_too_wide": _ci_too_wide(p50, p50_lo, p50_hi),
        },
        "stage_ms": {
            stage: {
                "n": len(lst),
                "p50": percentile(lst, 50),
                "p95": percentile(lst, 95),
                "p99": percentile(lst, 99),
            }
            for stage, lst in stage_values.items()
        },
        "stage_c_histogram": histogram(stage_values["C"], STAGE_C_HISTOGRAM_BUCKETS),
    }


def _ci_too_wide(point: float, lo: float, hi: float) -> bool:
    """Per design §8.5: CI wider than 50% of median = inconclusive."""
    if point <= 0:
        return True
    return (hi - lo) > 0.5 * point


# ---------------------------------------------------------------------------
# Calibration gate (design §6.5)
# ---------------------------------------------------------------------------


def determine_gate_status(
    fixture_kind: str,
    aggregate_p95_fire: float,
    baseline_p95_fire: float | None,
    threshold_pct: float,
) -> tuple[str, str]:
    """Return (gate_status, banner_text) per design §6.5 matrix.

    Synthetic-v* fixtures always render `pending` — only customer-anon can
    flip to passed/failed/voided based on ±threshold_pct comparison to the
    most-recent synthetic-v2 baseline.
    """
    if fixture_kind == "customer-anon":
        if baseline_p95_fire is None or baseline_p95_fire <= 0:
            return (
                "pending",
                f"⚠️  customer-anon run without synthetic-v2 baseline available; "
                f"gate cannot evaluate. Run synthetic-v2 first then re-aggregate.",
            )
        delta_pct = abs(aggregate_p95_fire - baseline_p95_fire) / baseline_p95_fire * 100
        if delta_pct <= threshold_pct:
            return (
                "passed",
                f"✅ calibration passed: customer-anon P95={aggregate_p95_fire:.0f}ms "
                f"within ±{threshold_pct:.0f}% of synthetic-v2 P95={baseline_p95_fire:.0f}ms "
                f"(delta {delta_pct:.1f}%)",
            )
        return (
            "failed",
            f"❌ calibration failed: customer-anon P95={aggregate_p95_fire:.0f}ms "
            f"outside ±{threshold_pct:.0f}% of synthetic-v2 P95={baseline_p95_fire:.0f}ms "
            f"(delta {delta_pct:.1f}%); synthetic-v2 baseline marked voided, "
            f"all external references must be reviewed",
        )
    # synthetic-v1 / synthetic-v2 default
    return (
        "pending",
        f"🟡 {fixture_kind} pending customer-anon validation; "
        f"baseline not yet calibrated against real workload",
    )


def load_baseline_p95_fire(baseline_glob: str | None) -> float | None:
    """Load the most-recent matching aggregate JSON and return its fire P95.
    Returns None if no match or if the file lacks the expected structure.
    """
    if not baseline_glob:
        return None
    matches = sorted(glob.glob(baseline_glob))
    if not matches:
        return None
    try:
        data = json.loads(Path(matches[-1]).read_text(encoding="utf-8"))
        return data.get("fire", {}).get("e2e_ms", {}).get("p95")
    except (json.JSONDecodeError, OSError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def aggregate(
    results_dir: Path,
    baseline_glob: str | None,
    gate_threshold_pct: float,
    n_resamples: int,
    seed: int,
) -> dict:
    runs = load_per_run_files(results_dir)
    fixture_kinds = {r.get("fixture_kind", "unknown") for r in runs}
    if len(fixture_kinds) > 1:
        print(
            f"WARN: mixed fixture_kinds in run set: {fixture_kinds}; "
            f"using {next(iter(fixture_kinds))} for gate decision",
            file=sys.stderr,
        )
    fixture_kind = next(iter(fixture_kinds))

    rng = random.Random(seed)
    fire = aggregate_phase(runs, "fire", rng, n_resamples)
    resolve = aggregate_phase(runs, "resolve", rng, n_resamples)

    baseline_p95 = load_baseline_p95_fire(baseline_glob)
    gate_status, banner = determine_gate_status(
        fixture_kind,
        fire.get("e2e_ms", {}).get("p95", 0.0),
        baseline_p95,
        gate_threshold_pct,
    )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fixture_kind": fixture_kind,
        "n_runs_total": len(runs),
        "gate_status": gate_status,
        "gate_banner": banner,
        "gate_threshold_pct": gate_threshold_pct,
        "baseline_p95_fire": baseline_p95,
        "fire": fire,
        "resolve": resolve,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir", type=str, default="bench-results",
        help="Directory containing per-run-*.json files (default: bench-results)",
    )
    parser.add_argument(
        "--baseline-glob", type=str, default=None,
        help="Glob for prior aggregate JSON to compare against (customer-anon gate); "
             "e.g. 'bench-results/e2e-*-synthetic-v2.json'",
    )
    parser.add_argument(
        "--gate-threshold-pct", type=float, default=DEFAULT_GATE_THRESHOLD_PCT,
        help=f"±%% threshold for customer-anon vs synthetic-v2 gate (default: {DEFAULT_GATE_THRESHOLD_PCT})",
    )
    parser.add_argument(
        "--bootstrap", type=int, default=DEFAULT_BOOTSTRAP_N,
        help=f"Bootstrap resample count for 95%% CI (default: {DEFAULT_BOOTSTRAP_N})",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="RNG seed for bootstrap reproducibility (default: 42)",
    )
    parser.add_argument(
        "--out", type=str, default=None,
        help="Output aggregate JSON path (default: <results-dir>/e2e-<ISO>-<fixture>.json)",
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"ERROR: results dir {results_dir} does not exist", file=sys.stderr)
        return 1

    try:
        agg = aggregate(
            results_dir=results_dir,
            baseline_glob=args.baseline_glob,
            gate_threshold_pct=args.gate_threshold_pct,
            n_resamples=args.bootstrap,
            seed=args.seed,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.out:
        out_path = Path(args.out)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = results_dir / f"e2e-{ts}-{agg['fixture_kind']}.json"
    out_path.write_text(json.dumps(agg, indent=2), encoding="utf-8")

    print(agg["gate_banner"])
    print(f"  fire   P50={agg['fire'].get('e2e_ms', {}).get('p50', 'N/A'):.0f}ms "
          f"P95={agg['fire'].get('e2e_ms', {}).get('p95', 'N/A'):.0f}ms "
          f"P99={agg['fire'].get('e2e_ms', {}).get('p99', 'N/A'):.0f}ms "
          f"(n={agg['fire'].get('n_valid', 0)})")
    print(f"  resolve P50={agg['resolve'].get('e2e_ms', {}).get('p50', 'N/A'):.0f}ms "
          f"P95={agg['resolve'].get('e2e_ms', {}).get('p95', 'N/A'):.0f}ms "
          f"P99={agg['resolve'].get('e2e_ms', {}).get('p99', 'N/A'):.0f}ms "
          f"(n={agg['resolve'].get('n_valid', 0)})")
    print(f"  output: {out_path}")

    # Last-line single-line JSON summary per A-15 convention.
    summary = {
        "fixture_kind": agg["fixture_kind"],
        "gate_status": agg["gate_status"],
        "n_runs": agg["n_runs_total"],
        "fire_p95_ms": agg["fire"].get("e2e_ms", {}).get("p95", -1),
        "resolve_p95_ms": agg["resolve"].get("e2e_ms", {}).get("p95", -1),
    }
    print(json.dumps(summary))
    # Exit 0 even on pending — gate failure shouldn't block the run; CI
    # workflow checks the gate_status from the JSON if it wants to alert.
    return 0


if __name__ == "__main__":
    sys.exit(main())
