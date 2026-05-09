"""Tests for analyze_bench_history.py — bench-record nightly aggregator.

Closes the audit gap (P1-5 / 444 LOC tool was 0% covered). Targets the spine:
  - RunSample dataclass
  - BenchStats: per_run_medians / median / cv / max_min_ratio /
    within_run_cv_mean / verdict
  - parse_bench_file (regex + line iteration)
  - aggregate (RunSample → BenchStats by name)
  - format_ns (ns / µs / ms / s humanizer + NaN handling)
  - render_markdown_table (table shape + threshold line)

OUT OF SCOPE here (require gh CLI auth / live network):
  - _gh, list_recent_runs, download_artifact
  - main() end-to-end (involves the gh-CLI chain)
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

import analyze_bench_history as ab


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stats(per_run_samples: dict[int, list[float]],
                bench: str = "BenchmarkX") -> ab.BenchStats:
    """Build a BenchStats by simulating aggregate() population."""
    bs = ab.BenchStats(bench=bench)
    for run_id, samples in per_run_samples.items():
        for s in samples:
            bs.samples.append(s)
            bs.runs.add(run_id)
            bs.samples_by_run.setdefault(run_id, []).append(s)
    return bs


# ---------------------------------------------------------------------------
# RunSample
# ---------------------------------------------------------------------------
class TestRunSample:
    def test_dataclass_fields(self):
        s = ab.RunSample(run_id=1, bench="BenchX", ns_per_op=42.5)
        assert s.run_id == 1
        assert s.bench == "BenchX"
        assert s.ns_per_op == 42.5


# ---------------------------------------------------------------------------
# BenchStats — pure-stat properties
# ---------------------------------------------------------------------------
class TestBenchStatsCounts:
    def test_n_samples_and_n_runs(self):
        bs = _make_stats({1: [10, 20], 2: [30, 40, 50]})
        assert bs.n_samples == 5
        assert bs.n_runs == 2

    def test_empty(self):
        bs = ab.BenchStats(bench="X")
        assert bs.n_samples == 0
        assert bs.n_runs == 0


class TestBenchStatsPerRunMedians:
    def test_one_run(self):
        bs = _make_stats({1: [10, 20, 30]})
        assert bs.per_run_medians == [20]

    def test_multiple_runs(self):
        # run 1 median = 20, run 2 median = 50
        bs = _make_stats({1: [10, 20, 30], 2: [40, 50, 60]})
        assert sorted(bs.per_run_medians) == [20, 50]

    def test_skips_empty_runs(self):
        bs = ab.BenchStats(bench="X")
        bs.runs.add(1)
        bs.samples_by_run[1] = []  # empty list
        assert bs.per_run_medians == []


class TestBenchStatsMedian:
    def test_single_run(self):
        bs = _make_stats({1: [100, 200]})
        # Per-run median = 150; median of [150] = 150.
        assert bs.median == 150

    def test_multiple_runs(self):
        bs = _make_stats({1: [10], 2: [20], 3: [30]})
        # Per-run medians = [10, 20, 30]; median = 20.
        assert bs.median == 20

    def test_empty_returns_nan(self):
        bs = ab.BenchStats(bench="X")
        assert math.isnan(bs.median)


class TestBenchStatsCV:
    def test_low_variance_low_cv(self):
        # Per-run medians: [100, 100, 100] → stdev=0 → CV=0
        bs = _make_stats({1: [100], 2: [100], 3: [100]})
        assert bs.cv == 0.0

    def test_known_cv_value(self):
        # Per-run medians: [80, 100, 120]
        # mean = 100, stdev = 20, CV = 0.2
        bs = _make_stats({1: [80], 2: [100], 3: [120]})
        assert pytest.approx(bs.cv, rel=1e-6) == 0.2

    def test_single_run_returns_nan(self):
        bs = _make_stats({1: [100, 200]})
        # Only 1 run → CV undefined.
        assert math.isnan(bs.cv)

    def test_zero_mean_returns_nan(self):
        bs = _make_stats({1: [0], 2: [0]})
        assert math.isnan(bs.cv)


class TestBenchStatsMaxMinRatio:
    def test_basic(self):
        # Per-run medians: [10, 20] → ratio = 2.0
        bs = _make_stats({1: [10], 2: [20]})
        assert bs.max_min_ratio == 2.0

    def test_zero_min_returns_nan(self):
        bs = _make_stats({1: [0], 2: [10]})
        assert math.isnan(bs.max_min_ratio)

    def test_empty_returns_nan(self):
        bs = ab.BenchStats(bench="X")
        assert math.isnan(bs.max_min_ratio)


class TestBenchStatsWithinRunCVMean:
    def test_zero_jitter(self):
        # Within each run all samples identical → within-run CV = 0.
        bs = _make_stats({1: [100, 100, 100], 2: [200, 200, 200]})
        assert bs.within_run_cv_mean == 0.0

    def test_single_sample_per_run_returns_nan(self):
        # Need ≥ 2 samples per run for within-run stdev — 1 sample → no CV.
        bs = _make_stats({1: [100], 2: [200]})
        assert math.isnan(bs.within_run_cv_mean)


class TestBenchStatsVerdict:
    def test_insufficient_when_one_run(self):
        bs = _make_stats({1: [100, 100]})
        verdict, reasons = bs.verdict()
        assert verdict == "INSUFFICIENT"
        assert len(reasons) == 1
        assert "need" in reasons[0].lower()

    def test_go_when_all_thresholds_met(self):
        # 3 runs, identical medians → CV=0, ratio=1, both within thresholds.
        bs = _make_stats({1: [100], 2: [100], 3: [100]})
        verdict, reasons = bs.verdict()
        assert verdict == "GO"
        assert reasons == []

    def test_no_go_high_cv(self):
        # Per-run medians [50, 100, 200] — CV ≈ 0.61, way over 0.25 threshold.
        bs = _make_stats({1: [50], 2: [100], 3: [200]})
        verdict, reasons = bs.verdict()
        assert verdict == "NO-GO"
        assert any("CV" in r for r in reasons)

    def test_no_go_high_ratio(self):
        # Per-run medians [100, 100, 200] — ratio=2.0 > 1.30.
        # CV is also high here (0.47); both thresholds breached.
        bs = _make_stats({1: [100], 2: [100], 3: [200]})
        verdict, reasons = bs.verdict()
        assert verdict == "NO-GO"
        assert any("max/min" in r for r in reasons)


# ---------------------------------------------------------------------------
# parse_bench_file
# ---------------------------------------------------------------------------
class TestParseBenchFile:
    def test_parses_canonical_line(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text(
            "BenchmarkScanDirHierarchical_1000-4   93   35422664 ns/op   "
            "1024 B/op   2 allocs/op\n",
            encoding="utf-8",
        )
        samples = list(ab.parse_bench_file(f, run_id=42))
        assert len(samples) == 1
        s = samples[0]
        assert s.run_id == 42
        assert s.bench == "BenchmarkScanDirHierarchical_1000"
        assert s.ns_per_op == 35422664.0

    def test_parses_decimal_ns_per_op(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text(
            "BenchmarkX-8   100   123.45 ns/op\n",
            encoding="utf-8",
        )
        samples = list(ab.parse_bench_file(f, run_id=1))
        assert len(samples) == 1
        assert samples[0].ns_per_op == 123.45

    def test_skips_non_bench_lines(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text(
            "goos: linux\n"
            "goarch: amd64\n"
            "pkg: github.com/x\n"
            "BenchmarkA-4   50   1000 ns/op\n"
            "PASS\n"
            "ok    github.com/x   1.234s\n",
            encoding="utf-8",
        )
        samples = list(ab.parse_bench_file(f, run_id=1))
        assert len(samples) == 1
        assert samples[0].bench == "BenchmarkA"

    def test_empty_file_yields_nothing(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_text("", encoding="utf-8")
        assert list(ab.parse_bench_file(f, run_id=1)) == []

    def test_multiple_iterations_same_bench(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text(
            "BenchmarkX-4   50   1000 ns/op\n"
            "BenchmarkX-4   50   1100 ns/op\n"
            "BenchmarkX-4   50   1050 ns/op\n",
            encoding="utf-8",
        )
        samples = list(ab.parse_bench_file(f, run_id=1))
        assert len(samples) == 3
        assert all(s.bench == "BenchmarkX" for s in samples)


# ---------------------------------------------------------------------------
# aggregate
# ---------------------------------------------------------------------------
class TestAggregate:
    def test_groups_by_bench(self):
        samples = [
            ab.RunSample(run_id=1, bench="BenchA", ns_per_op=100),
            ab.RunSample(run_id=1, bench="BenchA", ns_per_op=110),
            ab.RunSample(run_id=2, bench="BenchA", ns_per_op=120),
            ab.RunSample(run_id=1, bench="BenchB", ns_per_op=999),
        ]
        out = ab.aggregate(samples)
        assert set(out.keys()) == {"BenchA", "BenchB"}
        assert out["BenchA"].n_samples == 3
        assert out["BenchA"].n_runs == 2
        assert out["BenchB"].n_samples == 1

    def test_samples_by_run_populated(self):
        samples = [
            ab.RunSample(run_id=1, bench="BenchA", ns_per_op=100),
            ab.RunSample(run_id=1, bench="BenchA", ns_per_op=110),
            ab.RunSample(run_id=2, bench="BenchA", ns_per_op=200),
        ]
        out = ab.aggregate(samples)
        bs = out["BenchA"]
        assert sorted(bs.samples_by_run[1]) == [100, 110]
        assert bs.samples_by_run[2] == [200]

    def test_empty_input(self):
        assert ab.aggregate([]) == {}


# ---------------------------------------------------------------------------
# format_ns
# ---------------------------------------------------------------------------
class TestFormatNs:
    @pytest.mark.parametrize("ns,expected_substr", [
        (500, "ns"),       # < 1k → ns
        (1_500, "µs"),     # < 1M → µs
        (5_000_000, "ms"), # < 1G → ms
        (2_000_000_000, "s"),  # ≥ 1G → s
    ])
    def test_unit_selection(self, ns, expected_substr):
        out = ab.format_ns(ns)
        assert expected_substr in out

    def test_nan_returns_em_dash(self):
        assert ab.format_ns(float("nan")) == "—"

    def test_exact_thresholds(self):
        # 1000 ns is exactly the µs boundary — should be "1.0 µs".
        assert "µs" in ab.format_ns(1000)
        # 999 ns stays in "ns".
        assert "ns" in ab.format_ns(999)


# ---------------------------------------------------------------------------
# render_markdown_table
# ---------------------------------------------------------------------------
class TestRenderMarkdownTable:
    def test_includes_run_count_header(self):
        stats = {"BenchA": _make_stats({1: [100], 2: [100]})}
        out = ab.render_markdown_table(stats, n_runs_total=10, n_runs_succeeded=8)
        assert "8/10 runs" in out

    def test_threshold_line_present(self):
        stats = {"BenchA": _make_stats({1: [100], 2: [100]})}
        out = ab.render_markdown_table(stats, n_runs_total=10, n_runs_succeeded=10)
        # Should mention the thresholds from issue #67.
        assert "CV" in out
        assert "max/min" in out
        assert f"{ab.CV_THRESHOLD:.0%}" in out

    def test_empty_stats_still_renders(self):
        out = ab.render_markdown_table({}, n_runs_total=0, n_runs_succeeded=0)
        # Should produce at least the header + threshold lines.
        assert "Bench history analysis" in out

    def test_bench_name_in_output(self):
        stats = {"BenchmarkScanDirHierarchical": _make_stats(
            {1: [100], 2: [100]}, bench="BenchmarkScanDirHierarchical")}
        out = ab.render_markdown_table(stats, n_runs_total=2, n_runs_succeeded=2)
        assert "BenchmarkScanDirHierarchical" in out
