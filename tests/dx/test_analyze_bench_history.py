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
    d = dict(fixture_json=fixture, fixture_open_issue=None, fixture_open_body=None,
             fixture_open_labels=None, cache_dir=None, workflow="bench-record.yaml",
             trend_limit=14, recent_nights=3, min_floor_pct=5.0, canary_floor_mult=3.0,
             creep_floor_pct=10.0, assignee="vencil", dry_run=True)
    d.update(over)
    return types.SimpleNamespace(**d)


def _write_fixture(tmp_path: Path, nights: list[ab.NightRecord]) -> Path:
    # cpu_model is emitted only when set, so every pre-#1396 fixture above stays
    # byte-identical (→ unstratified fallback → unchanged expectations).
    data = [{"run_id": n.run_id, "createdAt": n.created_at, "benches": n.medians,
             **({"cpu_model": n.cpu_model} if n.cpu_model else {})}
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
        # NOTE: the simulated issue now needs a readable state marker. Closing an
        # issue whose marker is absent is no longer allowed — see
        # TestMarkerHealthGatesClose::test_absent_marker_blocks_the_close.
        nights = [_hnight(i, 35e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=99,
                           fixture_open_body=ab._render_state_marker(
                               [[_BENCH, "sustained", 40e6, _EPYC]]))
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


# ===========================================================================
# Stateful issue lifecycle (#754 follow-up): update body in place + comment
# only on a flagged-set transition + recovering label. Kills the daily comment
# spam that made #702 unreadable.
# ===========================================================================

def _finding(bench, kind):
    return ab.TrendFinding(bench=bench, kind=kind, today_ns=39e6, anchor_ns=35e6,
                           recent_typical_ns=39e6, pct_vs_anchor=11.4,
                           pct_typical_vs_anchor=11.4)


class TestStatefulHelpers:
    def test_finding_state_is_canonical_sorted(self):
        state = ab._finding_state([_finding("Bz", "creep"), _finding("Ba", "sustained")])
        assert state == [["Ba", "sustained"], ["Bz", "creep"]]

    def test_state_marker_roundtrip_with_nested_array(self):
        state = [["Ba", "sustained"], ["Bz", "creep"]]
        body = "table…\n" + ab._render_state_marker(state)
        assert ab._parse_state_marker(body) == state

    def test_render_body_embeds_parseable_hidden_marker(self):
        body = ab.render_trend_issue_body([_finding(_BENCH, "creep")], {
            "canary_cv": 0.07, "floor_pct": 10.0, "creep_floor_pct": 20.0,
            "n_nights": 14, "recent_k": 3})
        assert "<!-- perf-trend-state" in body          # hidden HTML comment
        assert ab._parse_state_marker(body) == [[_BENCH, "creep"]]

    def test_parse_marker_absent_or_garbage_returns_none(self):
        assert ab._parse_state_marker("legacy body, no marker") is None
        assert ab._parse_state_marker("") is None
        assert ab._parse_state_marker(None) is None
        assert ab._parse_state_marker("<!-- perf-trend-state v1 not-json -->") is None

    def test_is_recovering(self):
        assert ab._is_recovering([_finding(_BENCH, "creep")]) is True
        assert ab._is_recovering([_finding("A", "creep"), _finding("B", "sustained")]) is False
        assert ab._is_recovering([]) is False

    def test_recovering_label_change(self):
        L = ab.RECOVERING_LABEL
        creep = [_finding(_BENCH, "creep")]
        sust = [_finding(_BENCH, "sustained")]
        prior_sust = [[_BENCH, "sustained"]]
        prior_creep = [[_BENCH, "creep"]]
        # sustained→creep, not yet labelled → add
        assert ab._recovering_label_change(creep, prior_sust, []) == "add"
        # creep-from-start (never sustained), not labelled → no-op (matches docs)
        assert ab._recovering_label_change(creep, prior_creep, []) is None
        # creep continues while already labelled → persist (no redundant add)
        assert ab._recovering_label_change(creep, prior_creep, [L]) is None
        # sustained returns while labelled → remove
        assert ab._recovering_label_change(sust, prior_creep, [L]) == "remove"
        # sustained→creep but already labelled → no redundant add
        assert ab._recovering_label_change(creep, prior_sust, [L]) is None

    def test_transition_unchanged_is_none(self):
        s = [[_BENCH, "sustained"]]
        assert ab._state_transition_comment(s, s) is None

    def test_transition_newly_and_cleared(self):
        c = ab._state_transition_comment([["Old", "sustained"]], [["New", "creep"]])
        assert c and "Newly flagged" in c and "`New`" in c
        assert "Recovered" in c and "`Old`" in c

    def test_transition_escalation_and_easing(self):
        up = ab._state_transition_comment([[_BENCH, "creep"]], [[_BENCH, "sustained"]])
        assert "Escalated to sustained" in up and f"`{_BENCH}`" in up
        down = ab._state_transition_comment([[_BENCH, "sustained"]], [[_BENCH, "creep"]])
        assert "Eased to creep" in down


class TestStatefulLifecycleDryRun:
    def _sustained_nights(self):
        return [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
               [_flat(i, 35e6) for i in range(3, 8)]

    def _creep_only_nights(self):
        # recent MEDIAN up but one recent night dipped → creep, not sustained
        return [_flat(0, 39.2e6), _flat(1, 39.2e6), _flat(2, 35.7e6)] + \
               [_flat(i, 35e6) for i in range(3, 8)]

    def test_same_state_refreshes_body_without_comment(self, tmp_path, capsys):
        prior = ab._render_state_marker([[_BENCH, "sustained"]])
        args = _trend_args(_write_fixture(tmp_path, self._sustained_nights()),
                           fixture_open_issue=42, fixture_open_body=prior)
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would update body" in err
        assert "no state change → body only" in err

    def test_changed_state_will_comment(self, tmp_path, capsys):
        prior = ab._render_state_marker([[_BENCH, "creep"]])  # was creep, now sustained
        args = _trend_args(_write_fixture(tmp_path, self._sustained_nights()),
                           fixture_open_issue=42, fixture_open_body=prior)
        assert ab.run_trend_watch(args) == 0
        assert "state changed → will comment" in capsys.readouterr().err

    def test_legacy_body_migration_is_silent(self, tmp_path, capsys):
        args = _trend_args(_write_fixture(tmp_path, self._sustained_nights()),
                           fixture_open_issue=42,
                           fixture_open_body="## old body, no state marker")
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would update body" in err and "no state change → body only" in err

    def test_sustained_to_creep_would_add_recovering_label(self, tmp_path, capsys):
        # Prior night was sustained; tonight only creep remains → add the label.
        prior = ab._render_state_marker([[_BENCH, "sustained"]])
        args = _trend_args(_write_fixture(tmp_path, self._creep_only_nights()),
                           fixture_open_issue=42, fixture_open_body=prior)
        assert ab.run_trend_watch(args) == 0
        assert f"would add `{ab.RECOVERING_LABEL}`" in capsys.readouterr().err

    def test_creep_from_start_does_not_add_recovering_label(self, tmp_path, capsys):
        # Never sustained (prior was already creep) → label must NOT be added.
        prior = ab._render_state_marker([[_BENCH, "creep"]])
        args = _trend_args(_write_fixture(tmp_path, self._creep_only_nights()),
                           fixture_open_issue=42, fixture_open_body=prior)
        assert ab.run_trend_watch(args) == 0
        assert ab.RECOVERING_LABEL not in capsys.readouterr().err

    def test_sustained_return_would_remove_recovering_label(self, tmp_path, capsys):
        # Issue currently carries the recovering label but sustained is back → remove.
        prior = ab._render_state_marker([[_BENCH, "creep"]])
        args = _trend_args(_write_fixture(tmp_path, self._sustained_nights()),
                           fixture_open_issue=42, fixture_open_body=prior,
                           fixture_open_labels=[ab.RECOVERING_LABEL])
        assert ab.run_trend_watch(args) == 0
        assert f"would remove `{ab.RECOVERING_LABEL}`" in capsys.readouterr().err

    def test_close_removes_stale_recovering_label(self, tmp_path, capsys):
        # Perf recovered (no findings) → close AND strip a lingering recovering label.
        # A readable marker is now a precondition of any auto-close (follow-up B).
        nights = [_hnight(i, 35e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=ab._render_state_marker(
                               [[_BENCH, "creep", 40e6, _EPYC]]),
                           fixture_open_labels=[ab.RECOVERING_LABEL])
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" in err
        assert f"would remove stale `{ab.RECOVERING_LABEL}`" in err


# ===========================================================================
# #1396 — the nightly runner pool is heterogeneous, and the watchdog was blind
# to it in three separate ways:
#   B  the `cpu:` header was never parsed, so a false alarm could not even be
#      diagnosed without re-downloading every artifact;
#   C  the anchor mixed host classes (guaranteed false positives), and
#      `findings == []` meant "recovered" even when nothing was evaluable;
#   D  recovery was judged against the SLIDING anchor, which drifts onto the
#      regression and then declares victory.
# ===========================================================================

_XEON = "Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz"
_XEON_OTHER = "INTEL(R) XEON(R) PLATINUM 8573C"
_EPYC = "AMD EPYC 7763 64-Core Processor"


def _hnight(idx: int, bench_ns: float, cpu: str | None,
            canary_ns: float = 360_000.0) -> ab.NightRecord:
    """One night, with a host class attached."""
    n = _flat(idx, bench_ns, canary_ns)
    n.cpu_model = cpu
    return n


# ── B: parse the `cpu:` header ──────────────────────────────────────────────
class TestParseCpuModel:
    def test_parses_cpu_header(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text(
            "goos: linux\ngoarch: amd64\npkg: github.com/x\n"
            f"cpu: {_EPYC}\n"
            "BenchmarkA-4   50   1000 ns/op\n",
            encoding="utf-8",
        )
        assert ab.parse_cpu_model(f) == _EPYC

    def test_absent_header_returns_none(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("goos: linux\nBenchmarkA-4   50   1000 ns/op\n", encoding="utf-8")
        assert ab.parse_cpu_model(f) is None

    def test_empty_value_is_not_a_host_class(self, tmp_path):
        f = tmp_path / "b.txt"
        f.write_text("cpu: \nBenchmarkA-4   50   1000 ns/op\n", encoding="utf-8")
        assert ab.parse_cpu_model(f) is None

    def test_first_header_wins_across_packages(self, tmp_path):
        # Multi-package runs repeat the header; they are the same machine.
        f = tmp_path / "b.txt"
        f.write_text(f"cpu: {_EPYC}\nBenchmarkA-4 5 1 ns/op\ncpu: {_EPYC}\n",
                     encoding="utf-8")
        assert ab.parse_cpu_model(f) == _EPYC

    def test_distinct_skus_are_distinct_classes(self):
        # Two Xeons are two machines. Folding them into a "Xeon" family would
        # rebuild the cross-host anchor this whole change exists to kill.
        assert _XEON != _XEON_OTHER


class TestFixtureCpuModel:
    def test_cpu_model_round_trips(self, tmp_path):
        nights = [_hnight(0, 35e6, _EPYC), _hnight(1, 35e6, _XEON)]
        loaded = ab.night_records_from_fixture(_write_fixture(tmp_path, nights))
        assert [n.cpu_model for n in loaded] == [_EPYC, _XEON]

    def test_missing_field_is_none(self, tmp_path):
        loaded = ab.night_records_from_fixture(
            _write_fixture(tmp_path, [_flat(0, 35e6), _flat(1, 35e6)]))
        assert all(n.cpu_model is None for n in loaded)


# ── C: same-class stratification + three-state verdict ──────────────────────
class TestStratifiedAnalyzeTrend:
    def _run(self, nights, **over):
        kw = dict(recent_k=3, min_floor_pct=5.0, canary_floor_mult=3.0,
                  creep_floor_pct=10.0)
        kw.update(over)
        return ab.analyze_trend(nights, **kw)

    def _mixed_host_window(self, cpu_recent):
        """Tonight's class sits at 39e6 and has ALWAYS sat there; the settled
        window is dominated by a faster host class at 35e6. Unstratified this is
        a textbook `sustained` (+11.4% for 3 straight nights) — and a textbook
        #1396 false positive, because nothing regressed: a different CPU ran."""
        return ([_hnight(i, 39e6, cpu_recent) for i in range(3)] +
                [_hnight(i, 35e6, _EPYC) for i in range(3, 6)] +
                [_hnight(i, 39e6, cpu_recent) for i in range(6, 9)])

    def test_cross_host_anchor_no_longer_fires(self):
        findings, meta = self._run(self._mixed_host_window(_XEON))
        assert findings == []
        assert meta["status"] == ab.STATUS_CLEAR
        assert meta["stratified"] is True
        assert meta["today_cpu_model"] == _XEON

    def test_same_data_unstratified_still_fires(self):
        # The counterfactual: identical numbers, host class unknown → the old
        # (unstratified) behaviour is preserved exactly, false positive and all.
        nights = self._mixed_host_window(_XEON)
        for n in nights:
            n.cpu_model = None
        findings, meta = self._run(nights)
        assert any(f.bench == _BENCH and f.kind == "sustained" for f in findings)
        assert meta["stratified"] is False
        assert meta["today_cpu_model"] is None

    def test_real_within_class_regression_still_fires(self):
        # Fire arithmetic is untouched: a genuine step INSIDE one host class is
        # still caught (and the foreign-class nights are simply not consulted).
        nights = ([_hnight(i, 39e6, _EPYC) for i in range(3)] +
                  [_hnight(3, 20e6, _XEON)] +
                  [_hnight(i, 35e6, _EPYC) for i in range(4, 9)])
        findings, meta = self._run(nights)
        assert any(f.bench == _BENCH and f.kind == "sustained" for f in findings)
        assert meta["status"] == ab.STATUS_FINDINGS

    def test_thin_stratum_is_inconclusive_not_clear(self):
        # Tonight's class has 3 recent + only 2 settled nights — below
        # MIN_SETTLED_SAME_CLASS. A 2-sample median is not a central tendency, so
        # the bench is not judged at all.
        nights = ([_hnight(i, 39e6, _XEON) for i in range(3)] +
                  [_hnight(3, 35e6, _EPYC)] +
                  [_hnight(4, 35e6, _XEON)] +
                  [_hnight(5, 35e6, _EPYC)] +
                  [_hnight(6, 35e6, _XEON)] +
                  [_hnight(7, 35e6, _EPYC)])
        findings, meta = self._run(nights)
        assert findings == []
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        assert _BENCH in meta["inconclusive_benches"]
        assert meta["evaluated_benches"] == []

    def test_one_more_same_class_night_makes_it_evaluable(self):
        # Same shape as above plus a third same-class settled night → judged.
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)] +
                  [_hnight(3, 35e6, _EPYC)] +
                  [_hnight(i, 35e6, _XEON) for i in range(4, 7)] +
                  [_hnight(7, 35e6, _EPYC)])
        findings, meta = self._run(nights)
        assert findings == []
        assert meta["status"] == ab.STATUS_CLEAR
        assert meta["evaluated_benches"] == [_BENCH]

    def test_meta_reports_window_composition(self):
        nights = self._mixed_host_window(_XEON)
        _, meta = self._run(nights)
        assert meta["cpu_class_counts"] == {_XEON: 6, _EPYC: 3}
        assert meta["n_class_nights"] == 6
        assert meta["n_nights"] == 9
        assert meta["min_settled"] == ab.MIN_SETTLED_SAME_CLASS

    def test_unknown_class_counted_as_unknown(self):
        nights = [_hnight(i, 35e6, None) for i in range(8)]
        _, meta = self._run(nights)
        assert meta["cpu_class_counts"] == {"unknown": 8}
        assert meta["min_settled"] == 2   # legacy threshold on the fallback path


class TestRenderBodyHostClassDisclosure:
    def test_stratified_body_names_tonights_host_and_composition(self):
        f = _finding(_BENCH, "sustained")
        body = ab.render_trend_issue_body([f], {
            "canary_cv": 0.07, "floor_pct": 10.0, "creep_floor_pct": 20.0,
            "n_nights": 14, "recent_k": 3, "stratified": True,
            "today_cpu_model": _EPYC, "n_class_nights": 9,
            "cpu_class_counts": {_EPYC: 9, _XEON: 5}, "min_settled": 3,
            "inconclusive_benches": ["BenchmarkQuiet"]})
        assert _EPYC in body and _XEON in body
        assert "×9" in body and "×5" in body
        assert "stratification: **ON**" in body
        assert "BenchmarkQuiet" in body            # what was NOT judged tonight

    def test_unstratified_body_says_so_loudly(self):
        body = ab.render_trend_issue_body([_finding(_BENCH, "creep")], {
            "canary_cv": 0.07, "floor_pct": 10.0, "creep_floor_pct": 20.0,
            "n_nights": 14, "recent_k": 3, "stratified": False,
            "today_cpu_model": None, "cpu_class_counts": {"unknown": 14}})
        assert "NOT stratified" in body
        assert "unknown" in body

    def test_legacy_meta_without_host_keys_still_renders(self):
        # render_trend_issue_body must not require the new meta keys.
        body = ab.render_trend_issue_body([_finding(_BENCH, "creep")], {
            "canary_cv": 0.07, "floor_pct": 10.0, "creep_floor_pct": 20.0,
            "n_nights": 14, "recent_k": 3})
        assert "NOT stratified" in body


# ── D: v2 state marker + frozen anchor ──────────────────────────────────────
class TestStateMarkerV2:
    def test_v1_marker_still_parses(self):
        # Migration: an issue opened before #1396 carries a v1 payload. It must
        # keep parsing (else every open issue silently loses its state).
        body = f'body…\n<!-- perf-trend-state v1 [["{_BENCH}","sustained"]] -->'
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]

    def test_v1_marker_has_no_frozen_anchor(self):
        body = f'<!-- perf-trend-state v1 [["{_BENCH}","sustained"]] -->'
        assert ab._parse_frozen_anchors(body) == {}

    def test_v2_marker_round_trips_anchor_and_host(self):
        rows = ab._frozen_state([_finding(_BENCH, "sustained")], {}, _EPYC)
        assert rows == [[_BENCH, "sustained", 35e6, _EPYC]]
        body = ab._render_state_marker(rows)
        assert "perf-trend-state v2" in body
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]
        assert ab._parse_frozen_anchors(body) == {_BENCH: (35e6, _EPYC)}

    def test_frozen_anchor_is_carried_forward_not_rebaselined(self):
        # THE point of D: tonight's anchor has drifted up to 38e6, but the issue
        # was filed against 35e6. The marker must keep 35e6.
        prior = {_BENCH: (35e6, _EPYC)}
        drifted = ab.TrendFinding(bench=_BENCH, kind="sustained", today_ns=42e6,
                                  anchor_ns=38e6, recent_typical_ns=42e6,
                                  pct_vs_anchor=10.5, pct_typical_vs_anchor=10.5)
        assert ab._frozen_state([drifted], prior, _EPYC) == \
            [[_BENCH, "sustained", 35e6, _EPYC]]

    def test_body_embeds_frozen_anchor(self):
        body = ab.render_trend_issue_body(
            [_finding(_BENCH, "sustained")],
            {"canary_cv": 0.01, "floor_pct": 5.0, "creep_floor_pct": 10.0,
             "n_nights": 14, "recent_k": 3, "stratified": True,
             "today_cpu_model": _EPYC, "cpu_class_counts": {_EPYC: 14},
             "n_class_nights": 14})
        assert ab._parse_frozen_anchors(body) == {_BENCH: (35e6, _EPYC)}

    def test_malformed_rows_degrade_to_no_state(self):
        assert ab._parse_state_marker('<!-- perf-trend-state v2 ["flat"] -->') is None
        assert ab._parse_frozen_anchors('<!-- perf-trend-state v2 ["flat"] -->') == {}
        # A non-numeric / non-positive anchor is dropped rather than trusted.
        assert ab._parse_frozen_anchors(
            f'<!-- perf-trend-state v2 [["{_BENCH}","creep","oops",null]] -->') == {}
        assert ab._parse_frozen_anchors(
            f'<!-- perf-trend-state v2 [["{_BENCH}","creep",0,null]] -->') == {}

    def test_frozen_host_may_be_null(self):
        # Frozen while the host class was unknown → value-only comparison later.
        body = ab._render_state_marker([[_BENCH, "creep", 35e6, None]])
        assert ab._parse_frozen_anchors(body) == {_BENCH: (35e6, None)}


class TestRecoveryBlockers:
    def test_no_blockers_when_below_frozen_baseline(self):
        assert ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                     {_BENCH: 34e6}, _EPYC, 0.05) == []

    def test_still_above_frozen_baseline_blocks(self):
        # The false-recovery case: the sliding anchor has drifted so nothing
        # fires, but tonight is still +20% over the baseline the issue was filed
        # against. Closing here would be the bug.
        blockers = ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                         {_BENCH: 42e6}, _EPYC, 0.05)
        assert len(blockers) == 1
        assert "frozen baseline" in blockers[0]

    def test_different_host_class_blocks(self):
        blockers = ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                         {_BENCH: 20e6}, _XEON, 0.05)
        assert len(blockers) == 1
        assert "not comparable" in blockers[0]

    def test_unknown_host_class_tonight_blocks(self):
        blockers = ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                         {_BENCH: 20e6}, None, 0.05)
        assert blockers and "not comparable" in blockers[0]

    def test_bench_absent_tonight_blocks(self):
        blockers = ab._recovery_blockers({_BENCH: (35e6, _EPYC)}, {}, _EPYC, 0.05)
        assert blockers and "absent" in blockers[0]

    def test_null_frozen_host_blocks_it_does_not_compare_values_only(self):
        # ⚠️ THIS EXPECTATION IS THE REVERSE OF THE ONE THIS TEST SHIPPED WITH.
        # It used to assert that a frozen row with an UNKNOWN host class falls
        # back to comparing the raw numbers. That is the cross-class close this
        # whole mechanism exists to prevent, just reached through the null: the
        # issue was frozen on a machine nobody recorded, and a night on a
        # different, faster machine then "proves" recovery — while
        # `_close_comment` printed "measured on the same host class". Unknown is
        # not a match; an unverified same-class claim is not evidence.
        blockers = ab._recovery_blockers({_BENCH: (35e6, None)},
                                         {_BENCH: 34e6}, _XEON, 0.05)
        assert blockers and "not comparable" in blockers[0]

    def test_both_classes_unknown_still_blocks(self):
        # Symmetric case: nothing is known about either side, so nothing about
        # comparability has been established. The unstratified fallback may still
        # FIRE (legacy detection behaviour is preserved) but it may not CLOSE.
        blockers = ab._recovery_blockers({_BENCH: (35e6, None)},
                                         {_BENCH: 1e6}, None, 0.05)
        assert blockers and "not comparable" in blockers[0]

    def test_boundary_value_exactly_on_the_floor_blocks(self):
        # `>=`, not `>`: a bench sitting EXACTLY on anchor × (1 + floor) has not
        # come back below the floor. New debt introduced by the frozen-anchor
        # commit and never pinned; a `>` here would silently close on the
        # boundary.
        exact = 35e6 * 1.05
        assert ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                     {_BENCH: exact}, _EPYC, 0.05)
        assert ab._recovery_blockers({_BENCH: (35e6, _EPYC)},
                                     {_BENCH: math.nextafter(exact, 0.0)}, _EPYC, 0.05) == []

    def test_five_to_ten_percent_band_is_pinned_to_the_floor(self):
        # Which floor the close uses was never nailed down: every fixture sat
        # far from it. +6% over the frozen baseline with a 5% floor must BLOCK;
        # the same value with a 10% floor must CLOSE.
        six_pct = {_BENCH: 35e6 * 1.06}
        assert ab._recovery_blockers({_BENCH: (35e6, _EPYC)}, six_pct, _EPYC, 0.05)
        assert ab._recovery_blockers({_BENCH: (35e6, _EPYC)}, six_pct, _EPYC, 0.10) == []

    def test_empty_frozen_map_never_blocks(self):
        # v1 / legacy issue → pre-#1396 close behaviour.
        assert ab._recovery_blockers({}, {_BENCH: 99e6}, _EPYC, 0.05) == []


class TestCloseComment:
    def test_frozen_close_states_what_was_compared(self):
        c = ab._close_comment({_BENCH: (35e6, _EPYC)}, {_BENCH: 34e6}, _EPYC, 0.05)
        assert "frozen baseline" in c
        assert _EPYC in c
        # The old text claimed a recovery the watchdog could not observe.
        assert "has returned below the floor" not in c

    def test_legacy_close_labels_its_weaker_evidence(self):
        c = ab._close_comment({}, {}, _EPYC, 0.05)
        assert "sliding" in c
        assert "has returned below the floor" not in c


# ── D + C end-to-end through run_trend_watch (dry-run, offline) ─────────────
class TestThreeStateLifecycleDryRun:
    def _same_class(self, ns, n=8, cpu=_EPYC):
        return [_hnight(i, ns, cpu) for i in range(n)]

    def test_inconclusive_never_closes_an_open_issue(self, tmp_path, capsys):
        # Tonight's class has 3 recent + 2 settled nights → nothing evaluable.
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)] +
                  [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                   _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                   _hnight(7, 35e6, _EPYC)])
        marker = ab._render_state_marker([[_BENCH, "sustained", 35e6, _EPYC]])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        assert "INCONCLUSIVE" in out.out
        assert "NOT closing perf-trend issue #42" in out.err
        assert "would close" not in out.err

    def test_inconclusive_does_not_open_an_issue_either(self, tmp_path, capsys):
        nights = ([_hnight(i, 45e6, _XEON) for i in range(3)] +
                  [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                   _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                   _hnight(7, 35e6, _EPYC)])
        args = _trend_args(_write_fixture(tmp_path, nights))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        assert "would open" not in out.err
        assert "INCONCLUSIVE" in out.out

    def test_frozen_anchor_blocks_the_false_recovery(self, tmp_path, capsys):
        # The Critical bug, end to end. The regression is PERMANENT: every night
        # in the window sits at 42e6, so the sliding anchor has caught up and
        # nothing fires. Pre-#1396 this closed the issue announcing recovery.
        nights = self._same_class(42e6)
        marker = ab._render_state_marker([[_BENCH, "sustained", 35e6, _EPYC]])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "NOT closing perf-trend issue #42" in err
        assert "still ≥ frozen baseline" in err
        assert "would close" not in err

    def test_genuine_recovery_below_frozen_baseline_closes(self, tmp_path, capsys):
        nights = self._same_class(34e6)
        marker = ab._render_state_marker([[_BENCH, "sustained", 35e6, _EPYC]])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        assert "would close" in capsys.readouterr().err

    def test_other_host_class_tonight_does_not_close(self, tmp_path, capsys):
        # Tonight ran on a faster machine and looks great. That is not evidence
        # the regression on the frozen host class is gone.
        nights = self._same_class(20e6, cpu=_XEON)
        marker = ab._render_state_marker([[_BENCH, "sustained", 35e6, _EPYC]])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "NOT closing perf-trend issue #42" in err
        assert "not comparable" in err

    def test_v1_marker_issue_still_closes(self, tmp_path, capsys):
        # Migration path: no frozen baseline exists on a pre-#1396 issue, so it
        # keeps the old close behaviour rather than being wedged open forever.
        nights = self._same_class(35e6)
        marker = '<!-- perf-trend-state v1 [["%s","sustained"]] -->' % _BENCH
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        assert "would close" in capsys.readouterr().err

    def test_new_issue_freezes_tonights_anchor(self, tmp_path, capsys):
        nights = ([_hnight(i, 39e6, _EPYC) for i in range(3)] +
                  [_hnight(i, 35e6, _EPYC) for i in range(3, 8)])
        args = _trend_args(_write_fixture(tmp_path, nights))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        assert "would open" in out.err
        frozen = ab._parse_frozen_anchors(out.out)
        assert frozen == {_BENCH: (35e6, _EPYC)}

    def test_update_preserves_the_original_frozen_anchor(self, tmp_path, capsys):
        # Night N: the regression has partly aged into the window, so tonight's
        # sliding anchor is 39e6 — but the issue was filed against 35e6. The
        # refreshed body must still carry 35e6.
        nights = ([_hnight(i, 44e6, _EPYC) for i in range(5)] +
                  [_hnight(i, 39e6, _EPYC) for i in range(5, 9)])
        marker = ab._render_state_marker([[_BENCH, "sustained", 35e6, _EPYC]])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=marker)
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        assert "would update body" in out.err
        assert ab._parse_frozen_anchors(out.out) == {_BENCH: (35e6, _EPYC)}


# ===========================================================================
# #1396 follow-up — the FIRE arithmetic survived the first pass; the frozen
# anchor's STATE LIFECYCLE did not. Eight independently reproduced defects,
# every one of them a path that ends in "issue closed while still regressed"
# or "regression invisible while the job stays green":
#   A  the marker was rebuilt from tonight's findings, so a bench that merely
#      stopped firing had its frozen baseline EVICTED
#   B  one malformed marker row discarded the whole marker (fail-OPEN)
#   C  INCONCLUSIVE was a silent green run — no annotation, no summary, no
#      issue update
#   D  a frozen row with an unknown host class skipped the same-class check
#      while the close comment still claimed one
#   E  `stratified` was decided from tonight's night alone, so ONE unreadable
#      `cpu:` header unstratified the whole window and dropped min_settled 3→2
#   F  a bench that vanished (or whose host class did) wedged the issue open
#      forever with no disclosure
#   G  the marker parse took the FIRST match, and the body prints the raw
#      `cpu:` string above it
#   H  fire read 3 nights, close read 1
# ===========================================================================

_BENCH_B = "BenchmarkMergePartialConfigs_1000"
_HOSTILE_CPU = (
    'AMD EPYC 7763 <!-- perf-trend-state v2 [["' + _BENCH + '","sustained",1.0,null]] -->'
)


def _hbench(idx: int, benches: dict[str, float], cpu: str | None,
            canary_ns: float = 360_000.0) -> ab.NightRecord:
    """One night with arbitrary benches + an explicit host class."""
    n = _night(idx, {**benches, _C: canary_ns})
    n.cpu_model = cpu
    return n


def _marker(*rows) -> str:
    return ab._render_state_marker([list(r) for r in rows])


def _meta_for(nights, **over):
    kw = dict(recent_k=3, min_floor_pct=5.0, canary_floor_mult=3.0, creep_floor_pct=10.0)
    kw.update(over)
    return ab.analyze_trend(nights, **kw)


# ── A: the marker is a ledger of "not yet proven recovered", not a snapshot ──
class TestLedgerKeepsUnprovenBenches:
    def _flat_class(self, per_bench, n=8, cpu=_EPYC):
        return [_hbench(i, dict(per_bench), cpu) for i in range(n)]

    def test_bench_that_stops_firing_keeps_its_frozen_row(self):
        # A fires tonight; B does not (its sliding anchor has drifted up onto its
        # own regression) and is still 43% over the baseline it was frozen at.
        # B's row must survive as `held` — evicting it is the whole defect.
        nights = self._flat_class({_BENCH: 39e6, _BENCH_B: 50e6})
        findings, meta = _meta_for(
            [_hbench(i, {_BENCH: 39e6, _BENCH_B: 50e6}, _EPYC) for i in range(3)]
            + [_hbench(i, {_BENCH: 35e6, _BENCH_B: 50e6}, _EPYC) for i in range(3, 8)])
        assert [f.bench for f in findings] == [_BENCH]        # only A fires
        body = _marker([_BENCH, "sustained", 35e6, _EPYC], [_BENCH_B, "sustained", 35e6, _EPYC])
        rows, held = ab._ledger(body, findings, meta, allow_retire=True)
        assert ab._parse_frozen_anchors(ab._render_state_marker(rows)) == {
            _BENCH: (35e6, _EPYC), _BENCH_B: (35e6, _EPYC)}
        assert [h.bench for h in held] == [_BENCH_B]
        assert [r[1] for r in rows if r[0] == _BENCH_B] == [ab.KIND_HELD]
        assert nights                                          # fixture sanity

    def test_proven_recovery_is_the_only_way_a_row_retires(self):
        nights = self._flat_class({_BENCH: 39e6, _BENCH_B: 34e6})
        findings, meta = _meta_for(
            [_hbench(i, {_BENCH: 39e6, _BENCH_B: 34e6}, _EPYC) for i in range(3)]
            + [_hbench(i, {_BENCH: 35e6, _BENCH_B: 34e6}, _EPYC) for i in range(3, 8)])
        body = _marker([_BENCH, "sustained", 35e6, _EPYC], [_BENCH_B, "sustained", 35e6, _EPYC])
        rows, held = ab._ledger(body, findings, meta, allow_retire=True)
        assert [r[0] for r in rows] == [_BENCH]                # B proved it, B is gone
        assert held == []
        assert nights

    def test_inconclusive_night_may_not_retire_anything(self):
        # allow_retire=False: a night that judged nothing cannot retire a row,
        # even one whose number happens to look good.
        nights = self._flat_class({_BENCH: 34e6})
        _findings, meta = _meta_for(nights)
        body = _marker([_BENCH, "sustained", 35e6, _EPYC])
        rows, held = ab._ledger(body, [], meta, allow_retire=False)
        assert [r[0] for r in rows] == [_BENCH]
        assert [h.code for h in held] == ["pending"]

    def test_two_benches_recovering_on_different_nights_do_not_false_close(
            self, tmp_path, capsys):
        # END TO END, the reproduction. Night 1: A fires, B is silently
        # regressed. Night 2: A has recovered and nothing fires at all. With the
        # marker rebuilt from findings, B's frozen baseline was thrown away on
        # night 1 and night 2 closed the issue with B still +43%.
        n1 = ([_hbench(i, {_BENCH: 39e6, _BENCH_B: 50e6}, _EPYC) for i in range(3)]
              + [_hbench(i, {_BENCH: 35e6, _BENCH_B: 50e6}, _EPYC) for i in range(3, 8)])
        args1 = _trend_args(_write_fixture(tmp_path, n1), fixture_open_issue=42,
                            fixture_open_body=_marker(
                                [_BENCH, "sustained", 35e6, _EPYC],
                                [_BENCH_B, "sustained", 35e6, _EPYC]))
        assert ab.run_trend_watch(args1) == 0
        night1_body = capsys.readouterr().out
        assert ab._parse_frozen_anchors(night1_body) == {
            _BENCH: (35e6, _EPYC), _BENCH_B: (35e6, _EPYC)}

        n2 = [_hbench(i, {_BENCH: 34e6, _BENCH_B: 50e6}, _EPYC) for i in range(8)]
        night2 = tmp_path / "n2"
        night2.mkdir(exist_ok=True)
        args2 = _trend_args(_write_fixture(night2, n2), fixture_open_issue=42,
                            fixture_open_body=night1_body)
        assert ab.run_trend_watch(args2) == 0
        err = capsys.readouterr().err
        assert "NOT closing perf-trend issue #42" in err
        assert "would close" not in err
        assert _BENCH_B in err

    def test_held_row_is_not_announced_as_recovered(self):
        # `held` means "still owed proof". Letting it reach the transition
        # comment would post "Recovered (no longer flagged)" about exactly the
        # bench whose recovery has NOT been shown.
        prior = [[_BENCH, "sustained"], [_BENCH_B, ab.KIND_HELD]]
        assert ab._state_transition_comment(prior, [[_BENCH, "sustained"]]) is None
        c = ab._state_transition_comment(prior, [[_BENCH, "creep"]])
        assert c and "Recovered" not in c and "Eased to creep" in c

    def test_bench_that_stopped_firing_is_not_announced_as_recovered(self):
        # The other direction of the same rule. Here the bench IS gone from the
        # flagged set (so the marker-side filter above cannot help) and is held
        # only in tonight's freshly computed ledger. Announcing "Recovered" here
        # would put the false all-clear back in the notification layer right
        # after we removed it from the close decision — the sliding anchor
        # drifting onto the regression is the likeliest reason it stopped firing.
        held = [ab.HeldRow(bench=_BENCH_B, anchor_ns=35e6, cpu_model=_EPYC,
                           code="above", reason="still above the frozen baseline")]
        c = ab._state_transition_comment(
            [[_BENCH, "sustained"], [_BENCH_B, "sustained"]],
            [[_BENCH, "sustained"]], held)
        assert c is not None
        assert "Recovered" not in c
        assert "NOT proved recovered" in c and _BENCH_B in c

    def test_a_genuinely_retired_bench_is_still_announced_as_recovered(self):
        # Counterpart: absent from the ledger means it passed _recovery_block,
        # so the recovery claim is earned and must survive.
        c = ab._state_transition_comment(
            [[_BENCH, "sustained"], [_BENCH_B, "sustained"]],
            [[_BENCH, "sustained"]], [])
        assert c and "Recovered (no longer flagged):" in c and _BENCH_B in c

    def test_held_row_does_not_drive_the_recovering_label(self):
        # The label means "sustained cleared, creep remains". A held row is not
        # a sustained finding and must not be read as one.
        assert ab._recovering_label_change(
            [_finding(_BENCH, "creep")], [[_BENCH_B, ab.KIND_HELD]], []) is None


# ── B: an unreadable marker must fail CLOSED ────────────────────────────────
class TestMarkerHealthGatesClose:
    _DAMAGED = ('<!-- perf-trend-state v2 [["%s","sustained",35000000.0,"%s"],["oops"]] -->'
                % (_BENCH, _EPYC))

    def test_one_broken_row_no_longer_discards_the_good_ones(self):
        health, rows = ab._state_marker_rows(self._DAMAGED)
        assert health == ab.MARKER_DAMAGED
        assert ab._parse_frozen_anchors(self._DAMAGED) == {_BENCH: (35e6, _EPYC)}
        assert len(rows) == 1

    def test_damaged_marker_does_not_free_a_regressed_issue(self, tmp_path, capsys):
        # The measured repro: +43% over the frozen baseline, one junk row in the
        # marker, and the whole marker was dropped → blockers empty → closed.
        nights = [_hnight(i, 50e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=self._DAMAGED)
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" not in err
        assert "::warning::" in err

    def test_damaged_marker_blocks_even_when_surviving_rows_are_satisfied(
            self, tmp_path, capsys):
        # Every row we CAN read says "recovered" — but a row we could not read
        # may have said anything, so this is not evidence of recovery.
        nights = [_hnight(i, 34e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=self._DAMAGED)
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" not in err
        assert "state marker is damaged" in err
        assert "::warning::" in err
        assert "would update body" not in err     # never overwrite unreadable state

    def test_absent_marker_blocks_the_close(self, tmp_path, capsys):
        # Hand-deleted marker (or an issue a human labelled `perf-trend`). The
        # watchdog does not know what it was watching → it must not close it.
        nights = [_hnight(i, 34e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body="## someone edited this by hand")
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" not in err
        assert "state marker is absent" in err
        assert f"would add `{ab.UNVERIFIABLE_LABEL}`" in err

    def test_v1_marker_keeps_the_documented_legacy_close(self):
        health, rows = ab._state_marker_rows(
            '<!-- perf-trend-state v1 [["%s","sustained"]] -->' % _BENCH)
        assert health == ab.MARKER_LEGACY_V1
        assert len(rows) == 1

    def test_empty_v2_payload_is_healthy_not_legacy(self):
        # Every row retired. Same shape as an empty v1 payload, opposite meaning
        # — which is why the declared version is parsed rather than guessed.
        assert ab._state_marker_rows(ab._render_state_marker([]))[0] == ab.MARKER_OK


# ── C: INCONCLUSIVE must be loud ────────────────────────────────────────────
class TestInconclusiveIsLoud:
    def _thin_stratum(self, ns=45e6):
        # 3 recent XEON nights + only 2 settled XEON → nothing evaluable.
        return ([_hnight(i, ns, _XEON) for i in range(3)]
                + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                   _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                   _hnight(7, 35e6, _EPYC)])

    def test_real_regression_on_an_inconclusive_night_is_annotated(
            self, tmp_path, capsys, monkeypatch):
        # A REAL +30% permanent step on a night with too few same-class peers
        # used to produce: green tick, exit 0, no annotation, no summary,
        # nothing on the issue. Nobody would ever learn it happened.
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        args = _trend_args(_write_fixture(tmp_path, self._thin_stratum()))
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "::warning::" in err and "INCONCLUSIVE" in err
        assert "not a passing perf verdict" in err
        assert "INCONCLUSIVE" in summary.read_text(encoding="utf-8")

    def test_inconclusive_summary_names_the_host_class_and_the_counts(
            self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, self._thin_stratum())))
        text = summary.read_text(encoding="utf-8")
        assert _XEON in text
        assert "need ≥ 3 recent + 3 settled" in text
        capsys.readouterr()

    def test_inconclusive_refreshes_the_open_issue_with_the_night_it_saw(
            self, tmp_path, capsys):
        args = _trend_args(_write_fixture(tmp_path, self._thin_stratum()),
                           fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained", 35e6, _EPYC]))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        assert "would update body of perf-trend issue #42" in out.err
        assert "INCONCLUSIVE — not evaluated tonight" in out.err
        assert "NOT closing perf-trend issue #42" in out.err

    def test_inconclusive_body_says_which_night_and_that_it_was_not_judged(self):
        nights = ([_hnight(i, 45e6, _XEON) for i in range(3)]
                  + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                     _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                     _hnight(7, 35e6, _EPYC)])
        _findings, meta = _meta_for(nights)
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        body = ab.render_trend_issue_body([], meta)
        assert "Not evaluated tonight (INCONCLUSIVE)" in body
        assert meta["today_night"] in body            # WHICH night these numbers are
        assert "nothing is closed" in body

    def test_inconclusive_night_does_not_downgrade_a_flagged_row(self, tmp_path):
        # Regression guard for the ledger itself: writing `held` over a
        # `sustained` row on a night that measured NOTHING is a state change
        # made on no evidence, and it reads next night as sustained→gone, i.e. a
        # "Recovered (no longer flagged)" comment for a bench nobody looked at.
        # An INCONCLUSIVE night preserves the last kind the watchdog knew.
        _f, meta = _meta_for(self._thin_stratum())
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        rows, _held = ab._ledger(_marker([_BENCH, "sustained", 35e6, _EPYC]), [], meta,
                                 allow_retire=False)
        assert ab._parse_state_marker(ab._render_state_marker(rows)) == [[_BENCH, "sustained"]]
        # …and a night that COULD judge does downgrade it (that is a real
        # transition, and the "no longer flagged" comment is then earned).
        _f2, meta2 = _meta_for([_hnight(i, 34e6, _EPYC) for i in range(8)])
        rows2, _h2 = ab._ledger(_marker([_BENCH, "sustained", 50e6, _EPYC]), [], meta2,
                                allow_retire=True)
        assert rows2 == []                      # 34e6 < 50e6 → proved, retired
        rows3, _h3 = ab._ledger(_marker([_BENCH, "sustained", 30e6, _EPYC]), [], meta2,
                                allow_retire=True)
        assert [r[1] for r in rows3] == [ab.KIND_HELD]     # not proved → held
        assert tmp_path                          # fixture dir unused, keep signature

    def test_inconclusive_still_never_fires_and_never_closes(self, tmp_path, capsys):
        args = _trend_args(_write_fixture(tmp_path, self._thin_stratum()),
                           fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained", 35e6, _EPYC]))
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would open" not in err and "would close" not in err


# ── D: an unverified same-class claim is not evidence and is not printed ────
class TestCloseCommentClaims:
    def test_close_comment_omits_the_same_class_claim_when_unverified(self):
        c = ab._close_comment({_BENCH: (35e6, None)}, {_BENCH: 34e6}, _EPYC, 0.05)
        assert "same host class" not in c

    def test_close_comment_keeps_the_claim_when_every_row_matches(self):
        c = ab._close_comment({_BENCH: (35e6, _EPYC)}, {_BENCH: 34e6}, _EPYC, 0.05)
        assert "same host class" in c and _EPYC in c

    def test_close_comment_says_recent_window_not_tonight(self):
        c = ab._close_comment({_BENCH: (35e6, _EPYC)}, {_BENCH: 34e6}, _EPYC, 0.05)
        assert "recent-window median" in c


# ── E: one unreadable header must not unstratify the whole window ───────────
class TestStratificationScope:
    def test_single_unreadable_night_does_not_open_a_30_percent_ticket(self):
        # Tonight's `cpu:` failed to parse; the rest of the window is labelled.
        # Unstratified (the old fallback) this is a textbook +30.8% sustained
        # across a 2-night settled minimum — a CLEAR night turned into a filed
        # regression by one unparsed line.
        nights = ([_hnight(0, 39e6, None)]
                  + [_hnight(i, 39e6, _EPYC) for i in (1, 2)]
                  + [_hnight(i, 29.8e6, _XEON) for i in range(3, 8)])
        findings, meta = _meta_for(nights)
        assert findings == []
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        assert meta["stratification"] == ab.STRATA_TONIGHT_UNKNOWN
        assert meta["min_settled"] == ab.MIN_SETTLED_SAME_CLASS   # never relaxed to 2

    def test_fully_unlabelled_window_keeps_the_legacy_path(self):
        # The ONE case that may still fall back: nothing to stratify by at all.
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 29.8e6) for i in range(3, 8)]
        findings, meta = _meta_for(nights)
        assert any(f.kind == "sustained" for f in findings)
        assert meta["stratification"] == ab.STRATA_LEGACY
        assert meta["min_settled"] == 2

    def test_tonight_unknown_body_does_not_claim_an_unstratified_verdict(self):
        nights = ([_hnight(0, 39e6, None)]
                  + [_hnight(i, 39e6, _EPYC) for i in (1, 2)]
                  + [_hnight(i, 29.8e6, _XEON) for i in range(3, 8)])
        _f, meta = _meta_for(nights)
        body = ab.render_trend_issue_body([], meta)
        assert "unreadable" in body
        assert "NOT stratified" not in body     # no verdict was produced at all


# ── F: a wedged issue is disclosed, never auto-closed ───────────────────────
class TestUnverifiableDisclosure:
    def _clear_night_without(self, bench_missing=True):
        # BenchmarkB is measured every night (so the night is CLEAR, not
        # INCONCLUSIVE); _BENCH has vanished from the run entirely.
        benches = {_BENCH_B: 35e6} if bench_missing else {_BENCH_B: 35e6, _BENCH: 34e6}
        return [_hbench(i, benches, _EPYC) for i in range(8)]

    def test_vanished_bench_holds_the_row_and_counts_the_nights(self):
        _f, meta = _meta_for(self._clear_night_without())
        assert meta["status"] == ab.STATUS_CLEAR
        body = _marker([_BENCH, "sustained", 35e6, _EPYC])
        rows, held = ab._ledger(body, [], meta, allow_retire=True)
        assert [h.code for h in held] == ["absent"]
        assert [h.streak for h in held] == [1]
        assert rows == [[_BENCH, ab.KIND_HELD, 35e6, _EPYC, 1]]

    def test_streak_accumulates_across_nights_and_survives_the_marker(self):
        _f, meta = _meta_for(self._clear_night_without())
        body = _marker([_BENCH, "sustained", 35e6, _EPYC])
        for expected in (1, 2, 3):
            rows, held = ab._ledger(body, [], meta, allow_retire=True)
            assert [h.streak for h in held] == [expected]
            body = ab._render_state_marker(rows)
        assert ab._parse_held_streaks(body) == {_BENCH: 3}

    def test_streak_resets_once_the_bench_is_measurable_again(self):
        _f, meta = _meta_for([_hbench(i, {_BENCH_B: 35e6, _BENCH: 50e6}, _EPYC)
                              for i in range(8)])
        body = _marker([_BENCH, ab.KIND_HELD, 35e6, _EPYC, 9])
        rows, held = ab._ledger(body, [], meta, allow_retire=True)
        assert [h.code for h in held] == ["above"]      # measured, still regressed
        assert [h.streak for h in held] == [0]
        assert rows == [[_BENCH, ab.KIND_HELD, 35e6, _EPYC]]   # 5th element dropped

    def test_body_discloses_the_wedge_once_it_crosses_the_threshold(self):
        _f, meta = _meta_for(self._clear_night_without())
        body_in = _marker([_BENCH, ab.KIND_HELD, 35e6, _EPYC,
                           ab.UNVERIFIABLE_DISCLOSE_AT - 1])
        rows, held = ab._ledger(body_in, [], meta, allow_retire=True)
        body = ab.render_trend_issue_body([], meta, rows=rows, held=held)
        assert "Held — awaiting proof of recovery" in body
        assert "Not verifiable for ≥ 3 consecutive nights" in body
        assert f"`{_BENCH}` ({ab.UNVERIFIABLE_DISCLOSE_AT} nights)" in body
        assert ab.UNVERIFIABLE_LABEL in body

    def test_wedged_issue_gets_the_label_and_is_not_closed(self, tmp_path, capsys):
        nights = self._clear_night_without()
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker(
                               [_BENCH, ab.KIND_HELD, 35e6, _EPYC,
                                ab.UNVERIFIABLE_DISCLOSE_AT - 1]))
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" not in err
        assert f"would add `{ab.UNVERIFIABLE_LABEL}`" in err
        assert "absent from the recent same-class window" in err

    def test_label_is_dropped_again_once_the_bench_comes_back(self, tmp_path, capsys):
        nights = [_hbench(i, {_BENCH_B: 35e6, _BENCH: 50e6}, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, ab.KIND_HELD, 35e6, _EPYC, 9]),
                           fixture_open_labels=[ab.UNVERIFIABLE_LABEL])
        assert ab.run_trend_watch(args) == 0
        assert f"would remove `{ab.UNVERIFIABLE_LABEL}`" in capsys.readouterr().err


# ── G: the marker parse must not be hijackable from the body above it ───────
class TestMarkerHijack:
    def test_parse_takes_the_last_marker_not_the_first(self):
        body = ("host: " + _HOSTILE_CPU + "\n\nreal table…\n"
                + _marker([_BENCH_B, "sustained", 42e6, _EPYC]))
        assert ab._parse_frozen_anchors(body) == {_BENCH_B: (42e6, _EPYC)}

    def test_hostile_cpu_string_is_neutered_when_rendered(self):
        _f, meta = _meta_for([_hnight(i, 35e6, _HOSTILE_CPU) for i in range(8)])
        body = ab.render_trend_issue_body(
            [_finding(_BENCH_B, "sustained")], meta,
            frozen={_BENCH_B: (42e6, _HOSTILE_CPU)})
        # Exactly one marker survives in the rendered body, and it is ours.
        assert len(ab._STATE_MARKER_RE.findall(body)) == 1
        assert ab._parse_frozen_anchors(body) == {_BENCH_B: (42e6, _HOSTILE_CPU)}
        assert "&lt;!--" in body

    def test_end_to_end_hostile_host_class_does_not_erase_the_frozen_state(
            self, tmp_path, capsys):
        nights = ([_hnight(i, 39e6, _HOSTILE_CPU) for i in range(3)]
                  + [_hnight(i, 35e6, _HOSTILE_CPU) for i in range(3, 8)])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker(
                               [_BENCH, "sustained", 30e6, _HOSTILE_CPU]))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr().out
        assert ab._parse_frozen_anchors(out) == {_BENCH: (30e6, _HOSTILE_CPU)}


# ── H: close reads the same window fire does ────────────────────────────────
class TestCloseUsesTheRecentWindow:
    def test_one_lucky_night_does_not_close_a_permanent_regression(
            self, tmp_path, capsys):
        # Tonight dipped to 34e6; the other two recent nights are still at 50e6.
        # Judging on tonight alone retired a live +43% regression.
        nights = ([_hnight(0, 34e6, _EPYC)]
                  + [_hnight(i, 50e6, _EPYC) for i in range(1, 8)])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained", 35e6, _EPYC]))
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" not in err
        assert "still ≥ frozen baseline" in err

    def test_recent_median_below_the_frozen_baseline_closes(self, tmp_path, capsys):
        nights = ([_hnight(i, 34e6, _EPYC) for i in range(2)]
                  + [_hnight(i, 50e6, _EPYC) for i in range(2, 8)])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained", 35e6, _EPYC]))
        assert ab.run_trend_watch(args) == 0
        assert "would close" in capsys.readouterr().err

    def test_recent_medians_use_the_same_alignment_rule_as_fire(self):
        # Present in only 2 of the 3 recent nights → not comparable, exactly as
        # the fire path refuses to judge it.
        nights = ([_hbench(0, {_BENCH_B: 35e6}, _EPYC)]
                  + [_hbench(i, {_BENCH: 34e6, _BENCH_B: 35e6}, _EPYC) for i in range(1, 8)])
        _f, meta = _meta_for(nights)
        assert _BENCH not in meta["recent_medians"]
        assert _BENCH_B in meta["recent_medians"]


# ── I: gaps a mutation sweep found in the tests themselves ──────────────────
class TestStratificationKeyIsTheRawString:
    def test_distinct_skus_are_distinct_classes(self):
        # This test used to assert `_XEON != _XEON_OTHER` — two test-local
        # constants, zero calls into the module under test. It is now fed
        # through analyze_trend: tonight's Xeon must NOT be anchored on the
        # other Xeon's nights, which is what folding SKUs into a "Xeon family"
        # would do (and would rebuild the #1396 hole).
        nights = ([_hnight(i, 39e6, _XEON) for i in range(3)]
                  + [_hnight(i, 35e6, _XEON_OTHER) for i in range(3, 8)])
        findings, meta = _meta_for(nights)
        assert findings == []                       # +11.4% across SKUs is NOT a trend
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        assert meta["cpu_class_counts"] == {_XEON: 3, _XEON_OTHER: 5}
        assert meta["n_class_nights"] == 3

    def test_same_sku_across_the_window_does_fire(self):
        # Control for the above: identical numbers on ONE SKU still fire, so the
        # assertion above is about the stratification key, not about the floor.
        nights = ([_hnight(i, 39e6, _XEON) for i in range(3)]
                  + [_hnight(i, 35e6, _XEON) for i in range(3, 8)])
        findings, _meta = _meta_for(nights)
        assert any(f.kind == "sustained" for f in findings)


class TestRecentWindowIsStratifiedToo:
    def test_foreign_class_nights_inside_the_recent_window_are_skipped(self):
        # Every earlier fixture put tonight's class contiguously at the top, so
        # positional slicing of the recent window would have passed all of them.
        # Two foreign, much faster nights sit at positions 1-2 here: taken
        # positionally the recent window would read [39, 20, 20] — sustained's
        # all() breaks, creep's median collapses to 20e6, and the close path's
        # recent median would "prove" a recovery that never happened.
        nights = ([_hnight(0, 39e6, _EPYC), _hnight(1, 20e6, _XEON),
                   _hnight(2, 20e6, _XEON)]
                  + [_hnight(i, 39e6, _EPYC) for i in (3, 4)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(5, 10)])
        findings, meta = _meta_for(nights)
        assert any(f.kind == "sustained" for f in findings)
        assert findings[0].today_ns == 39e6
        assert findings[0].recent_typical_ns == 39e6      # the 20e6 nights are not in it
        assert meta["recent_medians"][_BENCH] == 39e6

    def test_foreign_class_nights_cannot_manufacture_a_finding_either(self):
        # Mirror image: foreign SLOW nights inside the recent window must not
        # push a flat same-class series over the floor (positionally the recent
        # median would be 90e6 against a 35e6 anchor = a filed ticket).
        nights = ([_hnight(0, 35e6, _EPYC), _hnight(1, 90e6, _XEON),
                   _hnight(2, 90e6, _XEON)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(3, 10)])
        findings, _meta = _meta_for(nights)
        assert findings == []


class TestCanaryIsStratified:
    def test_noise_floor_comes_from_tonights_class_only(self):
        # `canary_ns` was never once passed as a keyword in the suite, so the
        # canary series being stratified was untested. Tonight's class has a
        # DEAD-QUIET canary (floor stays at the 5% minimum) while the other
        # class is wild (3× its CV would cap the floor at 10%). A +6% step then
        # separates the two: it fires only if the canary is drawn per class.
        wild = [200_000.0, 500_000.0, 210_000.0, 490_000.0, 220_000.0]
        nights = ([_hnight(i, 37.1e6, _EPYC, canary_ns=360_000.0) for i in range(3)]
                  + [_hnight(i, 35e6, _EPYC, canary_ns=360_000.0) for i in range(3, 6)]
                  + [_hnight(6 + i, 35e6, _XEON, canary_ns=c) for i, c in enumerate(wild)])
        findings, meta = _meta_for(nights)
        assert meta["canary_cv"] == 0.0
        assert meta["floor_pct"] == pytest.approx(5.0)
        assert any(f.bench == _BENCH and f.kind == "sustained" for f in findings)

    def test_same_window_with_one_class_would_have_been_silenced(self):
        # The counterfactual for the above: put the wild canary on TONIGHT's
        # class and the +6% step is (correctly) below the noise floor.
        wild = [200_000.0, 500_000.0, 210_000.0, 490_000.0, 220_000.0, 480_000.0,
                230_000.0, 470_000.0]
        nights = ([_hnight(i, 37.1e6, _EPYC, canary_ns=wild[i]) for i in range(3)]
                  + [_hnight(i, 35e6, _EPYC, canary_ns=wild[i]) for i in range(3, 8)])
        _findings, meta = _meta_for(nights)
        assert meta["floor_pct"] > 6.0


class TestTonightIsPinnedToTonight:
    def test_today_is_the_newest_night_not_merely_some_night(self):
        # Every trend fixture used to give all recent nights the SAME value, so
        # "today" being tonight was never actually asserted. Distinct values
        # here: today_ns must be the newest night's, and meta must name it.
        nights = ([_hnight(0, 41e6, _EPYC), _hnight(1, 40e6, _EPYC),
                   _hnight(2, 39e6, _EPYC)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(3, 8)])
        findings, meta = _meta_for(nights)
        assert findings[0].today_ns == 41e6                     # not 39e6, not 40e6
        assert findings[0].recent_typical_ns == 40e6            # median of the three
        assert meta["today_night"] == nights[0].created_at
        assert meta["today_run_id"] == nights[0].run_id
        assert meta["today_night"] > nights[1].created_at       # newest-first, really
