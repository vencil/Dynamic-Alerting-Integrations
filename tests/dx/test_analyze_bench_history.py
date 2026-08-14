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

import argparse
import json
import math
import subprocess
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
        # Literal, not `f"{ab.CV_THRESHOLD:.0%}"` — that renders whatever the
        # constant happens to be, so the assertion holds for any threshold and
        # proves only that the table interpolates *something*. 25% is the #67
        # gate value and is quoted in the playbook.
        assert "CV ≤ 25%" in out
        assert "max/min ≤ 1.3×" in out

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

# ── WIRE FORMAT — literals on purpose ───────────────────────────────────────
# These values LEAVE THE PROCESS: they become GitHub label names, issue titles,
# the hidden state marker parsed back the next night, and the prose an operator
# greps for. Asserting `meta["status"] == ab.STATUS_CLEAR` only proves the module
# agrees with itself — rename the value and every such assertion stays green
# while every already-open issue, every saved query and every runbook line
# breaks. The module's names are tied to these literals in exactly one place
# (`TestWireFormatIsPinned`); everywhere else the literal is the expectation.
_CANARY = "BenchmarkControlCanaryCPU"
_C = _CANARY
_LABEL = "perf-trend"
_RECOVERING = "perf-trend:recovering"
_REPO = "vencil/Dynamic-Alerting-Integrations"
_TITLE = "Nightly bench trend regression detected"
_CLEAR, _FINDINGS, _INCONCLUSIVE = "CLEAR", "FINDINGS", "INCONCLUSIVE"
_STRATA_ON = "on"
_STRATA_LEGACY = "legacy-unstratified"
_STRATA_TONIGHT_UNKNOWN = "tonight-unknown"
_MIN_SETTLED = 3


class TestWireFormatIsPinned:
    """The one place module names are bound to their on-the-wire values.

    Every constant here is either sent to GitHub or parsed back out of GitHub.
    A rename that keeps the identifier but changes the string is a silent
    protocol break for issues that already exist, so it must fail HERE (one
    obvious, deliberate edit) rather than nowhere."""

    def test_status_values(self):
        assert (ab.STATUS_CLEAR, ab.STATUS_FINDINGS, ab.STATUS_INCONCLUSIVE) == \
            ("CLEAR", "FINDINGS", "INCONCLUSIVE")

    def test_stratification_values(self):
        assert (ab.STRATA_ON, ab.STRATA_LEGACY, ab.STRATA_TONIGHT_UNKNOWN) == \
            ("on", "legacy-unstratified", "tonight-unknown")

    def test_github_facing_values(self):
        assert ab.PERF_TREND_LABEL == "perf-trend"
        assert ab.RECOVERING_LABEL == "perf-trend:recovering"
        assert ab.REPO == "vencil/Dynamic-Alerting-Integrations"
        assert ab.WORKFLOW_FILE == "bench-record.yaml"
        assert ab.ARTIFACT_FILE == "bench-baseline.txt"

    def test_detector_thresholds(self):
        assert ab.CANARY_BENCH == "BenchmarkControlCanaryCPU"
        assert ab.CV_THRESHOLD == 0.25
        assert ab.RATIO_THRESHOLD == 1.30
        assert ab.MIN_RUN_RELIABILITY == 26
        assert ab.MIN_SETTLED_SAME_CLASS == 3

    def test_inconclusive_reason_codes(self):
        assert (ab.REASON_NO_RECENT, ab.REASON_THIN_ANCHOR) == ("no-recent", "thin-anchor")
        # `thin-anchor` was itself two causes: the WINDOW's settled same-class
        # nights being short, vs THIS bench's own history being short — see
        # TestThinAnchorSeparatesTheWindowFromTheBench.
        assert ab.REASON_THIN_BENCH == "thin-bench-history"
        # The third cause exists only for benches named in an issue's marker
        # that are absent from the window entirely (a rename) — see
        # TestUnverifiedCauseHasAThirdOption.
        assert ab.REASON_ABSENT == "absent-from-window"

    def test_state_marker_is_byte_exact(self):
        # The marker is written by tonight's run and re-parsed by tomorrow's, so
        # its serialisation is a wire format between two processes.
        assert ab._render_state_marker([["Ba", "sustained"], ["Bz", "creep"]]) == \
            '<!-- perf-trend-state v1 [["Ba","sustained"],["Bz","creep"]] -->'


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
        L = _RECOVERING
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
        # Line-bound, not substring-bound. The old form asserted four substrings
        # against the whole comment, so swapping `newly` and `cleared` in the
        # renderer produced a comment that says the recovered bench is newly
        # flagged and the new one recovered — and the test still passed
        # (verified). Each label must be bound to its OWN bench.
        c = ab._state_transition_comment([["Old", "sustained"]], [["New", "creep"]])
        lines = c.splitlines()
        assert "**Newly flagged:** `New`" in lines
        assert "**Recovered (no longer flagged):** `Old`" in lines

    def test_transition_escalation_and_easing(self):
        up = ab._state_transition_comment([[_BENCH, "creep"]], [[_BENCH, "sustained"]])
        assert f"**Escalated to sustained:** `{_BENCH}`" in up.splitlines()
        down = ab._state_transition_comment([[_BENCH, "sustained"]], [[_BENCH, "creep"]])
        assert f"**Eased to creep:** `{_BENCH}`" in down.splitlines()

    def test_a2_dropping_out_while_unmeasured_is_not_a_recovery(self):
        # A2. `Old` was flagged and tonight did not measure it. Announcing
        # "Recovered (no longer flagged)" is a claim about a number nobody took.
        c = ab._state_transition_comment([["Old", "sustained"], ["New", "creep"]],
                                         [["New", "creep"]], unverified=["Old"])
        lines = c.splitlines()
        assert "**Not measured tonight (state unknown, NOT recovered):** `Old`" in lines
        assert not any(li.startswith("**Recovered") for li in lines)

    def test_a2_a_measured_bench_still_recovers_normally(self):
        # Control: same shape, but `Old` WAS measured tonight → real recovery.
        c = ab._state_transition_comment([["Old", "sustained"], ["New", "creep"]],
                                         [["New", "creep"]], unverified=[])
        lines = c.splitlines()
        assert "**Recovered (no longer flagged):** `Old`" in lines
        assert not any(li.startswith("**Not measured") for li in lines)


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
        assert f"would add `{_RECOVERING}`" in capsys.readouterr().err

    def test_creep_from_start_does_not_add_recovering_label(self, tmp_path, capsys):
        # Never sustained (prior was already creep) → label must NOT be added.
        prior = ab._render_state_marker([[_BENCH, "creep"]])
        args = _trend_args(_write_fixture(tmp_path, self._creep_only_nights()),
                           fixture_open_issue=42, fixture_open_body=prior)
        assert ab.run_trend_watch(args) == 0
        assert _RECOVERING not in capsys.readouterr().err

    def test_sustained_return_would_remove_recovering_label(self, tmp_path, capsys):
        # Issue currently carries the recovering label but sustained is back → remove.
        prior = ab._render_state_marker([[_BENCH, "creep"]])
        args = _trend_args(_write_fixture(tmp_path, self._sustained_nights()),
                           fixture_open_issue=42, fixture_open_body=prior,
                           fixture_open_labels=[_RECOVERING])
        assert ab.run_trend_watch(args) == 0
        assert f"would remove `{_RECOVERING}`" in capsys.readouterr().err

    def test_close_removes_stale_recovering_label(self, tmp_path, capsys):
        # Perf recovered (no findings) → close AND strip a lingering recovering label.
        nights = [_flat(i, 35e6) for i in range(8)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=ab._render_state_marker([[_BENCH, "creep"]]),
                           fixture_open_labels=[_RECOVERING])
        assert ab.run_trend_watch(args) == 0
        err = capsys.readouterr().err
        assert "would close" in err
        assert f"would remove stale `{_RECOVERING}`" in err


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
        # Multi-package runs repeat the header. The fixture used to write the
        # SAME value twice, so "first wins" was unobservable — last-wins passed
        # it too. Two DIFFERENT values make the claim testable; the first is the
        # one the artifact's own bench rows were measured under.
        f = tmp_path / "b.txt"
        f.write_text(f"cpu: {_EPYC}\nBenchmarkA-4 5 1 ns/op\ncpu: {_XEON}\n",
                     encoding="utf-8")
        assert ab.parse_cpu_model(f) == _EPYC

    def test_header_is_taken_verbatim_not_folded(self, tmp_path):
        # The docstring's promise is "Exact string equality, or nothing": no
        # case-folding, no punctuation stripping, no family normalisation.
        raw = "  Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz  "
        f = tmp_path / "b.txt"
        f.write_text(f"cpu:{raw}\nBenchmarkA-4 5 1 ns/op\n", encoding="utf-8")
        got = ab.parse_cpu_model(f)
        assert got == "Intel(R) Xeon(R) Platinum 8370C CPU @ 2.80GHz"   # only outer ws
        assert got != got.lower()
        assert "(R)" in got and "@" in got


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
        assert meta["status"] == _CLEAR
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
        assert meta["status"] == _FINDINGS

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
        assert meta["status"] == _INCONCLUSIVE
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
        assert meta["status"] == _CLEAR
        assert meta["evaluated_benches"] == [_BENCH]

    def test_meta_reports_window_composition(self):
        nights = self._mixed_host_window(_XEON)
        _, meta = self._run(nights)
        assert meta["cpu_class_counts"] == {_XEON: 6, _EPYC: 3}
        assert meta["n_class_nights"] == 6
        assert meta["n_nights"] == 9
        assert meta["min_settled"] == _MIN_SETTLED

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
            [_finding(_BENCH, "sustained")], {**base, "status": _FINDINGS})
        clear = ab.render_trend_issue_body([], {**base, "status": _CLEAR})
        inconcl = ab.render_trend_issue_body(
            [], {**base, "status": _INCONCLUSIVE, "n_class_nights": 4})
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
        assert meta["status"] == _INCONCLUSIVE
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
        assert meta["status"] == _INCONCLUSIVE
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

        def spy(findings, meta, state=None, tracked=None):
            seen["findings"], seen["state"] = findings, state
            return real(findings, meta, state, tracked)

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
        assert meta["status"] == _INCONCLUSIVE
        assert meta["stratification"] == _STRATA_TONIGHT_UNKNOWN
        assert meta["min_settled"] == _MIN_SETTLED   # never relaxed to 2

    def test_fully_unlabelled_window_keeps_the_legacy_path(self):
        # The ONE case that may still fall back: nothing to stratify by at all.
        nights = [_flat(0, 39e6), _flat(1, 39e6), _flat(2, 39e6)] + \
                 [_flat(i, 29.8e6) for i in range(3, 8)]
        findings, meta = _meta_for(nights)
        assert any(f.kind == "sustained" for f in findings)
        assert meta["stratification"] == _STRATA_LEGACY
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
        assert meta["status"] == _INCONCLUSIVE
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


# ===========================================================================
# ROUND 3 — the write plane and the sampling it was tested with.
#
# Three blind reviews agreed the DETECTION side is solid and the WRITE side is
# a vacuum. Measured, on the parent commit:
#
#   * inserting `assert dry_run and not gh_available` at the top of
#     `run_trend_watch` left all 131 tests GREEN — not one test had ever
#     executed a gh write, so all 33 write sites were unreachable and every
#     wire constant (label, repo, issue title, close comment, artifact name)
#     could be changed without a single failure;
#   * every window in the suite contained exactly ONE judgeable bench, so
#     nothing could distinguish a per-night verdict from a per-bench one;
#   * `open_issues` was never longer than 1, so the CLEAR branch's written
#     promise — "Close EVERY open perf-trend issue (not just [0])" — had no
#     test, and "close only the first" survived as a mutant.
#
# Everything below runs the REAL live-mode branch. The single seam is `ab._gh`
# (the subprocess boundary itself); `_gh_write`, `_list_open_trend_issues`,
# `_file_new_issue`, the label lifecycle and the close ordering are the shipped
# code, and the assertions are on the argv that would reach the GitHub CLI.
# ===========================================================================

_BODY_FLAG = "--body"


def _multi(idx: int, benches: dict[str, float], cpu: str | None = None,
           canary_ns: float | None = 360_000.0) -> ab.NightRecord:
    """One night carrying ARBITRARY benches (+ optional canary + host class)."""
    m = dict(benches)
    if canary_ns is not None:
        m[_C] = canary_ns
    n = _night(idx, m)
    n.cpu_model = cpu
    return n


def _issue(number: int, state=None, labels=(), body=None) -> dict:
    return {"number": number, "title": "(open)",
            "body": body if body is not None else (_marker(*state) if state else ""),
            "labels": [{"name": n} for n in labels]}


def _live(monkeypatch, tmp_path, nights, issues=(), fail=(), **over):
    """Run the watchdog with the WRITE PLANE ARMED: dry_run=False, gh present.

    ``--fixture-json`` hard-wires ``gh_available = False``, so no fixture-mode
    test can reach a write — which is precisely why the write plane had zero
    coverage. This harness takes the live branch instead and stubs only the
    subprocess boundary: `night_records_from_gh` is replaced (there is no
    GitHub to download from) and every `gh` invocation is recorded as argv.

    ``fail`` is a list of argv prefixes whose calls raise, so the failure
    handling (which of the remaining writes still happen, and what the job
    says about it) is exercised as shipped rather than assumed.
    """
    calls: list[list[str]] = []

    def fake_gh(cmd, capture=True):
        calls.append(list(cmd))
        for prefix in fail:
            if list(cmd)[:len(prefix)] == list(prefix):
                raise subprocess.CalledProcessError(1, ["gh", *cmd], "", "simulated failure")
        if list(cmd)[:2] == ["issue", "list"]:
            return json.dumps([dict(i) for i in issues])
        return ""

    def fake_run(cmd, **kw):        # `gh label create --force` goes direct
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ab, "_gh", fake_gh)
    monkeypatch.setattr(ab, "shutil", types.SimpleNamespace(
        which=lambda name: f"/usr/bin/{name}", rmtree=lambda *a, **k: None))
    monkeypatch.setattr(ab, "subprocess", types.SimpleNamespace(
        run=fake_run, CalledProcessError=subprocess.CalledProcessError,
        TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(ab, "night_records_from_gh",
                        lambda workflow, limit, cache_dir: list(nights))
    over.setdefault("dry_run", False)
    args = _trend_args(None, cache_dir=tmp_path / "cache", **over)
    rc = ab.run_trend_watch(args)
    return rc, calls


def _sel(calls, *prefix) -> list[list[str]]:
    return [c for c in calls if c[:len(prefix)] == list(prefix)]


def _one(calls, *prefix) -> list[str]:
    hits = _sel(calls, *prefix)
    assert len(hits) == 1, f"expected exactly one {list(prefix)} call, got {len(hits)}"
    return hits[0]


def _body_of(call: list[str]) -> str:
    return call[call.index(_BODY_FLAG) + 1]


def _sustained_window(cpu=None):
    return ([_multi(i, {_BENCH: 45.5e6}, cpu) for i in range(3)]
            + [_multi(i, {_BENCH: 35e6}, cpu) for i in range(3, 9)])


def _flat_window(cpu=None, ns=35e6):
    return [_multi(i, {_BENCH: ns}, cpu) for i in range(9)]


class TestWritePlaneArgv:
    """What actually reaches `gh`. Every string here is a wire format."""

    def test_new_issue_create_argv_is_exact(self, monkeypatch, tmp_path, capsys):
        rc, calls = _live(monkeypatch, tmp_path, _sustained_window())
        capsys.readouterr()
        assert rc == 0
        create = _one(calls, "issue", "create")
        assert create[:len(create) - 2] == [
            "issue", "create", "--repo", "vencil/Dynamic-Alerting-Integrations",
            "--title", "Nightly bench trend regression detected",
            "--label", "perf-trend", "--body", _body_of(create),
        ]
        assert create[-2:] == ["--assignee", "vencil"]
        assert _BENCH in _body_of(create)
        # The FIRST body an issue ever gets is the only one whose marker nothing
        # checked, and it is the one every later night reads back. Dropping it
        # here (`body.split("<!-- perf-trend-state")[0]`) left all 227 tests
        # green while disarming the close guard from the first step of the
        # lifecycle: no marker ⇒ `test_unreadable_marker_keeps_the_previous_
        # behaviour` ⇒ the next CLEAR night closes the issue unconditionally.
        assert ab._parse_state_marker(_body_of(create)) == [[_BENCH, "sustained"]]

    def test_label_is_ensured_before_the_first_issue_is_filed(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window())
        capsys.readouterr()
        assert _one(calls, "gh", "label", "create") == [
            "gh", "label", "create", "perf-trend",
            "--repo", "vencil/Dynamic-Alerting-Integrations",
            "--color", "FBCA04",
            "--description", "Nightly bench trend regression (auto-filed)", "--force",
        ]
        assert calls.index(["gh", "label", "create", "perf-trend", "--repo",
                            "vencil/Dynamic-Alerting-Integrations", "--color", "FBCA04",
                            "--description", "Nightly bench trend regression (auto-filed)",
                            "--force"]) < calls.index(_one(calls, "issue", "create"))

    def test_open_issue_query_argv_is_exact(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window())
        capsys.readouterr()
        assert _one(calls, "issue", "list") == [
            "issue", "list", "--repo", "vencil/Dynamic-Alerting-Integrations",
            "--label", "perf-trend", "--state", "open",
            "--json", "number,title,body,labels",
        ]

    def test_update_writes_body_then_transition_comment(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(),
                           issues=[_issue(42, state=[[_BENCH, "creep"]])])
        capsys.readouterr()
        edit = _one(calls, "issue", "edit", "42")
        assert edit[:5] == ["issue", "edit", "42", "--repo",
                            "vencil/Dynamic-Alerting-Integrations"]
        assert edit[5] == "--body"
        comment = _one(calls, "issue", "comment", "42")
        assert comment[:5] == ["issue", "comment", "42", "--repo",
                               "vencil/Dynamic-Alerting-Integrations"]
        assert "**Escalated to sustained:** " + f"`{_BENCH}`" in \
            _body_of(comment).splitlines()
        # Body BEFORE comment: the marker must be advanced first so a failed
        # comment is lost rather than repeated forever (see the source comment).
        assert calls.index(edit) < calls.index(comment)
        assert ab._parse_state_marker(_body_of(edit)) == [[_BENCH, "sustained"]]

    def test_no_transition_means_body_only_no_comment(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert _sel(calls, "issue", "edit", "42")
        assert _sel(calls, "issue", "comment") == []

    def test_close_argv_and_ordering(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(7, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        close = _one(calls, "issue", "close")
        assert close == ["issue", "close", "7", "--repo",
                         "vencil/Dynamic-Alerting-Integrations"]
        comment = _one(calls, "issue", "comment", "7")
        assert _body_of(comment).startswith("✅ Auto-closing (closed loop): no benchmark "
                                            "is above its floor")
        assert "SLIDING settled-window anchor" in _body_of(comment)
        assert calls.index(close) < calls.index(comment)

    def test_recovering_label_add_argv(self, monkeypatch, tmp_path, capsys):
        creep = ([_multi(0, {_BENCH: 39.2e6}), _multi(1, {_BENCH: 39.2e6}),
                  _multi(2, {_BENCH: 35.7e6})]
                 + [_multi(i, {_BENCH: 35e6}) for i in range(3, 9)])
        _rc, calls = _live(monkeypatch, tmp_path, creep,
                           issues=[_issue(9, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert ["gh", "label", "create", "perf-trend:recovering", "--repo",
                "vencil/Dynamic-Alerting-Integrations", "--color", "C2E0C6",
                "--description", "perf-trend: sustained cleared, creep remains",
                "--force"] in calls
        assert ["issue", "edit", "9", "--repo", "vencil/Dynamic-Alerting-Integrations",
                "--add-label", "perf-trend:recovering"] in calls

    def test_recovering_label_remove_argv(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(),
                           issues=[_issue(9, state=[[_BENCH, "creep"]],
                                          labels=["perf-trend:recovering"])])
        capsys.readouterr()
        assert ["issue", "edit", "9", "--repo", "vencil/Dynamic-Alerting-Integrations",
                "--remove-label", "perf-trend:recovering"] in calls

    def test_stale_recovering_label_is_stripped_before_the_close(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(9, state=[[_BENCH, "sustained"]],
                                          labels=["perf-trend:recovering"])])
        capsys.readouterr()
        strip = ["issue", "edit", "9", "--repo", "vencil/Dynamic-Alerting-Integrations",
                 "--remove-label", "perf-trend:recovering"]
        assert strip in calls
        assert calls.index(strip) < calls.index(_one(calls, "issue", "close"))

    def test_dry_run_still_writes_nothing(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(),
                           issues=[_issue(42, state=[[_BENCH, "creep"]])],
                           dry_run=True)
        capsys.readouterr()
        # The read happened; nothing else did. NOTE: only the `edit`/`comment`
        # pair is load-bearing here — this scenario has an open issue and
        # findings, so it reaches the UPDATE branch and the `close`/`create`
        # lines below are structurally unreachable, hence vacuously true. The
        # other three write branches are gated in TestDryRunGatesEveryBranch.
        assert _sel(calls, "issue", "list")
        assert _sel(calls, "issue", "edit") == []
        assert _sel(calls, "issue", "comment") == []
        assert _sel(calls, "issue", "close") == []
        assert _sel(calls, "issue", "create") == []


class TestEveryOpenIssueIsClosed:
    """`open_issues` was never longer than 1 in any test, so the CLEAR branch's
    written promise — "Close EVERY open perf-trend issue (not just [0])" — was
    unverified and `open_issues[:1]` survived as a mutant."""

    def test_clear_closes_all_three(self, monkeypatch, tmp_path, capsys):
        issues = [_issue(n, state=[[_BENCH, "sustained"]]) for n in (11, 22, 33)]
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(), issues=issues)
        capsys.readouterr()
        assert [c[2] for c in _sel(calls, "issue", "close")] == ["11", "22", "33"]
        # Bound to the issue NUMBERS, not just counted: three comments all
        # landing on #11 is the same count and a different (wrong) behaviour.
        assert [c[2] for c in _sel(calls, "issue", "comment")] == ["11", "22", "33"]

    def test_inconclusive_refreshes_all_three_and_closes_none(
            self, monkeypatch, tmp_path, capsys):
        nights = ([_multi(i, {_BENCH: 35e6}, _XEON) for i in range(3)]
                  + [_multi(3, {_BENCH: 35e6}, _EPYC), _multi(4, {_BENCH: 35e6}, _XEON),
                     _multi(5, {_BENCH: 35e6}, _EPYC), _multi(6, {_BENCH: 35e6}, _XEON),
                     _multi(7, {_BENCH: 35e6}, _EPYC)])
        issues = [_issue(n, state=[[_BENCH, "sustained"]]) for n in (11, 22, 33)]
        _rc, calls = _live(monkeypatch, tmp_path, nights, issues=issues)
        capsys.readouterr()
        assert _sel(calls, "issue", "close") == []
        assert [c[2] for c in _sel(calls, "issue", "edit")] == ["11", "22", "33"]


_BENCH_C = "BenchmarkRenderRulePack_500"
# Sorts BEFORE both _BENCH_B ("BenchmarkMerge…") and _BENCH ("BenchmarkScan…"),
# and is inserted LAST into every fixture row below — so the expected orderings
# in this class are only satisfiable if the module really sorts. See B4.
_BENCH_D = "BenchmarkAlertRouteFanout_256"


class TestMultiBenchWindow:
    """Every pre-existing window held exactly ONE judgeable bench, so per-night
    and per-bench verdicts were indistinguishable by construction."""

    def _mixed(self):
        # A regressed, B + D flat, C only appears in the settled half (vanished).
        nights = []
        for i in range(9):
            row = {_BENCH: 45.5e6 if i < 3 else 35e6, _BENCH_B: 12e6}
            if i >= 3:
                row[_BENCH_C] = 8e6
            row[_BENCH_D] = 9e6
            nights.append(_multi(i, row, _EPYC))
        return nights

    def test_each_bench_lands_in_its_own_bucket(self):
        findings, meta = _meta_for(self._mixed())
        assert [(f.bench, f.kind) for f in findings] == [(_BENCH, "sustained")]
        # A LITERAL ordering, not `sorted([...])`: computing the expectation the
        # same way the module does asserts nothing about the order, and the two
        # constants it was built from happened to make the tautology invisible.
        # Insertion order here is D-last, so this list can only match if the
        # module iterates its bench set in sorted order.
        assert meta["evaluated_benches"] == [_BENCH_D, _BENCH_B, _BENCH]
        assert meta["inconclusive_benches"] == [_BENCH_C]
        assert meta["status"] == _FINDINGS

    def test_one_regressed_bench_does_not_make_the_others_findings(self):
        findings, _meta = _meta_for(self._mixed())
        assert _BENCH_B not in {f.bench for f in findings}
        assert _BENCH_C not in {f.bench for f in findings}
        assert _BENCH_D not in {f.bench for f in findings}

    def test_the_canary_is_never_itself_a_finding(self):
        # The canary defines the noise floor; flagging it would be circular.
        nights = [_multi(i, {_BENCH: 35e6}, _EPYC, canary_ns=900_000.0) for i in range(3)] \
            + [_multi(i, {_BENCH: 35e6}, _EPYC, canary_ns=360_000.0) for i in range(3, 9)]
        findings, meta = _meta_for(nights)
        assert _C not in {f.bench for f in findings}
        assert _C not in meta["evaluated_benches"] + meta["inconclusive_benches"]


# ===========================================================================
# ROUND 3 / A — closing is a PER-BENCH claim, not a per-night one.
#
# The three-state verdict fixed the night granularity: a night that evaluated
# NOTHING never closes anything. A night that evaluated SOMETHING still closed
# everything. `status=CLEAR, evaluated=[B], inconclusive=[A]` is a night that
# measured B and did not measure A — and it closed the issue that was filed for
# A, announced A "Recovered (no longer flagged)", and could label the issue
# `recovering`, all without a single measurement of A.
# ===========================================================================

def _a_gone_b_clean(cpu=_EPYC):
    """A stopped reporting 3 nights ago (perf-timeout symptom) after a +30%
    step; B reports every night and is flat. Tonight: CLEAR, evaluated=[B],
    inconclusive=[A]."""
    nights = []
    for i in range(9):
        row = {_BENCH_B: 12e6}
        if i >= 3:
            row[_BENCH] = 45.5e6 if i < 6 else 35e6
        nights.append(_multi(i, row, cpu))
    return nights


class TestCloseIsGatedPerBench:
    def test_the_setup_really_is_clear_with_an_unmeasured_bench(self):
        # The precondition, asserted so the tests below cannot silently drift
        # into testing the INCONCLUSIVE path instead.
        _f, meta = _meta_for(_a_gone_b_clean())
        assert meta["status"] == _CLEAR
        assert meta["evaluated_benches"] == [_BENCH_B]
        assert meta["inconclusive_benches"] == [_BENCH]

    def test_clear_does_not_close_an_issue_tracking_an_unmeasured_bench(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "close") == []
        assert f"NOT closing perf-trend issue #42 — tonight could not verify `{_BENCH}`" in err

    def test_the_held_open_issue_says_so_in_its_body(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert "STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT" in body
        assert f"`{_BENCH}` (sustained)" in body
        # …and the marker is preserved, so tomorrow's guard is still armed.
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]

    def test_held_open_is_annotated_and_summarised(
            self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _a_gone_b_clean(),
              issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        err = capsys.readouterr().err
        assert "::warning::" in err and "stays open" in err
        assert "issue #42 held open" in summary.read_text(encoding="utf-8")

    def test_control_when_the_tracked_bench_IS_measured_it_still_closes(
            self, monkeypatch, tmp_path, capsys):
        # Same code path, same issue, same marker — only the data differs: A is
        # present tonight and clean. The guard must not have broken the loop.
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"

    def test_unreadable_marker_keeps_the_previous_behaviour(
            self, monkeypatch, tmp_path, capsys):
        # A legacy / hand-filed issue carries no per-bench claim to check.
        # Inventing a marker-health mechanism here is a separate design (it was
        # tried and reverted); the honest fallback is the old behaviour.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, body="## hand-written, no marker")])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"

    def test_empty_marker_also_closes(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, body=ab._render_state_marker([]))])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"

    def test_only_the_affected_issue_is_held_open(
            self, monkeypatch, tmp_path, capsys):
        # Per-issue, not per-run: an issue tracking the measured bench still
        # closes on the same night another issue is held open.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(11, state=[[_BENCH, "sustained"]]),
                                   _issue(22, state=[[_BENCH_B, "creep"]])])
        capsys.readouterr()
        assert [c[2] for c in _sel(calls, "issue", "close")] == ["22"]
        # #11 gets the held-open refresh, #22 the CLEAR refresh on its way out.
        assert [c[2] for c in _sel(calls, "issue", "edit")] == ["11", "22"]

    def test_unverified_benches_helper_uses_evaluated_not_inconclusive(self):
        # A bench that vanished from the window ENTIRELY is in neither list, and
        # is no more verified than one that was skipped.
        meta = {"evaluated_benches": ["B"], "inconclusive_benches": ["A"]}
        assert ab._unverified_benches([["A", "sustained"], ["B", "creep"],
                                       ["GoneForever", "creep"]], meta) == \
            ["A", "GoneForever"]
        assert ab._unverified_benches([["B", "creep"]], meta) == []
        assert ab._unverified_benches(None, meta) == []
        assert ab._unverified_benches([], meta) == []


def _a_gone_b_regressed(cpu=_EPYC):
    """A stopped reporting; B is a fresh +30% sustained regression."""
    nights = []
    for i in range(9):
        row = {_BENCH_B: 45.5e6 if i < 3 else 35e6}
        if i >= 3:
            row[_BENCH] = 35e6
        nights.append(_multi(i, row, cpu))
    return nights


def _a_gone_b_creep(cpu=_EPYC):
    """A stopped reporting; B shows creep only (one recent night dipped)."""
    recent = [39.2e6, 39.2e6, 35.7e6]
    nights = []
    for i in range(9):
        row = {_BENCH_B: recent[i] if i < 3 else 35e6}
        if i >= 3:
            row[_BENCH] = 35e6
        nights.append(_multi(i, row, cpu))
    return nights


class TestNoFabricatedRecovery:
    """A2 — the notification layer must not say about an unmeasured bench what
    the close layer is no longer allowed to conclude."""

    def test_carried_forward_bench_gets_no_recovered_comment(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_regressed(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        comment = _one(calls, "issue", "comment", "42")
        assert f"**Newly flagged:** `{_BENCH_B}`" in _body_of(comment).splitlines()
        assert "Recovered" not in _body_of(comment)

    def test_the_marker_keeps_tracking_the_unmeasured_bench(
            self, monkeypatch, tmp_path, capsys):
        # Without carry-forward the ledger forgets A tonight, and the very next
        # CLEAR night closes the issue with the guard above disarmed.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_regressed(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert ab._parse_state_marker(body) == [[_BENCH_B, "sustained"],
                                                [_BENCH, "sustained"]]

    def test_recovering_label_is_not_applied_on_an_unmeasured_sustained(
            self, monkeypatch, tmp_path, capsys):
        # "sustained cleared, only creep remains" is a claim about A. A was not
        # measured. The label repeats, in the notification layer, exactly the
        # false statement the comment layer was fixed not to make.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_creep(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert [c for c in calls if "--add-label" in c] == []

    def test_control_label_is_applied_when_the_sustained_bench_was_measured(
            self, monkeypatch, tmp_path, capsys):
        # Same transition, but A is present and clean tonight → genuinely
        # "sustained cleared, creep remains" → the label is correct.
        recent = [39.2e6, 39.2e6, 35.7e6]
        nights = [_multi(i, {_BENCH_B: recent[i], _BENCH: 35e6}, _EPYC) for i in range(3)] \
            + [_multi(i, {_BENCH_B: 35e6, _BENCH: 35e6}, _EPYC) for i in range(3, 9)]
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert ["issue", "edit", "42", "--repo", "vencil/Dynamic-Alerting-Integrations",
                "--add-label", "perf-trend:recovering"] in calls

    def test_label_helper_blocks_add_but_not_remove(self):
        creep = [_finding(_BENCH_B, "creep")]
        sust = [_finding(_BENCH_B, "sustained")]
        prior = [[_BENCH, "sustained"]]
        assert ab._recovering_label_change(creep, prior, [], unverified=[]) == "add"
        assert ab._recovering_label_change(creep, prior, [], unverified=[_BENCH]) is None
        # Removal is driven by a sustained finding that WAS measured tonight.
        assert ab._recovering_label_change(sust, prior, ["perf-trend:recovering"],
                                           unverified=[_BENCH]) == "remove"


class TestInconclusiveBodyMatchesItsMarker:
    """A3 — the body was overwritten with "nothing is flagged" prose while the
    marker in the SAME body said `sustained`. Two halves of one document
    contradicting each other, with no way for a reader to tell which is stale."""

    def _thin(self):
        return ([_hnight(i, 35e6, _XEON) for i in range(3)]
                + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                   _hnight(5, 35e6, _EPYC), _hnight(6, 35e6, _XEON),
                   _hnight(7, 35e6, _EPYC)])

    def test_body_shows_what_the_issue_is_still_tracking(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, self._thin(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert f"**This issue is still tracking `{_BENCH}` (sustained).**" in body
        assert "STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT" in body
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]

    def test_body_no_longer_claims_nothing_is_flagged(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, self._thin(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert "nothing is flagged" not in body
        assert "no NEW finding was produced" in body
        assert "nothing is closed" in body

    def test_a_markerless_issue_gets_no_tracking_claim(self):
        _f, meta = _meta_for(self._thin())
        body = ab.render_trend_issue_body([], meta, state=[], tracked=[])
        assert "STILL TRACKED" not in body
        assert "still tracking" not in body


class TestInconclusiveCauseIsAttributed:
    """A4 — "too few same-class nights" was printed for both `continue`
    branches. For a bench that STOPPED REPORTING that is a misdiagnosis, and on
    an all-one-class window it printed "only 14 of 14 window nights ran on
    tonight's host class" — a sentence that argues against itself."""

    def _all_one_class_bench_vanished(self):
        # 14 nights, ALL the same host class; the only bench stops reporting.
        return ([_multi(i, {}, _EPYC) for i in range(3)]
                + [_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(3, 14)])

    def test_reasons_are_recorded_separately(self):
        _f, meta = _meta_for(self._all_one_class_bench_vanished())
        assert meta["status"] == _INCONCLUSIVE
        assert meta["inconclusive_reasons"] == {_BENCH: "no-recent"}

    def test_thin_anchor_is_recorded_as_thin_anchor(self):
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)]
                  + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                     _hnight(5, 35e6, _EPYC)])
        _f, meta = _meta_for(nights)
        assert meta["inconclusive_reasons"] == {_BENCH: "thin-anchor"}

    def test_stopped_reporting_is_not_blamed_on_the_window(
            self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        args = _trend_args(_write_fixture(
            tmp_path, self._all_one_class_bench_vanished()))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        text = out.out + out.err + summary.read_text(encoding="utf-8")
        assert "did not report on all 3 newest same-class nights" in text
        assert "perf-timeout/crash symptom" in text
        # The self-contradicting sentence is gone.
        assert "only 14 of 14 window nights" not in text
        assert "settled same-class nights are behind them" not in text

    def test_thin_window_still_says_thin_window(self, tmp_path, capsys):
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)]
                  + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                     _hnight(5, 35e6, _EPYC)])
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        out = capsys.readouterr().out
        assert "only 4 of 6 window nights ran on tonight's host class" in out
        assert "perf-timeout/crash symptom" not in out

    def test_both_causes_appear_when_both_happen(self):
        # A vanished from the recent nights; C has too thin an anchor.
        nights = ([_multi(i, {_BENCH_B: 12e6}, _XEON) for i in range(3)]
                  + [_multi(3, {_BENCH_B: 12e6, _BENCH: 35e6}, _XEON)]
                  + [_multi(i, {_BENCH_B: 12e6, _BENCH: 35e6}, _EPYC) for i in range(4, 8)])
        _f, meta = _meta_for(nights)
        assert meta["inconclusive_reasons"] == {_BENCH: "no-recent", _BENCH_B: "thin-anchor"}
        causes = " | ".join(ab._inconclusive_causes(meta))
        assert "perf-timeout/crash symptom" in causes and f"`{_BENCH}`" in causes
        assert "window nights ran on tonight's host class" in causes
        assert f"`{_BENCH_B}`" in causes

    def test_tonight_unknown_names_its_own_cause(self):
        nights = ([_hnight(0, 39e6, None)]
                  + [_hnight(i, 39e6, _EPYC) for i in (1, 2)]
                  + [_hnight(i, 29.8e6, _XEON) for i in range(3, 8)])
        _f, meta = _meta_for(nights)
        assert ab._inconclusive_causes(meta) == [
            "tonight's `cpu:` header is unreadable while the rest of the window is "
            "labelled, so there is no same-class population to judge anything against"]


class TestUnstratifiedFallbackIsAnnounced:
    """A5 — the legacy unstratified path is a KNOWN false-positive mode, and the
    job it ran in looked identical to a stratified one."""

    def test_legacy_window_warns_and_summarises(self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        nights = [_flat(i, 35e6) for i in range(8)]
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        err = capsys.readouterr().err
        assert "::warning::" in err and "running UNSTRATIFIED" in err
        assert "UNSTRATIFIED verdict" in summary.read_text(encoding="utf-8")

    def test_stratified_window_does_not_warn(self, tmp_path, capsys):
        nights = [_hnight(i, 35e6, _EPYC) for i in range(8)]
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        assert "UNSTRATIFIED" not in capsys.readouterr().err


class TestCanaryWithoutSamplesIsNotZeroJitter:
    """A6 — `_cv` returns 0.0 for "measured, perfectly stable" AND for "never
    measured", and the body printed the second one as **0.00%**: a positive
    assertion of zero runner jitter, made from zero samples."""

    def _no_canary(self):
        return [_multi(i, {_BENCH: 35e6}, _EPYC, canary_ns=None) for i in range(9)]

    def test_meta_counts_the_canary_samples(self):
        _f, meta = _meta_for(self._no_canary())
        assert meta["canary_n"] == 0
        _f2, meta2 = _meta_for([_hnight(i, 35e6, _EPYC) for i in range(9)])
        assert meta2["canary_n"] == 9

    def test_body_says_not_measured_instead_of_zero(self):
        _f, meta = _meta_for(self._no_canary())
        body = ab.render_trend_issue_body([], meta)
        assert "Control-canary night-to-night CV: **not measured**" in body
        assert "0.00%" not in body
        assert "the absence of a measurement" in body

    def test_floor_line_does_not_credit_an_absent_canary(self):
        _f, meta = _meta_for(self._no_canary())
        body = ab.render_trend_issue_body([], meta)
        assert "(fixed minimum only — no canary measurement to raise it)" in body
        assert "canary-noise-scaled" not in body

    def test_one_sample_is_still_not_a_cv(self):
        nights = [_multi(0, {_BENCH: 35e6}, _EPYC, canary_ns=360_000.0)] \
            + [_multi(i, {_BENCH: 35e6}, _EPYC, canary_ns=None) for i in range(1, 9)]
        _f, meta = _meta_for(nights)
        assert meta["canary_n"] == 1
        assert "not measured" in ab.render_trend_issue_body([], meta)

    def test_a_real_measurement_still_reports_a_percentage(self):
        _f, meta = _meta_for([_hnight(i, 35e6, _EPYC) for i in range(9)])
        body = ab.render_trend_issue_body([], meta)
        assert "Control-canary night-to-night CV: **0.00%**" in body
        assert "max of fixed minimum and canary-noise-scaled" in body


class TestGhWriteFailuresAreLoud:
    """A7 — every write failure on the update/close paths was a single stderr
    line in a job that still ends green, and the auto-close comment was posted
    whether or not the close succeeded."""

    def test_failed_body_edit_is_annotated(self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _sustained_window(),
              issues=[_issue(42, state=[[_BENCH, "creep"]])],
              fail=[("issue", "edit")])
        err = capsys.readouterr().err
        assert "::warning::" in err and "body refresh of issue #42 FAILED" in err
        assert "body refresh of issue #42 failed" in summary.read_text(encoding="utf-8")

    def test_failed_close_does_not_post_the_auto_close_comment(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])],
                           fail=[("issue", "close")])
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "comment") == []
        assert "was NOT closed" in err
        assert "::warning::" in err and "closing recovered issue #42 FAILED" in err

    def test_successful_close_still_comments(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert len(_sel(calls, "issue", "comment", "42")) == 1

    def test_failed_transition_comment_is_annotated(self, monkeypatch, tmp_path, capsys):
        _live(monkeypatch, tmp_path, _sustained_window(),
              issues=[_issue(42, state=[[_BENCH, "creep"]])],
              fail=[("issue", "comment")])
        err = capsys.readouterr().err
        assert "transition comment on issue #42 FAILED" in err

    def test_failed_label_write_is_annotated(self, monkeypatch, tmp_path, capsys):
        _live(monkeypatch, tmp_path, _sustained_window(),
              issues=[_issue(42, state=[[_BENCH, "creep"]],
                             labels=["perf-trend:recovering"])],
              fail=[("issue", "edit", "42", "--repo",
                     "vencil/Dynamic-Alerting-Integrations", "--remove-label")])
        err = capsys.readouterr().err
        assert "removing `perf-trend:recovering` from issue #42 FAILED" in err

    def test_a_successful_run_annotates_nothing(self, monkeypatch, tmp_path, capsys):
        _live(monkeypatch, tmp_path, _sustained_window(_EPYC),
              issues=[_issue(42, state=[[_BENCH, "creep"]])])
        assert "::warning::" not in capsys.readouterr().err


class TestMarkerParseNeverRedsTheRun:
    """A8 — `except (ValueError, TypeError)` covered the payloads this code
    WRITES, not the ones it READS. An issue body is human-editable."""

    def test_deeply_nested_json_degrades_instead_of_raising(self, capsys):
        # json.loads raises RecursionError (a RuntimeError) here — it escaped
        # the narrow except, reached main() and exited 1 every single night
        # until someone hand-edited the issue body back.
        hostile = "<!-- perf-trend-state v1 " + "[" * 20_000 + "]" * 20_000 + " -->"
        assert ab._parse_state_marker(hostile) is None
        assert "state marker unreadable" in capsys.readouterr().err

    def test_the_whole_run_survives_a_hostile_marker(
            self, monkeypatch, tmp_path, capsys):
        hostile = "<!-- perf-trend-state v1 " + "[" * 20_000 + "]" * 20_000 + " -->"
        rc, calls = _live(monkeypatch, tmp_path, _sustained_window(),
                          issues=[_issue(42, body=hostile)])
        capsys.readouterr()
        assert rc == 0                       # the RecursionError does not escape
        # This assertion used to be `_sel(calls, "issue", "edit", "42")` — the
        # run reached the write plane. It now deliberately does NOT: a hostile
        # marker is PRESENT-but-UNREADABLE, and refreshing the body would replace
        # it with a clean one (R7-F2, `TestUnreadableMarkerIsNotOverwritten`).
        # What this test is about is unchanged: the run survives and exits 0.
        assert not _sel(calls, "issue", "edit", "42")
        assert _sel(calls, "issue", "list")  # …having actually run the write path

    def test_ordinary_garbage_still_returns_none(self):
        assert ab._parse_state_marker("<!-- perf-trend-state v1 [1] -->") is None
        assert ab._parse_state_marker('<!-- perf-trend-state v1 ["a"] -->') is None
        assert ab._parse_state_marker("<!-- perf-trend-state v1 [not json] -->") is None


class TestArgparseLowerBounds:
    """A9 — `--recent-nights 0` reached the detector, where `series[:0]` is
    empty, every `len(recent) < 0` guard is vacuously false, and `recent[0]`
    raised IndexError: a traceback and exit 1 out of a nightly watchdog."""

    def _parse(self, monkeypatch, *argv):
        monkeypatch.setattr("sys.argv", ["analyze_bench_history.py", *argv])
        with pytest.raises(SystemExit) as exc:
            ab.main()
        return exc.value.code

    def test_zero_recent_nights_is_a_usage_error(self, monkeypatch, capsys):
        assert self._parse(monkeypatch, "--trend-watch", "--recent-nights", "0") == 2
        assert "must be >= 1" in capsys.readouterr().err

    def test_negative_and_zero_windows_rejected(self, monkeypatch, capsys):
        assert self._parse(monkeypatch, "--trend-watch", "--trend-limit", "0") == 2
        assert self._parse(monkeypatch, "--limit", "-1") == 2
        capsys.readouterr()

    def test_one_is_still_accepted(self):
        assert ab._at_least(1)("1") == 1
        with pytest.raises(argparse.ArgumentTypeError):
            ab._at_least(1)("0")


class TestFreeFormHostStringIsNeuteredEverywhere:
    """A10 — `_safe_md` guarded the issue body; the SAME free-form string went
    raw into the `::warning::` annotation and the markdown step summary."""

    def _hostile_thin(self):
        return ([_hnight(i, 35e6, _HOSTILE_CPU) for i in range(3)]
                + [_hnight(i, 35e6, _EPYC) for i in range(3, 8)])

    def test_annotation_and_summary_carry_no_live_marker(
            self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        args = _trend_args(_write_fixture(tmp_path, self._hostile_thin()))
        assert ab.run_trend_watch(args) == 0
        out = capsys.readouterr()
        text = out.out + out.err + summary.read_text(encoding="utf-8")
        assert "AMD EPYC 7763" in text                       # still disclosed…
        assert ab._STATE_MARKER_RE.findall(text) == []       # …but inert
        assert "&lt;!--" in text


class TestWindowSpanWording:
    """A11 — "Detected across the last N nightly runs" quoted the whole window
    while the verdict used only the same-class nights inside it."""

    def test_stratified_body_counts_same_class_nights(self):
        nights = ([_hnight(i, 45.5e6, _EPYC) for i in range(3)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(3, 6)]
                  + [_hnight(i, 35e6, _XEON) for i in range(6, 9)])
        findings, meta = _meta_for(nights)
        assert meta["n_class_nights"] == 6 and meta["n_nights"] == 9
        body = ab.render_trend_issue_body(findings, meta)
        assert ("Detected across the last **6** nights that ran on tonight's host class "
                "(of **9** in the `bench-record` window)." in body)

    def test_unstratified_body_says_the_window_is_unstratified(self):
        nights = [_flat(i, 35e6) for i in range(9)]
        _f, meta = _meta_for(nights)
        body = ab.render_trend_issue_body([], meta)
        assert ("Detected across the last **9** nightly `bench-record` runs "
                "(no host-class stratification — see below)." in body)


class TestClearNightRefreshesTheBody:
    """A12 — nothing refreshed the body on a CLEAR night, so the closing comment
    landed directly under the table of the LAST night that found something: a
    "+30.8% sustained" table above "✅ Auto-closing"."""

    def test_close_path_writes_a_clear_body(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert "### ✅ Nothing above its floor tonight (CLEAR)" in body
        assert "| Rule |" not in body                    # the stale table is gone
        # …and the marker agrees with that prose. See
        # TestClosingWritesTheLedgerItsOwnProseImplies for why it must.
        assert ab._parse_state_marker(body) == []

    def test_body_is_written_after_the_close_not_before(
            self, monkeypatch, tmp_path, capsys):
        # Close first (a comment/edit blip must never leave a recovered issue
        # open); the body refresh and the comment are gated on it succeeding.
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert calls.index(_one(calls, "issue", "close")) < \
            calls.index(_one(calls, "issue", "edit", "42"))

    def test_failed_close_leaves_the_old_body_alone(self, monkeypatch, tmp_path, capsys):
        # If the issue is still open, overwriting its body with a CLEAR verdict
        # would misdescribe an open ticket.
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])],
                           fail=[("issue", "close")])
        capsys.readouterr()
        assert _sel(calls, "issue", "edit") == []

    def test_dry_run_announces_the_refresh(self, tmp_path, capsys):
        nights = [_hnight(i, 35e6, _EPYC) for i in range(9)]
        args = _trend_args(_write_fixture(tmp_path, nights), fixture_open_issue=42,
                           fixture_open_body=_marker([_BENCH, "sustained"]))
        assert ab.run_trend_watch(args) == 0
        assert "would refresh body of #42 to tonight's CLEAR verdict" in \
            capsys.readouterr().err


# ===========================================================================
# ROUND 3 / B4 — mutants that survived the suite as it stood.
#
# Each test below was written against a specific surviving mutation: revert the
# behaviour it names and this test goes red. The fire arithmetic itself is NOT
# changed by anything in this round — these pin it so it cannot drift.
# ===========================================================================

class TestFloorComparisonIsInclusive:
    """`>=` vs `>` at the floor. Nothing in the suite sat ON the boundary, so
    both comparisons passed every test."""

    ANCHOR = 35e6

    def _window(self, recent_values):
        return ([_multi(i, {_BENCH: v}, _EPYC) for i, v in enumerate(recent_values)]
                + [_multi(i, {_BENCH: self.ANCHOR}, _EPYC) for i in range(3, 9)])

    def test_sustained_fires_at_exactly_the_floor(self):
        exact = self.ANCHOR * (1 + 5.0 / 100.0)      # the rule's own expression
        findings, meta = _meta_for(self._window([exact, exact, exact]))
        assert meta["floor_pct"] == pytest.approx(5.0)
        assert [(f.bench, f.kind) for f in findings] == [(_BENCH, "sustained")]

    def test_sustained_is_silent_one_ulp_below_the_floor(self):
        under = math.nextafter(self.ANCHOR * (1 + 5.0 / 100.0), 0.0)
        findings, _meta = _meta_for(self._window([under, under, under]))
        assert findings == []

    def test_creep_fires_at_exactly_the_creep_floor(self):
        # One recent night at the anchor keeps `sustained` out of it, so the
        # boundary being tested is unambiguously creep's.
        exact = self.ANCHOR * (1 + 10.0 / 100.0)
        findings, meta = _meta_for(self._window([exact, exact, self.ANCHOR]))
        assert meta["creep_floor_pct"] == pytest.approx(10.0)
        assert [(f.bench, f.kind) for f in findings] == [(_BENCH, "creep")]

    def test_creep_is_silent_one_ulp_below_its_floor(self):
        under = math.nextafter(self.ANCHOR * (1 + 10.0 / 100.0), 0.0)
        findings, _meta = _meta_for(self._window([under, under, self.ANCHOR]))
        assert findings == []


class TestFindingNumbersAreTheRealNumbers:
    """`pct_vs_anchor` / `pct_typical_vs_anchor` had only ever been checked on
    hand-built TrendFinding objects, so the formulas — and the two fields being
    swapped — were untested end-to-end."""

    def test_every_field_of_a_real_finding(self):
        nights = ([_multi(0, {_BENCH: 42e6}, _EPYC), _multi(1, {_BENCH: 38.5e6}, _EPYC),
                   _multi(2, {_BENCH: 38.5e6}, _EPYC)]
                  + [_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(3, 9)])
        findings, _meta = _meta_for(nights)
        f = findings[0]
        assert (f.bench, f.kind) == (_BENCH, "sustained")
        assert f.today_ns == 42e6                                  # newest night
        assert f.anchor_ns == 35e6                                 # settled median
        assert f.recent_typical_ns == 38.5e6                       # median of recent
        assert f.pct_vs_anchor == pytest.approx(20.0)              # 42.0 / 35 - 1
        assert f.pct_typical_vs_anchor == pytest.approx(10.0)      # 38.5 / 35 - 1
        # …and the two percentages are NOT interchangeable.
        assert f.pct_vs_anchor != f.pct_typical_vs_anchor

    def test_the_body_prints_both_percentages_in_their_own_columns(self):
        nights = ([_multi(0, {_BENCH: 42e6}, _EPYC), _multi(1, {_BENCH: 38.5e6}, _EPYC),
                   _multi(2, {_BENCH: 38.5e6}, _EPYC)]
                  + [_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(3, 9)])
        findings, meta = _meta_for(nights)
        row = [li for li in ab.render_trend_issue_body(findings, meta).splitlines()
               if li.startswith(f"| `{_BENCH}`")]
        assert row == [f"| `{_BENCH}` | sustained | 42.0 ms | +20.0% | +10.0% |"]


class TestStateMarkerSerialisation:
    """The marker is a wire format between tonight's process and tomorrow's."""

    def test_regex_captures_the_whole_nested_array(self):
        # Non-greedy `(\[.*?\])` stops at the first `]`, so a two-row marker
        # parses as invalid JSON and the prior state silently becomes None —
        # which reads downstream as "legacy issue, no prior state".
        body = "table…\n" + ab._render_state_marker([["A", "creep"], ["B", "sustained"]])
        assert ab._parse_state_marker(body) == [["A", "creep"], ["B", "sustained"]]

    def test_parse_is_order_independent(self):
        body = ab._render_state_marker([["Z", "creep"], ["A", "sustained"]])
        assert ab._parse_state_marker(body) == [["A", "sustained"], ["Z", "creep"]]

    def test_render_is_order_independent_too(self):
        assert ab._finding_state([_finding("Z", "creep"), _finding("A", "sustained")]) == \
            [["A", "sustained"], ["Z", "creep"]]

    def test_roundtrip_through_a_rendered_body_is_lossless(self):
        _f, meta = _meta_for([_hnight(i, 35e6, _EPYC) for i in range(9)])
        state = [["A", "creep"], ["B", "sustained"]]
        body = ab.render_trend_issue_body([], meta, state=state)
        assert ab._parse_state_marker(body) == state

    def test_carry_forward_merges_without_duplicating(self):
        assert ab._carry_forward_state([["B", "creep"]], [["A", "sustained"]]) == \
            [["A", "sustained"], ["B", "creep"]]
        # A row present in BOTH keeps tonight's kind, not the stale one.
        assert ab._carry_forward_state([["A", "creep"]], [["A", "sustained"]]) == \
            [["A", "creep"]]
        assert ab._carry_forward_state([], []) == []


class TestBodyDisclosureLines:
    """The body is the only artefact a human reads. Its explanatory lines had
    no assertions at all, so any of them could be deleted silently."""

    def _body(self):
        nights = ([_hnight(i, 45.5e6, _EPYC) for i in range(3)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(3, 9)])
        findings, meta = _meta_for(nights)
        return ab.render_trend_issue_body(findings, meta)

    def test_floor_line(self):
        assert ("- Effective floor: **5.0%** (max of fixed minimum and "
                "canary-noise-scaled); creep floor: **10.0%**." in self._body())

    def test_canary_line(self):
        assert ("- Control-canary night-to-night CV: **0.00%** "
                "(`BenchmarkControlCanaryCPU`; movement below the floor is "
                "indistinguishable from runner noise)." in self._body())

    def test_rule_explanation_line(self):
        assert ("- Sustained rule = all 3 most-recent nights above the anchored "
                "(settled-window-median) baseline; creep rule = the recent-window MEDIAN "
                "above that same anchor (catches a step-change that a single noisy night "
                "hides from `sustained`)." in self._body())

    def test_table_header(self):
        lines = self._body().splitlines()
        assert "| Bench | Rule | Today | today vs anchor | recent-median vs anchor |" in lines
        assert "|---|---|---:|---:|---:|" in lines

    def test_footer_states_the_closing_contract(self):
        body = self._body()
        assert "never on a night that could evaluate nothing" in body
        # The per-bench half of the contract is CONDITIONAL on the marker being
        # readable — it is the marker that names what the issue is tracking, and
        # the body a human can edit is where it lives. See
        # TestUnreadableMarkerIsNotSilentlyAbsent.
        assert ("as long as the hidden state marker at the end of this body stays READABLE, "
                "never while any benchmark this issue is tracking went unmeasured") in body
        assert "never while ANY benchmark this issue is tracking went unmeasured" not in body

    def test_which_night_line(self):
        nights = [_hnight(i, 35e6, _EPYC) for i in range(9)]
        _f, meta = _meta_for(nights)
        body = ab.render_trend_issue_body([], meta)
        assert (f"Newest night in this window: **{nights[0].created_at}** "
                f"(run `{nights[0].run_id}`)." in body)


class TestShortWindowThreshold:
    """`recent_nights + 2` — the bail-out boundary had never been probed from
    both sides, so `+ 1` and `+ 3` both survived."""

    def test_one_night_short_bails_out(self, tmp_path, capsys):
        nights = [_flat(i, 35e6) for i in range(4)]        # K + 1
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        err = capsys.readouterr().err
        assert "only 4 usable nights — need ≥ 5" in err
        assert "did NOT run" in err

    def test_exactly_the_threshold_runs(self, tmp_path, capsys):
        nights = [_flat(i, 35e6) for i in range(5)]        # K + 2
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        out = capsys.readouterr()
        assert "did NOT run" not in out.err
        assert "No sustained" in out.out


class TestVanishedBenchGuard:
    """A bench that stops reporting must be SKIPPED, never collapsed so an old
    night masquerades as tonight."""

    def test_skipped_with_the_right_reason(self):
        nights = ([_multi(i, {_BENCH_B: 12e6}, _EPYC) for i in range(3)]
                  + [_multi(i, {_BENCH_B: 12e6, _BENCH: 52.5e6}, _EPYC)
                     for i in range(3, 6)]
                  + [_multi(i, {_BENCH_B: 12e6, _BENCH: 35e6}, _EPYC)
                     for i in range(6, 10)])
        findings, meta = _meta_for(nights)
        assert {f.bench for f in findings} == set()
        assert meta["inconclusive_reasons"] == {_BENCH: "no-recent"}
        assert meta["evaluated_benches"] == [_BENCH_B]

    def test_a_gap_in_the_middle_of_the_recent_window_also_skips(self):
        # Present tonight and two nights ago, missing in between: the recent
        # window is not complete, so there is no honest "all K nights" claim.
        nights = ([_multi(0, {_BENCH: 45.5e6}, _EPYC), _multi(1, {}, _EPYC),
                   _multi(2, {_BENCH: 45.5e6}, _EPYC)]
                  + [_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(3, 9)])
        findings, meta = _meta_for(nights)
        assert findings == []
        assert meta["inconclusive_reasons"] == {_BENCH: "no-recent"}


class TestStratificationKeyIsNotFolded:
    """The docstring's promise is "Exact string equality, or nothing"."""

    def test_case_variants_are_distinct_classes(self):
        lower = _EPYC.lower()
        nights = ([_hnight(i, 45.5e6, _EPYC) for i in range(3)]
                  + [_hnight(i, 35e6, lower) for i in range(3, 9)])
        findings, meta = _meta_for(nights)
        assert findings == []                              # +30% ACROSS classes is not a trend
        assert meta["cpu_class_counts"] == {_EPYC: 3, lower: 6}
        assert meta["status"] == _INCONCLUSIVE

    def test_punctuation_variants_are_distinct_classes(self):
        stripped = _EPYC.replace("-", " ")
        nights = ([_hnight(i, 45.5e6, _EPYC) for i in range(3)]
                  + [_hnight(i, 35e6, stripped) for i in range(3, 9)])
        _findings, meta = _meta_for(nights)
        assert sorted(meta["cpu_class_counts"]) == sorted([_EPYC, stripped])

    def test_control_the_identical_string_is_one_class(self):
        nights = ([_hnight(i, 45.5e6, _EPYC) for i in range(3)]
                  + [_hnight(i, 35e6, _EPYC) for i in range(3, 9)])
        findings, meta = _meta_for(nights)
        assert meta["cpu_class_counts"] == {_EPYC: 9}
        assert [f.kind for f in findings] == ["sustained"]


# ===========================================================================
# ROUND 4 — the ledger arithmetic on the write plane, and the holes round 3
# left behind it.
#
# Round 3 gave the close path a per-bench guard and gave the marker a
# carry-forward. Both are correct about what they ADD to the ledger and were
# never asked what they must REMOVE from it: the held-open path wrote the whole
# prior ledger back verbatim, so a row measured clean tonight was re-asserted
# every night, forever. Every test below names the behaviour it pins; revert
# that behaviour and exactly this test goes red.
# ===========================================================================


def _b_gone_a_clean(cpu=_EPYC):
    """Mirror of `_a_gone_b_clean`: B stopped reporting 3 nights ago, A reports
    every night and is flat. Tonight: CLEAR, evaluated=[A], inconclusive=[B]."""
    nights = []
    for i in range(9):
        row = {_BENCH: 35e6}
        if i >= 3:
            row[_BENCH_B] = 12e6
        nights.append(_multi(i, row, cpu))
    return nights


_BENCH_E = "BenchmarkValidateSchema_128"


def _two_benches_regress(cpu=_EPYC):
    """A and B both step +30% together — the only window in the suite that
    produces MORE THAN ONE finding on one night."""
    return ([_multi(i, {_BENCH: 45.5e6, _BENCH_B: 15.6e6}, cpu) for i in range(3)]
            + [_multi(i, {_BENCH: 35e6, _BENCH_B: 12e6}, cpu) for i in range(3, 9)])


class TestHeldOpenLedgerIsPruned:
    """A1 — the held-open CLEAR path rendered the body with `state=prior_state`
    (the FINDINGS path uses `state=state_for_body`), so the ledger only ever
    grew. Every test in `TestCloseIsGatedPerBench` uses a SINGLE-ROW marker, and
    on a single-row marker the right answer and the wrong one are byte-identical
    — which is exactly why the mutation survived. Two rows, one of them measured
    clean tonight, is the shape that tells them apart."""

    _PRIOR = [[_BENCH, "sustained"], [_BENCH_B, "sustained"]]

    def _night_one(self, monkeypatch, tmp_path, capsys):
        """One held-open night: A stopped reporting, B measured and clean."""
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=self._PRIOR)])
        capsys.readouterr()
        return calls, _body_of(_one(calls, "issue", "edit", "42"))

    def test_the_row_measured_clean_tonight_is_dropped(
            self, monkeypatch, tmp_path, capsys):
        _calls, body = self._night_one(monkeypatch, tmp_path, capsys)
        # B WAS evaluated tonight and was under its floor. Keeping its row is
        # re-asserting a flag the night just disproved.
        assert ab._parse_state_marker(body) == [[_BENCH, "sustained"]]

    def test_the_unmeasured_row_is_still_kept_and_still_holds_the_issue_open(
            self, monkeypatch, tmp_path, capsys):
        calls, body = self._night_one(monkeypatch, tmp_path, capsys)
        assert _sel(calls, "issue", "close") == []
        assert "STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT" in body
        assert f"`{_BENCH}` (sustained)" in body

    def test_a_stale_row_would_silence_the_next_real_regression(
            self, monkeypatch, tmp_path, capsys):
        # Night 2: A still gone, B genuinely regresses. With the stale row the
        # ledger already says `B sustained`, so prior == current, the transition
        # comment is None and every subscriber is notified of nothing.
        _calls, night_one_body = self._night_one(monkeypatch, tmp_path, capsys)
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_regressed(),
                           issues=[_issue(42, body=night_one_body)])
        capsys.readouterr()
        comment = _one(calls, "issue", "comment", "42")
        assert f"**Newly flagged:** `{_BENCH_B}`" in _body_of(comment).splitlines()

    def test_a_stale_row_would_report_a_new_regression_as_good_news(
            self, monkeypatch, tmp_path, capsys):
        # Same night 2, but B comes back as creep rather than sustained. Against
        # the stale `sustained` row that diff reads as an IMPROVEMENT — "Eased to
        # creep" — for a benchmark that has just started regressing.
        _calls, night_one_body = self._night_one(monkeypatch, tmp_path, capsys)
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_creep(),
                           issues=[_issue(42, body=night_one_body)])
        capsys.readouterr()
        comment = _body_of(_one(calls, "issue", "comment", "42"))
        assert f"**Newly flagged:** `{_BENCH_B}`" in comment.splitlines()
        assert "Eased to creep" not in comment

    def test_alternating_outages_still_let_the_issue_close(
            self, monkeypatch, tmp_path, capsys):
        # Two benches taking turns being unmeasurable is not exotic — host-class
        # stratification produces that parity on its own. With the ledger frozen
        # at its widest, one of the two is always unverified and the issue can
        # never close again.
        _calls, night_one_body = self._night_one(monkeypatch, tmp_path, capsys)
        _rc, calls = _live(monkeypatch, tmp_path, _b_gone_a_clean(),
                           issues=[_issue(42, body=night_one_body)])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"

    def test_the_pruned_ledger_matches_what_the_findings_path_would_write(self):
        # The held-open path is the FINDINGS path with an empty finding set, so
        # the two must agree on the rule. This is the equivalence the fix relies
        # on (`state=tracked` == `_carry_forward_state([], tracked)`).
        tracked = [[_BENCH, "sustained"]]
        assert ab._carry_forward_state([], tracked) == tracked


class TestClosingWritesTheLedgerItsOwnProseImplies:
    """A1 — the close path wrote the PRIOR ledger back (`state=prior_state or []`).

    Control flow makes that provably wrong: the held-open guard above `continue`s
    on any unverified row, so a night that reaches the close is a night that
    measured EVERY row of the marker and found every one under its floor. The
    ledger written at close time is therefore 100% rows the same night disproved
    — the exact error the held-open path six lines up exists to prevent — and
    because no `tracked=` is passed there is not even a STILL TRACKED line to
    disclose it. `[]` is the only value consistent with the body's own prose.

    KNOWN LIMITATION pinned below: a reopened issue consequently carries no
    per-bench claim. That is #1396 lifecycle follow-up, not a defect to be
    patched with another marker field."""

    def _close_then_read_back(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"
        return _body_of(_one(calls, "issue", "edit", "42"))

    def test_the_closed_body_writes_an_empty_ledger(
            self, monkeypatch, tmp_path, capsys):
        body = self._close_then_read_back(monkeypatch, tmp_path, capsys)
        assert ab._parse_state_marker(body) == []

    def test_the_hidden_marker_does_not_contradict_the_visible_verdict(
            self, monkeypatch, tmp_path, capsys):
        # The symptom in one assertion: prose that says CLEAR above a marker that
        # says `sustained`, with nothing in between to warn the reader.
        body = self._close_then_read_back(monkeypatch, tmp_path, capsys)
        assert "### ✅ Nothing above its floor tonight (CLEAR)" in body
        assert "none is above its floor" in body
        assert "STILL TRACKED BY THIS ISSUE" not in body
        assert "sustained" not in body.splitlines()[-1]      # the marker line

    def test_two_disproved_rows_are_both_dropped(
            self, monkeypatch, tmp_path, capsys):
        # A single-row marker cannot tell "keep the ledger" from "keep only the
        # unverified rows" apart from "blank it": with one row that was measured
        # clean, two of the three answers coincide. Two rows, both measured clean
        # tonight, separates all three.
        nights = [_multi(i, {_BENCH: 35e6, _BENCH_B: 12e6}, _EPYC) for i in range(9)]
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"],
                                                     [_BENCH_B, "creep"]])])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"
        assert ab._parse_state_marker(_body_of(_one(calls, "issue", "edit", "42"))) == []

    def test_a_reopened_issue_does_not_replay_a_disproved_transition(
            self, monkeypatch, tmp_path, capsys):
        # The retained ledger's worst downstream effect. Reopen, then B starts a
        # genuinely NEW creep. Against a stale `[[B, sustained]]` row that diff
        # reads as `Eased to creep` — good news for a bench that just began to
        # regress — and can even add the `recovering` label on that false diff.
        nights = [_multi(i, {_BENCH: 35e6, _BENCH_B: 12e6}, _EPYC) for i in range(9)]
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"],
                                                     [_BENCH_B, "sustained"]])])
        capsys.readouterr()
        closed_body = _body_of(_one(calls, "issue", "edit", "42"))
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_creep(),
                           issues=[_issue(42, body=closed_body)])
        capsys.readouterr()
        comment = _body_of(_one(calls, "issue", "comment", "42"))
        assert f"**Newly flagged:** `{_BENCH_B}`" in comment.splitlines()
        assert "Eased to creep" not in comment
        assert _sel(calls, "issue", "edit", "42", "--repo", _REPO,
                    "--add-label") == []

    def test_known_limitation_a_reopened_issue_has_no_per_bench_guard(
            self, monkeypatch, tmp_path, capsys):
        # PINNED, NOT ENDORSED. Blanking the ledger costs the reopen guard: the
        # next CLEAR night closes a reopened issue without checking the bench it
        # was filed for. Re-arming that needs issue-lifecycle state (a `closed_on`
        # field was explicitly rejected — every round it grows a new defect), so
        # it is a tracked limitation. If this test ever goes red because the
        # guard came back, DELETE the test — do not restore the ledger.
        closed_body = self._close_then_read_back(monkeypatch, tmp_path, capsys)
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, body=closed_body)])
        capsys.readouterr()
        assert _one(calls, "issue", "close")[2] == "42"


class TestHeldOpenWarningNamesWhatIsStuck:
    """A3 — "issue #42 held open" repeated every night with no way to tell a
    bench that is down for one night from one that was renamed a month ago. Only
    the second one needs a human, and only the second one never resolves."""

    def _run(self, monkeypatch, tmp_path, capsys, nights, state):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, nights, issues=[_issue(42, state=state)])
        return capsys.readouterr().err + summary.read_text(encoding="utf-8")

    def test_the_warning_says_how_long_the_bench_has_been_silent(
            self, monkeypatch, tmp_path, capsys):
        text = self._run(monkeypatch, tmp_path, capsys, _a_gone_b_clean(),
                         [[_BENCH, "sustained"]])
        assert f"`{_BENCH}` last reported 3 same-class nights ago" in text

    def test_a_bench_absent_from_the_whole_window_says_so(
            self, monkeypatch, tmp_path, capsys):
        text = self._run(monkeypatch, tmp_path, capsys, _flat_window(_EPYC),
                         [[_BENCH_C, "sustained"]])
        assert f"`{_BENCH_C}` has not reported in ANY of the last 9 same-class nights" in text

    def test_the_step_summary_carries_it_too(self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _a_gone_b_clean(),
              issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        text = summary.read_text(encoding="utf-8")
        assert "issue #42 held open" in text
        assert f"`{_BENCH}` last reported 3 same-class nights ago" in text

    def test_the_counter_is_measured_from_the_newest_night(self):
        nights = _a_gone_b_clean()
        assert ab._nights_since_seen(nights, _BENCH) == 3      # absent from 0,1,2
        assert ab._nights_since_seen(nights, _BENCH_B) == 0    # reported tonight
        assert ab._nights_since_seen(nights, "BenchmarkNeverSeen") == len(nights)

    def test_the_phrase_is_singular_for_one_night(self):
        nights = ([_multi(0, {_BENCH_B: 12e6}, _EPYC)]
                  + [_multi(i, {_BENCH: 35e6, _BENCH_B: 12e6}, _EPYC) for i in range(1, 9)])
        assert ab._held_open_detail(nights, _BENCH) == f"`{_BENCH}` last reported 1 night ago"
        assert ab._held_open_detail(nights, _BENCH, True) == \
            f"`{_BENCH}` last reported 1 same-class night ago"

    def test_a_bench_present_tonight_but_unjudgeable_is_not_called_silent(self):
        nights = [_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(9)]
        assert ab._held_open_detail(nights, _BENCH) == \
            f"`{_BENCH}` reported tonight but could not be judged"


class TestUnverifiedCauseHasAThirdOption:
    """A4 — a bench named only in the marker can be absent from the window
    ENTIRELY, in which case `analyze_trend` never iterates it and it carries no
    reason code. `_inconclusive_causes` then fell through to its "nothing
    appeared at all" clause — printed on a CLEAR night, i.e. beside a verdict
    that exists BECAUSE other benchmarks appeared."""

    def _text(self, monkeypatch, tmp_path, capsys, state):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _flat_window(_EPYC),
              issues=[_issue(42, state=state)])
        return capsys.readouterr().err + summary.read_text(encoding="utf-8")

    def test_the_self_contradicting_sentence_is_gone(
            self, monkeypatch, tmp_path, capsys):
        text = self._text(monkeypatch, tmp_path, capsys, [[_BENCH_C, "sustained"]])
        assert "no benchmark appeared in tonight's same-class window at all" not in text

    def test_the_cause_names_the_rename(self, monkeypatch, tmp_path, capsys):
        text = self._text(monkeypatch, tmp_path, capsys, [[_BENCH_C, "sustained"]])
        assert "did not appear in ANY of the 9 nights in tonight's window" in text
        assert "RENAMED or deleted benchmark" in text
        assert f"`{_BENCH_C}`" in text

    def test_the_two_existing_causes_are_word_for_word_unchanged(
            self, monkeypatch, tmp_path, capsys):
        text = self._run_no_recent(monkeypatch, tmp_path, capsys)
        assert "did not report on all 3 newest same-class nights" in text
        assert "perf-timeout/crash symptom" in text

    def _run_no_recent(self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _a_gone_b_clean(),
              issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        return capsys.readouterr().err + summary.read_text(encoding="utf-8")

    def test_the_helper_reports_all_three_causes_in_order(self):
        meta = {"inconclusive_reasons": {"BenchA": ab.REASON_NO_RECENT,
                                         "BenchB": ab.REASON_THIN_ANCHOR},
                "recent_k": 3, "n_class_nights": 4, "n_nights": 9, "min_settled": 3}
        causes = ab._unverified_causes(meta, ["BenchB", "BenchGone", "BenchA"])
        assert len(causes) == 3
        assert "perf-timeout/crash symptom" in causes[0] and "`BenchA`" in causes[0]
        assert "ran on tonight's host class" in causes[1] and "`BenchB`" in causes[1]
        assert "RENAMED or deleted" in causes[2] and "`BenchGone`" in causes[2]

    def test_the_window_level_helper_keeps_its_own_fallback(self):
        # `_inconclusive_causes` answers a DIFFERENT question and keeps the
        # sentence that was wrong only for the per-issue one.
        assert ab._inconclusive_causes({"inconclusive_reasons": {}}) == \
            ["no benchmark appeared in tonight's same-class window at all"]

    def test_tonight_unknown_still_explains_itself_once(self):
        meta = {"stratification": _STRATA_TONIGHT_UNKNOWN}
        assert ab._unverified_causes(meta, ["A"]) == ab._inconclusive_causes(meta)


class TestRecoveringLabelBlockIsScoped:
    """A5 — the `add` was blocked by ANY unverified bench in the prior state.
    "The sustained regression cleared, only creep remains" is a claim about the
    benches the prior state called `sustained`; a `creep` row going unmeasured
    says nothing about it, and vetoing on it withholds real news."""

    def test_an_unrelated_creep_row_does_not_veto_the_label(self):
        creep = [_finding(_BENCH_B, "creep")]
        prior = [[_BENCH, "creep"], [_BENCH_B, "sustained"]]
        assert ab._recovering_label_change(creep, prior, [], unverified=[_BENCH]) == "add"

    def test_the_sustained_row_itself_still_vetoes_it(self):
        creep = [_finding(_BENCH_B, "creep")]
        prior = [[_BENCH, "sustained"], [_BENCH_B, "creep"]]
        assert ab._recovering_label_change(creep, prior, [], unverified=[_BENCH]) is None

    def test_end_to_end_the_label_is_applied(self, monkeypatch, tmp_path, capsys):
        # A (creep in the marker) stopped reporting; B was sustained and is now
        # creep-only, measured tonight. That IS "on the mend", and it is exactly
        # the notification the blanket block swallowed.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_creep(),
                           issues=[_issue(42, state=[[_BENCH, "creep"],
                                                     [_BENCH_B, "sustained"]])])
        capsys.readouterr()
        assert ["issue", "edit", "42", "--repo", "vencil/Dynamic-Alerting-Integrations",
                "--add-label", "perf-trend:recovering"] in calls

    def test_removal_is_still_untouched_by_the_unverified_set(self):
        sust = [_finding(_BENCH_B, "sustained")]
        prior = [[_BENCH, "sustained"]]
        assert ab._recovering_label_change(sust, prior, ["perf-trend:recovering"],
                                           unverified=[_BENCH]) == "remove"


class TestDryRunGatesEveryBranch:
    """B2 — `--dry-run` was proved on ONE of the four write branches (update).
    Deleting `not args.dry_run` from create, close or the INCONCLUSIVE refresh
    left the suite green, i.e. `--dry-run` could write to GitHub."""

    def test_the_create_branch_writes_nothing(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC), dry_run=True)
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "create") == []
        assert _sel(calls, "gh", "label", "create") == []
        assert "would open" in err

    def test_the_close_branch_writes_nothing(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(7, state=[[_BENCH, "sustained"]],
                                          labels=["perf-trend:recovering"])],
                           dry_run=True)
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "close") == []
        assert _sel(calls, "issue", "edit") == []
        assert _sel(calls, "issue", "comment") == []
        assert "would close" in err and "would refresh body" in err

    def test_the_inconclusive_branch_writes_nothing(self, monkeypatch, tmp_path, capsys):
        nights = ([_multi(i, {_BENCH: 35e6}, _XEON) for i in range(3)]
                  + [_multi(3, {_BENCH: 35e6}, _EPYC), _multi(4, {_BENCH: 35e6}, _XEON),
                     _multi(5, {_BENCH: 35e6}, _EPYC), _multi(6, {_BENCH: 35e6}, _XEON),
                     _multi(7, {_BENCH: 35e6}, _EPYC)])
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])],
                           dry_run=True)
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "edit") == []
        assert "would update body of perf-trend issue #42" in err

    def test_the_held_open_branch_writes_nothing(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])],
                           dry_run=True)
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "edit") == []
        assert "would update body of" in err


class TestMultipleFindingsInOneNight:
    """B5 — the whole suite topped out at ONE finding per window, so every
    multi-finding rendering path (table, comment, ledger, multi-issue FINDINGS
    branch) had never executed."""

    def test_two_findings_are_two_table_rows(self):
        findings, meta = _meta_for(_two_benches_regress())
        assert [(f.bench, f.kind) for f in findings] == \
            [(_BENCH_B, "sustained"), (_BENCH, "sustained")]
        rows = [li for li in ab.render_trend_issue_body(findings, meta).splitlines()
                if li.startswith("| `Benchmark")]
        assert len(rows) == 2
        assert rows[0].startswith(f"| `{_BENCH_B}` | sustained |")
        assert rows[1].startswith(f"| `{_BENCH}` | sustained |")

    def test_findings_come_out_in_sorted_order(self):
        # `for bench in sorted(benches)` is the ONLY thing that makes the finding
        # order deterministic: `benches` is a set and str hashing is randomised
        # per process, and `meta`'s lists are re-sorted on the way out so the
        # loop order leaks nowhere else. Two benches would catch an unsorted loop
        # about half the time; five inserted in scrambled order catch it in 119
        # runs out of 120.
        scrambled = [_BENCH, _BENCH_D, _BENCH_C, _BENCH_E, _BENCH_B]
        nights = [_multi(i, {b: (45.5e6 if i < 3 else 35e6) for b in scrambled}, _EPYC)
                  for i in range(9)]
        findings, _meta = _meta_for(nights)
        assert [f.bench for f in findings] == \
            [_BENCH_D, _BENCH_B, _BENCH_C, _BENCH, _BENCH_E]

    def test_one_transition_category_can_name_two_benches(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _two_benches_regress(),
                           issues=[_issue(42, body=ab._render_state_marker([]))])
        capsys.readouterr()
        comment = _body_of(_one(calls, "issue", "comment", "42"))
        assert f"**Newly flagged:** `{_BENCH_B}`, `{_BENCH}`" in comment.splitlines()

    def test_the_ledger_can_carry_two_unverified_rows(self):
        state = [[_BENCH, "sustained"], [_BENCH_B, "creep"], [_BENCH_C, "sustained"]]
        meta = {"evaluated_benches": [_BENCH_C]}
        unverified = ab._unverified_benches(state, meta)
        assert unverified == [_BENCH_B, _BENCH]
        assert ab._tracked_rows(state, unverified) == \
            [[_BENCH_B, "creep"], [_BENCH, "sustained"]]

    def test_two_tracked_rows_are_named_together_in_the_body(
            self, monkeypatch, tmp_path, capsys):
        nights = []
        for i in range(9):
            row = {_BENCH_B: 12e6}
            if i >= 3:
                row[_BENCH] = 35e6
            nights.append(_multi(i, row, _EPYC))
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"],
                                                     [_BENCH_C, "creep"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert f"`{_BENCH_C}` (creep), `{_BENCH}` (sustained)" in body
        assert "tonight could not measure them" in body
        assert ab._parse_state_marker(body) == [[_BENCH_C, "creep"],
                                                [_BENCH, "sustained"]]

    def test_findings_touch_only_the_first_of_several_open_issues(
            self, monkeypatch, tmp_path, capsys):
        # Pinned as SHIPPED behaviour, NOT as desirable: the FINDINGS branch
        # files a new issue only when `open_issues` is empty and updates only
        # `[0]`, so one wedged issue absorbs every later regression in the repo.
        # See the KNOWN LIMITATION block in the module.
        issues = [_issue(11, body=ab._render_state_marker([])),
                  _issue(22, state=[[_BENCH_C, "creep"]])]
        _rc, calls = _live(monkeypatch, tmp_path, _two_benches_regress(), issues=issues)
        capsys.readouterr()
        assert [c[2] for c in _sel(calls, "issue", "edit")] == ["11"]
        assert [c[2] for c in _sel(calls, "issue", "comment")] == ["11"]
        assert _sel(calls, "issue", "create") == []


class TestCanarySampleCountBoundary:
    """B6 — `canary_n >= 2` is the judgement "this is a measurement, not the
    absence of one", and `canary_n == 2` never occurred in the suite: `>= 3` and
    `>= 6` both survived. `_cv`'s own `len < 2` boundary was equally open."""

    def _canary_on(self, n):
        """9 same-class nights; exactly the newest `n` carry the canary."""
        return [_multi(i, {_BENCH: 35e6}, _EPYC,
                       canary_ns=360_000.0 if i < n else None)
                for i in range(9)]

    def test_two_samples_is_a_measurement(self):
        _f, meta = _meta_for(self._canary_on(2))
        assert meta["canary_n"] == 2
        body = ab.render_trend_issue_body([], meta)
        assert "Control-canary night-to-night CV: **0.00%**" in body
        assert "max of fixed minimum and canary-noise-scaled" in body
        assert "not measured" not in body

    def test_one_sample_is_not(self):
        _f, meta = _meta_for(self._canary_on(1))
        assert meta["canary_n"] == 1
        assert "Control-canary night-to-night CV: **not measured**" in \
            ab.render_trend_issue_body([], meta)

    def test_cv_needs_two_points_not_three(self):
        # `len(vals) < 2` → `< 3` reports a real two-night jitter as 0.0, which
        # is the same value the code uses for "never measured" — and it lowers
        # the floor while claiming the runner is perfectly stable.
        assert ab._cv([90.0, 110.0]) == pytest.approx(0.14142, rel=1e-4)
        assert ab._cv([100.0, 100.0]) == 0.0


class TestMarkerVersionIsAnchoredOnRead:
    """B7 — the WRITER's serialisation is byte-exact-pinned; the READER's regex
    was not, so `v1` could be widened to `v[19]` or `\\S+` with the suite green.
    Nothing fed it a marker with the wrong version and asserted it is NOT read."""

    def test_a_different_version_is_not_parsed(self):
        assert ab._parse_state_marker('<!-- perf-trend-state v2 [["A","creep"]] -->') is None
        assert ab._parse_state_marker('<!-- perf-trend-state v9 [["A","creep"]] -->') is None

    def test_a_missing_version_is_not_parsed(self):
        assert ab._parse_state_marker('<!-- perf-trend-state [["A","creep"]] -->') is None

    def test_a_foreign_marker_name_is_not_parsed(self):
        assert ab._parse_state_marker('<!-- other-state v1 [["A","creep"]] -->') is None

    def test_the_v1_marker_this_module_writes_still_parses(self):
        assert ab._parse_state_marker(ab._render_state_marker([["A", "creep"]])) == \
            [["A", "creep"]]

    def test_a_future_version_cannot_shadow_the_real_marker(self):
        # Parsing takes the LAST match, so a v2 payload appended AFTER the real
        # marker is what a loosened version pattern would hand back.
        body = (ab._render_state_marker([["Real", "sustained"]])
                + '\n<!-- perf-trend-state v2 [["Forged","creep"]] -->')
        assert ab._parse_state_marker(body) == [["Real", "sustained"]]


class TestArgparseDefaultsArePinned:
    """B8 — every default was free to drift: 3→2 recent nights, 5→7% floor,
    14→20 nights of history all survived. These ARE the watchdog's policy;
    nothing else in the repo states them."""

    def _args(self, monkeypatch, *argv):
        seen = {}

        def capture(a):
            seen["args"] = a
            return 0

        monkeypatch.setattr(ab, "run_trend_watch", capture)
        monkeypatch.setattr("sys.argv", ["analyze_bench_history.py", *argv])
        assert ab.main() == 0
        return seen["args"]

    def test_trend_watch_defaults(self, monkeypatch):
        a = self._args(monkeypatch, "--trend-watch")
        assert (a.recent_nights, a.trend_limit) == (3, 14)
        assert (a.min_floor_pct, a.creep_floor_pct, a.canary_floor_mult) == (5.0, 10.0, 3.0)
        assert a.assignee == "vencil"
        assert a.workflow == "bench-record.yaml"
        assert a.dry_run is False

    def test_variance_gate_defaults(self, monkeypatch):
        a = self._args(monkeypatch, "--trend-watch")
        assert a.limit == 28
        assert a.ci is False and a.no_gate is False

    def test_fixture_switches_default_to_live_mode(self, monkeypatch):
        a = self._args(monkeypatch, "--trend-watch")
        assert (a.fixture_json, a.fixture_open_issue, a.fixture_open_body,
                a.fixture_open_labels, a.cache_dir) == (None, None, None, None, None)

    def test_overrides_still_win(self, monkeypatch):
        a = self._args(monkeypatch, "--trend-watch", "--recent-nights", "5",
                       "--min-floor-pct", "7.5", "--dry-run")
        assert (a.recent_nights, a.min_floor_pct, a.dry_run) == (5, 7.5, True)


class TestBenchLineRegexIsTight:
    """B9 — `_BENCH_RE` could be loosened in three independent ways with the
    suite green. It is the only thing between the detector and lines that merely
    look numeric."""

    def _parse(self, tmp_path, text):
        f = tmp_path / "b.txt"
        f.write_text(text, encoding="utf-8")
        return list(ab.parse_bench_file(f, run_id=1))

    def test_the_vcpu_suffix_is_required(self, tmp_path):
        # `go test -bench` always emits `-N`; without it the line is not a row
        # this tool wrote, and its columns mean something else.
        assert self._parse(tmp_path, "BenchmarkX   100   123 ns/op\n") == []

    def test_the_unit_must_be_exactly_ns_per_op(self, tmp_path):
        assert self._parse(tmp_path, "BenchmarkX-4   100   123 ns/opx\n") == []
        assert self._parse(tmp_path, "BenchmarkX-4   100   123 us/op\n") == []

    def test_a_bench_row_must_start_the_line(self, tmp_path):
        assert self._parse(tmp_path, "  BenchmarkX-4   100   123 ns/op\n") == []
        assert self._parse(tmp_path, "ok BenchmarkX-4   100   123 ns/op\n") == []

    def test_both_numeric_columns_are_required(self, tmp_path):
        assert self._parse(tmp_path, "BenchmarkX-4   many   123 ns/op\n") == []
        assert self._parse(tmp_path, "BenchmarkX-4   100   fast ns/op\n") == []

    def test_the_pattern_itself_is_anchored(self):
        # `parse_bench_file` uses `.match`, which anchors on its own — so the
        # `^` in the pattern is observable ONLY here. Pinned because the pattern
        # is module-level state that a future caller may `.search` with.
        assert ab._BENCH_RE.search("ok BenchmarkX-4   100   123 ns/op") is None

    def test_the_canonical_row_still_parses(self, tmp_path):
        s = self._parse(tmp_path, "BenchmarkX-4   100   123 ns/op   1024 B/op\n")
        assert [(x.bench, x.ns_per_op) for x in s] == [("BenchmarkX", 123.0)]


class TestCpuHeaderRegexIsTight:
    """B9 (second half) — `_CPU_RE` decides the stratification key, so a
    loosened pattern silently re-merges host classes."""

    def _cpu(self, tmp_path, text):
        f = tmp_path / "b.txt"
        f.write_text(text, encoding="utf-8")
        return ab.parse_cpu_model(f)

    def test_an_indented_header_is_not_a_header(self, tmp_path):
        assert self._cpu(tmp_path, f"   cpu: {_EPYC}\n") is None

    def test_a_mid_line_mention_is_not_a_header(self, tmp_path):
        assert self._cpu(tmp_path, f"note: the cpu: Fake\ncpu: {_EPYC}\n") == _EPYC

    def test_the_pattern_itself_is_anchored(self):
        # Same reasoning as `_BENCH_RE`: `parse_cpu_model` matches per line, so
        # dropping `^` is invisible through the function alone.
        assert ab._CPU_RE.search("note: the cpu: Fake") is None

    def test_trailing_whitespace_is_trimmed_and_the_value_stays_whole(self, tmp_path):
        # `(\S.*?)` is non-greedy: without the `\s*$` tail it matches ONE
        # character and the whole runner pool collapses into class "A".
        assert self._cpu(tmp_path, f"cpu: {_EPYC}   \n") == _EPYC


# ===========================================================================
# ROUND 5 — findings from the fifth blind review.
#
# Two behaviour fixes (A1 close-path ledger, A2 off-class misdiagnosis) and
# seven test holes. Every test below names the mutation it kills; reverting the
# behaviour it describes turns it red. The fire arithmetic is untouched.
# ===========================================================================

_XEON_B = _XEON


def _off_class_window():
    """Tonight is EPYC; `_BENCH_C` only ever ran on the XEON nights.

    It IS in the window and it is NOT in tonight's stratum — the ordinary state
    of a heterogeneous runner pool, and the one that used to be reported as a
    rename."""
    return ([_multi(i, {_BENCH: 35e6}, _EPYC) for i in range(8)]
            + [_multi(i, {_BENCH: 35e6, _BENCH_C: 9e6}, _XEON) for i in range(8, 12)])


class TestOffClassIsNotDiagnosedAsARename:
    """A2 — `_nights_since_seen`/`_held_open_detail` scanned the WHOLE window
    while `_unverified_causes` classified from tonight's STRATUM and fell back
    to `REASON_ABSENT` whenever there was no reason code. One sentence then said
    both "last reported 8 nights ago" and "did not appear in ANY of the 12
    nights … a RENAMED or deleted benchmark reaches this state and never leaves
    it". On a heterogeneous pool that is the common case, not a rename."""

    def _text(self, monkeypatch, tmp_path, capsys, nights, state):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, nights, issues=[_issue(42, state=state)])
        return capsys.readouterr().err + summary.read_text(encoding="utf-8")

    def test_the_precondition_really_is_off_class_not_absent(self):
        _f, meta = _meta_for(_off_class_window())
        assert meta["status"] == _CLEAR
        assert meta["evaluated_benches"] == [_BENCH]
        # `_BENCH_C` is in the window but never in tonight's stratum, so
        # `analyze_trend` never iterates it: no reason code, but it IS a name
        # the window has seen.
        assert _BENCH_C not in meta["inconclusive_reasons"]
        assert meta["window_benches"] == [_BENCH_C, _BENCH]

    def test_the_cause_says_other_machine_not_rename(
            self, monkeypatch, tmp_path, capsys):
        text = self._text(monkeypatch, tmp_path, capsys,
                          _off_class_window(), [[_BENCH_C, "sustained"]])
        assert ("DID report inside the window, but never on tonight's host class "
                f"(`{_EPYC}`, 8 of 12 window nights)") in text
        assert "it is NOT a rename" in text
        assert "RENAMED or deleted benchmark" not in text
        assert "never leaves it" not in text

    def test_the_two_helpers_now_count_the_same_nights(
            self, monkeypatch, tmp_path, capsys):
        # The self-contradiction, asserted directly: the duration phrase and the
        # cause phrase must describe ONE population. 8 same-class nights, not
        # "3 nights ago" out of a 12-night window.
        text = self._text(monkeypatch, tmp_path, capsys,
                          _off_class_window(), [[_BENCH_C, "sustained"]])
        assert (f"`{_BENCH_C}` has not reported in ANY of the last 8 same-class nights"
                in text)
        assert "last reported 4 nights ago" not in text

    def test_a_genuine_rename_is_still_called_a_rename(
            self, monkeypatch, tmp_path, capsys):
        # The fourth cause must not swallow the third: `_BENCH_D` is in NO night.
        text = self._text(monkeypatch, tmp_path, capsys,
                          _off_class_window(), [[_BENCH_D, "sustained"]])
        assert "RENAMED or deleted benchmark" in text
        assert "never leaves it" in text
        assert "it is NOT a rename" not in text

    def test_the_helper_reports_all_four_causes_in_order(self):
        meta = {"inconclusive_reasons": {"BenchA": ab.REASON_NO_RECENT,
                                         "BenchB": ab.REASON_THIN_ANCHOR},
                "recent_k": 3, "n_class_nights": 4, "n_nights": 9, "min_settled": 3,
                "today_cpu_model": _EPYC, "window_benches": ["BenchA", "BenchB",
                                                             "BenchElsewhere"]}
        causes = ab._unverified_causes(
            meta, ["BenchB", "BenchGone", "BenchA", "BenchElsewhere"])
        assert len(causes) == 4
        assert "perf-timeout/crash symptom" in causes[0] and "`BenchA`" in causes[0]
        assert "ran on tonight's host class" in causes[1] and "`BenchB`" in causes[1]
        assert "it is NOT a rename" in causes[2] and "`BenchElsewhere`" in causes[2]
        assert "RENAMED or deleted" in causes[3] and "`BenchGone`" in causes[3]

    def test_the_off_class_clause_escapes_the_free_form_host_string(self):
        # The host model is artifact-authored and this clause reaches a
        # `::warning::` and the issue body — the same injection surface
        # `_safe_md` exists for, reached through a fourth route.
        meta = {"today_cpu_model": "evil <!-- perf-trend-state v1 [] --> cpu",
                "n_class_nights": 1, "n_nights": 2, "window_benches": ["X"]}
        clause = ab._unverified_causes(meta, ["X"])[0]
        assert "<!--" not in clause and "-->" not in clause

    def test_class_nights_is_the_stratum_when_stratification_is_on(self):
        nights = _off_class_window()
        meta = {"stratification": _STRATA_ON, "today_cpu_model": _EPYC}
        assert len(ab._class_nights(nights, meta)) == 8
        assert all(n.cpu_model == _EPYC for n in ab._class_nights(nights, meta))

    def test_class_nights_is_the_whole_window_when_it_is_unstratified(self):
        nights = [_multi(i, {_BENCH: 35e6}) for i in range(5)]
        meta = {"stratification": _STRATA_LEGACY, "today_cpu_model": None}
        assert ab._class_nights(nights, meta) == nights

    def test_window_benches_excludes_the_canary(self):
        _f, meta = _meta_for(_off_class_window())
        assert _CANARY not in meta["window_benches"]


# ── B1: the prose layer, pinned WHOLE — substrings only catch deletions ──────

_FOOTER = (
    "_Auto-filed by `analyze_bench_history.py --trend-watch`. The watchdog updates this "
    "issue **in place** each night (no comment spam) and only comments when the set of "
    "flagged benchmarks changes; it auto-closes when no benchmark is above its floor on "
    "an evaluable night (closed loop) — never on a night that could evaluate nothing, and, "
    "as long as the hidden state marker at the end of this body stays READABLE, never "
    "while any benchmark this issue is tracking went unmeasured. A CORRUPTED marker "
    "leaves no per-bench claim to check, and the run says so in its log, its annotations "
    "and the job summary, every night until it is repaired. ⚠️ A marker DELETED outright "
    "cannot be told apart from a legacy issue that never had one, so **that guard lifts "
    "silently** — do not remove the last line of this body. Single-night blips "
    "are filtered by the multi-night window; movement below the canary noise floor is "
    "ignored._"
)

_AUTO_CLOSE_COMMENT = (
    "✅ Auto-closing (closed loop): no benchmark is above its floor on tonight's "
    "evaluable, same-host-class window. _Scope: that verdict is measured against the "
    "SLIDING settled-window anchor, which drifts with the data — it is not proof that "
    "perf returned to the level this issue was filed at. A new issue is filed if it "
    "regresses again._"
)


class TestProseIsPinnedNotJustSampled:
    """B1 — the prose layer had ZERO anti-fabrication coverage.

    Every prose assertion in this suite was `substring in text`, which detects
    DELETIONS and is blind to ADDITIONS. With 284 tests green it was possible to
    add, to shipped output: "Perf has been verified to have returned to the level
    this issue was filed at." to the auto-close comment (the sentence the module
    comment forbids by name), "This issue auto-closes on ANY night with no
    findings, including nights that evaluated nothing." to the body footer (which
    contradicts the three-state contract), a benchmark that does not exist to the
    STILL TRACKED line, "All tracked benchmarks have recovered and the issue will
    be closed tomorrow." to the held-open warning, and "All benchmarks passed." to
    the INCONCLUSIVE body and step summary.

    These blocks are CONSTANT — no interpolation, or interpolation that is fully
    determined by the fixture — so they are pinned WHOLE: exact string equality,
    or `== ` on a contiguous slice of `splitlines()`. An inserted sentence
    anywhere inside them is then red by construction, not by luck of the
    substring chosen."""

    # ---- constants with no interpolation at all ---------------------------

    def test_the_auto_close_comment_is_pinned_word_for_word(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        assert _body_of(_one(calls, "issue", "comment", "42")) == _AUTO_CLOSE_COMMENT

    def test_the_auto_close_comment_does_not_claim_a_verified_recovery(self):
        # The one sentence the module comment names as forbidden, spelled out so
        # the intent survives a future reword of `_AUTO_CLOSE_COMMENT`.
        assert "not proof that perf returned to the level" in _AUTO_CLOSE_COMMENT
        assert "has been verified to have returned" not in _AUTO_CLOSE_COMMENT

    def test_the_body_footer_is_pinned_word_for_word(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC))
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "create"))
        assert _FOOTER in body.splitlines()          # exact LINE, not substring
        assert body.splitlines().count(_FOOTER) == 1

    def test_the_footer_does_not_promise_an_unconditional_auto_close(self):
        assert "never on a night that could evaluate nothing" in _FOOTER
        assert "including nights that evaluated nothing" not in _FOOTER

    # ---- blocks whose interpolation the fixture fully determines -----------

    def test_the_clear_body_tail_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        tail = body.splitlines()
        head = tail.index("### ✅ Nothing above its floor tonight (CLEAR)") - 1
        assert tail[head:] == [
            "",
            "### ✅ Nothing above its floor tonight (CLEAR)",
            "",
            "Benchmarks WERE evaluated on tonight's host class and none is above its floor.",
            "",
            _FOOTER,
            "",
            "<!-- perf-trend-state v1 [] -->",
        ]

    def test_the_held_open_body_tail_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        lines = _body_of(_one(calls, "issue", "edit", "42")).splitlines()
        head = lines.index("### ✅ Nothing above its floor tonight (CLEAR)") - 1
        assert lines[head:] == [
            "",
            "### ✅ Nothing above its floor tonight (CLEAR)",
            "",
            "Benchmarks WERE evaluated on tonight's host class and none is above its floor.",
            "",
            f"⚠️ **But `{_BENCH}` (sustained) — what this issue was filed for — is NOT "
            "among the benchmarks evaluated tonight.** A clean verdict on the OTHER "
            "benchmarks is not evidence about this one, so the issue stays open.",
            "",
            _FOOTER,
            "",
            f'<!-- perf-trend-state v1 [["{_BENCH}","sustained"]] -->',
        ]

    def test_the_still_tracked_line_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        # "add a benchmark that does not exist" is an ADDITION inside a line the
        # old assertions matched by substring.
        _rc, calls = _live(monkeypatch, tmp_path, _a_gone_b_clean(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        lines = _body_of(_one(calls, "issue", "edit", "42")).splitlines()
        assert (f"- ⚠️ STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT: `{_BENCH}` "
                "(sustained) — tonight could not measure it, so the state above is the "
                "last judged night's, and this issue is NOT closed.") in lines

    def test_the_held_open_warning_line_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _a_gone_b_clean(),
              issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        err = capsys.readouterr().err
        detail = (
            f"issue #42 is tracking `{_BENCH}`, and tonight did NOT evaluate it "
            f"(`{_BENCH}` last reported 3 same-class nights ago) — did not report on all "
            "3 newest same-class nights (a benchmark that STOPS reporting is the classic "
            f"perf-timeout/crash symptom, not a thin window): `{_BENCH}` — a CLEAR "
            "verdict on the OTHER benchmarks is not evidence about it, so the issue "
            "stays open"
        )
        assert f"::warning::perf-trend watchdog: {detail}." in err.splitlines()
        assert (f"⚠️ **perf-trend watchdog: issue #42 held open** — {detail}."
                in summary.read_text(encoding="utf-8").splitlines())

    def test_the_inconclusive_body_tail_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        nights = [_multi(i, {_BENCH: 35e6}, _EPYC if i == 0 else _XEON) for i in range(9)]
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        lines = _body_of(_one(calls, "issue", "edit", "42")).splitlines()
        head = lines.index(
            f"### ⚠️ Not evaluated tonight (INCONCLUSIVE) — host class `{_EPYC}`") - 1
        assert lines[head:] == [
            "",
            f"### ⚠️ Not evaluated tonight (INCONCLUSIVE) — host class `{_EPYC}`",
            "",
            "- did not report on all 3 newest same-class nights (a benchmark that STOPS "
            "reporting is the classic perf-timeout/crash symptom, not a thin window): "
            f"`{_BENCH}`",
            "",
            "No benchmark could be measured against a same-class anchor tonight, so no "
            "NEW finding was produced and — the point of the three-state verdict — "
            "**nothing is closed either**. Silence from a detector that never ran is not "
            "evidence of recovery.",
            "",
            f"**This issue is still tracking `{_BENCH}` (sustained).** That is the state "
            "the last night which COULD judge it left behind, not a measurement from "
            "tonight — tonight says nothing about it either way.",
            "",
            _FOOTER,
            "",
            f'<!-- perf-trend-state v1 [["{_BENCH}","sustained"]] -->',
        ]

    def test_the_inconclusive_step_summary_is_pinned_whole(
            self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        nights = [_multi(i, {_BENCH: 35e6}, _EPYC if i == 0 else _XEON) for i in range(9)]
        _live(monkeypatch, tmp_path, nights)
        capsys.readouterr()
        assert summary.read_text(encoding="utf-8").splitlines() == [
            f"⚠️ **perf-trend watchdog: INCONCLUSIVE** — nothing evaluable tonight on "
            f"host class {_EPYC} — did not report on all 3 newest same-class nights (a "
            "benchmark that STOPS reporting is the classic perf-timeout/crash symptom, "
            f"not a thin window): `{_BENCH}`. Newest night in the window: "
            "2026-05-28T03:00:00Z.",
        ]


# ── B2: prior state and labels must come from the SAME issue ────────────────

class TestPriorStateAndLabelsComeFromTheEditedIssue:
    """B2 — the FINDINGS path edits `open_issues[0]` but read its body and its
    labels through the same index by coincidence: changing either read to `[-1]`
    left the suite green. With two open tickets that means issue B's ledger
    decides what is written onto issue A."""

    def _two_issues(self, monkeypatch, tmp_path, capsys):
        # #11 is edited (index 0). #22 exists only to be the WRONG source.
        return _live(monkeypatch, tmp_path, _sustained_window(_EPYC),
                     issues=[_issue(11, state=[[_BENCH_C, "sustained"]]),
                             _issue(22, state=[[_BENCH, "sustained"]],
                                    labels=[ab.RECOVERING_LABEL])])

    def test_the_edited_issue_is_the_first_one(self, monkeypatch, tmp_path, capsys):
        _rc, calls = self._two_issues(monkeypatch, tmp_path, capsys)
        capsys.readouterr()
        assert [c[2] for c in _sel(calls, "issue", "edit")] == ["11"]

    def test_the_carried_ledger_is_the_edited_issues_own(
            self, monkeypatch, tmp_path, capsys):
        # #11 tracks `_BENCH_C` (absent from the window → unverified → carried
        # forward). Read #22's ledger instead and `_BENCH_C` vanishes from #11.
        _rc, calls = self._two_issues(monkeypatch, tmp_path, capsys)
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "11"))
        assert ab._parse_state_marker(body) == [[_BENCH_C, "sustained"],
                                                [_BENCH, "sustained"]]
        assert f"`{_BENCH_C}` (sustained)" in body

    def test_the_transition_is_computed_against_the_edited_issues_prior(
            self, monkeypatch, tmp_path, capsys):
        # Against #11's prior this is `Newly flagged`. Against #22's prior the
        # state is unchanged, so there is no comment at all and every subscriber
        # of #11 is told nothing.
        _rc, calls = self._two_issues(monkeypatch, tmp_path, capsys)
        capsys.readouterr()
        comment = _body_of(_one(calls, "issue", "comment", "11"))
        assert f"**Newly flagged:** `{_BENCH}`" in comment.splitlines()

    def test_the_recovering_label_is_read_from_the_edited_issue(
            self, monkeypatch, tmp_path, capsys):
        # #22 carries the label, #11 does not. Reading #22's labels makes the
        # run strip a label from #11 that #11 never had.
        _rc, calls = self._two_issues(monkeypatch, tmp_path, capsys)
        capsys.readouterr()
        assert [c for c in calls if "--remove-label" in c] == []
        assert [c for c in calls if "--add-label" in c] == []


# ── B3: fixture mode is the only thing keeping offline tests off GitHub ─────

class TestFixtureModeCanNeverReachGitHub:
    """B3 — `gh_available = bool(args.fixture_json is None and shutil.which("gh"))`.

    Delete the first half and the suite stays green, because no test ever put
    `gh` on the PATH while running a fixture. On a CI runner `gh` is ALWAYS on
    the PATH, so that half is the only thing standing between an offline test
    and a real write to the real repository."""

    def _fixture_run(self, monkeypatch, tmp_path, nights, **over):
        calls: list[list[str]] = []
        monkeypatch.setattr(ab, "_gh",
                            lambda cmd, capture=True: (calls.append(list(cmd)), "")[1])
        # `gh` IS installed — the CI-runner reality the guard has to survive.
        monkeypatch.setattr(ab, "shutil", types.SimpleNamespace(
            which=lambda name: f"/usr/bin/{name}", rmtree=lambda *a, **k: None))
        monkeypatch.setattr(ab, "subprocess", types.SimpleNamespace(
            run=lambda cmd, **kw: (calls.append(list(cmd)),
                                   types.SimpleNamespace(returncode=0, stdout="",
                                                         stderr=""))[1],
            CalledProcessError=subprocess.CalledProcessError,
            TimeoutExpired=subprocess.TimeoutExpired))
        args = _trend_args(_write_fixture(tmp_path, nights), dry_run=False, **over)
        assert ab.run_trend_watch(args) == 0
        return calls

    def test_the_findings_create_branch_writes_nothing(
            self, monkeypatch, tmp_path, capsys):
        calls = self._fixture_run(monkeypatch, tmp_path, _sustained_window(_EPYC))
        capsys.readouterr()
        assert calls == []

    def test_the_findings_update_branch_writes_nothing(
            self, monkeypatch, tmp_path, capsys):
        calls = self._fixture_run(monkeypatch, tmp_path, _sustained_window(_EPYC),
                                  fixture_open_issue=42,
                                  fixture_open_body=_marker([_BENCH_C, "creep"]))
        capsys.readouterr()
        assert calls == []

    def test_the_close_branch_writes_nothing(self, monkeypatch, tmp_path, capsys):
        calls = self._fixture_run(monkeypatch, tmp_path, _flat_window(_EPYC),
                                  fixture_open_issue=42,
                                  fixture_open_body=_marker([_BENCH, "sustained"]))
        capsys.readouterr()
        assert calls == []

    def test_the_inconclusive_branch_writes_nothing(
            self, monkeypatch, tmp_path, capsys):
        nights = [_multi(i, {_BENCH: 35e6}, _EPYC if i == 0 else _XEON) for i in range(9)]
        calls = self._fixture_run(monkeypatch, tmp_path, nights,
                                  fixture_open_issue=42,
                                  fixture_open_body=_marker([_BENCH, "sustained"]))
        capsys.readouterr()
        assert calls == []


# ── B4: every list rendering must be proved on MORE THAN ONE element ────────

class TestListRenderingsAreProvedOnTwoElements:
    """B4 — four renderings joined a list and were only ever exercised with one
    item, so `[:1]` (print the first, drop the rest) survived everywhere: both
    `tracked` renderings, the held-open `stuck` phrase, and the FINDINGS step
    summary. A watchdog that names one of three stuck benchmarks is a watchdog
    that hides two."""

    _TWO = [[_BENCH_C, "sustained"], [_BENCH_D, "creep"]]

    def _held_open(self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, state=self._TWO)])
        err = capsys.readouterr().err
        return calls, err + summary.read_text(encoding="utf-8")

    def test_the_still_tracked_body_line_names_both(
            self, monkeypatch, tmp_path, capsys):
        calls, _text = self._held_open(monkeypatch, tmp_path, capsys)
        assert (f"- ⚠️ STILL TRACKED BY THIS ISSUE, NOT VERIFIED TONIGHT: "
                f"`{_BENCH_D}` (creep), `{_BENCH_C}` (sustained) — tonight could not "
                "measure them, so the state above is the last judged night's, and this "
                "issue is NOT closed.") in _body_of(
                    _one(calls, "issue", "edit", "42")).splitlines()

    def test_the_clear_verdict_block_names_both(
            self, monkeypatch, tmp_path, capsys):
        calls, _text = self._held_open(monkeypatch, tmp_path, capsys)
        assert (f"⚠️ **But `{_BENCH_D}` (creep), `{_BENCH_C}` (sustained) — what this "
                "issue was filed for — is NOT among the benchmarks evaluated tonight.** "
                "A clean verdict on the OTHER benchmarks is not evidence about this one, "
                "so the issue stays open.") in _body_of(
                    _one(calls, "issue", "edit", "42")).splitlines()

    def test_the_held_open_warning_names_both_and_dates_both(
            self, monkeypatch, tmp_path, capsys):
        _calls, text = self._held_open(monkeypatch, tmp_path, capsys)
        assert (f"(`{_BENCH_D}` has not reported in ANY of the last 9 same-class nights; "
                f"`{_BENCH_C}` has not reported in ANY of the last 9 same-class nights)"
                in text)

    def test_the_findings_step_summary_names_every_finding(
            self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _two_benches_regress(_EPYC))
        capsys.readouterr()
        assert summary.read_text(encoding="utf-8").splitlines() == [
            "⚠️ **perf-trend watchdog: 2 benchmark(s) above the floor** — "
            f"`{_BENCH_B}` (sustained), `{_BENCH}` (sustained)",
        ]

    def test_the_findings_table_renders_every_finding(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _two_benches_regress(_EPYC))
        capsys.readouterr()
        rows = [l for l in _body_of(_one(calls, "issue", "create")).splitlines()
                if l.startswith("| `Benchmark")]
        assert len(rows) == 2
        assert rows[0].startswith(f"| `{_BENCH_B}` | sustained |")
        assert rows[1].startswith(f"| `{_BENCH}` | sustained |")


# ── B5: ordering is a wire format — the body is diffed nightly ──────────────

class TestOrderingIsDeterministic:
    """B5 — several orderings came from SET iteration and nothing pinned them.
    They land in the issue body and in `::warning::` annotations, both of which a
    human diffs against last night's, so an unstable order is noise that hides
    the one line that really changed.

    ⚠️ HONEST SCOPE: the three `sorted()` wrappers in `meta` itself
    (`evaluated_benches` / `inconclusive_benches` / `inconclusive_reasons`) are
    EQUIVALENT MUTANTS — `analyze_trend` iterates `for bench in sorted(benches)`,
    so those lists are already in sorted order when they are wrapped and no test
    can distinguish the wrapper from its absence. The order guarantee actually
    lives in that loop, and that is what the first two tests below kill."""

    _NIGHTS = [
        _multi(i, {"BenchmarkZeta": 35e6, "BenchmarkAlpha": 12e6,
                   "BenchmarkMike": 20e6, "BenchmarkQuebec": 8e6,
                   "BenchmarkBravo": 5e6}, _EPYC)
        for i in range(9)
    ]

    def test_evaluated_benches_is_sorted_not_set_ordered(self):
        _f, meta = _meta_for(self._NIGHTS)
        assert meta["evaluated_benches"] == [
            "BenchmarkAlpha", "BenchmarkBravo", "BenchmarkMike",
            "BenchmarkQuebec", "BenchmarkZeta"]

    def test_inconclusive_benches_and_reasons_are_sorted_not_set_ordered(self):
        # Drop three of the five from the newest night: they become
        # REASON_NO_RECENT, in whatever order the bench set happened to iterate.
        nights = [_multi(0, {"BenchmarkZeta": 35e6, "BenchmarkAlpha": 12e6}, _EPYC)]
        nights += self._NIGHTS[1:]
        _f, meta = _meta_for(nights)
        assert meta["inconclusive_benches"] == [
            "BenchmarkBravo", "BenchmarkMike", "BenchmarkQuebec"]
        assert list(meta["inconclusive_reasons"]) == [
            "BenchmarkBravo", "BenchmarkMike", "BenchmarkQuebec"]

    def test_window_benches_is_sorted(self):
        _f, meta = _meta_for(self._NIGHTS)
        assert meta["window_benches"] == [
            "BenchmarkAlpha", "BenchmarkBravo", "BenchmarkMike",
            "BenchmarkQuebec", "BenchmarkZeta"]

    def test_unverified_benches_is_sorted(self):
        # A genuine set difference — nothing upstream imposes an order here.
        state = [[b, "creep"] for b in
                 ["BenchmarkZeta", "BenchmarkAlpha", "BenchmarkMike",
                  "BenchmarkQuebec", "BenchmarkBravo", "BenchmarkXray"]]
        assert ab._unverified_benches(state, {"evaluated_benches": []}) == [
            "BenchmarkAlpha", "BenchmarkBravo", "BenchmarkMike",
            "BenchmarkQuebec", "BenchmarkXray", "BenchmarkZeta"]

    def test_inconclusive_causes_sorts_the_names_inside_one_clause(self):
        meta = {"inconclusive_reasons": {"BenchmarkZeta": ab.REASON_NO_RECENT,
                                         "BenchmarkAlpha": ab.REASON_NO_RECENT,
                                         "BenchmarkMike": ab.REASON_NO_RECENT},
                "recent_k": 3}
        assert ab._inconclusive_causes(meta) == [
            "did not report on all 3 newest same-class nights (a benchmark that STOPS "
            "reporting is the classic perf-timeout/crash symptom, not a thin window): "
            "`BenchmarkAlpha`, `BenchmarkMike`, `BenchmarkZeta`"]

    def test_unverified_causes_sorts_the_names_inside_one_clause(self):
        meta = {"inconclusive_reasons": {}, "n_nights": 9}
        assert ab._unverified_causes(
            meta, ["BenchmarkZeta", "BenchmarkAlpha", "BenchmarkMike"]) == [
            "did not appear in ANY of the 9 nights in tonight's window, so tonight "
            "neither judged nor skipped them — a RENAMED or deleted benchmark reaches "
            "this state and never leaves it: `BenchmarkAlpha`, `BenchmarkMike`, "
            "`BenchmarkZeta`"]


# ── B6: the canary CV was only ever measured on a constant series ───────────

class TestCanaryCVNumberIsRealNotJustZero:
    """B6 — every canary fixture in the suite used the constant 360_000.0, so
    `canary_cv` was 0.0 in every single test and the rendered `**0.00%**` was
    indistinguishable from "not measured". The canary's whole job is to raise the
    floor; a `_cv` that always returned 0.0 would have passed everything."""

    _CANARIES = [360_000.0, 400_000.0] * 4 + [360_000.0]

    def _nights(self):
        return [_multi(i, {_BENCH: 35e6}, _EPYC, canary_ns=self._CANARIES[i])
                for i in range(9)]

    def test_the_percentage_printed_is_the_measured_cv(self):
        _f, meta = _meta_for(self._nights())
        assert meta["canary_n"] == 9
        assert ab._render_floor_lines(meta)[1] == (
            "- Control-canary night-to-night CV: **5.58%** "
            "(`BenchmarkControlCanaryCPU`; movement below the floor is "
            "indistinguishable from runner noise).")

    def test_the_measured_cv_actually_raises_the_floor(self):
        # 3 × 5.58% = 16.7% for the sustained rule, capped at 10%; the creep cap
        # is 20%, so creep goes to 16.7%. A constant canary leaves them at the
        # 5% / 10% defaults — which is what every other fixture pinned.
        _f, meta = _meta_for(self._nights())
        assert ab._render_floor_lines(meta)[0] == (
            "- Effective floor: **10.0%** (max of fixed minimum and "
            "canary-noise-scaled); creep floor: **16.7%**.")
        _f2, flat = _meta_for(_flat_window(_EPYC))
        assert flat["floor_pct"] == 5.0 and flat["creep_floor_pct"] == 10.0

    def test_the_body_carries_the_non_zero_number(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, self._nights(),
                           issues=[_issue(42, state=[[_BENCH, "sustained"]])])
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "edit", "42"))
        assert "**5.58%**" in body
        assert "**0.00%**" not in body

    def test_a_constant_canary_still_prints_zero(self):
        # The control: 0.00% is correct WHEN it is measured, and stays
        # distinguishable from "not measured" (TestCanaryWithoutSamplesIsNotZeroJitter).
        _f, meta = _meta_for(_flat_window(_EPYC))
        assert meta["canary_n"] == 9 and meta["canary_cv"] == 0.0
        assert "**0.00%**" in ab._render_floor_lines(meta)[1]


# ── B7: the ledger equivalence, proved on the paths instead of on a helper ──

class TestBothPathsWriteTheSameLedgerRule:
    """B7 — the old test asserted `_carry_forward_state([], tracked) == tracked`,
    which is a property of a helper the held-open path does not call. It was
    green before the fix it was written for, and stayed green when the paths
    disagreed. The claim worth pinning is about the two PATHS: one rule, applied
    to the same ledger and the same night, differing only in whether tonight
    produced findings."""

    _PRIOR = [[_BENCH, "sustained"], [_BENCH_B, "sustained"]]

    def _marker_written(self, monkeypatch, tmp_path, capsys, nights):
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, state=self._PRIOR)])
        capsys.readouterr()
        return ab._parse_state_marker(_body_of(_one(calls, "issue", "edit", "42")))

    def test_the_clear_path_keeps_only_the_unmeasured_row(
            self, monkeypatch, tmp_path, capsys):
        # A unmeasurable, B measured and clean → B's row is disproved and drops.
        assert self._marker_written(monkeypatch, tmp_path, capsys,
                                    _a_gone_b_clean()) == [[_BENCH, "sustained"]]

    def test_the_findings_path_keeps_the_unmeasured_row_and_adds_tonights(
            self, monkeypatch, tmp_path, capsys):
        # Same ledger, same unmeasurable A — but B regresses tonight.
        assert self._marker_written(monkeypatch, tmp_path, capsys,
                                    _a_gone_b_regressed()) == [
            [_BENCH_B, "sustained"], [_BENCH, "sustained"]]

    def test_the_findings_path_rewrites_a_measured_rows_kind(
            self, monkeypatch, tmp_path, capsys):
        # B was `sustained` in the ledger and is `creep` tonight: measured, so
        # tonight's kind wins. The carried row is untouched.
        assert self._marker_written(monkeypatch, tmp_path, capsys,
                                    _a_gone_b_creep()) == [
            [_BENCH_B, "creep"], [_BENCH, "sustained"]]

    def test_the_two_paths_agree_on_the_carried_row_itself(
            self, monkeypatch, tmp_path, capsys):
        clear = self._marker_written(monkeypatch, tmp_path, capsys, _a_gone_b_clean())
        findings = self._marker_written(monkeypatch, tmp_path, capsys,
                                        _a_gone_b_regressed())
        carried = [r for r in findings if r[0] == _BENCH]
        assert carried == clear == [[_BENCH, "sustained"]]


# ── R6-F1: an unreadable marker is not an absent one ────────────────────────

# The measured repro: the SAME body, one `]` short. The regex still matches (it
# is greedy up to the LAST `]` before `-->`), so the payload reaches
# `json.loads` and dies there — the one degraded path in this file that used to
# report itself with a bare stderr line and nothing else.
_INTACT_MARKER = ab._render_state_marker([[_BENCH_C, "sustained"]])
_BROKEN_MARKER = _INTACT_MARKER.replace("]] -->", "] -->")


class TestUnreadableMarkerIsNotSilentlyAbsent:
    """R6-F1 — `_parse_state_marker` returns None for two OPPOSITE situations:
    no marker at all (a legacy issue that never recorded a per-bench claim) and
    a marker that is there and did not parse (a claim was recorded and cannot be
    read). Both were silent to Actions, and the second one hands the CLEAR path
    an issue that looks like it is tracking nothing — so it closes it, while the
    body footer promised, unconditionally, that it never closes while a tracked
    benchmark went unmeasured. The body is human-editable: this is inside the
    threat model, not a theoretical corruption.

    Behaviour is deliberately UNCHANGED (still closes; no marker-health state
    machine). What changed is that the run says which of the two happened."""

    def test_the_broken_marker_really_is_the_intact_one_minus_one_bracket(self):
        assert _BROKEN_MARKER.count("]") == _INTACT_MARKER.count("]") - 1
        assert ab._marker_match(_BROKEN_MARKER) is not None      # regex still matches
        assert ab._parse_state_marker(_INTACT_MARKER) == [[_BENCH_C, "sustained"]]

    def test_the_parse_failure_is_annotated_and_summarised(
            self, tmp_path, capsys, monkeypatch):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert ab._parse_state_marker(_BROKEN_MARKER) is None
        err = capsys.readouterr().err
        assert "::warning::" in err
        assert "PRESENT but UNREADABLE" in err
        assert "NOT the same as an issue with no marker" in err
        assert "state marker UNREADABLE" in summary.read_text(encoding="utf-8")
        assert "per-bench close guard is disarmed" in summary.read_text(encoding="utf-8")

    def test_an_absent_marker_stays_silent(self, tmp_path, capsys, monkeypatch):
        # The other half of the separation: a legacy body has nothing to lose,
        # so it must NOT produce the annotation above (that is what would make
        # the annotation meaningless).
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert ab._parse_state_marker("a legacy body with no marker at all") is None
        assert capsys.readouterr().err == ""
        assert not summary.exists()

    def test_the_close_still_happens_and_is_no_longer_silent(
            self, monkeypatch, tmp_path, capsys):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, body=_BROKEN_MARKER)])
        err = capsys.readouterr().err
        assert _sel(calls, "issue", "close", "42")               # behaviour unchanged
        assert "::warning::" in err and "PRESENT but UNREADABLE" in err
        assert "per-bench close guard is disarmed" in summary.read_text(encoding="utf-8")

    def test_one_bracket_decides_whether_the_guard_can_see_the_claim(
            self, monkeypatch, tmp_path, capsys):
        # Same window, same tracked bench, same night — only the marker differs.
        _rc, intact = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                            issues=[_issue(42, body=_INTACT_MARKER)])
        capsys.readouterr()
        _rc, broken = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                            issues=[_issue(42, body=_BROKEN_MARKER)])
        capsys.readouterr()
        assert not _sel(intact, "issue", "close", "42")           # guard holds
        assert _sel(broken, "issue", "close", "42")               # guard is blind

    def test_neither_of_those_two_nights_is_silent(
            self, monkeypatch, tmp_path, capsys):
        # The regression this finding is about: the broken-marker night produced
        # ZERO annotations and no step summary file, while the intact one
        # produced one annotation and a summary. Now both speak.
        for body in (_INTACT_MARKER, _BROKEN_MARKER):
            summary = tmp_path / f"summary-{len(body)}.md"
            monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
            _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                  issues=[_issue(42, body=body)])
            err = capsys.readouterr().err
            assert sum(l.startswith("::warning::") for l in err.splitlines()) == 1
            assert summary.exists() and summary.read_text(encoding="utf-8").strip()

    def test_the_footer_no_longer_promises_it_unconditionally(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC))
        capsys.readouterr()
        body = _body_of(_one(calls, "issue", "create"))
        assert "never while ANY benchmark this issue is tracking went unmeasured" not in body
        assert ("as long as the hidden state marker at the end of this body stays READABLE"
                in body)


# ── R7-F1: the footer promised the one case the code is silent about ────────

# A body shaped like a real one — prose ABOVE, marker on its own last line — so
# "delete the marker" below is a LINE deletion (what a human editing the issue
# actually does), not "pass the empty string".
_WITH_MARKER = ("## Nightly bench trend regression\n\nsome prose\n\n"
                + _marker([_BENCH, "sustained"]))


class TestFooterDoesNotPromiseWhatTheCodeCannotDeliver:
    """R7-F1 — the footer's parenthetical said an "edited-away OR corrupted"
    marker is reported in "its log, its annotations and the job summary". Half of
    that is true. Deleting the marker LINE outright is the other half, and it is
    reported nowhere, by design: `_parse_state_marker` returns None for an ABSENT
    marker in silence because ABSENT is type-indistinguishable from a legacy
    issue's body, which never made a per-bench claim and has nothing to warn
    about. So the promise could only have been kept by nagging every legacy
    ticket every night.

    The fix is the sentence, not the code: the footer now promises the readable
    and the corrupted case only, and DISCLOSES that a deleted marker lifts the
    guard silently. See `test_a_deleted_marker_really_is_silent` for the
    measurement the wording had to be made honest about."""

    def _created_body(self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC))
        capsys.readouterr()
        return _body_of(_one(calls, "issue", "create"))

    def test_the_edited_away_promise_is_gone(self, monkeypatch, tmp_path, capsys):
        body = self._created_body(monkeypatch, tmp_path, capsys)
        assert "edited-away" not in body
        # …and the surviving half is still promised for the case that DOES speak.
        assert ("A CORRUPTED marker leaves no per-bench claim to check, and the run says "
                "so in its log, its annotations and the job summary") in body

    def test_the_silent_case_is_disclosed_instead_of_promised(
            self, monkeypatch, tmp_path, capsys):
        body = self._created_body(monkeypatch, tmp_path, capsys)
        assert ("A marker DELETED outright cannot be told apart from a legacy issue that "
                "never had one, so **that guard lifts silently**") in body

    def test_a_deleted_marker_really_is_silent(
            self, monkeypatch, tmp_path, capsys):
        """The measurement the wording is answerable to: take the SAME body and
        delete the marker line. The issue is closed on a CLEAR night, and not one
        of the three channels the old footer named says the word `marker`."""
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        deleted = "\n".join(l for l in _WITH_MARKER.splitlines()
                            if "perf-trend-state" not in l)
        assert ab._marker_match(deleted) is None            # the line really is gone
        assert ab._parse_state_marker(_WITH_MARKER) == [[_BENCH, "sustained"]]
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, body=deleted)])
        out = capsys.readouterr()
        assert _sel(calls, "issue", "close", "42")          # the guard is gone with it
        assert "marker" not in out.err and "marker" not in out.out
        assert "marker" not in (summary.read_text(encoding="utf-8")
                                if summary.exists() else "")


# ── R7-F2: the annotation was washed away by the same night that raised it ──

def _marker_warnings(err: str) -> list[str]:
    """ONLY the state-marker annotation lines.

    ⚠️ Read this before weakening any assertion below to `"::warning::" in err`
    or to a COUNT of warning lines. Every night this class drives carries other
    annotations (CLEAR-but-INCOMPLETE, UNSTRATIFIED, held-open, gh-write
    failures), so a marker annotation that silently stopped firing still leaves
    the count above zero — the assertion goes green while measuring nothing.
    `test_other_warnings_on_the_same_night_would_have_masked_this` pins that the
    masking is real, not hypothetical."""
    return [l for l in err.splitlines()
            if l.startswith("::warning::") and "PRESENT but UNREADABLE" in l]


def _next_body(calls, num: int, prior_body: str) -> str:
    """The body this issue CARRIES INTO the next night.

    The marker in the issue body is the only memory this tool has across nights,
    so a cross-night test is only testing the shipped code if night N+1 is fed
    what night N actually left behind: the last body written tonight, or the
    untouched prior body when tonight wrote none. (The benchmark playbook makes
    the same point about the close-rate harness that measured a model instead of
    this module.)"""
    edits = [c for c in _sel(calls, "issue", "edit", str(num)) if _BODY_FLAG in c]
    return _body_of(edits[-1]) if edits else prior_body


def _inconclusive_window():
    """Tonight ran on a host class no settled night shares → nothing evaluable."""
    return [_multi(i, {_BENCH: 35e6}, _EPYC if i == 0 else _XEON) for i in range(9)]


class TestUnreadableMarkerIsNotOverwritten:
    """R7-F2 — the annotation R6-F1 added was destroyed by the same run that
    emitted it. Both body-refresh paths rewrite the body with a WELL-FORMED
    marker, so night N said "PRESENT but UNREADABLE" and then replaced the
    corrupt marker with a clean one; night N+1 onwards was green and silent, the
    evidence gone for good and the per-bench close guard still disarmed.

    Measured on the parent commit:

        night N   warnings: 2 (incl. "PRESENT but UNREADABLE")
                  wrote back marker: <!-- perf-trend-state v1 [] -->
        night N+1 (CLEAR)  closed: True  warnings: []  summary: none
        control (N does not overwrite): N+1 still emits 1 warning

    The INCONCLUSIVE path's own comment forbids exactly this — "the marker is
    rewritten from the PRIOR state, NOT from tonight's empty finding set" — and
    its `or []` did it anyway on the unreadable branch.

    The fix is a guard on an existing write, using the ABSENT/UNREADABLE split
    `_parse_state_marker` already draws. Behaviour on the two cases it must not
    touch is unchanged: ABSENT still writes (that write IS the legacy-issue
    migration) and the CLOSE path still closes."""

    # ---- the guard itself, per path ---------------------------------------

    def test_the_findings_night_does_not_overwrite_a_broken_marker(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC),
                           issues=[_issue(42, body=_BROKEN_MARKER)])
        err = capsys.readouterr().err
        assert not _sel(calls, "issue", "edit", "42")
        assert not _sel(calls, "issue", "comment", "42")
        assert _marker_warnings(err)
        assert "NOT refreshing body of perf-trend issue #42" in err

    def test_the_inconclusive_night_does_not_overwrite_a_broken_marker(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _inconclusive_window(),
                           issues=[_issue(42, body=_BROKEN_MARKER)])
        err = capsys.readouterr().err
        assert not _sel(calls, "issue", "edit", "42")
        assert not _sel(calls, "issue", "close", "42")     # INCONCLUSIVE never closes
        assert _marker_warnings(err)
        assert "NOT refreshing body of perf-trend issue #42" in err

    # ---- the two cases the guard must NOT catch ---------------------------

    def test_an_absent_marker_still_gets_its_migration_write_on_findings(
            self, monkeypatch, tmp_path, capsys):
        # A legacy body has no per-bench claim to preserve, and this write is the
        # migration that plants the first marker. Blocking it here would freeze
        # every legacy ticket out of the lifecycle permanently.
        _rc, calls = _live(monkeypatch, tmp_path, _sustained_window(_EPYC),
                           issues=[_issue(42, body="## legacy body, no marker")])
        capsys.readouterr()
        assert ab._parse_state_marker(_body_of(_one(calls, "issue", "edit", "42"))) == \
            [[_BENCH, "sustained"]]

    def test_an_absent_marker_still_gets_its_migration_write_on_inconclusive(
            self, monkeypatch, tmp_path, capsys):
        _rc, calls = _live(monkeypatch, tmp_path, _inconclusive_window(),
                           issues=[_issue(42, body="## legacy body, no marker")])
        capsys.readouterr()
        assert ab._parse_state_marker(_body_of(_one(calls, "issue", "edit", "42"))) == []

    def test_a_readable_marker_is_still_refreshed_on_both_paths(
            self, monkeypatch, tmp_path, capsys):
        _rc, findings = _live(monkeypatch, tmp_path, _sustained_window(_EPYC),
                              issues=[_issue(42, body=_INTACT_MARKER)])
        _rc, inconc = _live(monkeypatch, tmp_path, _inconclusive_window(),
                            issues=[_issue(42, body=_INTACT_MARKER)])
        capsys.readouterr()
        assert _sel(findings, "issue", "edit", "42")
        assert _sel(inconc, "issue", "edit", "42")

    def test_the_close_path_is_untouched(self, monkeypatch, tmp_path, capsys):
        # Deliberately NOT in this fix's scope: a CLEAR night still closes an
        # issue whose marker it cannot read (R6-F1 kept that on purpose — the
        # marker-health state machine was designed, implemented and reverted).
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, body=_BROKEN_MARKER)])
        capsys.readouterr()
        assert _sel(calls, "issue", "close", "42")

    # ---- the point of all of it: it still speaks the NEXT night ------------

    @pytest.mark.parametrize("night_n", ["findings", "inconclusive"],
                             ids=["night-N-findings", "night-N-inconclusive"])
    def test_the_corruption_survives_the_night_and_is_reported_again(
            self, monkeypatch, tmp_path, capsys, night_n):
        """The cross-night assertion. Night N+1 is fed the body night N really
        left behind (`_next_body`), which is the whole mechanism at issue."""
        summary_n = tmp_path / "n.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_n))
        nights = (_sustained_window(_EPYC) if night_n == "findings"
                  else _inconclusive_window())
        _rc, calls = _live(monkeypatch, tmp_path, nights,
                           issues=[_issue(42, body=_BROKEN_MARKER)])
        assert _marker_warnings(capsys.readouterr().err)          # night N speaks
        carried = _next_body(calls, 42, _BROKEN_MARKER)

        # ⚠️ The next-night assertion comes FIRST on purpose. Checking
        # `carried == _BROKEN_MARKER` up here would be the assertion that fires
        # under a reverted fix, and it only says "the body did not change" — the
        # CLAIM is that the corruption is still reported a night later, so that
        # is what has to be measured, on the night after, from a fresh run.
        summary_n1 = tmp_path / "n1.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_n1))
        _rc, _calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                            issues=[_issue(42, body=carried)])
        err = capsys.readouterr().err
        assert len(_marker_warnings(err)) == 1                    # night N+1 too
        assert "state marker UNREADABLE" in summary_n1.read_text(encoding="utf-8")
        # …and only now, the mechanism behind it.
        assert carried == _BROKEN_MARKER
        assert ab._parse_state_marker(carried) is None

    def test_the_control_night_shows_what_overwriting_would_have_cost(
            self, monkeypatch, tmp_path, capsys):
        """The counterfactual, in-suite: feed night N+1 the body the OLD code
        would have written (a clean, empty marker) instead of the one it now
        leaves alone. Same night, same window — and it is silent."""
        washed = ab._render_state_marker([])     # what the old code wrote back
        summary = tmp_path / "s.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _rc, calls = _live(monkeypatch, tmp_path, _flat_window(_EPYC),
                           issues=[_issue(42, body=washed)])
        err = capsys.readouterr().err
        assert ab._parse_state_marker(washed) == []      # readable ⇒ nothing to report
        assert _marker_warnings(err) == []
        assert _sel(calls, "issue", "close", "42")       # …and it closes, silently

    def test_other_warnings_on_the_same_night_would_have_masked_this(
            self, monkeypatch, tmp_path, capsys):
        """Why `_marker_warnings` filters instead of counting. This night carries
        the CLEAR-but-INCOMPLETE annotation as well, so `"::warning::" in err`
        and `count > 0` both stay green with the marker annotation deleted."""
        _rc, _calls = _live(monkeypatch, tmp_path, _new_bench_only_in_the_newest_three(),
                            issues=[_issue(42, body=_BROKEN_MARKER)])
        err = capsys.readouterr().err
        allw = [l for l in err.splitlines() if l.startswith("::warning::")]
        assert len(allw) > len(_marker_warnings(err)) >= 1
        assert any("CLEAR but INCOMPLETE" in l for l in allw)


# ── R6-F2: a CLEAR night that judged only part of the window ────────────────

def _new_bench_only_in_the_newest_three(cpu=_EPYC):
    """14 nights, ALL on tonight's host class. `_BENCH` is flat throughout;
    `_BENCH_B` appears only in the newest 3 nights and carries a real +30%.

    So tonight's stratum is not thin at all (11 settled same-class nights) —
    `_BENCH_B` simply has no history of its own to anchor against, which is both
    the F2 fixture (a partial CLEAR) and the F3 fixture (the cause of it)."""
    return ([_multi(i, {_BENCH: 35e6, _BENCH_B: 45.5e6}, cpu) for i in range(3)]
            + [_multi(i, {_BENCH: 35e6}, cpu) for i in range(3, 14)])


class TestPartialClearIsNotAnUnconditionalAllClear:
    """R6-F2 — CLEAR is a verdict about the benches tonight COULD judge. With no
    open issue, a night that judged one bench and skipped another (carrying a
    real +30%) printed one unconditional green tick, zero annotations, and never
    created the step summary file at all. That is exactly follow-up C's failure
    mode — a detector that did not run looking like one that found nothing —
    surviving one branch further down."""

    def _run(self, tmp_path, capsys, monkeypatch, nights):
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        assert ab.run_trend_watch(_trend_args(_write_fixture(tmp_path, nights))) == 0
        return capsys.readouterr(), summary

    def test_the_fixture_is_a_partial_clear(self):
        _f, meta = _meta_for(_new_bench_only_in_the_newest_three())
        assert meta["status"] == _CLEAR
        assert meta["evaluated_benches"] == [_BENCH]
        assert meta["inconclusive_benches"] == [_BENCH_B]

    def test_the_unjudged_bench_is_annotated_and_summarised(
            self, tmp_path, capsys, monkeypatch):
        out, summary = self._run(tmp_path, capsys, monkeypatch,
                                 _new_bench_only_in_the_newest_three())
        assert summary.exists()                    # the file was never created before
        text = summary.read_text(encoding="utf-8")
        assert "CLEAR but INCOMPLETE" in text and f"`{_BENCH_B}`" in text
        assert "::warning::" in out.err and "CLEAR but INCOMPLETE" in out.err
        assert "is not evidence about the others" in out.err

    def test_the_green_tick_is_no_longer_unconditional(
            self, tmp_path, capsys, monkeypatch):
        out, _summary = self._run(tmp_path, capsys, monkeypatch,
                                  _new_bench_only_in_the_newest_three())
        assert "✅ No sustained nightly bench regression." not in out.out.splitlines()
        assert "NOT judged" in out.out and f"`{_BENCH_B}`" in out.out

    def test_a_fully_judged_clear_night_is_unchanged(
            self, tmp_path, capsys, monkeypatch):
        # The control: when nothing was skipped the old sentence is the honest
        # one and must survive verbatim, with no annotation and no summary.
        out, summary = self._run(tmp_path, capsys, monkeypatch, _flat_window(_EPYC))
        assert "✅ No sustained nightly bench regression." in out.out.splitlines()
        assert "::warning::" not in out.err
        assert not summary.exists()


# ── R6-F3: `thin-anchor` was two causes wearing one name ────────────────────

class TestThinAnchorSeparatesTheWindowFromTheBench:
    """R6-F3 — third recurrence of one self-contradiction. On a window where all
    14 nights ARE tonight's host class, a bench with 3 nights of its own history
    was told "`X` reported tonight but could not be judged — only 14 of 14 window
    nights ran on tonight's host class": the first half says it ran here
    tonight, the second blames a host-class composition that is 14/14 tonight's
    class, and the actual cause (the bench's own history) is not mentioned."""

    def test_the_reason_code_names_the_bench_not_the_window(self):
        _f, meta = _meta_for(_new_bench_only_in_the_newest_three())
        assert meta["inconclusive_reasons"] == {_BENCH_B: ab.REASON_THIN_BENCH}
        assert meta["n_class_nights"] == 14 and meta["n_nights"] == 14
        assert meta["n_settled_class"] == 11          # the window is NOT thin

    def test_the_clause_drops_the_contradiction_and_names_the_cause(self):
        _f, meta = _meta_for(_new_bench_only_in_the_newest_three())
        causes = " | ".join(ab._inconclusive_causes(meta))
        assert "window nights ran on tonight's host class" not in causes
        assert "its OWN history on this host class is what is short" in causes
        assert "not the window's host-class composition" in causes
        assert f"`{_BENCH_B}`" in causes

    def test_a_genuinely_thin_window_still_blames_the_window(self):
        # Unchanged behaviour for the case the name was coined for: 4 of 6
        # nights are tonight's class and only 1 of them is settled.
        nights = ([_hnight(i, 35e6, _XEON) for i in range(3)]
                  + [_hnight(3, 35e6, _EPYC), _hnight(4, 35e6, _XEON),
                     _hnight(5, 35e6, _EPYC)])
        _f, meta = _meta_for(nights)
        assert meta["inconclusive_reasons"] == {_BENCH: ab.REASON_THIN_ANCHOR}
        causes = " | ".join(ab._inconclusive_causes(meta))
        assert "only 4 of 6 window nights ran on tonight's host class" in causes
        assert "leaving 1 settled same-class night(s) behind the 3 most recent" in causes
        assert "OWN history" not in causes

    def test_the_per_issue_helper_reports_the_new_cause_too(self):
        # `_unverified_causes` answers a different question and keeps its own
        # ordering tuple; a cause code missing from THAT tuple drops the clause
        # silently, leaving the held-open warning with an empty reason. Pinned
        # here because the same night's CLEAR-but-incomplete annotation quotes
        # `_inconclusive_causes`, which would mask the loss end-to-end.
        _f, meta = _meta_for(_new_bench_only_in_the_newest_three())
        causes = ab._unverified_causes(meta, [_BENCH_B])
        assert len(causes) == 1
        assert "its OWN history on this host class is what is short" in causes[0]
        assert f"`{_BENCH_B}`" in causes[0]

    def test_the_held_open_warning_no_longer_argues_against_itself(
            self, monkeypatch, tmp_path, capsys):
        # End-to-end, the sentence as an operator receives it — scoped to the
        # held-open lines, so a clause arriving from some OTHER annotation on
        # the same night cannot stand in for this one.
        summary = tmp_path / "summary.md"
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        _live(monkeypatch, tmp_path, _new_bench_only_in_the_newest_three(),
              issues=[_issue(42, state=[[_BENCH_B, "sustained"]])])
        text = capsys.readouterr().err + summary.read_text(encoding="utf-8")
        held = " ".join(l for l in text.splitlines()
                        if "is tracking" in l or "held open" in l)
        assert held
        assert f"`{_BENCH_B}` reported tonight but could not be judged" in held
        assert "window nights ran on tonight's host class" not in held
        assert "its OWN history on this host class is what is short" in held

    def test_the_two_thin_causes_partition_rather_than_overlap(self):
        # When the WINDOW is short every bench's anchor is short too, so the
        # window is what gets named and `thin-bench-history` cannot occur beside
        # it. The pair partitions the old `thin-anchor`; it does not double-count
        # a night, which is what would put both sentences in one warning.
        thin_window = ([_multi(i, {_BENCH: 35e6, _BENCH_B: 12e6}, _XEON) for i in range(3)]
                       + [_multi(3, {_BENCH: 35e6}, _XEON),
                          _multi(4, {_BENCH: 35e6, _BENCH_B: 12e6}, _EPYC)])
        _f, meta = _meta_for(thin_window)
        assert meta["n_settled_class"] == 1
        assert set(meta["inconclusive_reasons"]) == {_BENCH, _BENCH_B}
        assert set(meta["inconclusive_reasons"].values()) == {ab.REASON_THIN_ANCHOR}
        # …and the other direction is the fixture above: a window that is NOT
        # thin never produces `thin-anchor`.
        _f2, meta2 = _meta_for(_new_bench_only_in_the_newest_three())
        assert set(meta2["inconclusive_reasons"].values()) == {ab.REASON_THIN_BENCH}
