#!/usr/bin/env python3
"""The workload closure is defined once; make every copy of it prove it agrees.

SSOT: `.github/bench-reference.yaml` → `workload_closure.helpers`.

`bench-record.yaml` (the nightly) reads that file at runtime, so it cannot
drift. `bench-workload-effect.yaml` CANNOT: a `workflow_dispatch` input's
`default:` must be a literal string — GitHub Actions does not evaluate
`${{ }}` there — and that default is the experiment knob the workflow exists to
vary. So the list is duplicated in that file by necessity.

⛔ A duplicated definition is not the problem; a duplicated definition that can
diverge SILENTLY is. The failure mode is specific and invisible: a helper
dropped from one copy stays pinned to the reference side, `W` quietly becomes a
hybrid tree, and `W/R` stops meaning "same implementation, only the test code
differs" — while every number on screen still renders normally. That is the
exact shape `bench-workload-effect.yaml`'s own header warns about, and the
closure probe there does not catch it.

This lint turns that silence into a red hook. It compares the literal helper
lists in the workflow against the pin file and fails on any disagreement.

    python3 -X utf8 scripts/tools/lint/check_workload_closure_drift.py

Exit codes follow the dev-rules #13 convention: 0 clean, 1 violation,
2 caller error (a file missing or unparseable).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
try:
    from _lib_compat import try_utf8_stdout  # noqa: E402
except Exception:  # pragma: no cover - compat shim optional
    def try_utf8_stdout() -> None:  # type: ignore
        pass
from _lib_exitcodes import (  # noqa: E402
    EXIT_CALLER_ERROR,
    EXIT_OK,
    EXIT_VIOLATION,
)

REPO = pathlib.Path(__file__).resolve().parents[3]
PIN = REPO / ".github" / "bench-reference.yaml"
WORKFLOW = REPO / ".github" / "workflows" / "bench-workload-effect.yaml"

# Both literal copies live on lines that assign the space-separated list: the
# `default:` of the workflow_dispatch input, and the `OVERLAY_HELPERS:` env
# expression used for the pull_request self-test. Matching on "a quoted run of
# *_test.go names" finds them without hard-coding either line's shape — a new
# third copy would be caught too, which a two-anchor regex would miss.
_LIST_RE = re.compile(r"'((?:[A-Za-z0-9_]+_test\.go)(?:\s+[A-Za-z0-9_]+_test\.go)*)'")


def _rel(p: pathlib.Path) -> str:
    """Repo-relative for readability, absolute when that is impossible.

    ⛔ `Path.relative_to` RAISES for a path outside the repo, and three of the
    four call sites are inside error branches — so the crash would land exactly
    where the tool is supposed to be degrading gracefully, converting a clean
    exit 2 into a traceback. Found by this file's own tests (which retarget the
    two paths into tmp_path), not in production, where both constants are
    always under REPO.
    """
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def _fail(msg: str) -> int:
    print(f"[workload-closure-drift] {msg}", file=sys.stderr)
    return EXIT_VIOLATION


def main() -> int:
    try_utf8_stdout()
    # No options: both paths are structural (the SSOT pin file and the one
    # workflow that must duplicate it), so there is nothing to parameterise.
    # The parser exists anyway so an unrecognised flag exits 2 instead of
    # being silently ignored — tests/shared/test_tool_exit_codes.py asserts
    # exactly that, and a lint that ignores its own argv is a lint whose
    # invocation nobody can trust.
    argparse.ArgumentParser(
        description="Fail if the literal workload-closure helper lists in "
        ".github/workflows/bench-workload-effect.yaml disagree with their SSOT "
        "in .github/bench-reference.yaml."
    ).parse_args()

    for p in (PIN, WORKFLOW):
        if not p.is_file():
            print(f"[workload-closure-drift] not a file: {p}", file=sys.stderr)
            return EXIT_CALLER_ERROR
    # ⛔ `list()` on the raw value is not a type check: a scalar `helpers:
    # config_test.go` becomes ['c','o','n',...] and the run reports
    # EXIT_VIOLATION with a per-character diff — a caller error dressed up as a
    # real finding, which is the worst of the three outcomes. Validate the
    # shape explicitly instead. OSError/UnicodeError are caught for the same
    # reason: an unreadable reference point is "cannot check" (2), never
    # "checked" (0) and never "violation" (1).
    try:
        pin = yaml.safe_load(PIN.read_text(encoding="utf-8"))
        helpers = pin["workload_closure"]["helpers"]
        if (not isinstance(helpers, list) or not helpers
                or not all(isinstance(h, str) and h.strip() for h in helpers)):
            raise TypeError(
                "workload_closure.helpers must be a non-empty list of "
                f"non-empty strings, got {type(helpers).__name__} "
                f"{helpers!r} — an empty or malformed closure would make this "
                f"lint vacuously true")
        expected = list(helpers)
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError) as exc:
        print(f"[workload-closure-drift] {PIN} has no usable "
              f"workload_closure.helpers ({type(exc).__name__}: {exc}) — "
              f"refusing to pass a check whose reference point is missing",
              file=sys.stderr)
        return EXIT_CALLER_ERROR

    try:
        text = WORKFLOW.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"[workload-closure-drift] cannot read "
              f"{_rel(WORKFLOW)} ({type(exc).__name__}) — a lint "
              f"that cannot read its subject has not checked it",
              file=sys.stderr)
        return EXIT_CALLER_ERROR
    found = [m.group(1).split() for m in _LIST_RE.finditer(text)]
    if not found:
        return _fail(
            f"found no literal helper list in {_rel(WORKFLOW)}. Either "
            f"the duplication is gone (delete this lint and say so) or the shape "
            f"changed and this check is now vacuous — a lint that matches nothing "
            f"passes forever, which is worse than not having it.")

    bad = [got for got in found if got != expected]
    if bad:
        return _fail(
            f"{_rel(WORKFLOW)} disagrees with {_rel(PIN)}:\n"
            f"  pin file : {expected}\n"
            + "".join(f"  workflow : {got}\n" for got in bad)
            + "  ⛔ A helper missing from one copy stays pinned to the reference\n"
              "     side; W becomes a hybrid tree and W/R silently stops meaning\n"
              "     'same implementation, only test code differs'. Fix the copy,\n"
              "     or change the pin file if the closure genuinely moved.")

    print(f"[workload-closure-drift] {len(found)} literal cop{'y' if len(found) == 1 else 'ies'} "
          f"in {_rel(WORKFLOW)} agree with {_rel(PIN)} "
          f"({len(expected)} helpers)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
