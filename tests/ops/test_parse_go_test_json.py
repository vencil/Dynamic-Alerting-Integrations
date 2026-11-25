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

import parse_go_test_json as pgtj  # noqa: E402
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


# ── -timeout expiry (#3177) ──────────────────────────────────────────────────
#
# The fixture is the REAL nightly stream that #2977 misread as a test-isolation
# failure (run 38627373961, Go 1.26.8), cut down to the lines that matter: the
# package start, one unrelated passing package, the timeout panic with its
# `running tests:` block and the first goroutine header, and the package-level
# FAIL. GitHub's per-line timestamp prefix was stripped; every JSON line is
# otherwise byte-for-byte what `go test -json` emitted. Note the panic lines DO
# carry a `Test` (test2json attributes them to the last test it saw), so a
# timeout cannot be recognised by "output with no Test".
TIMEOUT_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "go_test_json_package_timeout.jsonl"
TIMEOUT_PKG = "github.com/vencil/threshold-exporter/cmd/da-guard"
_RUNNING = "TestGuard_RedundantAdviceNeverMovesMetricsAcrossAliasSpellings/cCnullL31/"


def _timeout_lines() -> list[str]:
    return TIMEOUT_FIXTURE.read_text(encoding="utf-8").splitlines()


def _pkg_output(pkg: str, text: str, test: str | None = None) -> str:
    ev: dict = {"Action": "output", "Package": pkg, "Output": text}
    if test is not None:
        ev["Test"] = test
    return json.dumps(ev)


def test_real_timeout_stream_is_attributed_to_the_timeout() -> None:
    """The exact shape that read as "exit 1, nothing attributable" on #2977."""
    res = parse_stream(_timeout_lines(), go_exit_code=1)
    problems = []
    if res["timed_out"] is not True:
        problems.append(f"timed_out is {res['timed_out']!r} on a real timeout stream")
    if res["unattributed_failure"] is not False:
        problems.append("a recognised timeout still reads as unattributed")
    if res["failed_tests"] != []:
        problems.append(
            f"failed_tests is {res['failed_tests']!r} — a timeout emits no per-test "
            "fail, and running is not failing"
        )
    expected = [_RUNNING + s for s in ("pBoth", "pC50", "pCnull", "pLnull")]
    if res["timeout_running_tests"] != expected:
        problems.append(
            f"running tests {res['timeout_running_tests']!r} != {expected!r} "
            "(names must come without the `(Ns)` suffix)"
        )
    if res["timeout_packages"] != [
        {"package": TIMEOUT_PKG, "after": "30m0s", "running_tests": expected}
    ]:
        problems.append(f"timeout_packages wrong: {res['timeout_packages']!r}")
    assert not problems, "timeout reduction is wrong:\n  " + "\n  ".join(problems)


def test_timeout_fragment_is_failed(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(TIMEOUT_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    frag = build_fragment(module="m", input_path=raw, go_exit_code=1,
                          run_url="u", workdir="w", packages="./...")
    assert frag["failed"] is True
    assert frag["timed_out"] is True


def test_timeout_panic_without_the_package_verdict_stays_unattributed() -> None:
    """Fail closed: a stream cut off mid-dump has not proven what happened."""
    lines = [ln for ln in _timeout_lines() if '"Action":"fail"' not in ln]
    res = parse_stream(lines, go_exit_code=1)
    assert res["timed_out"] is False
    assert res["unattributed_failure"] is True


def test_prose_mentioning_a_timeout_does_not_flip_the_verdict() -> None:
    """A t.Log line is indented, so the line-anchored panic pattern cannot match it."""
    lines = [
        _pkg_output("p", "    x_test.go:9: panic: test timed out after 1s\n", "TestX"),
        _pkg_output("p", "retrying after the request timed out\n", "TestX"),
        _fail(None, pkg="p"),
    ]
    res = parse_stream(lines, go_exit_code=1)
    assert res["timed_out"] is False
    assert res["unattributed_failure"] is True


def test_interleaved_packages_keep_their_own_running_tests() -> None:
    """`go test ./...` interleaves packages; a name must stay with its package."""
    lines = [
        _pkg_output("a", "panic: test timed out after 10m0s\n", "TestA1"),
        _pkg_output("b", "panic: test timed out after 30m0s\n", "TestB"),
        _pkg_output("a", "\trunning tests:\n", "TestA1"),
        _pkg_output("b", "\trunning tests:\n", "TestB"),
        _pkg_output("a", "\t\tTestA1 (9m59s)\n", "TestA1"),
        _pkg_output("b", "\t\tTestB/sub (2s)\n", "TestB"),
        _pkg_output("a", "\t\tTestA2 (1s)\n", "TestA1"),
        _pkg_output("a", "\n", "TestA1"),
        # After the block ends, a line that merely looks like an entry is not one.
        _pkg_output("a", "\t\tTestNotRunning (1s)\n", "TestA1"),
        _pkg_output("b", "\n", "TestB"),
        _fail(None, pkg="a"),
        _fail(None, pkg="b"),
        _pkg_output("c", "ok  \tc\t0.1s\n"),
    ]
    res = parse_stream(lines, go_exit_code=1)
    assert res["timeout_packages"] == [
        {"package": "a", "after": "10m0s", "running_tests": ["TestA1", "TestA2"]},
        {"package": "b", "after": "30m0s", "running_tests": ["TestB/sub"]},
    ]
    assert res["timeout_running_tests"] == ["TestA1", "TestA2", "TestB/sub"]


# ── per-test repetition counts ───────────────────────────────────────────────
#
# #3343 was titled "repeat-run failure" although its test failed in all ten
# repetitions (a missing binary). The report can only tell "every repetition"
# from "k of n" if the parser counts each top-level test's pass/fail events.


def _pass(test: str, pkg: str = "racesample") -> str:
    return json.dumps({"Action": "pass", "Package": pkg, "Test": test, "Elapsed": 0.01})


def test_test_runs_counts_pass_and_fail_per_top_level_test() -> None:
    lines = [_fail("TestAlways") for _ in range(10)]
    lines += [_fail("TestSometimes") for _ in range(3)] + [_pass("TestSometimes") for _ in range(7)]
    # Subtests are not counted: the parent's verdict covers them.
    lines += [_fail("TestSometimes/child") for _ in range(3)]
    lines += [_pass("TestClean") for _ in range(10)]
    # The package-level verdicts carry no Test and must not be counted.
    lines += [_fail(None), json.dumps({"Action": "pass", "Package": "other"})]
    res = parse_stream(lines, go_exit_code=1)
    assert res["test_runs"] == {
        "TestAlways": {"fail": 10, "pass": 0},
        "TestSometimes": {"fail": 3, "pass": 7},
    }, "only tests that failed at least once are recorded, keyed by top-level name"


def test_test_runs_is_empty_for_a_clean_run_and_present_in_the_fragment(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(_pass("TestA") + "\n", encoding="utf-8")
    frag = build_fragment(module="m", input_path=raw, go_exit_code=0,
                          run_url="u", workdir="w", packages="./...")
    assert frag["test_runs"] == {}
    assert frag["failed"] is False


# ── slow packages (#3541 PR-B) ───────────────────────────────────────────────


def _pkg_verdict(pkg: str, elapsed, action: str = "pass") -> str:
    return json.dumps({"Action": action, "Package": pkg, "Elapsed": elapsed})


def test_slow_package_threshold_is_derived_from_the_timeout_constant() -> None:
    """One SSOT: the threshold is ratio x the -timeout, not a literal of its own."""
    res = parse_stream([], go_exit_code=0)
    assert res["slow_package_threshold_s"] == pgtj.GO_TEST_TIMEOUT_S * pgtj.SLOW_PACKAGE_RATIO
    assert res["go_test_timeout_s"] == pgtj.GO_TEST_TIMEOUT_S
    assert pgtj.SLOW_PACKAGE_RATIO == 0.5


def test_slow_packages_are_strictly_above_the_threshold_heaviest_first() -> None:
    line = pgtj.GO_TEST_TIMEOUT_S * pgtj.SLOW_PACKAGE_RATIO
    lines = [
        _pkg_verdict("m/below", line - 0.1),
        _pkg_verdict("m/at", line),
        _pkg_verdict("m/above", line + 0.1),
        _pkg_verdict("m/heaviest", line + 50, action="fail"),
        # A test-level event is not the package's time, however long.
        json.dumps({"Action": "pass", "Package": "m/test-level", "Test": "TestX",
                    "Elapsed": line * 2}),
        _pkg_verdict("m/garbage", "nan"),
    ]
    res = parse_stream(lines, go_exit_code=1)
    assert res["slow_packages"] == [
        {"package": "m/heaviest", "elapsed_s": round(line + 50, 3)},
        {"package": "m/above", "elapsed_s": round(line + 0.1, 3)},
    ]


def test_a_timed_out_package_is_not_also_slow() -> None:
    """The timeout is its own incident; filing it again as slow is noise."""
    res = parse_stream(_timeout_lines(), go_exit_code=1)
    assert res["timed_out"] is True
    assert res["slow_packages"] == []
    # The panic line alone excludes the package, whatever its Elapsed says.
    lines = [
        _pkg_output("m/t", "panic: test timed out after 30m0s\n", "TestT"),
        _pkg_verdict("m/t", pgtj.GO_TEST_TIMEOUT_S + 1.0, action="fail"),
        _pkg_verdict("m/slow", pgtj.GO_TEST_TIMEOUT_S * 0.9),
    ]
    got = parse_stream(lines, go_exit_code=1)["slow_packages"]
    assert [s["package"] for s in got] == ["m/slow"]


def test_a_slow_package_alone_does_not_fail_the_fragment(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    raw.write_text(_pkg_verdict("m/slow", pgtj.GO_TEST_TIMEOUT_S * 0.75) + "\n",
                   encoding="utf-8")
    frag = build_fragment(module="m", input_path=raw, go_exit_code=0,
                          run_url="u", workdir="w", packages="./...")
    assert frag["slow_packages"] == [{"package": "m/slow",
                                      "elapsed_s": pgtj.GO_TEST_TIMEOUT_S * 0.75}]
    assert frag["failed"] is False
    assert frag["unattributed_failure"] is False


def test_the_ratio_moves_the_line(monkeypatch) -> None:
    """The detector reads the constants at call time, so the ratio is the only knob."""
    lines = [_pkg_verdict(f"m/p{i}", s) for i, s in enumerate((100.0, 500.0, 1000.0))]
    monkeypatch.setattr(pgtj, "SLOW_PACKAGE_RATIO", 0.05)
    assert len(parse_stream(lines, go_exit_code=0)["slow_packages"]) == 3
    monkeypatch.setattr(pgtj, "SLOW_PACKAGE_RATIO", 1.0)
    assert parse_stream(lines, go_exit_code=0)["slow_packages"] == []
