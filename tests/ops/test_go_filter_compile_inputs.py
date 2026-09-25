"""Every file a Go test leg COMPILES must be able to wake that leg (#1399).

The runtime check (scripts/ops/go_test_reads.py) sees what the test binaries
open. It cannot see what `go test` compiles — `*.go`, go.mod, go.sum — because
the binary never opens its own sources. Those are derived here instead, from
two independent sources: ci.yml says which directory each Go leg tests and
under which gate; `git ls-files` says which module that directory belongs to
and which files make it up.

Without this, the module-root entries of the `go` filter (`tests/alertmanager-
inhibit/**`, `scripts/tools/ops/*.go`, `**/go.mod`, …) could each be deleted
with every test green: a PR editing only that module's code would then skip
the leg that tests it.

⚠️ Not covered: `//go:embed` targets (inside the module, so a module-root entry
covers them, but nothing here asserts it) and `go.work` (none is tracked).
"""
from __future__ import annotations

import importlib.util
import posixpath
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

sys.path.insert(0, str(ROOT / "scripts" / "ops"))
_SPEC = importlib.util.spec_from_file_location(
    "go_test_reads", ROOT / "scripts" / "ops" / "go_test_reads.py")
_READS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_READS)

# Go modules no ci.yml leg tests, with the reason. The set of tested modules
# plus this set must be every tracked go.mod — that equality, not a count, is
# what stops the leg discovery below from quietly finding nothing.
UNTESTED_GO_MODULES = {
    "scripts/tools/ops/bench-canary":
        "the bench canary workload; built and run by the bench-* workflows, "
        "never by a ci.yml test leg",
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


def compile_inputs(module: str, modules: frozenset[str],
                   tracked: frozenset[str]) -> list[str]:
    """Tracked `*.go` / go.mod / go.sum of `module`, nested modules excluded."""
    out = []
    for f in tracked:
        if not (f.endswith(".go") or posixpath.basename(f) in ("go.mod", "go.sum")):
            continue
        if module_of(posixpath.dirname(f), modules) == module:
            out.append(f)
    return sorted(out)


def compile_input_problems(legs: dict[str, str], gates: dict[str, list[str]],
                           tracked: frozenset[str], untested: set[str]) -> list[str]:
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
        inputs = compile_inputs(mod, modules, tracked)
        if not inputs:
            problems.append(f"{job}: module {mod} has no tracked Go source")
        for f in inputs:
            if not any(_READS.covers(g, f) for g in gates[job]):
                problems.append(f"{job} compiles {f}, which its gate does not cover")
    if tested | untested != modules:
        problems.append(
            f"tracked Go modules {sorted(modules)} != modules a leg tests "
            f"{sorted(tested)} + UNTESTED_GO_MODULES {sorted(untested)}")
    return problems


def _tracked() -> frozenset[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                         check=True, timeout=120).stdout.decode("utf-8")
    return frozenset(p for p in out.split("\0") if p)


def test_every_go_leg_is_woken_by_changes_to_what_it_compiles() -> None:
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    legs = go_test_legs(workflow)
    gates = {job: _READS.leg_gate_patterns(CI, job) for job in legs}
    problems = compile_input_problems(legs, gates, _tracked(), set(UNTESTED_GO_MODULES))
    assert not problems, (
        "a PR touching only these files skips the leg that compiles them, and "
        "`Go Tests (1.26)` reports green without building the change. Add the "
        "file's tree to that leg's filter in ci.yml:\n  " + "\n  ".join(problems))


@pytest.mark.parametrize("case", ["uncovered", "no-legs", "orphan-module"])
def test_the_problem_finder_reports_degenerate_inputs(case: str) -> None:
    """The assertion above is negative, so its finder must be seen to fire."""
    tracked = frozenset({"zmod/go.mod", "zmod/a.go", "zmod/sub/b.go",
                         "zmod/nested/go.mod", "zmod/nested/c.go"})
    legs = {"leg": "zmod/sub"}
    gates = {"leg": ["zmod/*.go", "**/go.mod", "zmod/sub/**"]}
    untested = {"zmod/nested"}
    assert compile_input_problems(legs, gates, tracked, untested) == []
    if case == "uncovered":
        gates = {"leg": ["zmod/*.go", "**/go.mod"]}          # drops zmod/sub/b.go
    elif case == "no-legs":
        legs, gates = {}, {}
    else:
        untested = set()
    assert compile_input_problems(legs, gates, tracked, untested)
