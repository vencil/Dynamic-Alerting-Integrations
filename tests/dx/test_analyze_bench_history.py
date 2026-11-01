"""Tests for analyze_bench_history.py — bench-record nightly aggregator.

Closes the audit gap (P1-5 / 444 LOC tool was 0% covered). Targets the spine:
  - RunSample dataclass
  - BenchStats: per_run_medians / median / cv / max_min_ratio /
    within_run_cv_mean / verdict
  - parse_bench_file (regex + line iteration)
  - aggregate (RunSample → BenchStats by name)
  - format_ns (ns / µs / ms / s humanizer + NaN handling)
  - render_markdown_table (table shape + threshold line)

Also pinned, with `_gh` faked (no network): `list_recent_runs`' `status`
default and `--json` field list, `download_artifact`'s cache sentinel, and
argparse bounds.

The `--trend-watch` watchdog and its tests were removed together (ADR-032 §6
withdrawn; ADR-039 §Not yet decided names what replaces it).

OUT OF SCOPE here (require gh CLI auth / live network):
  - the real `gh` subprocess behind `_gh`
  - main() end-to-end beyond the listing step
"""
from __future__ import annotations

import argparse
import math
import sys
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


class TestWindowPredicate:
    def test_the_default_predicate_stays_conservative(self):
        """⛔ THE DEFAULT MUST STAY `success`. `main()`'s aggregation path has
        no completeness check: it loops `download_artifact` →
        `parse_bench_file` straight into `all_samples`, so a permissive default
        would put truncated artifacts one function away from the stats.

        A specific break that reddens this: set the default to "completed".
        """
        import inspect
        default = inspect.signature(ab.list_recent_runs).parameters["status"].default
        assert default == "success", (
            "a caller that says nothing now inherits the PERMISSIVE predicate; "
            "the widening must be opt-in at call sites that check completeness"
        )

    def test_the_aggregation_path_does_not_inherit_the_widening(self, monkeypatch):
        """⛔ THE OTHER HALF, and the one the first cut got wrong. `main()`'s
        aggregate mode must still ask `success`, because nothing between its
        `download_artifact` and its statistics judges whether the artifact is
        whole.

        Driven through the REAL `main()` rather than by reading the call site:
        a source-level check passes on a decoy and on a line that no longer
        executes. `_gh` returns an empty list, so main bails right after the
        listing — which is exactly the point where the predicate is observable.

        A specific break that reddens this: pass `status="completed"` at the
        aggregation call site, or flip the module default back.
        """
        seen = {}

        def fake_gh(cmd):
            seen["cmd"] = list(cmd)
            return "[]"

        monkeypatch.setattr(ab, "_gh", fake_gh)
        # main() probes for the real binary before it ever calls `_gh`; this
        # container has no `gh`, and the probe is not what is under test here.
        monkeypatch.setattr(ab.shutil, "which", lambda _name: "/usr/bin/gh")
        monkeypatch.setattr(sys, "argv", ["analyze_bench_history.py", "--limit", "28"])
        ab.main()

        cmd = seen["cmd"]
        assert cmd[cmd.index("--status") + 1] == "success", (
            "the aggregation path widened to non-success runs, but it has no "
            "completeness check — a truncated artifact would reach the stats"
        )

    def test_the_run_conclusion_is_still_requested_from_gh(self, monkeypatch):
        """`paired_trend_watch.py` imports this function and records each
        night's `run_conclusion` from the `conclusion` field; if the `--json`
        field list ever drops it, that column silently becomes None.

        Asserted on BEHAVIOUR: spy the `gh` argv the function actually builds
        and read the `--json` value out of it. A check that greps the
        function's source would be satisfied by its own docstring.
        """
        seen = {}

        def fake_gh(cmd):
            seen["cmd"] = list(cmd)
            return "[]"

        monkeypatch.setattr(ab, "_gh", fake_gh)
        ab.list_recent_runs("bench-record.yaml", 14)

        cmd = seen["cmd"]
        json_fields = cmd[cmd.index("--json") + 1].split(",")
        assert "conclusion" in json_fields, (
            f"`gh run list --json` asks for {json_fields}; without `conclusion` "
            "the paired verdict records every night's run conclusion as None"
        )


class TestArgparseLowerBounds:
    """A window size below 1 is rejected by argparse as a usage error, not
    discovered later as an empty-slice traceback."""

    def _parse(self, monkeypatch, *argv):
        monkeypatch.setattr("sys.argv", ["analyze_bench_history.py", *argv])
        with pytest.raises(SystemExit) as exc:
            ab.main()
        return exc.value.code

    def test_zero_and_negative_limits_are_usage_errors(self, monkeypatch, capsys):
        assert self._parse(monkeypatch, "--limit", "0") == 2
        assert "must be >= 1" in capsys.readouterr().err
        assert self._parse(monkeypatch, "--limit", "-1") == 2
        capsys.readouterr()

    def test_the_trend_watch_flags_are_gone(self, monkeypatch, capsys):
        """The retired watchdog's entry point must not linger as a flag that
        parses and then does something else."""
        assert self._parse(monkeypatch, "--trend-watch") == 2
        assert "unrecognized arguments" in capsys.readouterr().err

    def test_one_is_still_accepted(self):
        assert ab._at_least(1)("1") == 1
        with pytest.raises(argparse.ArgumentTypeError):
            ab._at_least(1)("0")


class TestDownloadCache:
    """`download_artifact`'s cache hit requires the completion sentinel, not
    just the `.txt`: a run dir holding `bench-baseline.txt` but no sentinel is
    an INTERRUPTED download, and serving it would feed a truncated file to the
    stats (and to `paired_trend_watch.py`, which imports this function)."""

    @staticmethod
    def _seed(tmp_path, run_id, *, complete):
        d = tmp_path / f"run-{run_id}"
        d.mkdir(parents=True, exist_ok=True)
        (d / ab.ARTIFACT_FILE).write_text(
            "BenchmarkThing0-4   \t   10\t  100 ns/op\n", encoding="utf-8", newline="\n")
        if complete:
            (d / ab.DOWNLOAD_SENTINEL).write_text("", encoding="utf-8", newline="\n")
        return d

    def test_a_partial_cache_is_refetched(self, monkeypatch, tmp_path):
        """A specific break that reddens this: drop the `DOWNLOAD_SENTINEL`
        term from the cache condition."""
        self._seed(tmp_path, 909, complete=False)
        calls = []
        monkeypatch.setattr(ab, "_gh", lambda cmd: calls.append(cmd) or "")
        assert ab.download_artifact(909, tmp_path) is not None
        assert len(calls) == 1, "the partial cache was served instead of refetched"

    def test_a_complete_cache_is_not_refetched(self, monkeypatch, tmp_path):
        """The counterfactual: the sentinel must still SAVE a download, or the
        test above would pass on 'always re-download'."""
        self._seed(tmp_path, 910, complete=True)
        calls = []
        monkeypatch.setattr(ab, "_gh", lambda cmd: calls.append(cmd) or "")
        assert ab.download_artifact(910, tmp_path) is not None
        assert calls == [], "a complete cache was re-downloaded"

    def test_a_fresh_download_writes_the_sentinel_so_the_next_call_is_cached(
            self, monkeypatch, tmp_path):
        """The cache tests above pre-seed the sentinel, so neither notices if
        `download_artifact` stops WRITING one — the tool would then
        re-download every run on every call, and nothing would go red.

        A specific break that reddens this: delete the
        `(target / DOWNLOAD_SENTINEL).write_text(...)` line.
        """
        run_id = 911
        calls = []

        def fake_gh(cmd):
            calls.append(cmd)
            d = tmp_path / f"run-{run_id}"
            d.mkdir(parents=True, exist_ok=True)
            (d / ab.ARTIFACT_FILE).write_text(
                "cpu: TestCPU\nBenchmarkThing0-4   \t   10\t  100 ns/op\n",
                encoding="utf-8", newline="\n")
            return ""

        monkeypatch.setattr(ab, "_gh", fake_gh)
        first = ab.download_artifact(run_id, tmp_path)
        assert first is not None and len(calls) == 1
        assert (tmp_path / f"run-{run_id}" / ab.DOWNLOAD_SENTINEL).exists(), (
            "a complete download left no sentinel, so every later call "
            "re-downloads"
        )
        ab.download_artifact(run_id, tmp_path)
        assert len(calls) == 1, "the second call re-downloaded despite a complete fetch"

    def test_a_failed_sentinel_write_degrades_instead_of_crashing(
            self, monkeypatch, tmp_path, capsys):
        """A full disk while writing the cache sentinel must not raise out of
        `download_artifact`: degrading to "not cached" costs one re-download,
        crashing costs the run.

        A specific break that reddens this: remove the `except OSError` around
        the sentinel write in `download_artifact`.
        """
        run_id = 912
        real_write = Path.write_text

        def flaky_write(self, *a, **kw):
            if self.name == ab.DOWNLOAD_SENTINEL:
                raise OSError(28, "No space left on device")
            return real_write(self, *a, **kw)

        def fake_gh(cmd):
            d = tmp_path / f"run-{run_id}"
            d.mkdir(parents=True, exist_ok=True)
            real_write(d / ab.ARTIFACT_FILE,
                       "cpu: TestCPU\nBenchmarkThing0-4   \t   10\t  100 ns/op\n",
                       encoding="utf-8", newline="\n")
            return ""

        monkeypatch.setattr(ab, "_gh", fake_gh)
        monkeypatch.setattr(Path, "write_text", flaky_write)

        got = ab.download_artifact(run_id, tmp_path)

        assert got is not None, "a cache-file failure must not lose the artifact"
        assert not (tmp_path / f"run-{run_id}" / ab.DOWNLOAD_SENTINEL).exists()
        assert "could not write" in capsys.readouterr().err


class TestRetiredWatchdogStaysRetired:
    """The nightly no longer runs the absolute-ns/op watchdog or ships the
    completeness marker only it read (ADR-032 §待決 6 withdrawn). A job or an
    upload path that comes back would be a reader-less leftover, or a watchdog
    reintroduced without the decision being revisited."""

    @staticmethod
    def _workflow():
        yaml = pytest.importorskip("yaml")
        path = (Path(__file__).resolve().parents[2]
                / ".github" / "workflows" / "bench-record.yaml")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_no_trend_watch_job(self):
        assert "trend-watch" not in self._workflow()["jobs"]

    def test_the_marker_is_neither_written_nor_uploaded(self):
        steps = self._workflow()["jobs"]["bench-record"]["steps"]
        text = "\n".join(str(s.get("run", "")) + str(s.get("with", "")) for s in steps)
        assert "bench-baseline.rows" not in text
        assert "write_baseline_marker" not in text
