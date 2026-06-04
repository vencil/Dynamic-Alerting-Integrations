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

import json
import math
import types
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


# ===========================================================================
# Trend watchdog (--trend-watch) — sustained-regression detection + lifecycle.
# Codifies the scenarios validated ad-hoc during the v6 bench-gate redesign so
# the R1/R2 math, the canary noise floor (+ its cap), the sparse-series guard,
# and the issue open/update/close closed loop stay regression-protected.
# ===========================================================================

_BENCH = "BenchmarkScanDirHierarchical_1000"
_C = ab.CANARY_BENCH  # BenchmarkControlCanaryCPU


def _night(idx: int, benches: dict[str, float]) -> ab.NightRecord:
    """One nightly record; lower idx = newer (created_at descends with idx)."""
    return ab.NightRecord(
        run_id=1000 + idx,
        created_at=f"2026-05-{28 - idx:02d}T03:00:00Z",
        medians=dict(benches),
    )


def _flat(idx: int, bench_ns: float, canary_ns: float = 360_000.0) -> ab.NightRecord:
    return _night(idx, {_BENCH: bench_ns, _C: canary_ns})


def _trend_args(fixture: Path, **over) -> types.SimpleNamespace:
    d = dict(fixture_json=fixture, fixture_open_issue=None, cache_dir=None,
             workflow="bench-record.yaml", trend_limit=14, recent_nights=3,
             min_floor_pct=5.0, canary_floor_mult=3.0, creep_floor_pct=10.0,
             assignee="vencil", dry_run=True)
    d.update(over)
    return types.SimpleNamespace(**d)


def _write_fixture(tmp_path: Path, nights: list[ab.NightRecord]) -> Path:
    data = [{"run_id": n.run_id, "createdAt": n.created_at, "benches": n.medians}
            for n in nights]
    p = tmp_path / "nights.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


class TestCV:
    def test_fewer_than_two_points_is_zero(self):
        assert ab._cv([5.0]) == 0.0
        assert ab._cv([]) == 0.0

    def test_identical_points_zero(self):
        assert ab._cv([100.0, 100.0, 100.0]) == 0.0

    def test_zero_mean_is_zero(self):
        assert ab._cv([0.0, 0.0]) == 0.0

    def test_known_value(self):
        # [80, 100, 120] → mean 100, stdev 20 → CV 0.2
        assert pytest.approx(ab._cv([80.0, 100.0, 120.0]), rel=1e-6) == 0.2


class TestNightRecordsFromFixture:
    def test_loads_and_sorts_newest_first(self, tmp_path):
        # Deliberately write oldest-first; loader must sort newest-first.
        nights = [_flat(3, 35e6), _flat(0, 39e6), _flat(1, 38e6)]
        p = _write_fixture(tmp_path, nights)
        loaded = ab.night_records_from_fixture(p)
        created = [n.created_at for n in loaded]
        assert created == sorted(created, reverse=True)
        assert loaded[0].medians[_BENCH] == 39e6  # newest


class TestAnalyzeTrend:
    def _run(self, nights, **over):
        kw = dict(recent_k=3, min_floor_pct=5.0, canary_floor_mult=3.0,
                  creep_floor_pct=10.0)
        kw.update(over)
        return ab.analyze_trend(nights, **kw)

    def test_sustained_regression_detected(self):
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        findings, _ = self._run(nights)
        assert any(f.bench == _BENCH and f.kind == "sustained" for f in findings)

    def test_single_night_blip_silent(self):
        nights = [_flat(0, 42e6)] + [_flat(i, 35e6) for i in range(1, 8)]
        findings, _ = self._run(nights)
        assert findings == []

    def test_within_floor_silent(self):
        # +2.8% recent — below the 5% min floor.
        nights = [_flat(0, 36e6), _flat(1, 36e6), _flat(2, 36e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        findings, _ = self._run(nights)
        assert findings == []

    def test_creep_detected(self):
        # +2%/night ramp across the window (newest highest).
        nights = [_flat(9 - k, round(35e6 * (1.02 ** k))) for k in range(10)]
        nights.sort(key=lambda n: n.created_at, reverse=True)
        findings, _ = self._run(nights)
        assert any(f.bench == _BENCH for f in findings)

    def test_noisy_canary_does_not_silence_real_regression(self):
        # Real +15% sustained WITH a noisy canary (CV ~8%). The floor cap keeps
        # the canary from inflating the floor above the real effect.
        noisy = [330_000, 390_000, 335_000, 388_000, 360_000, 345_000, 378_000, 352_000]
        nights = [_flat(0, 40.25e6, noisy[0]), _flat(1, 40.3e6, noisy[1]),
                  _flat(2, 40.1e6, noisy[2])] + \
                 [_flat(i, 35e6, noisy[i]) for i in range(3, 8)]
        _, meta = self._run(nights)
        assert meta["floor_pct"] <= 10.0 + 1e-9   # cap enforced
        findings, _ = self._run(nights)
        assert any(f.bench == _BENCH for f in findings)

    def test_vanished_bench_is_skipped_not_misjudged(self):
        # Bench present only in OLDER nights (absent from newest 3 = perf-timeout
        # symptom), with a spike in the 2 oldest. Must be SKIPPED — not collapsed
        # so an old night masquerades as "today".
        nights = []
        for i in range(8):
            b = {_C: 360_000.0}
            if i >= 3:
                b[_BENCH] = 52.5e6 if i >= 6 else 35e6
            nights.append(_night(i, b))
        findings, _ = self._run(nights)
        assert all(f.bench != _BENCH for f in findings)

    def test_insufficient_nights_no_findings(self):
        nights = [_flat(i, 39e6) for i in range(3)]  # < recent_k + 2
        findings, _ = self._run(nights)
        assert findings == []

    def test_all_zero_no_crash_no_finding(self):
        nights = [_flat(i, 0.0, 0.0) for i in range(8)]
        findings, _ = self._run(nights)
        assert findings == []

    def test_creep_does_not_fire_on_lone_fast_outlier_night(self):
        # #702 regression. ONE anomalously-fast settled night (a lighter run or a
        # measurement glitch) must NOT pin the creep baseline. The recent nights
        # are flat at the true level — no regression — yet the old raw-`min`
        # baseline read them as "+75% vs best" and fired creep every night, so the
        # closed-loop issue could never close. Anchoring creep to the settled
        # MEDIAN shrugs the outlier off → no finding → the issue closes.
        nights = [_flat(0, 35e6), _flat(1, 35e6), _flat(2, 35e6)] + \
                 [_flat(3, 20e6)] + [_flat(i, 35e6) for i in range(4, 9)]
        findings, _ = self._run(nights)
        assert all(f.bench != _BENCH for f in findings)

    def test_creep_fires_when_recent_median_up_despite_one_noisy_night(self):
        # creep's distinct value over sustained: a real step-change where ONE
        # recent night dipped back to baseline (noise). sustained's all() misses
        # it; creep (recent MEDIAN vs anchor) still catches it.
        nights = [_flat(0, 39.2e6), _flat(1, 39.2e6), _flat(2, 35.7e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        findings, _ = self._run(nights)
        assert any(f.bench == _BENCH and f.kind == "creep" for f in findings)

    def test_creep_floor_rises_with_noisy_canary(self):
        # #702: the creep floor used to be pinned at its 10% default because it
        # shared the sustained cap (cap == default → max(0.10, ≤0.10) ≡ 0.10, a
        # no-op). A noisy canary must now lift the creep floor above 10% (its own
        # higher cap) so the noise-prone rule actually gets noise headroom, while
        # the sustained floor stays capped at 10%.
        noisy = [330_000, 390_000, 335_000, 388_000, 360_000, 345_000, 378_000, 352_000]
        nights = [_flat(i, 35e6, noisy[i]) for i in range(8)]
        _, meta = self._run(nights)
        assert meta["creep_floor_pct"] > 10.0 + 1e-9
        assert meta["creep_floor_pct"] <= 20.0 + 1e-9   # creep cap enforced
        assert meta["floor_pct"] <= 10.0 + 1e-9         # sustained cap unchanged


class TestRenderTrendIssueBody:
    def test_renders_table_with_finding(self):
        f = ab.TrendFinding(bench=_BENCH, kind="sustained", today_ns=39e6,
                            anchor_ns=35e6, recent_typical_ns=39e6,
                            pct_vs_anchor=11.4, pct_typical_vs_anchor=11.4)
        body = ab.render_trend_issue_body([f], {
            "canary_cv": 0.01, "floor_pct": 5.0, "creep_floor_pct": 10.0,
            "n_nights": 8, "recent_k": 3})
        assert _BENCH in body
        assert "sustained" in body
        assert "trend regression" in body.lower()

    def test_negative_pct_renders_signed_not_double_plus(self):
        # #702: a below-anchor creep finding used to print '+-1.2%' (hard-coded
        # '+' prefix on a negative value). Signed formatting fixes it.
        f = ab.TrendFinding(bench=_BENCH, kind="creep", today_ns=34e6,
                            anchor_ns=35e6, recent_typical_ns=39e6,
                            pct_vs_anchor=-1.2, pct_typical_vs_anchor=11.4)
        body = ab.render_trend_issue_body([f], {
            "canary_cv": 0.07, "floor_pct": 10.0, "creep_floor_pct": 20.0,
            "n_nights": 14, "recent_k": 3})
        assert "-1.2%" in body
        assert "+-" not in body
        assert "+11.4%" in body


class TestRunTrendWatchDryRun:
    def test_sustained_would_open_issue(self, tmp_path, capsys):
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        rc = ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights)))
        assert rc == 0
        assert "would open" in capsys.readouterr().err

    def test_clean_silent(self, tmp_path, capsys):
        nights = [_flat(i, 35e6) for i in range(8)]
        rc = ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights)))
        assert rc == 0
        assert "No sustained" in capsys.readouterr().out

    def test_recovered_with_open_issue_would_close(self, tmp_path, capsys):
        nights = [_flat(i, 35e6) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=99)
        rc = ab.run_trend_watch(args)
        assert rc == 0
        assert "would close" in capsys.readouterr().err

    def test_sustained_with_open_issue_would_update(self, tmp_path, capsys):
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=88)
        rc = ab.run_trend_watch(args)
        assert rc == 0
        assert "would update" in capsys.readouterr().err

    def test_insufficient_history_returns_zero(self, tmp_path, capsys):
        nights = [_flat(i, 39e6) for i in range(3)]
        rc = ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights)))
        assert rc == 0
        assert "not enough history" in capsys.readouterr().err


class TestMainTrendDispatch:
    """main() argparse + --trend-watch dispatch, fully offline via --fixture-json."""

    def test_main_trend_watch_fixture(self, tmp_path, monkeypatch, capsys):
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 35e6) for i in range(3, 8)]
        fixture = _write_fixture(tmp_path, nights)
        monkeypatch.setattr(
            "sys.argv",
            ["analyze_bench_history.py", "--trend-watch", "--dry-run",
             "--fixture-json", str(fixture)],
        )
        assert ab.main() == 0
        assert "would open" in capsys.readouterr().err
