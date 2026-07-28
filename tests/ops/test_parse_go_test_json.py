"""Unit tests for scripts/ops/parse_go_test_json.py (nightly-race evidence extraction).

The fixtures below are VERBATIM lines captured from a real `go test -race
-count=10 -json` run on Go 1.26 (a throwaway module with a deliberate failing
test, a deliberate data race, a failing subtest, and a deliberate compile
error), not hand-written approximations. That distinction is the point: the
33-night alerting outage this repo already survived (#1228) stayed green because
its tests only ever exercised synthetic input the author had imagined.

What each case is guarding:
  * dedup       — `-count=10` really does emit the same failing test name ten
                  times; without dedup the issue body lists it ten times.
  * absent Test — the PACKAGE-level fail event carries no `Test` key at all
                  (measured). Naively reading ev["Test"] raises; naively adding
                  it puts None in a list that later gets sorted.
  * build fail  — a compile error produces ZERO failing test names but a
                  non-zero exit. If that reads as "clean", the nightly goes
                  silent exactly when the module cannot even build.
  * data race   — the race banner must be detected AND attributed, and must NOT
                  be inferred from a test that merely prints the words.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "ops"))

from parse_go_test_json import build_fragment, parse_stream  # noqa: E402


def _fail(test: str | None, pkg: str = "racesample") -> str:
    ev: dict = {"Action": "fail", "Package": pkg, "Elapsed": 0.01}
    if test is not None:
        ev["Test"] = test
    return json.dumps(ev)


def _output(text: str, test: str | None = None) -> str:
    ev: dict = {"Action": "output", "Package": "racesample", "Output": text}
    if test is not None:
        ev["Test"] = test
    return json.dumps(ev)


def test_count10_repeats_are_deduped_and_sorted() -> None:
    """-count=10 emits one fail event per repetition; the body needs one line."""
    lines = [_fail("TestAlwaysFails") for _ in range(10)]
    lines += [_fail("TestSubtests/child_bad") for _ in range(10)]
    res = parse_stream(lines, go_exit_code=1)

    assert res["failed_tests"] == ["TestAlwaysFails", "TestSubtests/child_bad"]
    # The raw event count is retained so the issue body can say "2 distinct,
    # deduped from 20 fail events" — losing it would hide the repeat structure.
    assert res["fail_events"] == 20
    assert res["unattributed_failure"] is False


def test_package_level_fail_event_has_no_test_field() -> None:
    """Verbatim real event: `fail` with no `Test` key. Must not raise or leak None."""
    real = '{"Time":"2026-07-28T15:20:34.311539832Z","Action":"fail","Package":"racesample","Elapsed":0.02}'
    res = parse_stream([real, _fail("TestReal"), _fail(None), json.dumps(
        {"Action": "fail", "Package": "p", "Test": None})], go_exit_code=1)

    assert res["failed_tests"] == ["TestReal"]
    assert all(isinstance(t, str) for t in res["failed_tests"])
    assert res["fail_events"] == 4


def test_build_failure_yields_no_tests_but_is_not_clean() -> None:
    """Verbatim real Go 1.26 build-failure stream (compile error under -race)."""
    real = [
        '{"ImportPath":"brokensample [brokensample.test]","Action":"build-output","Output":"# brokensample [brokensample.test]\\n"}',
        '{"ImportPath":"brokensample [brokensample.test]","Action":"build-output","Output":"./broken_test.go:6:2: undefined: thisIdentifierDoesNotExist\\n"}',
        '{"ImportPath":"brokensample [brokensample.test]","Action":"build-fail"}',
        '{"Time":"2026-07-28T15:21:48.991887694Z","Action":"start","Package":"brokensample"}',
        '{"Time":"2026-07-28T15:21:48.991942813Z","Action":"output","Package":"brokensample","Output":"FAIL\\tbrokensample [build failed]\\n"}',
        '{"Time":"2026-07-28T15:21:48.991949544Z","Action":"fail","Package":"brokensample","Elapsed":0,"FailedBuild":"brokensample [brokensample.test]"}',
    ]
    res = parse_stream(real, go_exit_code=1)

    assert res["build_failed"] is True
    assert res["failed_tests"] == []
    # Attributed to the build, so NOT the "we have no idea" bucket...
    assert res["unattributed_failure"] is False
    # ...but emphatically not clean either.
    assert res["had_data_race"] is False


def test_data_race_is_detected_and_attributed() -> None:
    """Verbatim real race banner: an output event carrying the triggering test."""
    real = '{"Time":"2026-07-28T15:20:34.301760941Z","Action":"output","Package":"racesample","Test":"TestRealDataRace","Output":"WARNING: DATA RACE\\n"}'
    res = parse_stream([real, _fail("TestRealDataRace")], go_exit_code=1)

    assert res["had_data_race"] is True
    assert res["race_tests"] == ["TestRealDataRace"]


@pytest.mark.parametrize(
    "text",
    [
        "WARNING: DATA RACE\n",
        "race detected during execution of test\n",
        "found 2 data race(s)\n",
    ],
)
def test_each_real_race_marker_is_recognised(text: str) -> None:
    assert parse_stream([_output(text)], go_exit_code=1)["had_data_race"] is True


def test_prose_mentioning_a_race_does_not_flip_the_verdict() -> None:
    """Discriminability: mislabeling a repeat-run failure as a race is THE defect.

    A bare "DATA RACE" substring match would let a test's own log line rename the
    issue, which is how the old workflow called four non-race failures "race
    detector flake" in the first place.
    """
    res = parse_stream(
        [_output("checking for DATA RACE conditions in the pool\n"), _fail("TestFoo")],
        go_exit_code=1,
    )
    assert res["had_data_race"] is False
    assert res["failed_tests"] == ["TestFoo"]


def test_non_json_lines_are_counted_not_fatal() -> None:
    """Toolchain notices ("go: downloading go1.26.0") really do appear."""
    res = parse_stream(
        ["go: downloading go1.26.0 (linux/amd64)", "", "not json at all",
         '"a bare json string"', _fail("TestX")],
        go_exit_code=1,
    )
    assert res["failed_tests"] == ["TestX"]
    assert res["non_json_lines"] == 3


def test_nonzero_exit_with_nothing_attributable_is_flagged() -> None:
    """The dangerous state: a failure we cannot explain must say so, not stay quiet."""
    res = parse_stream([_output("some noise\n")], go_exit_code=1)
    assert res["unattributed_failure"] is True


def test_clean_run_is_clean() -> None:
    res = parse_stream([_output("ok  \tracesample\t0.5s\n")], go_exit_code=0)
    assert res["unattributed_failure"] is False
    assert res["failed_tests"] == []
    assert res["had_data_race"] is False


def test_missing_raw_file_is_never_reported_as_clean(tmp_path: Path) -> None:
    """A leg that produced no stream has proven nothing — even with exit code 0."""
    frag = build_fragment(
        module="tenant-api", input_path=tmp_path / "absent.jsonl",
        go_exit_code=0, run_url="u", workdir="w", packages="./...",
    )
    assert frag["raw_missing"] is True
    assert frag["unattributed_failure"] is True
    assert frag["failed"] is True


def test_truncated_utf8_does_not_raise(tmp_path: Path) -> None:
    """A killed test binary can leave a half-written multi-byte sequence."""
    raw = tmp_path / "raw.jsonl"
    raw.write_bytes(_fail("TestX").encode("utf-8") + b"\n" + b'{"Action":"output","Output":"\xe4\xb8')
    frag = build_fragment(
        module="m", input_path=raw, go_exit_code=1,
        run_url="u", workdir="w", packages="./...",
    )
    assert frag["failed_tests"] == ["TestX"]


def test_failed_flag_is_the_single_branch_point() -> None:
    """Every failure shape must set `failed`; a clean run must not."""
    base = dict(module="m", run_url="u", workdir="w", packages="./...")
    clean = build_fragment(input_path=Path("nope"), go_exit_code=0, **base)
    assert clean["failed"] is True  # missing raw file — fail closed
    assert parse_stream([], go_exit_code=0)["unattributed_failure"] is False
