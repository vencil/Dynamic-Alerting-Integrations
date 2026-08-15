"""Tests for pair_bench_ratio.py — ADR-032 nightly paired-measurement arithmetic.

The tool is small, but two of its behaviours are load-bearing for ADR-032 and
are what these tests actually pin down:

  1. A benchmark with no denominator must land in `inconclusive`, NEVER be
     absent-and-therefore-implicitly-fine. A new benchmark on main does not
     exist in the pinned reference version, and reading that as "clean" is the
     exact silent-regression failure the ADR exists to remove.
  2. Inputs that are not a valid pair must be REFUSED, not averaged. Both sides
     are supposed to come from one job on one machine; if the CPU headers
     disagree that assumption is broken and the ratio is meaningless.

OUT OF SCOPE: the workflow wiring (bench-record.yaml) — that is CI-only.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import pair_bench_ratio as pbr

CPU = "AMD EPYC 7763 64-Core Processor"


def side(rows: list[tuple[str, float]], cpu: str = CPU, rounds: int = 1) -> str:
    """Render a bench side the way bench_interleave.sh does — one header block
    per interleave round, because it appends each invocation's stdout."""
    out = []
    for _ in range(rounds):
        out += ["goos: linux", "goarch: amd64", "pkg: example/app", f"cpu: {cpu}"]
        out += [f"{name}-4  \t       5\t {ns:.0f} ns/op" for name, ns in rows]
        out += ["PASS"]
    return "\n".join(out) + "\n"


# --- read_side -------------------------------------------------------------

def test_read_side_collects_every_sample_and_the_first_cpu(tmp_path: Path):
    p = tmp_path / "s.txt"
    p.write_text(side([("BenchmarkA", 100.0)], rounds=3), encoding="utf-8")
    samples, cpu = pbr.read_side(p)
    assert cpu == CPU
    # Repeated headers are harmless; what matters is one sample per round.
    assert samples == {"BenchmarkA": [100.0, 100.0, 100.0]}


def test_read_side_ignores_non_benchmark_noise(tmp_path: Path):
    p = tmp_path / "s.txt"
    p.write_text(side([("BenchmarkA", 100.0)]) + "ok  \texample/app\t1.2s\n",
                 encoding="utf-8")
    samples, _ = pbr.read_side(p)
    assert list(samples) == ["BenchmarkA"]


# --- pair ------------------------------------------------------------------

def test_ratio_is_median_of_main_over_median_of_reference():
    ref = {"BenchmarkA": [100.0, 200.0, 300.0]}     # median 200
    main = {"BenchmarkA": [210.0, 220.0, 230.0]}    # median 220
    evaluated, inconclusive = pbr.pair(ref, main)
    assert inconclusive == {}
    assert evaluated["BenchmarkA"]["ratio"] == pytest.approx(1.1)
    assert evaluated["BenchmarkA"]["n_reference"] == 3
    assert evaluated["BenchmarkA"]["n_main"] == 3


def test_identical_sides_give_exactly_one():
    """The control canary runs the SAME binary into both sides, so its ratio is
    a live A/A. If this ever drifts from 1.0 the arithmetic is wrong, not the
    machine."""
    evaluated, _ = pbr.pair({"BenchmarkCanary": [11600.0]},
                            {"BenchmarkCanary": [11600.0]})
    assert evaluated["BenchmarkCanary"]["ratio"] == 1.0


def test_bench_absent_from_reference_is_inconclusive_not_clean():
    """A benchmark added to main after the reference tag was cut. It has no
    denominator, and it must be visibly unjudged rather than quietly omitted."""
    evaluated, inconclusive = pbr.pair({}, {"BenchmarkNew": [42.0]})
    assert evaluated == {}
    assert inconclusive == {"BenchmarkNew": "missing-in-reference"}


def test_bench_absent_from_main_is_inconclusive():
    """A benchmark that stopped reporting — the perf-timeout symptom. Also not
    a pass."""
    _, inconclusive = pbr.pair({"BenchmarkGone": [42.0]}, {})
    assert inconclusive == {"BenchmarkGone": "missing-in-main"}


def test_non_positive_reference_median_is_refused_not_divided():
    _, inconclusive = pbr.pair({"BenchmarkZero": [0.0]},
                               {"BenchmarkZero": [10.0]})
    assert inconclusive == {"BenchmarkZero": "reference-median-not-positive"}


# --- main() ----------------------------------------------------------------

def run(tmp_path: Path, ref_text: str, main_text: str) -> subprocess.CompletedProcess:
    (tmp_path / "ref.txt").write_text(ref_text, encoding="utf-8")
    (tmp_path / "main.txt").write_text(main_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(Path(pbr.__file__).resolve()),
         "--reference", str(tmp_path / "ref.txt"),
         "--main", str(tmp_path / "main.txt"),
         "--reference-tag", "exporter/v2.9.0",
         "--out", str(tmp_path / "out.json")],
        capture_output=True, text=True, timeout=60)


def test_happy_path_writes_the_expected_payload(tmp_path: Path):
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0), ("BenchmarkNew", 7.0)]))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "bench-paired/v1"
    assert payload["reference_tag"] == "exporter/v2.9.0"
    assert payload["cpu"] == CPU
    assert payload["evaluated"]["BenchmarkA"]["ratio"] == pytest.approx(1.05)
    assert payload["inconclusive"] == {"BenchmarkNew": "missing-in-reference"}


def test_mismatched_cpu_headers_are_refused(tmp_path: Path):
    """Both sides run in one job on one runner. Different CPUs means these are
    not a pair at all, and a ratio computed across them is the cross-machine
    comparison ADR-032 removes."""
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0)], cpu="AMD EPYC 9V74 80-Core Processor"))
    assert r.returncode == 2
    assert "different CPUs" in r.stderr


def test_side_with_no_benchmark_rows_is_refused(tmp_path: Path):
    r = run(tmp_path, "", side([("BenchmarkA", 100.0)]))
    assert r.returncode == 2
    assert "no benchmark rows" in r.stderr


def test_missing_input_file_is_a_caller_error(tmp_path: Path):
    (tmp_path / "main.txt").write_text(side([("BenchmarkA", 1.0)]), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(Path(pbr.__file__).resolve()),
         "--reference", str(tmp_path / "nope.txt"),
         "--main", str(tmp_path / "main.txt"),
         "--reference-tag", "exporter/v2.9.0",
         "--out", str(tmp_path / "out.json")],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "not a file" in r.stderr
