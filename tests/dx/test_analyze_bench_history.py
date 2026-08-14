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

    def test_short_window_bail_is_annotated_not_silent(
            self, tmp_path, capsys, monkeypatch):
        # The short-window bail was the LAST silent-green path in the watchdog:
        # a stderr line, exit 0, no `::warning::`, no step summary, no issue
        # touched — indistinguishable from a night that ran and found nothing.
        # A watchdog that did not run must say so in the places a human looks.
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        nights = [_flat(i, 39e6) for i in range(3)]
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "did NOT run" in err
        assert "not a passing perf verdict" in err
        text = summary.read_text(encoding="utf-8")
        assert "not evaluated" in text
        assert "only 3 usable nights" in text

    def test_short_window_bail_writes_no_summary_outside_actions(
            self, tmp_path, capsys, monkeypatch):
        # No GITHUB_STEP_SUMMARY (local run) → the annotation still fires, the
        # summary write is a no-op rather than a crash.
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        nights = [_flat(i, 39e6) for i in range(3)]
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        assert "::warning::" in capsys.readouterr().err


class TestFileNewIssueAnnotations:
    """The annotation must describe what actually happened, not what was about
    to be attempted. Before the fix the ONLY `::warning::` in the whole file was
    emitted BEFORE the unassigned retry and asserted the issue had been filed."""

    def _calls(self, monkeypatch, outcomes):
        """Stub _gh_write with a scripted list of results; record the calls."""
        seen = []
        it = iter(outcomes)

        def fake(cmd):
            seen.append(cmd)
            return next(it)

        monkeypatch.setattr(ab, "_gh_write", fake)
        return seen

    def test_assigned_create_succeeds_no_annotation(self, monkeypatch, capsys):
        calls = self._calls(monkeypatch, [True])
        assert ab._file_new_issue("body", "vencil", 1) is True
        assert len(calls) == 1 and "--assignee" in calls[0]
        assert "::warning::" not in capsys.readouterr().err

    def test_unassigned_retry_succeeds_then_says_so(self, monkeypatch, capsys):
        calls = self._calls(monkeypatch, [False, True])
        assert ab._file_new_issue("body", "some-org", 1) is True
        assert len(calls) == 2 and "--assignee" not in calls[1]
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "filed UNASSIGNED" in err
        # …and it does NOT claim a total failure.
        assert "NO issue could be filed" not in err

    def test_total_failure_says_no_issue_exists(self, monkeypatch, capsys, tmp_path):
        # THE BUG: both creates fail. The old code had already announced
        # "filing perf-trend issue unassigned" and then said nothing more, so the
        # run ended green with an annotation describing a ticket that was never
        # opened.
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        self._calls(monkeypatch, [False, False])
        assert ab._file_new_issue("body", "some-org", 2) is False
        err = capsys.readouterr().err
        assert "NO issue could be filed" in err
        assert "Nobody has been notified" in err
        # The false claim is gone: nothing says the issue WAS filed.
        assert "filed UNASSIGNED" not in err
        assert "issue creation FAILED" in summary.read_text(encoding="utf-8")

    def test_no_assignee_configured_still_reports_a_total_failure(
            self, monkeypatch, capsys):
        calls = self._calls(monkeypatch, [False])
        assert ab._file_new_issue("body", None, 1) is False
        assert len(calls) == 1 and "--assignee" not in calls[0]
        assert "NO issue could be filed" in capsys.readouterr().err


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
        nights = [_flat(i, 35e6) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=ab._render_state_marker([[_BENCH, "creep"]]),
                           fixture_open_labels=[ab.RECOVERING_LABEL])
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" in err
        assert f"would remove stale `{ab.RECOVERING_LABEL}`" in err


# ===========================================================================
# #1396 — the nightly runner pool is heterogeneous, and the watchdog was blind
# to it in two separate ways:
#   B  the `cpu:` header was never parsed, so a false alarm could not even be
#      diagnosed without re-downloading every artifact;
#   C  the anchor mixed host classes (guaranteed false positives), and
#      `findings == []` meant "recovered" even when nothing was evaluable.
#
# A third defect — recovery judged against the SLIDING anchor, which drifts onto
# the regression and then declares victory — is NOT fixed here. Its fix (a
# per-bench frozen baseline) was implemented, reviewed and reverted before merge;
# see the module docstring in analyze_bench_history.py. Nothing below asserts a
# frozen-baseline behaviour, because none exists.
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

    def test_each_of_the_three_states_renders_its_own_verdict_block(self):
        base = {"canary_cv": 0.01, "floor_pct": 5.0, "creep_floor_pct": 10.0,
                "n_nights": 14, "recent_k": 3, "stratified": True,
                "today_cpu_model": _EPYC, "n_class_nights": 14,
                "cpu_class_counts": {_EPYC: 14}, "min_settled": 3}
        findings = ab.render_trend_issue_body(
            [_finding(_BENCH, "sustained")], {**base, "status": ab.STATUS_FINDINGS})
        clear = ab.render_trend_issue_body([], {**base, "status": ab.STATUS_CLEAR})
        inconcl = ab.render_trend_issue_body(
            [], {**base, "status": ab.STATUS_INCONCLUSIVE, "n_class_nights": 4})
        # FINDINGS renders the table; the other two say IN WORDS why there is
        # none — an empty table under a "regression" heading is exactly how an
        # unevaluated night used to read as a recovered one.
        assert "| Rule |" in findings
        assert "| Rule |" not in clear and "Nothing above its floor tonight" in clear
        assert "| Rule |" not in inconcl and "Not evaluated tonight" in inconcl
        assert "nothing is closed" in inconcl and "nothing is closed" not in clear


# ── C: the three-state verdict end-to-end through run_trend_watch ───────────
class TestThreeStateLifecycleDryRun:
    def test_inconclusive_never_closes_an_open_issue(self, tmp_path, capsys):
        # Tonight's class has 3 recent + 2 settled nights → nothing evaluable.
        # Pre-#1396 `findings == []` walked straight into the close path.
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)] +
                  [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                   _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                   _hnight(7, 35e6, _EPYC)])
        marker = ab._render_state_marker([[_BENCH, "sustained"]])
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

    def test_clear_night_on_the_same_class_still_closes(self, tmp_path, capsys):
        # The control for the two above: CLEAR (benches WERE judged, none above
        # its floor) keeps the pre-existing closed loop. Only INCONCLUSIVE was
        # split out of it.
        nights = [_hnight(i, 35e6, _EPYC) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=ab._render_state_marker(
                               [[_BENCH, "sustained"]]))
        assert ab.run_trend_watch(args) == 0
        assert "would close" in capsys.readouterr().err


_BENCH_B = "BenchmarkMergePartialConfigs_1000"
# A `cpu:` header that ships its own lookalike marker. Disclosing the raw header
# (#1396's B half) is what put a free-form, artifact-authored string into the
# issue body ABOVE the watchdog's own marker.
_HOSTILE_CPU = (
    'AMD EPYC 7763 <!-- perf-trend-state v1 [["' + _BENCH + '","creep"]] -->'
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
                           fixture_open_body=_marker([_BENCH, "sustained"]))
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

    def test_inconclusive_body_refresh_preserves_the_prior_flagged_set(self):
        # The refresh above is what makes INCONCLUSIVE visible, and it is also
        # what can DESTROY state: it rewrites the body, marker included, on a
        # night that measured nothing. Writing tonight's empty finding set there
        # would erase the prior flagged set on no evidence — and the next night
        # that DID judge would then read `[] → [sustained]` and post "Newly
        # flagged" for a bench that has been flagged all along.
        _f, meta = _meta_for(self._thin_stratum())
        assert meta["status"] == ab.STATUS_INCONCLUSIVE
        prior = _marker([_BENCH, "sustained"])
        body = ab.render_trend_issue_body([], meta,
                                          state=ab._parse_state_marker(prior))
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]

    def test_inconclusive_refresh_of_a_markerless_issue_plants_an_empty_marker(self):
        # No prior state to preserve (legacy / hand-filed issue) → an empty
        # marker, not a crash, and not a fabricated flagged set.
        _f, meta = _meta_for(self._thin_stratum())
        body = ab.render_trend_issue_body([], meta,
                                          state=ab._parse_state_marker("no marker") or [])
        assert ab._parse_state_marker(body) == []

    def test_run_trend_watch_hands_the_prior_state_to_the_refresh(
            self, tmp_path, capsys, monkeypatch):
        # The wiring, not just the renderer: the INCONCLUSIVE branch is the one
        # caller allowed to override the marker, and it must override it with
        # what the issue already said — not with tonight's empty finding set.
        # (The gh write itself is unreachable in fixture mode, so the seam is
        # what gets asserted.)
        seen = {}
        real = ab.render_trend_issue_body

        def spy(findings, meta, state=None):
            seen["findings"], seen["state"] = findings, state
            return real(findings, meta, state)

        monkeypatch.setattr(ab, "render_trend_issue_body", spy)
        args = _trend_args(_write_fixture(tmp_path, self._thin_stratum()),
                           fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained"]))
        assert ab.run_trend_watch(args) == 0
        capsys.readouterr()
        assert seen["findings"] == []                       # nothing was judged
        assert seen["state"] == [[_BENCH, "sustained"]]     # …so nothing is forgotten

    def test_inconclusive_still_never_fires_and_never_closes(self, tmp_path, capsys):
        args = _trend_args(_write_fixture(tmp_path, self._thin_stratum()),
                           fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained"]))
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would open" not in err and "would close" not in err


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


# ── the marker parse must not be hijackable from the body above it ──────────
#
# ⚠️ These two guards look like leftovers from the reverted frozen-anchor work.
# They are not: the hole they close was opened by B. Disclosing the runner's raw
# `cpu:` header is what first put an artifact-authored, free-form string into the
# issue body — and the body prints it ABOVE the watchdog's own state marker.
class TestMarkerHijack:
    def test_parse_takes_the_last_marker_not_the_first(self):
        # Measured on the real body: the genuine marker sat at offset 1773 and
        # `re.search` matched the fake at 596. First-match means the prior state
        # is whatever the artifact says it is.
        body = ("host: " + _HOSTILE_CPU + "\n\nreal table…\n"
                + _marker([_BENCH_B, "sustained"]))
        assert ab._parse_state_marker(body) == [[_BENCH_B, "sustained"]]
        # The fake IS in the body and IS matchable — last-match is what saves it.
        assert len(ab._STATE_MARKER_RE.findall(body)) == 2

    def test_marker_match_returns_the_last_occurrence(self):
        body = _marker(["A", "creep"]) + "\n" + _marker(["Z", "sustained"])
        m = ab._marker_match(body)
        assert m is not None and '"Z"' in m.group(0)
        assert ab._marker_match("") is None
        assert ab._marker_match(None) is None
        assert ab._marker_match("no marker here") is None

    def test_hostile_cpu_string_is_neutered_when_rendered(self):
        _f, meta = _meta_for([_hnight(i, 35e6, _HOSTILE_CPU) for i in range(8)])
        body = ab.render_trend_issue_body([_finding(_BENCH_B, "sustained")], meta)
        # Exactly one marker survives in the rendered body, and it is ours.
        assert len(ab._STATE_MARKER_RE.findall(body)) == 1
        assert ab._parse_state_marker(body) == [[_BENCH_B, "sustained"]]
        assert "&lt;!--" in body                       # the delimiters were escaped

    def test_safe_md_escapes_both_delimiters_and_backticks(self):
        assert ab._safe_md("<!-- x -->") == "&lt;!-- x --&gt;"
        assert ab._safe_md("a `b` c") == "a 'b' c"     # stays inside its code span
        assert ab._safe_md(None) == ""                 # unknown host class
        assert ab._safe_md("") == ""
        assert ab._safe_md("AMD EPYC 7763") == "AMD EPYC 7763"   # ordinary strings pass

    def test_end_to_end_hostile_host_class_cannot_forge_the_prior_state(
            self, tmp_path, capsys):
        # The full path: a hostile `cpu:` header is disclosed in the body, and
        # the state read back out of that body is still the watchdog's own.
        nights = ([_hnight(i, 39e6, _HOSTILE_CPU) for i in range(3)]
                  + [_hnight(i, 35e6, _HOSTILE_CPU) for i in range(3, 8)])
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained"]))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr().out
        assert ab._parse_state_marker(out) == [[_BENCH, "sustained"]]

    def test_forged_marker_would_otherwise_fabricate_a_recovery_comment(self):
        # WHY it matters, stated as behaviour rather than as a parse detail: the
        # fake claims `_BENCH` was flagged as `creep`; tonight it is `sustained`.
        # Reading the fake as prior state turns a no-op night into an "Eased to
        # creep"/"Newly flagged" churn comment about numbers nobody measured.
        forged = "host: " + _HOSTILE_CPU + "\n" + _marker([_BENCH, "sustained"])
        assert ab._state_transition_comment(
            ab._parse_state_marker(forged), [[_BENCH, "sustained"]]) is None


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
        # all() breaks and creep's median collapses to 20e6, so a live
        # regression goes silent because the pool reshuffled.
        nights = ([_hnight(0, 39e6, _EPYC), _hnight(1, 20e6, _XEON),
                   _hnight(2, 20e6, _XEON)]
                  + [_hnight(i, 39e6, _EPYC) for i in (3, 4)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(5, 10)])
        findings, _meta = _meta_for(nights)
        assert any(f.kind == "sustained" for f in findings)
        assert findings[0].today_ns == 39e6
        assert findings[0].recent_typical_ns == 39e6      # the 20e6 nights are not in it

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
