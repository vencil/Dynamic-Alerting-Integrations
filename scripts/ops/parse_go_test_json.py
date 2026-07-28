#!/usr/bin/env python3
"""parse_go_test_json.py — turn one module's `go test -json` stream into a fragment.

Why this exists
===============
`.github/workflows/nightly-race.yaml` used to file an issue whose body said only
"go test -race -count=10 failed for <module>" plus a run URL. It never named the
failing test, so four consecutive nights of the SAME deterministic failure looked
like four unrelated reports (#1213 / #1230 / #1258 / #1263) and nobody noticed.
This script extracts the evidence the body needs, once, in a structured form the
report job can dedup and compare against.

Why Python rather than Go
-------------------------
The sibling `go test -json` reader in this repo (scripts/tools/ops/bench_filter.go)
is a `go run` one-off with NO tests — it has no module home, so there is nowhere
for a `_test.go` to live that CI would run. This parser is the ONLY thing standing
between a red nightly and a silent one, so it has to be tested. Python puts its
unit tests in `tests/ops/`, which the existing "Python Tests (3.13)" required check
already runs (and whose path filter already includes `.github/workflows/**` and
`scripts/**`). A Go parser would also force `setup-go` onto the report job purely
to parse JSON. Stdlib only — no install step.

Contract notes measured against a real Go 1.26 run, not assumed
---------------------------------------------------------------
* `-count=10` emits the SAME failing test name once per repetition (measured: 10
  `fail` events for one test). The dedup here is load-bearing, not cosmetic.
* `cmd/go` feeds the test binary's stdout AND stderr through test2json, so the
  race detector's `WARNING: DATA RACE` banner arrives as an `{"Action":"output"}`
  event attributed to the triggering test. Measured: attributed correctly.
* A build failure emits `{"Action":"build-output"}` + `{"Action":"build-fail"}`
  (ImportPath, no Package) and a package-level `{"Action":"fail","FailedBuild":...}`
  with NO `Test` field at all. So a non-zero exit with an EMPTY failing-test list
  is a real, expected state — it must not read as "clean".
* Non-JSON lines do occur (toolchain download notices, stray preamble). They are
  counted, never fatal.

Fail-closed direction
---------------------
Every ambiguity resolves toward "worse". A missing/unreadable raw file, an
unparseable stream, or a non-zero `go test` exit that yielded no attributable
cause all set `unattributed_failure`, which the report job treats exactly like a
named failure. The one thing this parser must never do is emit a clean fragment
for a run that actually failed.

Usage:
  parse_go_test_json.py --module NAME --input raw.jsonl --output frag-NAME.json \\
      --go-exit-code N [--run-url URL] [--workdir DIR] [--packages "./..."]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Precise race-detector banners. Deliberately NOT a bare "DATA RACE" substring:
# a test that merely PRINTS those words would flip the issue title to "DATA RACE",
# and mislabeling a deterministic failure as a race is the exact defect this
# rework exists to fix (#932 triage: "只在偵測到真 DATA RACE 時才標 race").
RACE_MARKERS = (
    "WARNING: DATA RACE",
    "race detected during execution of test",
)
RACE_SUMMARY_RE = re.compile(r"found \d+ data race")


def parse_stream(lines, go_exit_code: int) -> dict:
    """Reduce test2json events to the fields the report job needs.

    `lines` is any iterable of str. Kept separate from file/CLI handling so the
    unit tests exercise the real reduction rather than a re-implementation.
    """
    failed: set[str] = set()
    race_tests: set[str] = set()
    had_data_race = False
    build_failed = False
    fail_events = 0
    non_json_lines = 0
    total_events = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            non_json_lines += 1
            continue
        if not isinstance(ev, dict):
            # Valid JSON that is not an event object (e.g. a bare string).
            non_json_lines += 1
            continue

        total_events += 1
        action = ev.get("Action")

        # `FailedBuild` is present on the package-level fail event of a build
        # break; `build-fail` is the build-scoped event. Either alone is proof.
        if action == "build-fail" or ev.get("FailedBuild"):
            build_failed = True

        if action == "fail":
            fail_events += 1
            test = ev.get("Test")
            # Test is absent/None on the PACKAGE-level fail event (measured).
            # Guard the type too: a malformed stream must not put a non-string
            # into the sorted list and blow up on comparison.
            if isinstance(test, str) and test.strip():
                failed.add(test.strip())

        elif action == "output":
            out = ev.get("Output")
            if not isinstance(out, str):
                continue
            if any(m in out for m in RACE_MARKERS) or RACE_SUMMARY_RE.search(out):
                had_data_race = True
                test = ev.get("Test")
                if isinstance(test, str) and test.strip():
                    race_tests.add(test.strip())

    # A failing run we could not attribute to anything is the dangerous case:
    # the old workflow's body was exactly this state and it read as informative.
    unattributed = bool(go_exit_code) and not failed and not build_failed and not had_data_race

    return {
        "failed_tests": sorted(failed),
        "race_tests": sorted(race_tests),
        "had_data_race": had_data_race,
        "build_failed": build_failed,
        "unattributed_failure": unattributed,
        "go_exit_code": go_exit_code,
        "fail_events": fail_events,
        "non_json_lines": non_json_lines,
        "total_events": total_events,
    }


def build_fragment(
    module: str,
    input_path: Path,
    go_exit_code: int,
    run_url: str,
    workdir: str,
    packages: str,
) -> dict:
    raw_missing = False
    if input_path.is_file():
        # errors="replace": a truncated UTF-8 sequence (killed test binary) must
        # not turn the whole fragment into an exception — that would lose the
        # very run we are trying to report.
        with input_path.open("r", encoding="utf-8", errors="replace") as fh:
            result = parse_stream(fh, go_exit_code)
    else:
        raw_missing = True
        result = parse_stream([], go_exit_code)
        # No stream at all: we cannot prove the run was clean, so say so loudly
        # regardless of the exit code we were handed.
        result["unattributed_failure"] = True

    result.update(
        {
            "schema": 1,
            "module": module,
            "workdir": workdir,
            "packages": packages,
            "run_url": run_url,
            "raw_missing": raw_missing,
        }
    )
    # `failed` is the single field the report job branches on. Derive it here so
    # the definition of "this module is a problem" lives in exactly one place.
    result["failed"] = bool(
        result["failed_tests"]
        or result["had_data_race"]
        or result["build_failed"]
        or result["unattributed_failure"]
        or result["go_exit_code"]
    )
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--module", required=True)
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--go-exit-code", required=True, type=int)
    ap.add_argument("--run-url", default="")
    ap.add_argument("--workdir", default="")
    ap.add_argument("--packages", default="")
    args = ap.parse_args(argv)

    frag = build_fragment(
        module=args.module,
        input_path=args.input,
        go_exit_code=args.go_exit_code,
        run_url=args.run_url,
        workdir=args.workdir,
        packages=args.packages,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(frag, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"[parse_go_test_json] {args.module}: {json.dumps(frag, sort_keys=True)}")
    # Always exit 0: the fragment IS the product. The workflow fails the matrix
    # leg separately off the recorded go exit code, so a parse that "succeeds at
    # reporting a failure" must not itself go red and mask the upload step.
    return 0


if __name__ == "__main__":
    sys.exit(main())
