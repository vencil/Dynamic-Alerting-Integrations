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


# --- read_workload_drift() --------------------------------------------------
#
# Called directly, not through the CLI. The CLI tests below cover the same
# states end-to-end, but they run in a subprocess, so a branch that only they
# reach is invisible to coverage AND its failure message is a JSON diff rather
# than a name. Both properties matter for a function whose entire job is to keep
# three states apart.

def test_read_drift_no_path_is_not_requested():
    assert pbr.read_workload_drift(None) == {"status": "not-requested", "files": []}


def test_read_drift_missing_file_is_unreadable(tmp_path: Path):
    assert pbr.read_workload_drift(tmp_path / "nope.txt") == {
        "status": "unreadable", "files": []}


def test_read_drift_directory_is_unreadable_not_a_crash(tmp_path: Path):
    """A directory raises IsADirectoryError — an OSError, but worth pinning:
    the caller passes a path it built, and a layout change could aim it at one."""
    (tmp_path / "adir").mkdir()
    assert pbr.read_workload_drift(tmp_path / "adir") == {
        "status": "unreadable", "files": []}


def test_read_drift_undecodable_bytes_are_unreadable(tmp_path: Path):
    """⛔ UnicodeDecodeError is a ValueError, not an OSError."""
    p = tmp_path / "drift.bin"
    p.write_bytes(b"a_bench_test.go\n\xff\xfe\n")
    assert pbr.read_workload_drift(p) == {"status": "unreadable", "files": []}


def test_read_drift_empty_file_is_checked_with_nothing(tmp_path: Path):
    p = tmp_path / "drift.txt"
    p.write_text("", encoding="utf-8")
    assert pbr.read_workload_drift(p) == {"status": "checked", "files": []}


def test_read_drift_normalizes_blank_lines_whitespace_and_duplicates(tmp_path: Path):
    p = tmp_path / "drift.txt"
    p.write_text("b.go\n\n  a.go  \nb.go\n", encoding="utf-8")
    assert pbr.read_workload_drift(p) == {
        "status": "checked", "files": ["a.go", "b.go"]}


# --- main(), called in-process ----------------------------------------------
#
# The subprocess tests below are the real end-to-end check (they exercise the
# actual entry point, including the stdout hardening the carrier import does).
# But a subprocess is opaque to coverage, so every branch in `main()` would read
# as untested. This one in-process call walks the whole success path with a
# drift list attached, so the payload assembly is measured where it runs.

def test_main_in_process_walks_the_full_success_path(tmp_path, monkeypatch, capsys):
    (tmp_path / "ref.txt").write_text(side([("BenchmarkA", 100.0)]), encoding="utf-8")
    (tmp_path / "main.txt").write_text(side([("BenchmarkA", 105.0)]), encoding="utf-8")
    (tmp_path / "drift.txt").write_text("config_bench_test.go\n", encoding="utf-8")
    out = tmp_path / "nested" / "out.json"   # parent does not exist yet
    monkeypatch.setattr(sys, "argv", [
        "pair_bench_ratio.py",
        "--reference", str(tmp_path / "ref.txt"),
        "--main", str(tmp_path / "main.txt"),
        "--reference-tag", "exporter/v2.9.0",
        "--reference-sha", "3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0",
        "--workload-drift", str(tmp_path / "drift.txt"),
        "--out", str(out),
    ])
    assert pbr.main() == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "OK"
    assert payload["workload_drift"]["files"] == ["config_bench_test.go"]
    assert payload["evaluated"]["BenchmarkA"]["ratio"] == pytest.approx(1.05)
    assert "workload drift" in capsys.readouterr().out


# --- main(), through the real CLI -------------------------------------------

def run(tmp_path: Path, ref_text: str, main_text: str,
        *extra: str) -> subprocess.CompletedProcess:
    (tmp_path / "ref.txt").write_text(ref_text, encoding="utf-8")
    (tmp_path / "main.txt").write_text(main_text, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(Path(pbr.__file__).resolve()),
         "--reference", str(tmp_path / "ref.txt"),
         "--main", str(tmp_path / "main.txt"),
         "--reference-tag", "exporter/v2.9.0",
         "--out", str(tmp_path / "out.json"), *extra],
        capture_output=True, text=True, timeout=60)


def test_happy_path_writes_the_expected_payload(tmp_path: Path):
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0), ("BenchmarkNew", 7.0)]))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["schema"] == "bench-paired/v2"
    assert payload["reference_tag"] == "exporter/v2.9.0"
    assert payload["cpu"] == CPU
    assert payload["evaluated"]["BenchmarkA"]["ratio"] == pytest.approx(1.05)
    assert payload["inconclusive"] == {"BenchmarkNew": "missing-in-reference"}


def test_success_payload_carries_an_explicit_status(tmp_path: Path):
    """The unusable-measurement path writes `"status": "INCONCLUSIVE"`. If the
    success path omitted the key, every consumer would have to encode "absent
    means OK" — a default that is silently wrong the day a third status
    exists."""
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["status"] == "OK"


def test_reference_sha_is_recorded_verbatim(tmp_path: Path):
    """A tag can be re-pointed; the SHA is what was actually built. A night
    series that recorded only the tag cannot be re-read after a re-point."""
    sha = "3fd96b51f52e61566bb12c4c3fa23fed7e34dfa0"
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]),
            "--reference-sha", sha)
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["reference_sha"] == sha


def test_reference_sha_absent_is_null_not_missing(tmp_path: Path):
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]))
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["reference_sha"] is None


# --- workload drift --------------------------------------------------------
#
# Three states, for the same reason the verdict has three: "nobody checked" read
# as "nothing drifted" is the conflation that makes a changed fixture look like
# a code change (or the reverse) with nothing in the record to say so.

def test_drift_not_requested_is_distinguishable_from_no_drift(tmp_path: Path):
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]))
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["workload_drift"] == {"status": "not-requested", "files": []}


def test_drift_checked_and_empty_is_a_positive_statement(tmp_path: Path):
    """An empty file means "I compared both sides and nothing had drifted".

    That reading is only sound because the caller does not write the file at all
    when its own comparison could not run — see the enumeration guard in
    bench-record.yaml. Without that guard an empty file would also be what a
    failed comparison leaves behind, and the two are opposite facts.
    """
    (tmp_path / "drift.txt").write_text("", encoding="utf-8")
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]),
            "--workload-drift", str(tmp_path / "drift.txt"))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["workload_drift"] == {"status": "checked", "files": []}


def test_drift_files_are_recorded_deduped_and_sorted(tmp_path: Path):
    (tmp_path / "drift.txt").write_text(
        "config_bench_test.go\n\n  pkg/config/simulate_bench_test.go  \n"
        "config_bench_test.go\n", encoding="utf-8")
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 105.0)]),
            "--workload-drift", str(tmp_path / "drift.txt"))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["workload_drift"] == {
        "status": "checked",
        "files": ["config_bench_test.go", "pkg/config/simulate_bench_test.go"],
    }
    assert "workload drift" in r.stdout


def test_unreadable_drift_file_is_recorded_not_fatal(tmp_path: Path):
    """This is a disclosure aid. Failing the night's ratios because an
    annotation input went missing would trade a working measurement for a
    warning — and it must not be recorded as `checked` either."""
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]),
            "--workload-drift", str(tmp_path / "nope.txt"))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["workload_drift"] == {"status": "unreadable", "files": []}
    assert "unreadable" in r.stderr


def test_undecodable_drift_file_is_unreadable_not_a_crash(tmp_path: Path):
    """⛔ `UnicodeDecodeError` is a `ValueError`, not an `OSError`.

    Catching only `OSError` let one non-UTF-8 byte in the drift file raise
    through `main()`, and the workflow reads a non-zero exit as "the ratios
    failed" and marks the whole night INCONCLUSIVE — throwing away a good
    measurement over an annotation, which is precisely what this feature is
    documented as refusing to do. Found in review on PR #1455.
    """
    (tmp_path / "drift.bin").write_bytes(b"config_bench_test.go\n\xff\xfe_bad.go\n")
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 105.0)]),
            "--workload-drift", str(tmp_path / "drift.bin"))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert payload["workload_drift"] == {"status": "unreadable", "files": []}
    # The ratios themselves must survive — that is the whole point.
    assert payload["evaluated"]["BenchmarkA"]["ratio"] == pytest.approx(1.05)


def test_mismatched_cpu_headers_are_refused(tmp_path: Path):
    """Both sides run in one job on one runner. Different CPUs means these are
    not a pair at all, and a ratio computed across them is the cross-machine
    comparison ADR-032 removes."""
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0)], cpu="AMD EPYC 9V74 80-Core Processor"))
    assert r.returncode == 2
    assert "different CPUs" in r.stderr


def test_both_sides_missing_a_cpu_header_are_refused(tmp_path: Path):
    """⛔ `None == None` passes an equality check. Two header-less sides must be
    refused on ABSENCE, before any comparison — otherwise the same-machine
    premise is never actually verified and the ratios look perfectly fine."""
    headerless = "BenchmarkA-4  \t       5\t 100 ns/op\nPASS\n"
    r = run(tmp_path, headerless, headerless)
    assert r.returncode == 2
    assert "no `cpu:` header" in r.stderr
    assert not (tmp_path / "out.json").exists()


def test_one_side_missing_a_cpu_header_is_refused(tmp_path: Path):
    r = run(tmp_path,
            "BenchmarkA-4  \t       5\t 100 ns/op\nPASS\n",
            side([("BenchmarkA", 100.0)]))
    assert r.returncode == 2
    assert "reference side" in r.stderr


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


# ── workload digest (PR-A, ADR-032 §工作定義漂移) ──────────────────────────
#
# The drift LIST saturates: measured 2026-08-16/17/18 it was the same four lines
# verbatim, 20/20 benchmarks implicated while one had a step (precision 1/20).
# The digest is the same evidence compressed into something that can change.


def _digest_tsv(*records: tuple[str, str, str]) -> str:
    return "".join(f"{s}\t{f}\t{h}\n" for s, f, h in records)


_REC = (("reference", "config_bench_test.go", "a" * 64),
        ("reference", "config_test.go", "b" * 64),
        ("main", "config_bench_test.go", "a" * 64),
        ("main", "config_test.go", "c" * 64))


def _run_with_digest(tmp_path: Path, body: str | None):
    extra = []
    if body is not None:
        (tmp_path / "digest.tsv").write_text(body, encoding="utf-8")
        extra = ["--workload-digest", str(tmp_path / "digest.tsv")]
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]), *extra)
    assert r.returncode == 0, r.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["workload_digest"], r


def test_digest_absent_is_not_requested_not_clean(tmp_path: Path):
    """Same three-state discipline as the drift list. A consumer that cannot
    tell "nobody asked" from "asked, nothing moved" reads the first as the
    second, which is the conflation this whole ADR exists to stop."""
    dg, _ = _run_with_digest(tmp_path, None)
    assert dg["status"] == "not-requested"
    assert dg["sides"] == {}


def test_digest_happy_path_aggregates_per_side(tmp_path: Path):
    dg, r = _run_with_digest(tmp_path, _digest_tsv(*_REC))
    assert dg["status"] == "checked"
    assert set(dg["sides"]) == {"reference", "main"}
    assert dg["sides"]["reference"]["n_files"] == 2
    # The sides share one file and differ on the other, so the aggregates must
    # differ — a digest that collapsed them would report "same work" for a night
    # whose fixtures had actually diverged.
    assert dg["sides"]["reference"]["digest"] != dg["sides"]["main"]["digest"]
    assert "workload closure digest" in r.stdout


def test_digest_is_stable_and_order_independent(tmp_path: Path):
    a, _ = _run_with_digest(tmp_path, _digest_tsv(*_REC))
    b, _ = _run_with_digest(tmp_path, _digest_tsv(*reversed(_REC)))
    assert a["sides"] == b["sides"], "record order must not move the digest"


def test_digest_moves_when_a_file_changes(tmp_path: Path):
    before, _ = _run_with_digest(tmp_path, _digest_tsv(*_REC))
    changed = list(_REC)
    changed[1] = ("reference", "config_test.go", "d" * 64)
    after, _ = _run_with_digest(tmp_path, _digest_tsv(*changed))
    assert after["sides"]["reference"]["digest"] != before["sides"]["reference"]["digest"]
    assert after["sides"]["main"]["digest"] == before["sides"]["main"]["digest"]


def test_digest_moves_when_a_file_is_added_or_removed(tmp_path: Path):
    """A content-only hash would go quiet exactly when a new benchmark file
    lands — the moment the work definition changed most."""
    before, _ = _run_with_digest(tmp_path, _digest_tsv(*_REC))
    added, _ = _run_with_digest(
        tmp_path, _digest_tsv(*_REC, ("main", "new_bench_test.go", "e" * 64)))
    assert added["sides"]["main"]["digest"] != before["sides"]["main"]["digest"]
    removed, _ = _run_with_digest(
        tmp_path, _digest_tsv(*[r for r in _REC if r[1] != "config_test.go"]))
    assert removed["sides"]["main"]["digest"] != before["sides"]["main"]["digest"]


@pytest.mark.parametrize("body,why", [
    ("reference\tconfig_test.go\n", "wrong field count"),
    ("REF\tconfig_test.go\t" + "a" * 64 + "\n", "unknown side"),
    ("reference\t\t" + "a" * 64 + "\n", "empty path"),
    (_digest_tsv(*_REC) + "reference\tconfig_test.go\t" + "f" * 64 + "\n", "duplicate record"),
    ("reference\tconfig_test.go\t" + "a" * 64 + "\n", "one side only"),
])
def test_malformed_digest_is_unreadable_never_partial(tmp_path: Path, body: str, why: str):
    """⛔ A digest built from part of the closure is worse than none: it renders
    as a normal scalar and every comparison against it is meaningless."""
    dg, _ = _run_with_digest(tmp_path, body)
    assert dg["status"] == "unreadable", why
    assert dg["sides"] == {}


def test_unreadable_digest_does_not_fail_the_night(tmp_path: Path):
    """Same trade as the drift reader: a working measurement is never thrown
    away over a broken annotation."""
    dg, r = _run_with_digest(tmp_path, "garbage\n")
    assert dg["status"] == "unreadable"
    assert r.returncode == 0
    assert "NOT established" in r.stdout


def test_digest_moves_when_a_file_is_renamed_with_identical_content(tmp_path: Path):
    """⛔ Added because the intentional-break pass found this hole: dropping the
    PATH from the aggregate (hashing only the contents) left every other digest
    test green. A rename is a work-definition change — `find` picks the new name
    up and the old one out — so an aggregate blind to paths would report "same
    work" across it."""
    before, _ = _run_with_digest(tmp_path, _digest_tsv(*_REC))
    renamed = [(s, "renamed_test.go" if f == "config_test.go" else f, h)
               for s, f, h in _REC]
    after, _ = _run_with_digest(tmp_path, _digest_tsv(*renamed))
    assert after["sides"]["reference"]["digest"] != before["sides"]["reference"]["digest"]
    assert after["sides"]["main"]["digest"] != before["sides"]["main"]["digest"]


def _run_digest_path(tmp_path: Path, path: Path):
    """Point --workload-digest at `path` WITHOUT creating it first."""
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]),
            "--workload-digest", str(path))
    assert r.returncode == 0, r.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["workload_digest"], r


def test_absent_digest_file_is_unreadable_not_checked(tmp_path: Path):
    """⛔ Added because the intentional-break pass found this hole: flipping the
    FILE-READ failure branch to `checked` left all 40 other tests green. The
    parse-failure path was covered; this one was not — and it is the higher
    stake of the two, because the caller DELETES the file on purpose when its
    own comparison could not be trusted. Reading that deliberate absence as a
    clean bill of health is the exact "could not measure" → "measured and clean"
    conflation ADR-032 exists to remove."""
    dg, r = _run_digest_path(tmp_path, tmp_path / "never-written.tsv")
    assert dg["status"] == "unreadable"
    assert dg["sides"] == {}
    assert "NOT established" in r.stdout


def test_non_utf8_digest_file_is_unreadable_not_a_crash(tmp_path: Path):
    """`UnicodeDecodeError` is a `ValueError`, not an `OSError` — the drift
    reader was crashed by exactly this on PR #1455, which the workflow reads as
    "the ratios failed" and turns a usable night INCONCLUSIVE."""
    p = tmp_path / "binary.tsv"
    p.write_bytes(b"reference\tconfig_test.go\t\xff\xfe not utf-8\n")
    dg, r = _run_digest_path(tmp_path, p)
    assert dg["status"] == "unreadable"
    assert r.returncode == 0
