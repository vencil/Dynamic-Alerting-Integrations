"""Unit tests for tests/e2e-bench/aggregate.py.

Coverage focus: pure-stat functions (percentile, bootstrap CI, histogram),
gate decision logic, per-run loader. End-to-end aggregate orchestration is
exercised via a small synthetic fixture in test_aggregate_end_to_end.

Per S#32/S#35 lesson: assertions use loose statistical bounds (e.g.
"CI lower bound below point estimate; upper bound above") rather than
absolute equality, since bootstrap output is RNG-dependent.
"""
from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import pytest

AGGREGATE_PATH = Path(__file__).parent / "aggregate.py"


@pytest.fixture(scope="module")
def aggregate_module():
    spec = importlib.util.spec_from_file_location("aggregate", AGGREGATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aggregate"] = mod
    spec.loader.exec_module(mod)
    return mod


# ============================================================
# percentile
# ============================================================


def test_percentile_basic(aggregate_module):
    pct = aggregate_module.percentile
    assert pct([1, 2, 3, 4, 5], 50) == 3.0
    assert pct([1, 2, 3, 4, 5], 0) == 1.0
    assert pct([1, 2, 3, 4, 5], 100) == 5.0


def test_percentile_interpolated(aggregate_module):
    """P95 of 20 values: index = 19 * 0.95 = 18.05 → between idx 18 and 19."""
    values = list(range(1, 21))  # 1..20
    got = aggregate_module.percentile(values, 95)
    # idx 18 = 19, idx 19 = 20; interpolated at 0.05 → 19 + 0.05*1 = 19.05
    assert abs(got - 19.05) < 0.01


def test_percentile_empty(aggregate_module):
    assert aggregate_module.percentile([], 50) == 0.0


def test_percentile_single_value(aggregate_module):
    assert aggregate_module.percentile([42.0], 50) == 42.0
    assert aggregate_module.percentile([42.0], 99) == 42.0


# ============================================================
# bootstrap_ci
# ============================================================


def test_bootstrap_ci_lower_below_point_upper_above(aggregate_module):
    """Sanity: bootstrap CI brackets the point estimate."""
    rng = random.Random(42)
    values = [float(i) for i in range(1, 31)]  # 1..30
    point = aggregate_module.percentile(values, 95)
    lo, hi = aggregate_module.bootstrap_ci(values, 95, n_resamples=500, ci_pct=95, rng=rng)
    assert lo <= point <= hi, f"CI [{lo}, {hi}] should bracket point estimate {point}"


def test_bootstrap_ci_seeded_reproducible(aggregate_module):
    values = [float(i) for i in range(1, 31)]
    lo1, hi1 = aggregate_module.bootstrap_ci(values, 95, 500, 95, random.Random(42))
    lo2, hi2 = aggregate_module.bootstrap_ci(values, 95, 500, 95, random.Random(42))
    assert (lo1, hi1) == (lo2, hi2)


def test_bootstrap_ci_empty_returns_zero(aggregate_module):
    rng = random.Random(42)
    assert aggregate_module.bootstrap_ci([], 95, 100, 95, rng) == (0.0, 0.0)


def test_bootstrap_ci_tighter_with_more_resamples(aggregate_module):
    """Larger n_resamples → smoother bootstrap distribution → narrower CI
    on average. With seed=42 we verify the relationship for one specific
    sample. (Statistical, not theoretical — small chance of being equal.)"""
    values = [float(i) for i in range(1, 31)]
    lo_few, hi_few = aggregate_module.bootstrap_ci(values, 50, 50, 95, random.Random(42))
    lo_many, hi_many = aggregate_module.bootstrap_ci(values, 50, 2000, 95, random.Random(42))
    width_few = hi_few - lo_few
    width_many = hi_many - lo_many
    # With many resamples, width should be similar OR smaller than few.
    # Use loose factor of 1.5 to absorb seed-specific variance.
    assert width_many <= width_few * 1.5


# ============================================================
# histogram
# ============================================================


def test_histogram_cumulative_buckets(aggregate_module):
    """Histogram is Prometheus-style cumulative (le=N includes everything <= N)."""
    h = aggregate_module.histogram([100, 500, 1500, 4000, 8000], [1000, 5000, 10000])
    # le=1000: 100, 500 → 2
    # le=5000: 100, 500, 1500, 4000 → 4
    # le=10000: all 5
    # le=+Inf: all 5
    assert h == [
        {"le": 1000, "count": 2},
        {"le": 5000, "count": 4},
        {"le": 10000, "count": 5},
        {"le": "+Inf", "count": 5},
    ]


def test_histogram_empty_values(aggregate_module):
    h = aggregate_module.histogram([], [1000, 5000])
    for entry in h:
        assert entry["count"] == 0


# ============================================================
# _ci_too_wide
# ============================================================


def test_ci_too_wide_threshold_50pct(aggregate_module):
    # CI width > 50% of median → too wide
    assert aggregate_module._ci_too_wide(point=1000, lo=400, hi=1100) is True   # width 700, 70% of 1000
    assert aggregate_module._ci_too_wide(point=1000, lo=900, hi=1100) is False  # width 200, 20%
    # Edge case: zero / negative point
    assert aggregate_module._ci_too_wide(point=0, lo=0, hi=10) is True
    assert aggregate_module._ci_too_wide(point=-1, lo=0, hi=10) is True


# ============================================================
# determine_gate_status
# ============================================================


def test_gate_status_synthetic_always_pending(aggregate_module):
    status, banner = aggregate_module.determine_gate_status(
        "synthetic-v2", aggregate_p95_fire=4500.0, baseline_p95_fire=None, threshold_pct=30
    )
    assert status == "pending"
    assert "pending customer-anon validation" in banner


def test_gate_status_synthetic_v1_also_pending(aggregate_module):
    status, _ = aggregate_module.determine_gate_status(
        "synthetic-v1", aggregate_p95_fire=4000.0, baseline_p95_fire=10000.0, threshold_pct=30
    )
    # Synthetic-v1 / v2 ignore baseline — they can never fail the gate.
    assert status == "pending"


def test_gate_status_customer_within_threshold_passes(aggregate_module):
    # 4500 vs 4000 baseline = +12.5% → within ±30% → passed
    status, banner = aggregate_module.determine_gate_status(
        "customer-anon", aggregate_p95_fire=4500.0, baseline_p95_fire=4000.0, threshold_pct=30
    )
    assert status == "passed"
    assert "calibration passed" in banner


def test_gate_status_customer_above_threshold_fails(aggregate_module):
    # 6000 vs 4000 baseline = +50% → outside ±30% → failed
    status, banner = aggregate_module.determine_gate_status(
        "customer-anon", aggregate_p95_fire=6000.0, baseline_p95_fire=4000.0, threshold_pct=30
    )
    assert status == "failed"
    assert "calibration failed" in banner
    assert "voided" in banner


def test_gate_status_customer_below_threshold_fails(aggregate_module):
    # 1000 vs 4000 baseline = -75% → outside ±30% → also failed
    # (customer environment that's WAY faster than synthetic implies our
    # synthetic over-stresses; baseline is voided either way)
    status, _ = aggregate_module.determine_gate_status(
        "customer-anon", aggregate_p95_fire=1000.0, baseline_p95_fire=4000.0, threshold_pct=30
    )
    assert status == "failed"


def test_gate_status_customer_no_baseline_pending(aggregate_module):
    """customer-anon without baseline JSON cannot evaluate gate."""
    status, banner = aggregate_module.determine_gate_status(
        "customer-anon", aggregate_p95_fire=4500.0, baseline_p95_fire=None, threshold_pct=30
    )
    assert status == "pending"
    assert "without synthetic-v2 baseline" in banner


# ============================================================
# load_per_run_files
# ============================================================


def _write_run(dir_path: Path, run_id: int, warm_up: bool, fire_e2e: int, resolve_e2e: int):
    data = {
        "run_id": run_id,
        "warm_up": warm_up,
        "fixture_kind": "synthetic-v2",
        "gate_status": "pending",
        "fire": {
            "T0_unix_ns": 1000,
            "T1_unix_ns": 1050,
            "T2_unix_ns": 1195,
            "T3_unix_ns": 5120,
            "T4_unix_ns": 5165,
            "stage_ms": {"A": 50, "B": 145, "C": 3925, "D": 45},
            "e2e_ms": fire_e2e,
            "stage_ab_skipped": False,
        },
        "resolve": {
            "T0_unix_ns": 6000,
            "T1_unix_ns": 6048,
            "T2_unix_ns": 6190,
            "T3_unix_ns": 11110,
            "T4_unix_ns": 11155,
            "stage_ms": {"A": -1, "B": -1, "C": 4920, "D": 45},
            "e2e_ms": resolve_e2e,
            "stage_ab_skipped": True,
        },
    }
    (dir_path / f"per-run-{run_id:04d}.json").write_text(json.dumps(data))


def test_load_per_run_files_filters_warm_up(aggregate_module, tmp_path):
    _write_run(tmp_path, 0, warm_up=True, fire_e2e=4000, resolve_e2e=5000)
    _write_run(tmp_path, 1, warm_up=False, fire_e2e=4100, resolve_e2e=5100)
    _write_run(tmp_path, 2, warm_up=False, fire_e2e=4200, resolve_e2e=5200)
    runs = aggregate_module.load_per_run_files(tmp_path)
    assert len(runs) == 2
    assert all(not r.get("warm_up") for r in runs)


def test_load_per_run_files_empty_raises(aggregate_module, tmp_path):
    with pytest.raises(FileNotFoundError):
        aggregate_module.load_per_run_files(tmp_path)


def test_load_per_run_files_all_warm_up_raises(aggregate_module, tmp_path):
    _write_run(tmp_path, 0, warm_up=True, fire_e2e=4000, resolve_e2e=5000)
    with pytest.raises(ValueError, match="all .* were warm_up"):
        aggregate_module.load_per_run_files(tmp_path)


# ============================================================
# end-to-end aggregate
# ============================================================


def test_aggregate_end_to_end(aggregate_module, tmp_path):
    """Synthetic 30-run fixture; verify aggregate produces reasonable
    structure and percentiles in the expected ballpark."""
    # Write 30 non-warm-up runs with fire_e2e in 4000..4290 (mean 4145).
    for i in range(30):
        _write_run(tmp_path, i + 1, warm_up=False, fire_e2e=4000 + i * 10, resolve_e2e=5000 + i * 10)
    # Plus one warm-up run that should be ignored.
    _write_run(tmp_path, 0, warm_up=True, fire_e2e=99999, resolve_e2e=99999)

    agg = aggregate_module.aggregate(
        results_dir=tmp_path,
        baseline_glob=None,
        gate_threshold_pct=30,
        n_resamples=200,
        seed=42,
    )
    assert agg["fixture_kind"] == "synthetic-v2"
    assert agg["n_runs_total"] == 30
    assert agg["gate_status"] == "pending"  # synthetic-v2 always pending
    fire = agg["fire"]
    assert fire["n_valid"] == 30
    # fire_e2e values 4000..4290 step 10; P50 ≈ 4145.
    assert 4140 <= fire["e2e_ms"]["p50"] <= 4150
    # P95 ≈ 4280.
    assert 4270 <= fire["e2e_ms"]["p95"] <= 4290
    # CI bounds bracket P95 point.
    p95 = fire["e2e_ms"]["p95"]
    p95_lo, p95_hi = fire["e2e_ms"]["p95_ci95"]
    assert p95_lo <= p95 <= p95_hi
    # ci_too_wide should be False here (range 290 / median 4145 = ~7%).
    assert fire["e2e_ms"]["ci_too_wide"] is False


def test_aggregate_with_baseline_passes_gate(aggregate_module, tmp_path):
    """customer-anon vs prior synthetic-v2 baseline within ±30% → passed."""
    # Write 30 customer-anon runs with fire P95 ~4500.
    for i in range(30):
        data = {
            "run_id": i + 1,
            "warm_up": False,
            "fixture_kind": "customer-anon",
            "gate_status": "pending",
            "fire": {
                "stage_ms": {"A": 50, "B": 145, "C": 4255, "D": 50},
                "e2e_ms": 4500 + i * 10,  # 4500..4790, P95 ≈ 4780
                "stage_ab_skipped": False,
            },
            "resolve": {
                "stage_ms": {"A": -1, "B": -1, "C": 5000, "D": 50},
                "e2e_ms": 5050 + i * 10,
                "stage_ab_skipped": True,
            },
        }
        (tmp_path / f"per-run-{i+1:04d}.json").write_text(json.dumps(data))
    # Write a baseline aggregate JSON with synthetic-v2 P95 ~4000 (within
    # 30% of customer-anon P95 4780).
    baseline = {
        "fixture_kind": "synthetic-v2",
        "fire": {"e2e_ms": {"p95": 4000}},
    }
    baseline_dir = tmp_path / "baselines"
    baseline_dir.mkdir()
    (baseline_dir / "e2e-prior-synthetic-v2.json").write_text(json.dumps(baseline))

    agg = aggregate_module.aggregate(
        results_dir=tmp_path,
        baseline_glob=str(baseline_dir / "e2e-*-synthetic-v2.json"),
        gate_threshold_pct=30,
        n_resamples=200,
        seed=42,
    )
    # 4780 vs 4000 baseline = +19.5% → within ±30% → passed
    assert agg["gate_status"] == "passed"
    assert agg["baseline_p95_fire"] == 4000


def test_aggregate_handles_failed_measurements(aggregate_module, tmp_path):
    """Runs with e2e_ms < 0 (failed measurement) are skipped from stats."""
    # 28 valid runs + 2 failed (e2e_ms=-1).
    for i in range(28):
        _write_run(tmp_path, i + 1, warm_up=False, fire_e2e=4000 + i, resolve_e2e=5000 + i)
    _write_run(tmp_path, 29, warm_up=False, fire_e2e=-1, resolve_e2e=5100)
    _write_run(tmp_path, 30, warm_up=False, fire_e2e=-1, resolve_e2e=5200)
    agg = aggregate_module.aggregate(
        results_dir=tmp_path,
        baseline_glob=None,
        gate_threshold_pct=30,
        n_resamples=100,
        seed=42,
    )
    # n_runs_total counts ALL non-warm-up runs (30); n_valid in fire excludes
    # the 2 failures (28).
    assert agg["n_runs_total"] == 30
    assert agg["fire"]["n_valid"] == 28
    assert agg["resolve"]["n_valid"] == 30


def test_aggregate_all_failed_marks_skipped(aggregate_module, tmp_path):
    """If every run has failed fire measurement, fire phase is marked
    skipped with reason."""
    for i in range(30):
        _write_run(tmp_path, i + 1, warm_up=False, fire_e2e=-1, resolve_e2e=5000)
    agg = aggregate_module.aggregate(
        results_dir=tmp_path,
        baseline_glob=None,
        gate_threshold_pct=30,
        n_resamples=100,
        seed=42,
    )
    assert agg["fire"]["skipped"] is True
    assert agg["resolve"]["n_valid"] == 30  # resolve still works
