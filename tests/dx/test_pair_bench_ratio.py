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

⛔ ONE EXCEPTION TO THAT, added by TRK-367 / #1571: this file pins the SHAPE of
the `bench-paired` payload, and `bench-record.yaml` is the SECOND place that
writes one (the INCONCLUSIVE fallback, a hand-written `printf` JSON literal).
Pinning only the Python producer would leave the gate blind to exactly the
half it exists to catch — two writers, one drifting. See §P5 in dev-rules.
"""
from __future__ import annotations

import ast
import json
import re
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


# --- read_workload_digest(), called directly --------------------------------
#
# Same reason the drift block above calls its reader directly, and it was worth
# re-learning: the first version of this PR tested the digest ONLY through the
# CLI, and the coverage bot measured the cost — `pair_bench_ratio.py` fell
# 87.8% → 69.3%, the whole 240-283 body reading as untested. Nothing was
# actually untested (the subprocess cases below walk every state, and the
# intentional-break pass turned 6/6 red), but a subprocess is opaque to
# coverage, so the gap is invisible to anyone reading the report — and no gate
# catches it either, because the repo-wide floor (fail_under = 75) stays green
# at 83.7%.
#
# The second half of that convention matters just as much: when one of these
# fails it names the state that broke, where the subprocess version hands back
# a JSON diff. The CLI tests below stay as the end-to-end check; these pin the
# reader itself.

def test_read_digest_no_path_is_not_requested():
    assert pbr.read_workload_digest(None) == {
        "status": "not-requested", "sides": {}, "files": []}


def test_read_digest_missing_file_is_unreadable(tmp_path: Path):
    assert pbr.read_workload_digest(tmp_path / "nope.tsv") == {
        "status": "unreadable", "sides": {}, "files": []}


def test_read_digest_directory_is_unreadable_not_a_crash(tmp_path: Path):
    """IsADirectoryError is an OSError. Pinned for the same reason as the drift
    reader's twin: the caller passes a path it built, and a layout change could
    aim it at one."""
    (tmp_path / "adir").mkdir()
    assert pbr.read_workload_digest(tmp_path / "adir") == {
        "status": "unreadable", "sides": {}, "files": []}


def test_read_digest_undecodable_bytes_are_unreadable(tmp_path: Path):
    """⛔ UnicodeDecodeError is a ValueError, not an OSError — the except clause
    has to name UnicodeError explicitly or this path raises instead of
    degrading."""
    p = tmp_path / "digest.bin"
    p.write_bytes(b"reference\tconfig_test.go\t\xff\xfe\n")
    assert pbr.read_workload_digest(p) == {
        "status": "unreadable", "sides": {}, "files": []}


def _write_digest(dest_dir: Path, *records: tuple[str, str, str]) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    p = dest_dir / "digest.tsv"
    p.write_text(_digest_tsv(*records), encoding="utf-8")
    return p


def test_read_digest_happy_path_is_checked_with_both_sides(tmp_path: Path):
    got = pbr.read_workload_digest(_write_digest(tmp_path, *_REC))
    assert got["status"] == "checked"
    assert set(got["sides"]) == {"reference", "main"}
    assert all(got["sides"][s]["n_files"] == 2 for s in ("reference", "main"))
    assert got["files"] == ["config_bench_test.go", "config_test.go"]


def test_read_digest_sides_differ_when_a_files_content_differs(tmp_path: Path):
    """`config_test.go` is `b`… on the reference side and `c`… on main in _REC.
    That difference IS the signal, so the two aggregates must not collide."""
    got = pbr.read_workload_digest(_write_digest(tmp_path, *_REC))
    assert got["sides"]["reference"]["digest"] != got["sides"]["main"]["digest"]


def test_read_digest_is_order_independent(tmp_path: Path):
    """The caller's enumeration order is `find`'s, which is not guaranteed
    stable across runners. A digest that moved with it would report a
    work-definition change on every quiet night."""
    a = pbr.read_workload_digest(_write_digest(tmp_path / "a", *_REC))
    b = pbr.read_workload_digest(_write_digest(tmp_path / "b", *reversed(_REC)))
    assert a["sides"] == b["sides"]


def test_read_digest_blank_lines_are_skipped_not_fatal(tmp_path: Path):
    p = tmp_path / "digest.tsv"
    p.write_text("\n" + _digest_tsv(*_REC) + "  \n", encoding="utf-8")
    assert pbr.read_workload_digest(p)["status"] == "checked"


@pytest.mark.parametrize("body,why", [
    ("reference\tconfig_test.go\n", "wrong field count"),
    ("REF\tconfig_test.go\t" + "a" * 64 + "\n", "unknown side"),
    ("reference\t\t" + "a" * 64 + "\n", "empty path"),
    (_digest_tsv(*_REC) + "reference\tconfig_test.go\t" + "f" * 64 + "\n",
     "duplicate record"),
    ("reference\tconfig_test.go\t" + "a" * 64 + "\n", "one side only"),
    # ⛔ The bad line comes LAST here, after a complete valid set, and that
    # ordering is the whole point. The three cases above put the malformed line
    # first, so both sides stay empty and the "one-sided" guard catches them —
    # which means they pass even when the field-count guard is deleted. Measured
    # during the break pass: replacing that guard's `return` with `break` left
    # all five green. With this case, the same mutation yields a fully-populated
    # `checked` digest built from part of the input, which is exactly the
    # partial digest this function must never produce.
    (_digest_tsv(*_REC) + "reference\tconfig_test.go\n", "bad line after a valid set"),
    # ⛔ Shape-check the hash itself, not just its presence. The caller builds
    # these with `sha256sum | cut -d' ' -f1`; a pipeline that breaks while still
    # emitting a line (an error string, a truncated read, a `cut` on the wrong
    # field) used to sail through — measured before the guard,
    # `…\tnot-a-hash` returned `status: checked` with a normal-looking digest,
    # which is the "wrong number that renders correctly" failure this whole
    # line keeps re-learning. Both halves of the shape are pinned because a
    # length-only check passes `zzzz…` and a hex-only check passes `abc`.
    ("reference\tconfig_test.go\tnot-a-hash\n"
     + "main\tconfig_test.go\t" + "a" * 64 + "\n", "sha is not hex at all"),
    ("reference\tconfig_test.go\t" + "a" * 63 + "\n"
     + "main\tconfig_test.go\t" + "a" * 64 + "\n", "sha one char too short"),
    ("reference\tconfig_test.go\t" + "a" * 65 + "\n"
     + "main\tconfig_test.go\t" + "a" * 64 + "\n", "sha one char too long"),
    ("reference\tconfig_test.go\t" + "A" * 64 + "\n"
     + "main\tconfig_test.go\t" + "a" * 64 + "\n", "uppercase hex is not what sha256sum emits"),
    ("reference\tconfig_test.go\t" + "a" * 63 + "g\n"
     + "main\tconfig_test.go\t" + "a" * 64 + "\n", "right length, non-hex char"),
])
def test_read_digest_malformed_is_unreadable_never_partial(
        tmp_path: Path, body: str, why: str):
    """⛔ Every rejection returns empty `sides`, never the half it managed to
    parse. Asserted here as well as through the CLI because this is the branch
    where a partial digest would be born, and a partial digest renders exactly
    like a real one."""
    p = tmp_path / "digest.tsv"
    p.write_text(body, encoding="utf-8")
    got = pbr.read_workload_digest(p)
    assert got == {"status": "unreadable", "sides": {}, "files": []}, why


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


# ── §P5 gate: a required-key change must move the schema string ───────────
#
# ⛔ WHY THIS EXISTS (TRK-367 / #1571). `60f4523` added three top-level fields
# to `bench-paired/v1` — including `status`, which the consumer REQUIRES — and
# left the schema string untouched. Every legitimate artifact written before
# that commit then passed the `SUPPORTED_SCHEMAS` check and was killed one step
# later. Meanwhile `c7d0586` bumped v1→v2 for a single OPTIONAL field. The
# compatibility-breaking change did not bump; the harmless one did.
#
# ⚠️ That is not a rule being forgotten. It is a rule that did not exist —
# and nothing in the suite noticed, because the happy-path test asserts
# INDIVIDUAL KEYS and never the key SET, so a new required field turns nothing
# red. These five tests are the rule — and note that TWO of them are the
# rule while the other three are legibility; each docstring says which it is,
# because getting that backwards is itself a blind-review finding here.

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github/workflows/bench-record.yaml"

# The exact top-level shape each producer writes today. Changing either set is
# allowed — changing it WITHOUT moving the schema string is what is not.
_OK_KEYS = frozenset({
    "schema", "status", "reference_tag", "reference_sha",
    "cpu", "evaluated", "inconclusive", "workload_drift", "workload_digest",
})
_INCONCLUSIVE_KEYS = frozenset({
    "schema", "status", "reference_tag", "reference_sha",
    "reason", "workload_drift", "workload_digest",
})
_SCHEMA = "bench-paired/v2"


_PRINTF_PAYLOAD = r"printf '(\{\\n.*?\})\\n' \\"


def _parse_printf_payload(text: str, where: str) -> dict:
    """Pull the `printf`-written JSON payload out of a workflow file.

    ⛔ FAIL-CLOSED IN BOTH DIRECTIONS, and the second one was a blind-review
    finding against the first cut of this helper:

      no match    someone reformatted the `printf` — split it, switched to a
                  heredoc. A shape gate that silently finds nothing to check is
                  worse than no gate: it reports green for a surface it stopped
                  reading.
      two matches the first cut used `re.search`, which takes the FIRST hit in
                  the file and says nothing. Review inserted a decoy
                  `printf '{...}' \\` block ahead of the real one and the
                  extractor read the decoy. ⚠️ Measured: it is POSITION
                  dependent — a decoy placed after the real block is ignored,
                  so the failure is silent exactly half the time. Requiring
                  exactly one match removes the coin flip.
    """
    hits = re.findall(_PRINTF_PAYLOAD, text, re.S)
    assert len(hits) == 1, (
        f"expected exactly one `printf` JSON payload in {where}, found "
        f"{len(hits)}. Zero means the payload did not necessarily change — the "
        "way it is WRITTEN did; re-point this extractor and do not delete this "
        "test. Two or more means this extractor can no longer tell which block "
        "it is reading, and picking one silently is how a gate starts lying."
    )
    # `\n` is for the shell, `%s` are runtime substitutions. Neither affects
    # the key set or the schema string, which is all this gate reads.
    return json.loads(hits[0].replace("\\n", "\n").replace("%s", "PLACEHOLDER"))


def _workflow_fallback_payload() -> dict:
    return _parse_printf_payload(_WORKFLOW.read_text(encoding="utf-8"),
                                 "bench-record.yaml")


def _git(*args: str) -> str:
    """Run git in the repo, failing closed rather than degrading to a skip."""
    proc = subprocess.run(("git", "-C", str(_REPO)) + args,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc.stdout


def _commit_that_introduced(schema: str, path: str) -> str | None:
    """The newest commit whose diff to `path` added or removed `schema`.

    `None` means the string is not in this file's history yet — i.e. it is
    being introduced in the working tree right now, which is precisely the
    legitimate bump this rule asks for.
    """
    # ⛔ `--follow`, and its absence was a blind-review finding. Without it the
    # pathspec-limited log only sees commits that touched THIS path, so a commit
    # that renames the file registers the string as going 0→1 there and becomes
    # the answer — the commit under test anchoring itself, and any key added in
    # that same commit compares equal to itself. ⚠️ Measured in a throwaway repo:
    # rename + sneak a required key in one commit ⇒ plain pickaxe returns that
    # commit, `--follow` returns the real introduction. On this repo's real
    # history `--follow` changes no answer at all (v2 ⇒ c7d05869, v1 ⇒ still the
    # two commits that make it ambiguous, v99 ⇒ still none).
    out = _git("log", "--follow", "-S", f'"{schema}"',
               "--format=%H", "--", path).split()
    # ⛔ AMBIGUOUS HISTORY FAILS CLOSED, and this was a blind-review finding.
    # The first cut took `out[0]` — the NEWEST match — which a bump-and-revert
    # turns into the revert commit, whose key set already carries the added
    # field, so the comparison holds vacuously. ⚠️ Measured against real
    # history, not a hypothetical: `git log -S '"bench-paired/v1"'` returns TWO
    # commits (9599bdd5 introduced it, c7d05869 removed it), and `out[0]` is
    # the removal. When a string has been introduced more than once there is no
    # single baseline to measure against, and picking one silently is how a
    # gate starts lying.
    assert len(out) <= 1, (
        f"{schema!r} enters and leaves this file\'s history {len(out)} times "
        f"({', '.join(h[:8] for h in out)}), so there is no single commit that "
        "establishes what shape that version names. A human has to say which "
        "one is the baseline — this gate will not guess."
    )
    return out[0] if out else None


def _path_at(rev: str, path: str) -> str:
    """What `path` was called at `rev`, following renames.

    Fixing the anchor is only half of it: `git show <intro>:<today's path>` dies
    when the file has been renamed since, which is fail-closed but stickily so —
    the gate would stay red until the next schema bump. So ask git for the name
    it had in the commit we are about to read.
    """
    out = _git("log", "--follow", "--format=%x00%H", "--name-only", "--", path)
    sha, names = None, {}
    for line in out.splitlines():
        if line.startswith("\0"):
            sha = line[1:].strip()
        elif line.strip() and sha and sha not in names:
            names[sha] = line.strip()
    full = next((h for h in names if h.startswith(rev) or rev.startswith(h)), None)
    assert full is not None, (
        f"{path!r} has no name recorded at {rev[:8]} in its own --follow history. "
        "That should be impossible for a commit this function was handed; a human "
        "has to look, because guessing a filename is how a gate starts lying."
    )
    return names[full]


def _payload_keys(source: str) -> frozenset:
    """Top-level keys of the `payload = {...}` literal in `source`, via AST.

    ⛔ EXACTLY ONE, and that too was a blind-review finding. The first cut
    returned the first `ast.walk` hit — and `ast.walk` is breadth-first, so a
    decoy `payload = {...}` at module scope is visited BEFORE the real one
    inside `main()`. Review added a nine-key decoy, added a tenth required key
    to the real payload, and this read the decoy: 65 passed with the defect
    committed. A second literal means this function cannot tell which one it is
    reading; that is not a tie to break, it is a question to refuse.
    """
    found = []
    for n in ast.walk(ast.parse(source)):
        # ⚠️ BOTH forms, and missing the second one was a blind-review finding:
        # `payload: dict = {...}` parses as AnnAssign, not Assign, so an ordinary
        # type annotation made this return zero. Fail-closed, but the wrong way —
        # a legitimate no-op edit turning the gate red, and stickily so once it
        # lands in the anchor commit. A gate that cries wolf gets deleted.
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, ast.AnnAssign):
            targets = [n.target]
        else:
            continue
        if (any(isinstance(t, ast.Name) and t.id == "payload" for t in targets)
                and isinstance(n.value, ast.Dict)):
            found.append(n.value)
    assert len(found) == 1, (
        f"expected exactly one `payload = {{...}}` literal, found {len(found)}. "
        "Zero means the payload stopped being a literal (built by `.update()`, "
        "a comprehension, conditional keys — an annotated assignment used to "
        "land here too, which was a false positive, not a catch) and this "
        "extractor can no longer read it. Two or more means it cannot tell which is the real one."
    )
    return frozenset(k.value for k in found[0].keys)


def _produced_payload(tmp_path: Path) -> dict:
    """⛔ GROUND TRUTH: run the producer and read what it actually wrote.

    Two earlier cuts of this gate read the current shape out of SOURCE, and
    both were defeated:

      `git show HEAD`   blind to the change being made — the edit is in the
                        working tree, not HEAD, so it compared old against old
                        and passed 65/65. A pre-commit gate that can only see
                        what is already committed cannot stop anything.
      AST of the file   a decoy `payload = {...}` at module scope is what
                        `ast.walk` reaches first. 65 passed with the defect in.

    ⭐ Both failures are the same shape: a MODEL of the producer can be fooled;
    its OUTPUT cannot. So the current side runs the script, and source parsing
    survives only where running is not available — the historical side.
    """
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0)]))
    assert r.returncode == 0, r.stderr
    return json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))


_PY_PRODUCER = "scripts/tools/dx/pair_bench_ratio.py"
_WORKFLOW_PATH = ".github/workflows/bench-record.yaml"


def _producer_keys_at(rev: str) -> frozenset:
    return _payload_keys(_git("show", f"{rev}:{_path_at(rev, _PY_PRODUCER)}"))


def _workflow_keys_at(rev: str) -> frozenset:
    return frozenset(_parse_printf_payload(
        _git("show", f"{rev}:{_path_at(rev, _WORKFLOW_PATH)}"),
        f"bench-record.yaml@{rev[:8]}"))


def test_the_key_set_may_only_move_in_the_commit_that_moves_the_schema(tmp_path: Path):
    """⛔ THE RULE ITSELF, and the first cut of this gate did NOT enforce it.

    Blind review measured the hole: commit the required-field addition, then
    add that key to `_OK_KEYS` below and everything goes green again — the
    original defect committed in full, schema string never moved, 64 passed.
    ⇒ pinning against a literal in this file only enforces that the literal
    and the code agree. It is the author editing both, in one sitting.

    ⇒ the anchor is GIT HISTORY instead: the key set must equal the key set as
    of the commit that introduced the schema string the producer names today.
    Editing `_OK_KEYS` cannot satisfy that; only bumping the schema can, which
    is the rule stated as an assertion.

    ⚠️ When the schema string is NOT yet in this file's history, that means it
    is being introduced in the working tree right now — the legitimate bump —
    and this passes. That is not a hole: the next commit puts it in history and
    every later key change is measured against it.

    ⚠️ Needs full history. `ci.yml` checks out with `fetch-depth: 0`, and the
    `git show`-pinned fixtures in `test_paired_trend_watch.py` already depend
    on it and pass in CI — measured precedent, not an assumption.
    """
    produced = _produced_payload(tmp_path)
    schema = produced["schema"]
    intro = _commit_that_introduced(schema, _PY_PRODUCER)
    if intro is None:
        return  # schema being introduced right now; see docstring
    assert frozenset(produced) == _producer_keys_at(intro), (
        f"the payload key set changed since {intro[:8]}, the commit that "
        f"introduced {schema!r}, without the schema string moving with it. "
        "That is TRK-367 verbatim: `60f4523` added a REQUIRED field under an "
        "unchanged schema and every older artifact became unreadable. Bump the "
        "schema in BOTH producers, or revert the key change."
    )



def test_ok_payload_key_set_is_pinned_to_its_schema_version(tmp_path: Path):
    """The key SET, not individual keys. `test_happy_path_writes_the_expected
    _payload` asserts five keys by name and would stay green if a sixth
    REQUIRED one appeared — which is exactly what `60f4523` did.

    ⚠️ THIS ONE IS LEGIBILITY, NOT THE RULE. `_OK_KEYS` is a literal in this
    file, so an author who edits both sides silences it. The rule is enforced
    by `test_the_key_set_may_only_move_in_the_commit_that_moves_the_schema`,
    which anchors on git history instead. It earns its place by naming today's
    shape in one readable place, right next to the constant a reader has to
    trust.

    ⚠️ An earlier cut of this docstring also claimed it "fails first", so the
    reader would meet the friendly message before the history-anchored one.
    Blind review measured that and it is FALSE — pytest reports in definition
    order and the rule test is defined above this one. Claiming an ordering at
    all was the mistake: it rests on plugin config nobody here pins."""
    r = run(tmp_path,
            side([("BenchmarkA", 100.0)]),
            side([("BenchmarkA", 105.0)]))
    assert r.returncode == 0, r.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert set(payload) == set(_OK_KEYS), (
        "the OK payload's top-level keys moved. If that is intended, bump the "
        "schema string in BOTH producers and update _OK_KEYS/_SCHEMA here — "
        "a required-field change under an unchanged schema string is the "
        "defect this gate exists to stop (TRK-367)."
    )
    assert payload["schema"] == _SCHEMA


def test_the_workflow_key_set_may_only_move_in_the_commit_that_moves_the_schema():
    """⛔ THE RULE, second producer — and its ABSENCE was a blind-review finding.

    Round 3 measured the hole, and it was the ROUND 1 hole still standing on
    the half of the surface that never got anchored: add a required key to the
    workflow's `printf` payload, then add that key to `_INCONCLUSIVE_KEYS` —
    the edit the failure message itself points you at — and 65 passed with the
    defect committed and the schema string never moved. Two files, two lines,
    no trickery. The test next door claimed "same rule as above"; it was not
    the same rule, it was the literal-vs-literal check already rejected once.

    ⚠️ WEAKER THAN THE PYTHON SIDE, and pretending otherwise is exactly how
    this got missed: a GitHub Actions step cannot be run from a test, so there
    is no output to read and this compares TEXT — a model of the producer, and
    a model is the thing that got fooled twice. What it has instead is a model
    that refuses to guess: `_parse_printf_payload` fails closed on zero or 2+
    blocks, in the historical revision as well as the working tree.
    """
    payload = _workflow_fallback_payload()
    schema = payload["schema"]
    intro = _commit_that_introduced(schema, _WORKFLOW_PATH)
    if intro is None:
        return  # schema being introduced right now; same as the Python side
    assert frozenset(payload) == _workflow_keys_at(intro), (
        f"the workflow's INCONCLUSIVE payload key set changed since "
        f"{intro[:8]}, the commit that introduced {schema!r}, without the "
        "schema string moving with it. ⛔ Adding the key to _INCONCLUSIVE_KEYS "
        "silences the sibling test but not this one: bump the schema in BOTH "
        "producers, or revert the key change."
    )


def test_workflow_payload_key_set_is_pinned_to_its_schema_version():
    """The second producer's readable shape. 'Two writers, one drifted' is the
    shape of the original defect, so both get named here.

    ⚠️ LEGIBILITY, NOT THE RULE — same standing as its Python counterpart, and
    for the same reason: `_INCONCLUSIVE_KEYS` is a literal in this file, so an
    author who edits both sides silences it. The rule is enforced by
    `test_the_workflow_key_set_may_only_move_in_the_commit_that_moves_the
    _schema`. It earns its place by naming today's shape in one readable place;
    it does NOT fail before the rule test — see the note on its twin above."""
    payload = _workflow_fallback_payload()
    assert set(payload) == set(_INCONCLUSIVE_KEYS), (
        "the workflow's INCONCLUSIVE payload keys moved. If that is intended, "
        "bump the schema string in BOTH producers and update _INCONCLUSIVE_KEYS"
        "/_SCHEMA here — and note that updating them is NOT enough on its own."
    )
    assert payload["schema"] == _SCHEMA
    assert payload["status"] == "INCONCLUSIVE"


def test_both_producers_declare_the_same_schema_version(tmp_path: Path):
    """⛔ The cross-check, and the one a single-producer gate cannot make.

    Two files hardcode the schema string independently. Bumping one and not
    the other publishes two payload shapes under two different version labels
    from the same night's pipeline — a worse state than the bug this ticket
    started from, because the consumer would then be right to trust the label.
    """
    r = run(tmp_path, side([("BenchmarkA", 100.0)]), side([("BenchmarkA", 100.0)]))
    assert r.returncode == 0, r.stderr
    python_schema = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))["schema"]
    workflow_schema = _workflow_fallback_payload()["schema"]
    assert python_schema == workflow_schema, (
        f"schema strings disagree: pair_bench_ratio.py says {python_schema!r}, "
        f"bench-record.yaml says {workflow_schema!r}. Both producers feed the "
        "same consumer; bump them together."
    )
