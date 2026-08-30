"""Tests for write_baseline_marker.py — the completeness marker for
`bench-baseline.txt` (TRK-371 / #1635).

⛔ THIS FILE CARRIES THE §P5 GATE FOR THE BASELINE-SIDE ARTIFACT, and it exists
because §P5's existing body did NOT cover this artifact at all. `test_pair_
bench_ratio.py` pins the `bench-paired.json` key set against `bench-paired/v2`;
`grep -c baseline` over that file returns 0. So when #1635 added a required
field to the baseline side, nothing mechanical would have fired. That gap is
the reason this file is in the same PR as the field.

WHAT IS PINNED, and how strong each half actually is — stated separately
because the two are NOT equally strong, the same asymmetry §P5 concedes:

  1. The PRODUCER's key set ↔ its schema string. STRONG: the test RUNS
     `write_baseline_marker.py` and reads its real stdout/file. A model of the
     source can be wrong; the output cannot.
  2. The CONSUMER's declared expectations ↔ the producer's. STRONG: both are
     importable Python constants, compared directly.
  3. The WORKFLOW's two call sites. TEXT ONLY, and that is a real limit: an
     Actions step cannot be executed here, so this half asserts that both
     baseline steps invoke the producer, not that invoking it works in CI.

⚠️ There is exactly ONE producer, unlike the paired side's two. That is
deliberate: `bench-record.yaml` calls the same script from both the main and
the fallback baseline path, so the "two writers, one drifting" shape that §P5
was written about cannot occur here. Assertion 3 is what keeps it that way.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import analyze_bench_history as abh
import write_baseline_marker as wbm

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github/workflows/bench-record.yaml"
_SCRIPT = _REPO / "scripts/tools/dx/write_baseline_marker.py"

# ⛔ Literals, deliberately. If a required key is added or the schema moves,
# this file must be edited too — that edit is the self-indicting diff §P5 is
# built to produce. Editing these to silence a red is not a fix.
_MARKER_KEYS = frozenset({"schema", "rows"})
_MARKER_SCHEMA = "bench-baseline-rows/v1"

CPU = "AMD EPYC 7763 64-Core Processor"


def baseline(rows: int = 3, cpu: str = CPU, trailing_partial: bool = False) -> str:
    """Render a `go test -bench` dump the way bench_filter.go leaves it."""
    out = ["goos: linux", "goarch: amd64", f"cpu: {cpu}"]
    for i in range(rows):
        out.append(f"BenchmarkThing{i}-4   \t   93\t  {35422664 + i} ns/op\t 120 B/op")
    if trailing_partial:
        # A row truncated after its name: counted by `grep -c '^Benchmark'`,
        # NOT counted by the consumer's `_BENCH_RE`. This is the exact input
        # the two definitions disagree on.
        out.append("BenchmarkTruncated-4   \t   1000\t   ")
    return "\n".join(out) + "\n"


def run_producer(tmp_path: Path, text: str | None,
                 out_name: str = "bench-baseline.rows"):
    """Invoke the real script as a subprocess; return (proc, marker_path)."""
    src = tmp_path / "bench-baseline.txt"
    if text is not None:
        src.write_text(text, encoding="utf-8")
    marker = tmp_path / out_name
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--baseline", str(src), "--out", str(marker)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    return proc, marker


def parse_marker(path: Path) -> dict[str, str]:
    fields = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


# ── 1. producer key set ↔ schema (the §P5 assertion proper) ──────────────

def test_marker_key_set_is_pinned_to_its_schema_version(tmp_path: Path):
    """Run the producer; its REAL output must carry exactly the pinned keys.

    ⛔ Adding a required key without bumping `MARKER_SCHEMA` fails here. That is
    the whole rule: `60f4523` added a required `status` to `bench-paired/v1`
    without a bump, so every legal older artifact passed the schema check and
    was killed one step later with a message that named the wrong cause.
    """
    proc, marker = run_producer(tmp_path, baseline(rows=3))
    assert proc.returncode == 0, proc.stderr
    fields = parse_marker(marker)
    assert set(fields) == set(_MARKER_KEYS), (
        f"marker key set {sorted(fields)} != pinned {sorted(_MARKER_KEYS)}. "
        "⛔ If you added a required key, bump MARKER_SCHEMA in "
        "write_baseline_marker.py AND SUPPORTED_MARKER_SCHEMAS in "
        "analyze_bench_history.py, then update the literals here."
    )
    assert fields["schema"] == _MARKER_SCHEMA


def test_producer_and_consumer_agree_on_the_schema_string(tmp_path: Path):
    """The two declarations live in different modules and must not drift.

    They cannot be one constant: the producer imports the consumer for
    `_BENCH_RE`, so importing back would be circular. This is the seam that
    replaces that import.
    """
    proc, marker = run_producer(tmp_path, baseline())
    assert proc.returncode == 0, proc.stderr
    written = parse_marker(marker)["schema"]
    assert written == wbm.MARKER_SCHEMA
    assert written in abh.SUPPORTED_MARKER_SCHEMAS, (
        f"producer writes {written!r} but the consumer accepts only "
        f"{abh.SUPPORTED_MARKER_SCHEMAS} — every night would be refused"
    )


def test_consumer_required_keys_match_what_the_producer_writes(tmp_path: Path):
    proc, marker = run_producer(tmp_path, baseline())
    assert proc.returncode == 0, proc.stderr
    assert abh.MARKER_REQUIRED_KEYS == set(parse_marker(marker)) == _MARKER_KEYS


@pytest.mark.parametrize("text,why", [
    (baseline(rows=3), "plain three rows"),
    (baseline(rows=3, trailing_partial=True), "row truncated after its name"),
    ("cpu: X\nBenchmarkA-4\t10\t1 ns/op\r\nBenchmarkB-4\t10\t2 ns/op\r\n", "CRLF"),
    ("cpu: X\nBenchmarkNoSuffix\t10\t1 ns/op\n", "missing -<cpus> suffix"),
    ("cpu: X\nBenchmarkA-4\t10\t1.5 ns/op\n", "fractional ns/op"),
    ("cpu: X\nBenchmarkA-4\t10\t1 ms/op\n", "wrong unit"),
    ("cpu: X\n  BenchmarkA-4\t10\t1 ns/op\n", "leading whitespace"),
    ("", "empty file"),
    # ── added after a blind review showed the corpus above missed a
    # one-character fork (`\d+` → `\d*` on the iteration count) that changes
    # behaviour on a real `go test` shape. Each entry below separates the
    # current pattern from a specific plausible edit.
    ("cpu: X\nBenchmarkA-4\t\t1 ns/op\n", "no iteration count (separates \\d+ from \\d*)"),
    ("cpu: X\nBenchmarkFoo/size=10-4\t10\t1 ns/op\n", "subtest name (separates \\w from [^\\s])"),
    ("cpu: X\nbenchmarkfoo-4\t10\t1 ns/op\n", "lowercase (separates case-sensitive from IGNORECASE)"),
    ("cpu: X\nBenchmarkFoo.Bar-4\t10\t1 ns/op\n", "dotted name (separates the char class)"),
    ("cpu: X\nBenchmarkA-4\t10\t1 ns/opX\n", "trailing junk (separates \\b from no boundary)"),
    ("cpu: X\nBenchmarkA-4\t10\t1ns/op\n", "no space before unit (separates \\s+ from \\s*)"),
])
def test_producer_and_consumer_count_the_same_rows(text: str, why: str):
    """The producer's count must equal what the CONSUMER's parser yields.

    ⛔ THIS REPLACES AN ASSERTION THAT COULD NOT FAIL, and the replacement is
    recorded because the original is the defect class this change is about. It
    was `assert wbm._BENCH_RE is abh._BENCH_RE`, with a docstring claiming it
    caught "a future author who COPIES the pattern instead of importing it".
    It cannot: `re.compile` maintains an internal cache keyed on the pattern
    string, so compiling an identical pattern returns the IDENTICAL object.
    Verified — `re.compile(p) is re.compile(p)` → True — and the intentional
    break (replace the import with a verbatim copy) left it GREEN (1 passed).

    ⚠️ It was also the wrong PROPERTY. A verbatim copy is harmless; what harms
    is the two definitions DIVERGING. So this asserts the observable thing:
    over inputs chosen to sit on the regex's edges, the producer's count and
    the consumer's parser agree.

    ⛔ AND THE SCOPE IS NARROWER THAN THE FIRST DRAFT SAID. That draft claimed
    "a forked-and-edited pattern separates them" — an unbounded claim no finite
    corpus can support, and blind review broke it with a ONE-CHARACTER fork
    (`\\d+` → `\\d*` on the iteration count) that passed all 23 tests while
    genuinely diverging on `BenchmarkA-4\\t\\t100 ns/op`. What this test
    actually delivers: each corpus entry separates the current pattern from ONE
    named plausible edit, listed beside it. A fork that agrees with the current
    pattern on every entry below still passes. That is a coverage statement,
    not a proof of non-divergence.
    """
    src = Path(__import__("tempfile").mkdtemp()) / "bench-baseline.txt"
    src.write_text(text, encoding="utf-8", newline="")
    consumer_rows = len(list(abh.parse_bench_file(src, run_id=1)))
    assert wbm.count_rows(text) == consumer_rows, (
        f"producer and consumer disagree on {why!r}: "
        f"{wbm.count_rows(text)} vs {consumer_rows}"
    )


# ── 2. the workflow's two call sites (text-only half) ────────────────────

def _baseline_steps() -> list[dict]:
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = []
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if str(step.get("name", "")).startswith("Nightly baseline"):
                steps.append(step)
    return steps


def test_both_baseline_paths_exist_and_call_the_producer():
    """Both the paired-main path and the fallback path must write a marker.

    ⛔ The count is asserted too. If a third baseline path is ever added, this
    goes red rather than silently leaving one path producing unvouched files —
    "two writers, one drifting" is the exact §P5 failure shape.
    """
    steps = _baseline_steps()
    assert len(steps) == 2, (
        f"expected exactly 2 'Nightly baseline' steps, found {len(steps)}: "
        f"{[s.get('name') for s in steps]}"
    )
    for step in steps:
        assert "write_baseline_marker.py" in step["run"], (
            f"step {step['name']!r} produces bench-baseline.txt but never "
            "writes its completeness marker"
        )


def test_the_marker_is_uploaded_with_the_artifact():
    """A marker the consumer never receives is not a marker."""
    doc = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    paths = [
        step["with"]["path"]
        for job in doc["jobs"].values()
        for step in (job.get("steps") or [])
        if "upload-artifact" in str(step.get("uses", ""))
        and "bench-baseline" in str(step.get("with", {}).get("name", ""))
    ]
    assert paths, "no upload step for the bench-baseline artifact"
    assert any(abh.MARKER_FILE in p for p in paths), (
        f"{abh.MARKER_FILE} is written but never uploaded — "
        "`night_records_from_gh` would see every night as pre-marker"
    )


def test_the_stale_grep_row_count_echo_is_gone():
    """⛔ Two different 'row count' definitions must not both be published.

    `grep -c '^Benchmark'` counts lines the consumer's regex rejects (see
    `test_grep_and_regex_definitions_diverge_on_a_truncated_row`). Leaving the
    old echo beside the marker would print two numbers for one file and invite
    a reader to reconcile them.

    ⚠️ COMMENT LINES ARE STRIPPED FIRST, and the first draft of this test did
    not do that: it went red on the shell comment that EXPLAINS the removal,
    which quotes the old command. An assertion that fires on prose describing
    the fix is not asserting anything about the fix.
    """
    for step in _baseline_steps():
        code = "\n".join(ln for ln in step["run"].splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "grep -c '^Benchmark'" not in code, (
            f"step {step['name']!r} still echoes the old grep row count"
        )


# ── 3. the row definition itself ─────────────────────────────────────────

def test_grep_and_regex_definitions_diverge_on_a_truncated_row(tmp_path: Path):
    """The measurement behind the design note in the producer's docstring.

    Without this the claim "the two definitions disagree on exactly the input
    the marker exists to catch" is an assertion about code nobody ran.
    """
    text = baseline(rows=3, trailing_partial=True)
    grep_count = sum(1 for ln in text.splitlines() if ln.startswith("Benchmark"))
    regex_count = wbm.count_rows(text)
    assert grep_count == 4
    assert regex_count == 3
    assert grep_count != regex_count


def test_marker_counts_rows_the_way_the_consumer_parses_them(tmp_path: Path):
    proc, marker = run_producer(tmp_path, baseline(rows=7, trailing_partial=True))
    assert proc.returncode == 0, proc.stderr
    # 7 whole rows; the truncated 8th is NOT vouched for.
    assert parse_marker(marker)["rows"] == "7"


# ── 4. refusals: never vouch for something unread ────────────────────────

def test_missing_baseline_writes_no_marker(tmp_path: Path):
    proc, marker = run_producer(tmp_path, None)
    assert proc.returncode != 0
    assert not marker.exists(), (
        "a marker was written for a file that does not exist — it would vouch "
        "for nothing and the consumer would trust it"
    )


def test_baseline_with_no_parseable_rows_writes_no_marker(tmp_path: Path):
    proc, marker = run_producer(tmp_path, "goos: linux\ncpu: something\nPASS\n")
    assert proc.returncode != 0
    assert not marker.exists()


def test_a_file_of_only_truncated_rows_is_refused(tmp_path: Path):
    """Zero PARSEABLE rows, but `grep -c '^Benchmark'` would say 2."""
    text = "cpu: x\nBenchmarkA-4   \t 10\t\nBenchmarkB-4   \t 11\t\n"
    proc, marker = run_producer(tmp_path, text)
    assert proc.returncode != 0
    assert not marker.exists()


@pytest.mark.parametrize("rows", [1, 2, 40])
def test_row_count_is_exact_not_approximate(tmp_path: Path, rows: int):
    proc, marker = run_producer(tmp_path, baseline(rows=rows))
    assert proc.returncode == 0, proc.stderr
    assert parse_marker(marker)["rows"] == str(rows)


def test_out_pointing_at_a_directory_is_a_controlled_error(tmp_path: Path):
    """⛔ Blind-review nit. It already failed safe (non-zero, no marker), but
    via a raw `IsADirectoryError` traceback — and a stack trace in the nightly's
    log reads like the benchmark crashed rather than like a mis-set path.

    A specific break that reddens this: remove the `except OSError` around the
    write in `write_baseline_marker.main`.
    """
    outdir = tmp_path / "iam_a_dir"
    outdir.mkdir()
    src = tmp_path / "bench-baseline.txt"
    src.write_text(baseline(rows=2), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--baseline", str(src), "--out", str(outdir)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr, (
        f"uncaught exception instead of a controlled refusal:\n{proc.stderr}"
    )
    assert "::error::" in proc.stderr
    assert outdir.is_dir(), "the directory must be left alone"
