"""Every tracked Go module is tested by a ci.yml leg, or says why not.

ci.yml says which directory each Go leg runs `go test` in; `git ls-files` says
which module that directory belongs to and which modules exist. The set of
tested modules plus `UNTESTED_GO_MODULES` must be every tracked go.mod — that
equality, not a count, is what stops a new module from landing with no leg
running its tests while `Go Tests` reports green.

"A leg tests it" only means something if the leg's verdict reaches a required
check, so the second assertion here is that every job `go_test_legs` finds is
in the decision path of an aggregate gate (derived from the gates' own
`needs.*.result` bindings by tests/ops/test_ci_aggregate_gate_contract.py —
no list of leg ids). A Go leg dropped from the `go-tests` aggregate keeps
running and keeps this module's first assertion green; the second is what
turns red.

⚠️ Not covered: the same guarantee for the other test legs. The contract
module only requires every job to be anchored to a name registered in
.github/required-checks.yaml, required OR advisory, and it has no baseline to
notice a job changing anchor; "a job that runs `go test`" is recognisable
from the workflow, a pytest / vitest / playwright leg was measured not to be
(that module's docstring lists what each one looks like).

Until #3756 this module also checked that each leg's path filter covered the
files the leg compiles (#1399). The Go legs are not path-gated any more, so
there is no filter for a compile input to be missing from; the module-coverage
half is what remains. (Renamed from `test_go_filter_compile_inputs.py`.)
"""
from __future__ import annotations

import posixpath
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from _vendored_go import VENDORED_GO_MODULES

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Which jobs a required check waits for is already derived there; a second
# derivation here would be a second answer to the same question.
from test_ci_aggregate_gate_contract import (  # noqa: E402
    _decision_path,
    _gates,
)

# Go modules no ci.yml leg tests, with the reason. Vendored modules are
# compiled by the legs that `replace` them in, but their own upstream tests
# are run by no leg.
UNTESTED_GO_MODULES = {
    "scripts/tools/ops/bench-canary":
        "the bench canary workload; built and run by the bench-* workflows, "
        "never by a ci.yml test leg",
    **VENDORED_GO_MODULES,
}


def go_test_legs(workflow: dict) -> dict[str, str]:
    """job id -> working-directory of each job with a step running `go test`."""
    legs: dict[str, str] = {}
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "go test" in str(step.get("run", "")):
                wd = step.get("working-directory")
                if wd is None:
                    raise AssertionError(
                        f"{job_id} runs `go test` without a working-directory; "
                        "model which module that tests before relying on this")
                if legs.get(job_id, wd) != wd:
                    raise AssertionError(f"{job_id} runs `go test` in two directories")
                legs[job_id] = wd
    return legs


def module_of(directory: str, modules: frozenset[str]) -> str | None:
    d = directory.strip("/")
    while True:
        if d in modules:
            return d
        if not d:
            return None
        d = posixpath.dirname(d)


def module_leg_problems(legs: dict[str, str], tracked: frozenset[str],
                        untested: set[str]) -> list[str]:
    modules = frozenset(posixpath.dirname(f) for f in tracked
                        if posixpath.basename(f) == "go.mod")
    problems: list[str] = []
    tested: set[str] = set()
    for job, wd in sorted(legs.items()):
        mod = module_of(wd, modules)
        if mod is None:
            problems.append(f"{job} tests {wd}, which is in no tracked Go module")
            continue
        tested.add(mod)
    if tested | untested != modules:
        problems.append(
            f"tracked Go modules {sorted(modules)} != modules a leg tests "
            f"{sorted(tested)} + UNTESTED_GO_MODULES {sorted(untested)}")
    return problems


def _tracked() -> frozenset[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True, timeout=120).stdout.decode("utf-8")
    return frozenset(p for p in out.split("\0") if p)


def test_every_tracked_go_module_has_a_test_leg_or_a_reason() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    problems = module_leg_problems(
        go_test_legs(workflow), _tracked(), set(UNTESTED_GO_MODULES))
    assert not problems, (
        "a Go module's tests are run by no ci.yml leg, so `Go Tests` reports "
        "green without running them. Add a leg (and to the go-tests "
        "aggregate), or record why in UNTESTED_GO_MODULES:\n  "
        + "\n  ".join(problems))


def unweighed_go_legs(legs: dict[str, str], awaited: set[str]) -> list[str]:
    """Go test legs no aggregate gate waits for. PURE."""
    return sorted(job for job in legs if job not in awaited)


def test_every_go_test_leg_is_weighed_by_an_aggregate_gate() -> None:
    """A Go leg outside every aggregate fails without `Go Tests` noticing.

    ⛔ Stricter than the general accounting in the contract module, which is
    satisfied by a job anchored to ANY name in .github/required-checks.yaml,
    an `advisory:` one included. That is not enough here: the assertion above
    counts a module as tested BECAUSE one of these legs runs its tests, so a
    leg whose red blocks nothing would make that count a claim about nothing.
    The decision path of a gate is the only place a Go leg may be.
    """
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    legs = go_test_legs(workflow)
    assert legs, "no ci.yml job runs `go test`; this assertion examined nothing"
    gates = _gates(CI)
    assert gates, "no aggregate gate was discovered in ci.yml"
    awaited: set[str] = set()
    for gate in gates:
        awaited |= _decision_path(workflow["jobs"], gate)
    stray = unweighed_go_legs(legs, awaited)
    assert not stray, (
        f"these ci.yml jobs run `go test` but no aggregate gate waits for "
        f"them: {stray}. They can fail on every PR while the required "
        "`Go Tests` check is green. Add each to the go-tests aggregate — "
        "`needs:`, an env binding of its `.result`, and the loop.")


def test_the_unweighed_leg_finder_can_fail() -> None:
    legs = {"zq-a": "zmod", "zq-b": "zlib"}
    assert unweighed_go_legs(legs, {"zq-a", "zq-b", "zq-other"}) == []
    assert unweighed_go_legs(legs, {"zq-a"}) == ["zq-b"]
    assert unweighed_go_legs(legs, set()) == ["zq-a", "zq-b"]


@pytest.mark.parametrize("case", [
    "no-legs", "orphan-module", "leg-outside-module"])
def test_the_problem_finder_reports_degenerate_inputs(case: str) -> None:
    """The assertion above is negative, so its finder must be seen to fire."""
    tracked = frozenset({"zmod/go.mod", "zmod/a.go", "zmod/sub/b.go",
                         "zmod/nested/go.mod", "zmod/nested/c.go",
                         "zlib/go.mod", "zlib/l.go"})
    legs = {"leg": "zmod/sub"}
    untested = {"zmod/nested", "zlib"}
    assert module_leg_problems(legs, tracked, untested) == []
    if case == "no-legs":
        legs = {}
    elif case == "orphan-module":
        untested = {"zmod/nested"}
    else:
        legs = {"leg": "zmod/sub", "stray": "znowhere"}
    assert module_leg_problems(legs, tracked, untested)
