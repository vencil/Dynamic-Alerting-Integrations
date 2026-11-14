r"""The aggregate gates are required checks; this is what holds their verdict.

`ci.yml`, `docs-ci.yaml` and `validate.yaml` report most of main's REQUIRED
status checks from jobs that run no check at all. Each is a pure aggregation
gate: `if: always()`, a handful of `${{ needs.*.result }}` bindings in one
step's `env:`, and a few lines of shell that decide pass/fail (#1397, #1398).

## The contract (#3756)

    exit 0  ⟺  every leg the gate waits for concluded `success`

Nothing else passes: `failure`, `cancelled`, `skipped`, and a result that did
not resolve (the empty string) all fail the gate.

Until #3756 the contract had a second way to pass. The legs were path-gated —
a `detect-changes` job ran `dorny/paths-filter`, each leg carried `if:` on its
outputs — and a gate forgave a SKIPPED leg when the filter said the leg was
not needed. The filters were hand-kept lists of what each leg reads, and a
file missing from one skipped the leg that would have caught the break while
the gate reported green. That mechanism is retired: every PR runs every leg.
This module therefore asserts two things the old one could not:

  * the gates accept `success` and nothing else (the exit-code matrix), and a
    gate step may bind nothing but `.result` values — a `needs.*.outputs.*`
    binding, which is what forgiving a skip needs, is REFUSED;
  * the path gate does not come back: no job in a gate's decision path (the
    legs it weighs and everything they `needs:`) carries an `if:`, and no
    workflow that reports a required check runs a path-filter action. Both are
    read off the parsed YAML, never grepped.

And two things that keep those rules from being satisfied by looking away:

  * every job in a workflow that reports a required check is anchored to a
    name registered in `.github/required-checks.yaml`: it is in the `needs`
    closure (itself included) of a job reporting a `required:` or an
    `advisory:` name. There is no ledger of exceptions in this module — why a
    check blocks nothing is written once, under `advisory:` in that file.
    Dropping a leg out of its gate (`needs:`, the env binding and the loop
    together) leaves a job anchored to nothing. And the advisory half is not
    a way out for a leg: a job only an advisory check waits for must carry
    that check's name as the start of its own (a shard, `Python Coverage
    (1/3)` under `Python Coverage`);
  * no job in a decision path is a reusable-workflow call (job-level
    `uses:`). Its steps live in another file, so every rule here that reads
    steps and conditions would pass over it having read nothing.

⚠️ What this module does NOT cover.

It cannot check that these names are actually configured as required checks
— that lives in GitHub settings. `REQUIRED_CHECK_NAMES` below is read from
`.github/required-checks.yaml`, and `required-checks-drift.yml` is what
compares that file with the settings (#3696).

⛔ The biggest thing it does NOT cover, named because it is adjacent enough to
be mistaken for covered: `run: pytest … || true` on a test leg is the shell
spelling of `continue-on-error`, achieves exactly the same thing, sits in the
same decision path, and nothing forbids it ON A ci.yml TEST LEG — the
existing `|| true` guards are scoped to other workflows
(`tests/ops/test_guard_defaults_scopes.py`,
`tests/ops/test_nightly_scan_matrix_drift.py`, and
`tests/ops/test_secret_scan_gate.py`, which also bans `set +e`, `; exit 0`
and a trailing `exit 0` on any step whose exit code is load-bearing). It is
banned in the gate scripts here (see `_EXIT_SWALLOW`) and deliberately not on
the legs, because `python-tests-run` has a live and legitimate `|| true` on
its "Install amtool" step (a grep guard followed by an explicit empty-value
check). Candidate ticket, not a silent omission.

⛔ The anchoring rule has no baseline, so it cannot see a job CHANGING
anchor. Three moves stay green, each of them a visible edit rather than a
quiet one: a leg taken out of its gate and put under a NEW `advisory:` entry
(an edit of required-checks.yaml, with a reason to write); a leg taken out of
its gate, hung on an advisory job's `needs:` AND renamed to start with that
check's name; and a `kind: gate` entry moved to `advisory:` together with its
gate. "Every test leg is under a REQUIRED name" would close the first two and
was measured not to be decidable from the workflow: of the jobs in scope, the
portal and smoke legs run `npm` scripts, `federation-e2e` runs a shell runner
that starts pytest, and `python-coverage` runs pytest under an advisory name
by owner decision — a runner-name match misses the first three and refuses
the fourth. The one family where the leg IS recognisable keeps its own
stricter rule: a job running `go test` must be in a gate's decision path
(tests/ops/test_go_modules_have_a_test_leg.py).

⛔ A path gate written WITHOUT a known action and WITHOUT an `if:` on a job in
a decision path is not seen either: a leg whose test step wraps itself in a
shell `if git diff --name-only … | grep -q …; then` skips its own work and
exits 0. The two rules above close the shapes this repo actually used; they
are not a proof that no leg can decline to run its tests.

⚠️ The path-filter ACTION rule is a closed list of names
(`PATH_FILTER_ACTIONS` in scripts/ops/workflow_path_filter.py — one list,
read by that tool and by this module). A fork under another name, a local
composite action and a hand-written `git diff --name-only >> $GITHUB_OUTPUT`
step are not on it. That is accepted rather than patched with a heuristic:
such a step changes what runs only once an `if:` reads its output, and the
`if:` rule above does not depend on any name.

⛔ What a green run does NOT prove, stated because a green guard is read as a
guarantee. It does not prove the gate script runs as bash at all beyond the
inputs checked here (`runs-on`, `container`, `shell` and `defaults` are
asserted, but the runner's own behaviour is not observed), and it says
nothing about the required checks that are `kind: job` in
`.github/required-checks.yaml` — those report directly, and
`tests/ops/test_required_checks_ssot.py` holds their "cannot be skipped" rule.

## How a gate is FOUND (derived, not a list of job ids)

Scope: every workflow in which some job reports a `kind: gate` name from
`.github/required-checks.yaml`. Inside those, a gate is any job with a step
whose `env:` binds, through `${{ }}`, the `.result` of at least one job.

The env NAMES are read out of that mapping rather than known: the gates spell
their leg bindings `RUN_RESULT`, `R_TE`/`R_TA`/`R_AM`, `R_PORTAL`. A gate
written tomorrow inherits every assertion here without editing this file.
⛔ Pinning the job ids instead would have been "a denylist that happens to
contain today's members", the shape this repo keeps re-learning.

Discovery is cross-checked against an INDEPENDENT source — the `kind: gate`
names in `.github/required-checks.yaml`, each of which must be reported by a
discovered gate — so a discovery that quietly finds nothing cannot pass.

## How the shell is CHECKED (execute it, do not grep it)

The gate steps declare no `shell:`, so GitHub runs them as `bash -e {0}` — a
temp FILE, not stdin (workflow-syntax reference). The oracle here is the same:
extract the `run:` block, drop it in a temp file, run `bash -e` on it once per
cell of the leg axis, compare the exit code against the contract above.

⚠️ The contract is transcribed from the DESIGN (the owner ruling on #3756:
"aggregate 只接受 success"), not from the script — a truth table read off the
implementation would only restate whatever the implementation does, including
its bugs.

The value domain is complete rather than sampled. `needs.<job>.result` has
exactly four documented values (success / failure / cancelled / skipped —
contexts reference), plus the empty string for "the expression did not
resolve": an unresolvable property "will evaluate to an empty string"
(contexts reference). The leg axis is the full `5^legs` product up to three
legs and a strength-3 covering array beyond — `_leg_tuples` records both
flips of that decision and the measurements behind each.

⚠️ Cost is a function of the leg count and is measured where it matters, in
CI, by the `--durations` list at the end of the Python job's log. No figure
is kept here: earlier ones went stale within a month.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
# ⛔ ONE literal path string each — a `/ ".github" / "workflows"` split form
# registers this module against nothing in verify_diff's text_map.
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
# Not read through these names — discovery globs WORKFLOW_DIR — but they are
# the literal paths that tell verify_diff this module depends on them (#1398).
_ALSO_SCANNED = (ROOT / ".github/workflows/docs-ci.yaml",
                 ROOT / ".github/workflows/validate.yaml")
WORKFLOW_DIR = ROOT / ".github/workflows"

# The path-filter action list lives in the tool that refuses those actions in
# every workflow; this module reads it instead of keeping a second copy (the
# two had already drifted: one knew `tj-actions/changed-files`, the other was
# case-sensitive). One literal path string, for verify_diff's text_map.
_WPF_SOURCE = ROOT / "scripts/ops/workflow_path_filter.py"
sys.path.append(str(_WPF_SOURCE.parent))
import workflow_path_filter as _WPF  # noqa: E402  (scripts/ops, appended above)

BASH = shutil.which("bash")
# ⛔ Per-test, never `pytestmark`: everything except the three bash-backed
# tests (one truth table plus two negative controls) is pure YAML and must keep
# running on the Windows dev host. A module-level skip silently takes those
# with it.
needs_bash = pytest.mark.skipif(
    BASH is None, reason="needs bash to execute the gate script")


# ── loading ────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _load(path: Path) -> dict:
    """ci.yml is ~1.9k lines and every helper here wants it; parse it once."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# The required checks an aggregate gate reports: the `kind: gate` entries of
# .github/required-checks.yaml, the one source for main's required checks
# (tests/ops/test_required_checks_ssot.py checks that file against the
# workflows; required-checks-drift.yml checks it against GitHub's settings).
# Until #3696 this was a hand-copied set dated by a comment; it had silently
# dropped "Smoke Tests (Chromium)" until #3712 added it by hand.
#
# ⚠️ Why the gate subset and not every required check: the assertions below
# are about aggregate gates. A required check reported by an unconditional job
# (`Lint`, `commitlint`, …) is `kind: job` there, and its "cannot skip" rule
# lives in test_required_checks_ssot.py.
#
# ⛔ A SHRINKING edit to the `kind: gate` entries is still a branch-protection
# change. Blind review once deleted a name here and moved the gate's `name:`
# onto the leg it waits for: green, with the live API still requiring the
# check. The drift workflow now catches the file disagreeing with the
# settings; only human review catches both being changed together.
_SSOT = yaml.safe_load(
    (ROOT / ".github/required-checks.yaml").read_text(encoding="utf-8"))
_REQUIRED_ENTRIES = tuple(_SSOT["required"])
REQUIRED_CHECK_NAMES = frozenset(
    entry["name"] for entry in _REQUIRED_ENTRIES if entry.get("kind") == "gate"
)
_ALL_REQUIRED_NAMES = frozenset(entry["name"] for entry in _REQUIRED_ENTRIES)
# Checks that deliberately block nothing; the reasons are in the file and are
# not repeated here (tests/ops/test_required_checks_ssot.py holds "each has a
# reason, is reported by some job, and is not also required").
_ADVISORY_NAMES = frozenset(
    entry["name"] for entry in _SSOT.get("advisory") or [])


def _workflows_reporting(names: frozenset[str]) -> tuple[Path, ...]:
    """Workflows in which some job's literal `name:` is one of `names`.

    Derived from the tree rather than listed, so a required check moving to
    another workflow moves the scan with it. `sorted` keeps failure messages
    stable. ⚠️ Literal names only: a required name produced by a `${{ }}`
    expression is refused separately
    (`test_the_required_check_names_belong_to_the_gates_and_nothing_else`).
    """
    return tuple(
        path for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
        if any(str(job.get("name") or job_id) in names
               for job_id, job in (_load(path).get("jobs") or {}).items())
    )


@functools.lru_cache(maxsize=None)
def _gate_workflows() -> tuple[Path, ...]:
    """Every workflow that reports a `kind: gate` required check.

    Every gate assertion in this module iterates the gates of these workflows;
    the required-check NAME sweep additionally covers every workflow, because
    branch protection matches names across all of them.
    """
    return _workflows_reporting(REQUIRED_CHECK_NAMES)


@functools.lru_cache(maxsize=None)
def _required_check_workflows() -> tuple[Path, ...]:
    """Every workflow that reports ANY required check, `kind: job` included."""
    return _workflows_reporting(_ALL_REQUIRED_NAMES)


_VERSION_IN_NAME = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")


def test_no_required_check_name_carries_a_toolchain_version():
    """A version in a required-check name is a second copy of the version:
    every bump then also needs a branch-protection edit, and until the owner
    makes it the renamed gate never reports and every PR hangs. Both
    "Python Tests (3.13)" and "Go Tests (1.26)" were that shape; the versions
    live in .python-version and the modules' go.mod now."""
    bad = sorted(n for n in REQUIRED_CHECK_NAMES if _VERSION_IN_NAME.search(n))
    assert not bad, (
        f"required check names must not carry a version: {bad} — put the "
        "version in its SSOT file and keep the gate name stable")


# ── reading `${{ needs.X.… }}` out of a step's env ─────────────────────────

_RESULT_EXPR = re.compile(r"^\$\{\{\s*needs\.([\w-]+)\.result\s*\}\}$")
_MENTIONS_NEEDS = re.compile(r"needs\.[\w-]+")


@dataclass(frozen=True)
class _Bindings:
    legs: tuple[tuple[str, str], ...]         # ((env name, job id), …) sorted


def _step_bindings(path: Path, job_id: str, step: dict) -> _Bindings | None:
    """Classify one step's `env:` block. None means "not a gate step".

    ⛔ Fail-closed on shapes this module cannot read: an env value that
    mentions `needs.` but is not a bare `.result` RAISES. Absorbing it would
    drop the job out of every assertion below while it still reads as a gate
    to a human.

    ⛔ That includes `${{ needs.X.outputs.Y }}`, and deliberately (#3756).
    Binding another job's OUTPUT into the verdict step is how the retired
    path gates forgave a skipped leg (`skipped` and `*_changed != 'true'`
    → pass). The contract is now "success and nothing else", which needs no
    output; a gate that binds one is deciding on something this module does
    not vary, so it is refused rather than modelled.
    """
    results: dict[str, str] = {}
    for name, raw in (step.get("env") or {}).items():
        value = str(raw).strip()
        if matched := _RESULT_EXPR.match(value):
            results[name] = matched.group(1)
        elif _MENTIONS_NEEDS.search(value):
            raise AssertionError(
                f"{path.name}::{job_id} binds env {name}={value!r}, which "
                "references `needs.` in a shape this module does not parse. "
                "Every assertion in this file keys off these bindings, so an "
                "unparsed one removes the job from the gate set silently.\n"
                "⛔ If this is a job OUTPUT (`needs.<job>.outputs.<name>`): a "
                "gate passes when every leg it waits for concluded `success`, "
                "and on nothing else (#3756). An output in the verdict is the "
                "shape that used to turn a skipped leg into a pass — remove "
                "it. Moving the reference out of `env:` also makes this "
                "message go away — and it is the anti-pattern: the script "
                "then interpolates `${{ }}` directly, which "
                "`test_gate_scripts_are_executable_the_way_github_runs_them` "
                "refuses.")
    if not results:
        return None
    return _Bindings(legs=tuple(sorted(results.items())))


# The two shell spellings this module can simulate, and the argv GitHub
# documents for each. An unspecified shell is `bash -e {0}`; spelling `bash`
# explicitly is a DIFFERENT command, `bash --noprofile --norc -eo pipefail
# {0}`. Both are modelled rather than refused — blind review pointed out that
# refusing `bash` blocks a change that is strictly SAFER than the default.
# Everything else is refused, because the harness would then be running a
# different interpreter than CI.
_SHELL_ARGV = {
    None: ("-e",),
    "bash": ("--noprofile", "--norc", "-eo", "pipefail"),
}


@dataclass(frozen=True)
class Gate:
    workflow: str
    job_id: str
    check_name: str
    condition: str
    step: int
    script: str
    bindings: _Bindings
    shell: str | None          # the resolved `shell:`, None = unspecified

    @property
    def shell_argv(self) -> tuple[str, ...]:
        return (BASH,) + _SHELL_ARGV[self.shell]

    def __str__(self) -> str:
        return f"{self.workflow}::{self.job_id}"


def _resolved_shell(workflow: dict, job: dict, step: dict) -> str | None:
    """step > job `defaults.run` > workflow `defaults.run` > unspecified."""
    for holder in (step,
                   (job.get("defaults") or {}).get("run") or {},
                   (workflow.get("defaults") or {}).get("run") or {}):
        if "shell" in holder:
            return str(holder["shell"])
    return None


def _gates(path: Path) -> tuple[Gate, ...]:
    """Every aggregate gate in one workflow, derived from `env:` bindings."""
    gates: list[Gate] = []
    for job_id, job in _load(path)["jobs"].items():
        hits = [
            (index, step, bindings)
            for index, step in enumerate(job.get("steps") or [])
            if (bindings := _step_bindings(path, job_id, step)) is not None
        ]
        if not hits:
            continue
        if len(hits) > 1:
            raise AssertionError(
                f"{path.name}::{job_id} has {len(hits)} gate-shaped steps. A "
                "required check whose verdict is split across several steps "
                "is not modelled here — a later step can exit 0 after an "
                "earlier one already decided. Merge them, or model the "
                "sequence.")
        index, step, bindings = hits[0]
        shell = _resolved_shell(_load(path), job, step)
        if shell not in _SHELL_ARGV:
            raise AssertionError(
                f"{path.name}::{job_id} resolves to `shell: {shell!r}`. This "
                "module simulates only an unspecified shell (`bash -e {0}`) "
                "and an explicit `bash` (`bash --noprofile --norc -eo "
                "pipefail {0}`); anything else means the exit-code matrix "
                "would be asserting about an interpreter that never runs the "
                "gate. Add it to _SHELL_ARGV with the argv GitHub documents.")
        gates.append(Gate(
            workflow=path.name,
            job_id=job_id,
            check_name=str(job.get("name") or job_id),
            condition=str(job.get("if", "")),
            step=index,
            script=str(step.get("run") or "").replace("\r\n", "\n"),
            bindings=bindings,
            shell=shell,
        ))
    return tuple(gates)

def _discover_gates_or_fail() -> tuple[tuple[Gate, ...], AssertionError | None]:
    """Discovery, with its refusal captured instead of raised at import.

    ⛔ Every fail-closed `raise` above happens while `_gates_or_fail()` is being
    called for the `parametrize` decorator — i.e. at COLLECTION time. Blind
    review hit one and the result was `ERROR collecting`: every assertion in
    this file vanished at once with no message about why.

    ⚠️ What this buys is a readable failure, NOT isolation. A refusal still
    empties the gate set, so `_gates_or_fail` re-raises it in every assertion
    that iterates gates — otherwise those tests would iterate an empty tuple
    and pass. A later reviewer measured exactly that vacuous-green state and
    was right to call the earlier wording ("a gap in gate A must not take gate
    B's protection offline") false: it does, and the honest fix is to make all
    of them say so rather than to claim an isolation that is not implemented.
    """
    try:
        return tuple(gate for path in _gate_workflows()
                     for gate in _gates(path)), None
    except AssertionError as refusal:  # noqa: PERF203 - one call, one catch
        return (), refusal


_CI_GATES, _CI_GATE_REFUSAL = _discover_gates_or_fail()


def _ci_gates() -> tuple[Gate, ...]:
    return _CI_GATES


def _workflow_of(gate: Gate) -> Path:
    return WORKFLOW_DIR / gate.workflow


def _gates_or_fail() -> tuple[Gate, ...]:
    """Every assertion that iterates gates goes through here.

    An empty gate set makes a `for gate in …` assertion pass without looking
    at anything, so a discovery refusal has to be re-raised rather than
    silently yielding nothing.
    """
    if _CI_GATE_REFUSAL is not None:
        raise AssertionError(
            "gate discovery refused, so this assertion examined nothing:\n"
            + str(_CI_GATE_REFUSAL))
    return _CI_GATES


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


# ── the path gate must not come back (#3756) ───────────────────────────────

# "This job consults some other job's verdict", wherever it is written —
# `env:`, `run:`, `with:`, `if:`. Matched against the job's JSON dump so no
# spelling location is privileged.
_READS_A_RESULT = re.compile(r"needs\.[\w-]+\.result")

def _path_filter_steps(path: Path) -> list[str]:
    """Steps in one workflow that run a path-filter action. PURE over a path,
    so a synthetic workflow can prove it says YES.

    ⛔ WHICH actions those are is not decided here: the list and the matching
    are `workflow_path_filter.path_filter_steps`, the same function
    `workflow_path_filter.py generate --check` refuses a workflow with. A
    closed list of names is the weaker half of this guard — what it does not
    see, and why that is accepted, is written at the list and in this
    module's docstring. The half that does not depend on a name is
    `_conditional_jobs_in_decision_paths`.
    """
    return [f"{path.name}::{job_id} step {step!r} uses `{uses}`"
            for job_id, step, uses in _WPF.path_filter_steps(_load(path))]


def _reusable_calls_in_decision_paths(path: Path,
                                      gates: tuple["Gate", ...]) -> list[str]:
    """Jobs in some gate's decision path that call a reusable workflow
    (job-level `uses:`). PURE over a path.

    ⛔ Refused rather than followed. Such a job has no `steps:` here — they
    are in the called file, with their own `if:`, `continue-on-error` and
    path-filter steps. Every rule in this module that walks a decision path
    reads `jobs[job_id]` of THIS file, finds nothing to object to, and
    passes: a leg moved into a callee would carry any condition it liked.
    Following the call is possible for a local `./.github/workflows/x.yml`
    and impossible for `owner/repo/.github/workflows/x.yml@ref`; no leg
    needs either today, so the shape is closed instead of half-modelled.
    """
    jobs = _load(path)["jobs"]
    return [
        f"{path.name}::{job_id} (decision path of {gate}) calls the reusable "
        f"workflow `{jobs[job_id]['uses']}`"
        for gate in gates
        for job_id in sorted(_decision_path(jobs, gate))
        if "uses" in jobs[job_id]]


def _decision_path(jobs: dict, gate: "Gate") -> set[str]:
    """The legs a gate weighs, plus every job they transitively `needs:`.

    ⛔ Transitive on purpose. GitHub SKIPS a job whose dependency was skipped,
    so an `if:` on a job a leg merely `needs:` skips the leg just as an `if:`
    on the leg itself would — with no condition written on the leg at all.
    """
    seen: set[str] = set()
    todo = [job_id for _name, job_id in gate.bindings.legs]
    while todo:
        job_id = todo.pop()
        if job_id in seen or job_id not in jobs:
            continue
        seen.add(job_id)
        todo += _needs(jobs[job_id])
    return seen


def _conditional_jobs_in_decision_paths(path: Path,
                                        gates: tuple["Gate", ...]) -> list[str]:
    """Jobs in some gate's decision path that carry an `if:`, and steps of
    those jobs whose `if:` is anything but `always()`. PURE over a path.

    ⛔ Presence of the key, not its value. Whether a condition is a PATH
    condition cannot be decided by reading it — `needs.x.outputs.go_changed`
    is one, and so is `contains(github.event.pull_request.labels.*.name,
    'run-go')` fed by a labeller that looks at paths. What can be decided is
    that the gate fails on a skipped leg, so ANY condition that can be false
    on a pull request turns the required check red there. A leg that
    genuinely must not run on some event needs its gate's contract modelled
    here first (`_expected_exit`), not a quiet `if:`.

    ⛔ STEPS too, and that half is older than #3756: it moved here from the
    retired path-filter guard, where it was stated in terms of the gate but
    held something independent of it. A step skipped by its own `if:` is not
    a failing step — the JOB still concludes `success`, the gate passes, and
    the test the step would have run leaves no trace in the checks list.
    Worse than a skipped leg, which the gate now fails. The accepted set is
    one condition, `always()`, which can only WIDEN when a step runs; a
    denylist of dangerous spellings was tried four times and has no last
    member (`STEP_SWITCH_OFF_SPELLINGS` is what holds the accept-set shape).
    """
    jobs = _load(path)["jobs"]
    problems = []
    for gate in gates:
        for job_id in sorted(_decision_path(jobs, gate)):
            if "if" in jobs[job_id]:
                problems.append(
                    f"{path.name}::{job_id} (decision path of {gate}) has "
                    f"`if: {jobs[job_id]['if']!r}`")
            for index, step in enumerate(jobs[job_id].get("steps") or []):
                if "if" not in step:
                    continue
                if _normalised_condition(str(step["if"])) != "always()":
                    problems.append(
                        f"{path.name}::{job_id} step "
                        f"{step.get('name', index)!r} (decision path of "
                        f"{gate}) has step-level `if: {step['if']!r}`")
    return problems


# ⛔ Jobs that read some job's `.result` — the thing an aggregate gate does —
# but that this module cannot model, with the reason and the
# ticket. Asserted as an exact set, so a new one cannot join a silent majority
# and a fixed one cannot leave a stale excuse behind.
UNMODELLED_GATE_SHAPED_JOBS: dict[tuple[str, str], str] = {}


def _needs_closure(jobs: dict, roots: set[str]) -> set[str]:
    """`roots` plus every job they transitively `needs:`. PURE."""
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        job_id = todo.pop()
        if job_id in seen or job_id not in jobs:
            continue
        seen.add(job_id)
        todo += _needs(jobs[job_id])
    return seen


def _reporters_of(jobs: dict, names: frozenset[str]) -> dict[str, set[str]]:
    """job id -> the names in `names` it reports. PURE.

    ⛔ Through `_rendered_names`, so a matrix shard is compared by what GitHub
    will call it. A name that cannot be rendered reports nothing here: the
    job is then anchored only through some other job's `needs:`, or not at
    all.
    """
    found = {}
    for job_id, job in jobs.items():
        hit = set(_rendered_names(job_id, job) or ()) & names
        if hit:
            found[job_id] = hit
    return found


def _job_roles(path: Path, required_names: frozenset[str],
               advisory_names: frozenset[str]) -> dict[str, str | None]:
    """job id -> which registered name anchors it, or None. PURE over a path.

      "required"  in the `needs` closure (itself included) of a job that
                  reports a `required:` name — an aggregate gate and
                  everything it waits for, or a `kind: job` check;
      "advisory"  not that, but in the closure of a job that reports an
                  `advisory:` name.

    None is the answer this function exists for: a job that can fail, or
    never run, with nothing in `.github/required-checks.yaml` saying whether
    that matters.

    ⚠️ `needs:`, not the gates' `.result` bindings. For a gate the two are
    the same set, and `test_every_job_a_gate_waits_for_is_also_weighed` is
    what holds that; an advisory job has no bindings to read.
    """
    jobs = _load(path)["jobs"]
    required = _needs_closure(jobs, set(_reporters_of(jobs, required_names)))
    advisory = _needs_closure(jobs, set(_reporters_of(jobs, advisory_names)))
    return {job_id: ("required" if job_id in required
                     else "advisory" if job_id in advisory else None)
            for job_id in jobs}


def _unanchored_job_problems(roles: dict[tuple[str, str], str | None]) -> list[str]:
    """PURE."""
    return [
        f"{key[0]}::{key[1]} is anchored to no name in "
        ".github/required-checks.yaml: neither it nor any job that `needs:` "
        "it reports a `required:` or an `advisory:` check."
        for key in sorted(k for k, role in roles.items() if role is None)]


def _advisory_stowaway_problems(path: Path, required_names: frozenset[str],
                                advisory_names: frozenset[str]) -> list[str]:
    """Jobs only an advisory check waits for that are not part of that check
    by name. PURE over a path.

    ⛔ Why the advisory closure is not taken at face value: `needs:` on an
    advisory job is one line, and it would anchor ANY job — a test leg pulled
    out of its gate and hung there satisfies the anchoring rule with its red
    blocking nothing. What an advisory job legitimately waits for today is
    its own shards, and a shard says so in its name (`Python Coverage (1/3)`
    for `Python Coverage`), so that is the admission rule. A job in a
    required closure as well is not this rule's business: its verdict already
    blocks.
    """
    jobs = _load(path)["jobs"]
    required = _needs_closure(jobs, set(_reporters_of(jobs, required_names)))
    roots = _reporters_of(jobs, advisory_names)
    problems = []
    for root, names in sorted(roots.items()):
        for job_id in sorted(_needs_closure(jobs, {root}) - required - set(roots)):
            rendered = _rendered_names(job_id, jobs[job_id])
            if rendered is None or not all(
                    any(r.startswith(name) for name in names) for r in rendered):
                problems.append(
                    f"{path.name}::{job_id} ({jobs[job_id].get('name')!r}) is "
                    f"waited for only by {root}, which reports the advisory "
                    f"check {sorted(names)}, and its name does not start with "
                    "that check's name.")
    return problems


# ── the exit-code contract ─────────────────────────────────────────────────

# `needs.<job_id>.result`: "Possible values are success, failure, cancelled,
# or skipped" (contexts reference). The empty string is this module's own
# addition to that list, but not an undocumented one: "If you attempt to
# dereference a nonexistent property, it will evaluate to an empty string"
# (contexts reference) — a leg id misspelled in the binding renders that way,
# and including it only makes the matrix stricter.
_RESULT_DOMAIN = ("success", "failure", "cancelled", "skipped", "")


def _expected_exit(legs: tuple[str, ...]) -> int:
    """The contract (#3756): pass when every leg succeeded, fail otherwise.

    ⛔ Read this as a SPECIFICATION. It is the owner ruling on #3756 ("每支
    PR 都跑全部必要檢查，aggregate 只接受 success"), not a reading of the
    shell — an oracle copied off the implementation asserts only that the
    implementation still does what it does.

    ⛔ `skipped` is a FAILURE, and that is the half that changed. Until #3756
    this function took the detect job's result and its `*_changed` outputs and
    forgave a skip the path filter had asked for. There is no path filter
    now, so a skipped leg means something upstream of it broke; forgiving it
    is the skip-as-green defect (#1368) with nothing left to justify it.
    """
    return 0 if all(result == "success" for result in legs) else 1


# Up to this many legs the leg axis is the FULL product (125 tuples at three).
# Beyond it, a strength-3 covering array — see `_leg_tuples`.
_FULL_PRODUCT_MAX_LEGS = 3
_COVERING_STRENGTH = 3


def _bush_orthogonal_array(q: int) -> list[tuple[int, ...]]:
    """OA(q^3, q+1, q, 3) for prime q — Bush's construction.

    Row (a, b, c) is the polynomial a·x² + b·x + c evaluated at x = 0..q-1,
    plus a last column holding `a`. Any THREE columns of the result contain
    every one of the q^3 value triples exactly once, which is the property
    `_leg_tuples` needs; `q+1` is the most columns this construction gives.
    """
    assert q >= 2 and all(q % d for d in range(2, q)), "q must be prime"
    return [tuple((a * x * x + b * x + c) % q for x in range(q)) + (a,)
            for a, b, c in itertools.product(range(q), repeat=3)]


def _strength_3_rows(leg_count: int, q: int) -> set[tuple[int, ...]]:
    """Rows over `range(q)` in which every 3 positions see every value triple.

    ≤ q+1 legs: the Bush array as is (q^3 rows). More legs: the Bush rows
    extended by every value assignment of the extra columns, then a greedy
    pick of the rows covering the most still-uncovered (positions, values)
    triples until none remain. Deterministic — no randomness anywhere.
    """
    base = _bush_orthogonal_array(q)
    if leg_count <= q + 1:
        return {row[:leg_count] for row in base}
    extra = leg_count - (q + 1)
    positions = list(itertools.combinations(range(leg_count), 3))
    # Each candidate's (positions, values) triples, computed once — the
    # first cut recomputed them per greedy step and cost 10 s on CI for
    # the 7-leg control alone.
    candidates = [(row + tail,
                   frozenset((pos, tuple((row + tail)[i] for i in pos))
                             for pos in positions))
                  for row in base
                  for tail in itertools.product(range(q), repeat=extra)]
    uncovered = {(pos, vals) for pos in positions
                 for vals in itertools.product(range(q), repeat=3)}
    chosen: set[tuple[int, ...]] = set()
    while uncovered:
        best, best_cover = max(candidates,
                               key=lambda c: (len(c[1] & uncovered), c[0]))
        assert best_cover & uncovered, "greedy covering stalled"
        chosen.add(best)
        uncovered -= best_cover
    return chosen


def _leg_tuples(leg_count: int) -> list[tuple[str, ...]]:
    """The leg axis: the full product up to three legs, a strength-3 covering
    array beyond that, plus every all-equal and single-deviation tuple.

    ⛔ History, because the reasoning has flipped twice and both flips were
    measured. Round one shipped a covering design (all-equal plus every pair
    of positions) and blind review killed it: the saving in the dev container
    was 1.59 s vs 0.83 s for the 3-leg `go-tests` of the day, and the design's
    blind region was 60 of 125 tuples — including `('failure', 'skipped',
    'skipped')`, the ordinary docs-only-PR shape. The reverted docstring
    ended: "if a fourth leg ever makes this genuinely expensive, the honest
    move is to measure it again on CI."

    The fifth leg arrived (#1873 and its siblings) and CI was measured
    (#1899's `--durations`): `go-tests` at 5 legs is 5^5 = 3125 leg tuples ×
    15 detect/changed cells (an axis that left with the path gates, #3756)
    × 2 passes = 93,750 bash runs, **168 s on the
    4-core runner — 23 % of the whole Python job and its critical path**.
    That is the condition the old docstring named, so the sample is back,
    designed against the recorded blind spot rather than the old wish:

      * STRENGTH 3, not 2: any three legs, in any positions, see every one of
        the 125 value triples (Bush's OA(125, 6, 5, 3) for ≤ 6 legs; a greedy
        extension past that). `('failure', 'skipped', 'skipped')` and every
        other ≤3-leg pattern is therefore generated at every position.
      * The extremes the gate's `for` loop is really about are added
        explicitly: every all-equal tuple, and every tuple that deviates from
        all-`success` in exactly one leg (the "one leg went wrong" shape).
      * `test_leg_tuples_are_a_strength_3_covering_array` checks both
        properties by brute force, so the construction cannot rot silently.

    ⚠️ Honest boundary: a gate whose verdict depended on four or more legs
    jointly, in a way no three of them reveal, is invisible to this sample.
    The shipped gates reduce over legs with a single `for` loop (any bad leg
    → exit 1), which has no such interaction; a gate that grew one would
    need this bound raised, and the test above is where that shows.

    Sizes today: 3 legs → 125 (full), 5 legs → 125 + 20 extremes ≈ 145
    tuples instead of 3125.
    """
    if leg_count <= _FULL_PRODUCT_MAX_LEGS:
        return sorted(itertools.product(_RESULT_DOMAIN, repeat=leg_count))
    q = len(_RESULT_DOMAIN)
    rows = _strength_3_rows(leg_count, q)
    rows |= {(v,) * leg_count for v in range(q)}
    success = _RESULT_DOMAIN.index("success")
    for pos in range(leg_count):
        for v in range(q):
            row = [success] * leg_count
            row[pos] = v
            rows.add(tuple(row))
    return sorted(tuple(_RESULT_DOMAIN[i] for i in row) for row in rows)


def _cases(leg_count: int):
    """The leg covering set — the only axis a gate's verdict has."""
    yield from _leg_tuples(leg_count)


def _env_for(gate: Gate, legs: tuple[str, ...], summary: str) -> dict[str, str]:
    """A MINIMAL child env — nothing inherited that the gate might read.

    Inheriting the developer's environment is how a matrix cell quietly tests
    the wrong thing: a stray `RUN_RESULT` in the parent shell would make a row
    pass for a reason the row is not about.
    """
    env = {
        name: os.environ[name]
        for name in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME")
        if name in os.environ
    }
    for (name, _job), value in zip(gate.bindings.legs, legs):
        env[name] = value
    env.update(_simulated_runner_vars(summary))
    return env


def _simulated_runner_vars(summary_path: str) -> dict[str, str]:
    """Runner-supplied variables this harness can reproduce faithfully.

    ⛔ Small and explicit, and everything outside it is refused rather than
    guessed. Writing a line to the step summary is an ordinary thing to do in
    a gate and blocking it would be a tax on a diagnostic that cannot change
    an exit code — but the variable still has to be SUPPLIED, or the free-
    variable check is right that the matrix leaves it empty. Anything the
    harness cannot reproduce (a token, a run attempt counter) stays refused,
    because there the emptiness is the whole problem.
    """
    return {"GITHUB_STEP_SUMMARY": summary_path}


@dataclass(frozen=True)
class _Cell:
    legs: tuple[str, ...]
    rc: int
    stderr: str
    rc_runner: int      # same cell, re-run under a runner-like environment


def _run_matrix(script: str, gate: Gate) -> tuple[_Cell, ...]:
    """Execute `script` once per cell, the way GitHub would: `bash -e <file>`.

    ⛔ A temp FILE, matching the documented default shell `bash -e {0}`. The
    #1397 prototype fed the script on stdin, mistyped the path, and got "No
    such file or directory" — bash never ran the script at all, and because
    most rows of this table expect exit 1, "mostly right" looked like a
    working harness.

    The defence is structural rather than a marker string grepped out of the
    output: the contract only ever expects 0 or 1, while a script bash refuses
    to start exits 2 (parse error) or 127 (no such file), so EVERY cell
    mismatches at once.

    ⚠️ stderr IS policed, once per gate rather than once per cell — see
    `_matrix_violations`. Failing every cell that wrote to it reported 75
    near-identical violations for one added `>&2`, each saying "exit 0, which
    is correct"; the noise was the problem, not the rule. The rule itself is
    load-bearing: a gate that falls over at run time can still exit 0 (see
    `test_a_script_that_never_ran_is_not_mistaken_for_a_verdict`), and stderr
    is the only signal that separates that from a decision.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # Two script paths, so `$0` differs between the runs as well.
        plain = root / "gate.sh"
        under_runner = root / "_temp" / "runner-gate.sh"
        under_runner.parent.mkdir()
        for target in (plain, under_runner):
            target.write_text(script, encoding="utf-8", newline="\n")
        cells = list(_cases(len(gate.bindings.legs)))

        def once(legs, script_path: Path, extra: dict[str, str],
                 index: int) -> tuple[int, str]:
            # ⛔ Per-cell summary file. One shared file across 64 threads was
            # a real race the retry below would have smoothed over.
            summary = root / f"summary-{index}-{script_path.name}"
            env = _env_for(gate, legs, summary.as_posix())
            env.update(extra)
            argv = list(gate.shell_argv) + [script_path.as_posix()]
            # ⛔ Retry once when the answer falls outside the contract's range.
            # `{0,1}` IS the contract's range, so a retry cannot convert a
            # violation into a pass — but a `bash` that failed to START
            # answers 126/127, and under load on a Windows host that happens:
            # blind review measured one run of this module at `5 failed` and
            # the next, unchanged, at `19 passed`, every flaky cell a spawn
            # failure read as the gate's verdict. Noise in the FALSE-RED
            # direction, which is how a guard gets deleted.
            for attempt in (1, 2):
                done = subprocess.run(
                    argv, env=env, capture_output=True, encoding="utf-8",
                    errors="replace", timeout=60)
                if done.returncode in (0, 1) or attempt == 2:
                    break
            return done.returncode, done.stderr

        def run(indexed) -> _Cell:
            index, legs = indexed
            rc, err = once(legs, plain, {}, index)
            rc_runner, _ = once(legs, under_runner, _RUNNER_ENV, index)
            return _Cell(legs, rc, err, rc_runner)

        workers = min(64, (os.cpu_count() or 4) * 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return tuple(pool.map(run, enumerate(cells)))


# ⛔ Exact stderr text a gate is allowed to emit. Empty, and it is the seam the
# stderr message points at — an assertion message that names a mechanism must
# name one that exists. Add the literal text, never a pattern: the point is
# that a NEW noise source has to be looked at once.
ALLOWED_GATE_STDERR: frozenset[str] = frozenset()

# ⛔ THE DIFFERENTIAL RUN — the answer to four consecutive defeats.
#
# Four rounds of review each defeated the previous attempt to prove a gate
# script CANNOT reach runner state, and each fix extended an enumeration:
# `$VAR` spellings → `${#X}` / `${!X}` / `$((X))` → command substitution →
# an accept-set of commands. The fourth defeat is the one that settles it:
# `[ -v CI ]` and `[ -d /home/runner/work ]` use ONLY accept-set tokens and
# contain no `$` at all, because `[` is itself a channel to the environment
# and the filesystem. There is no last member to that list.
#
# So the question changes from "can it read runner state" (unprovable by
# inspection) to "DOES its verdict depend on anything this module does not
# vary" — which is measurable. Every cell is executed twice: once in the
# minimal environment, once with the script at a different path and the
# environment below layered on. If the two exit codes ever differ, the gate
# consulted something outside the modelled inputs, and it does not matter
# which spelling it used to do it.
#
# ⚠️ Honest limit: this catches environment- and `$0`-derived channels, not a
# probe of an absolute path that is absent on both runs (`[ -d /home/runner
# /work ]` reads False here and False again). `_TEST_OPERATORS` covers that
# side structurally, and the set of `test` unary operators — unlike the set of
# ways to read the environment — really is closed and documented.
_RUNNER_ENV = {
    "CI": "true",
    "GITHUB_ACTIONS": "true",
    "GITHUB_RUN_ATTEMPT": "2",
    "GITHUB_EVENT_NAME": "pull_request",
    "GITHUB_REF": "refs/pull/1/merge",
    "GITHUB_WORKSPACE": "/home/runner/work/repo/repo",
    "GITHUB_TOKEN": "zzsentinel",
    "RUNNER_OS": "Linux",
    "RUNNER_TEMP": "/home/runner/work/_temp",
}


def _matrix_violations(gate: Gate, cells: tuple[_Cell, ...]) -> list[str]:
    """PURE. Compare observed exit codes against the contract.

    Split out from the runner so a degenerate input can prove the comparison
    is able to say NO — a tripwire that only ever sees the real, passing gate
    is a tripwire nobody has watched fire.
    """
    problems: list[str] = []
    for cell in cells:
        want = _expected_exit(cell.legs)
        if cell.rc != cell.rc_runner:
            problems.append(
                f"{gate}: the verdict CHANGES with the environment — exit "
                f"{cell.rc} in the minimal env, {cell.rc_runner} under a "
                f"runner-like one ({sorted(_RUNNER_ENV)} plus a different "
                "script path), for the same modelled inputs "
                f"({cell.legs!r}). The gate "
                "consults something this module does not vary, so its "
                "truth table describes a run that will not happen on CI. "
                "⛔ It does not matter which spelling reached it — `$VAR`, "
                "`$((…))`, `$(cmd)`, `[ -v NAME ]` — the fix is to bind the "
                "value in the step's `env:` or stop reading it.")
        if cell.rc == want:
            continue
        bound = ", ".join(
            f"{name}={value!r}" for (name, _job), value
            in zip(gate.bindings.legs, cell.legs))
        problems.append(
            f"{gate}: {bound} -> exit {cell.rc} (contract says {want})")

    # ⛔ stderr is reported ONCE per gate, not once per cell. Failing every
    # cell that wrote to stderr produced 75 near-identical lines for one added
    # `>&2` diagnostic, each saying "exit N, which is correct" — pressure to
    # delete the diagnostic rather than to look. But it cannot be dropped
    # either: the control script `if [ "$X" = "y" ; then exit 0; fi` PARSES,
    # runs, and exits 0 because a failing `[` inside an `if` condition is not
    # an error — so the exit-code superset argument alone does not catch a
    # gate that is broken at run time. stderr is the only signal that
    # separates "decided" from "fell over".
    noisy = [cell for cell in cells
             if cell.stderr.strip() and cell.stderr.strip() not in ALLOWED_GATE_STDERR]
    if noisy:
        problems.append(
            f"{gate}: {len(noisy)} of {len(cells)} cells wrote to stderr, "
            f"e.g. {noisy[0].stderr.strip()!r}. Either the script is failing "
            "in a way its exit code hides, or the text is a deliberate "
            "diagnostic — in which case add it to `ALLOWED_GATE_STDERR` "
            "above.\n⛔ Do NOT delete the `>&2` to buy silence: a gate that "
            "falls over at run time and still exits 0 is invisible to the "
            "exit-code comparison, and this is the only thing that sees it.")
    return problems


# ═══════════════════════════════════════════════════════════════════════════
# assertions on the tree
# ═══════════════════════════════════════════════════════════════════════════

def test_gate_discovery_did_not_refuse() -> None:
    """Surface a fail-closed refusal as one failing test, not zero tests."""
    assert _CI_GATE_REFUSAL is None, str(_CI_GATE_REFUSAL)


def test_gates_were_discovered_in_every_gate_workflow() -> None:
    """Discovery is not allowed to quietly find nothing.

    ⛔ The matrix test is parametrised over the gate set: if it collapses to
    empty, pytest reports zero tests as a pass. The floor is not a count of
    gates — that would be taken from the very thing it protects — it is the
    independent source: every workflow that `.github/required-checks.yaml`
    says reports a `kind: gate` check must yield at least one discovered
    gate, and there must be such a workflow at all.
    (`test_the_required_check_names_belong_to_the_gates_and_nothing_else`
    then holds the per-name half.)
    """
    gates = _gates_or_fail()
    assert REQUIRED_CHECK_NAMES, (
        ".github/required-checks.yaml lists no `kind: gate` entry, so every "
        "assertion in this module iterates nothing. If the last aggregate "
        "gate is really gone, delete this module with it.")
    workflows = _gate_workflows()
    assert workflows, (
        f"no workflow reports any of {sorted(REQUIRED_CHECK_NAMES)} under a "
        "literal job `name:`, so no gate can be discovered.")
    empty = sorted(path.name for path in workflows
                   if not any(g.workflow == path.name for g in gates))
    assert not empty, (
        f"{empty} report a `kind: gate` required check but no aggregate gate "
        "was discovered in them: the job carrying the required name does not "
        "bind any `${{ needs.<job>.result }}` in a step's `env:`. Every "
        "assertion parametrised over the gate set silently skips those "
        "workflows.")


def test_no_job_in_a_gate_decision_path_carries_a_condition() -> None:
    """Re-adding a path `if:` to a leg — or to anything it `needs:` — is red.

    #3756 retired the path gates on the required legs. With them gone a gate
    passes only when every leg succeeded, so a leg that can be skipped by its
    own `if:` (or by an upstream job's) now fails the required check on every
    PR where the condition is false. That would surface at run time; this
    makes it surface in review, with the reason.

    No exception ledger, on purpose: when this was written no leg carried any
    non-path condition (every retired `if:` was a plain OR of
    `needs.detect-changes.outputs.*_changed == 'true'` atoms), so there is no
    retained condition to record. See `_conditional_jobs_in_decision_paths`
    for what a legitimate conditional leg would have to change first.

    The same sweep refuses a STEP of such a job whose `if:` is anything but
    `always()`: the step skips, the job still reports `success`, and the gate
    passes over a check that never ran.
    """
    gates = _gates_or_fail()
    problems: list[str] = []
    for path in _gate_workflows():
        problems += _conditional_jobs_in_decision_paths(
            path, tuple(g for g in gates if g.workflow == path.name))
    assert not problems, (
        "a job a required check waits for can be skipped by a condition:\n  "
        + "\n  ".join(problems)
        + "\nThe aggregate gates pass on `success` only (#3756), so wherever "
          "that condition is false the required check goes red — and where "
          "it is a PATH condition it is the retired mechanism coming back: a "
          "hand-kept filter that misses one input skips the leg that would "
          "have caught the break. ⛔ Remove the `if:`. Do not teach the gate "
          "to forgive the skip instead; that is the other half of the same "
          "mechanism, and `test_gate_exit_codes_match_the_contract` refuses "
          "it.\nFor a STEP-level `if:` the damage is quieter: the step "
          "skips, the job still concludes `success`, and the gate passes "
          "with the check never having run. Only `always()` is accepted "
          "there; put a real condition inside the step's shell instead, "
          "where a false branch is a decision the script makes and prints.")


def test_no_required_check_workflow_runs_a_path_filter_action() -> None:
    """`dorny/paths-filter` must not grow back into these workflows (#3756).

    Read from the parsed steps' `uses:`, not grepped: the workflows still
    MENTION the action in comments that explain why it is gone, and a grep
    cannot tell a mention from a step.

    Scope is every workflow that reports a required check of either kind —
    derived from `.github/required-checks.yaml`, so a required check added to
    another workflow brings that workflow under this rule. A workflow that
    backs no required check may still path-filter itself (the advisory ones
    do, through `on.pull_request.paths`).
    """
    workflows = _required_check_workflows()
    assert workflows, (
        "no workflow reports any required check by a literal name, so this "
        "assertion would examine nothing.")
    hits = [hit for path in workflows for hit in _path_filter_steps(path)]
    assert not hits, (
        "a workflow that reports a required check runs a path-filter "
        "action:\n  " + "\n  ".join(hits)
        + "\nRequired checks run on every PR (#3756). A path filter is a "
          "hand-kept list of what a job reads; the entry it lacks is a break "
          "that merges with the required check green. ⛔ If the job is "
          "genuinely too expensive to run on every PR, that is a decision "
          "about whether it should be REQUIRED — take it there, not by "
          "filtering it.")


def test_no_job_in_a_gate_decision_path_is_a_reusable_workflow_call() -> None:
    """A leg whose steps are in another file is a leg nothing here has read.

    See `_reusable_calls_in_decision_paths`: the `if:`, `continue-on-error`
    and path-filter rules all read the steps of the workflow that holds the
    gate, so a called workflow is outside every one of them.
    """
    gates = _gates_or_fail()
    problems: list[str] = []
    for path in _gate_workflows():
        problems += _reusable_calls_in_decision_paths(
            path, tuple(g for g in gates if g.workflow == path.name))
    assert not problems, (
        "a job a required check waits for is a reusable-workflow call:\n  "
        + "\n  ".join(problems)
        + "\nThe rules that keep a required leg from being skipped or "
          "switched off read the leg's `if:` and its steps. A job-level "
          "`uses:` has neither in this file — they are in the called "
          "workflow, where nothing in this module looks — so the leg would "
          "pass every rule unread. ⛔ Inline the steps. If a shared workflow "
          "is really wanted, teach `_decision_path` to follow local calls "
          "and apply every rule to the callee first; do not ledger it.")


def test_every_job_in_a_required_check_workflow_is_accounted_for() -> None:
    """A leg cannot leave its gate quietly (#3756).

    Removing a leg from an aggregate COMPLETELY — `needs:`, the env binding
    and the loop — satisfies every other assertion here: the gate still
    weighs exactly what it waits for. The leg keeps running, its red blocks
    nothing, and the required check reads green. While the legs were
    path-gated the retired `test_every_path_gated_leg_is_watched_by_a_gate`
    held this through the `if:`; with no `if:` left, it is held by anchoring
    EVERY job to a name in `.github/required-checks.yaml`, so there is no
    list of legs to forget to extend and no list of exceptions to outlive
    its jobs.

    Scope: every workflow that reports a required check of either kind.
    """
    workflows = _required_check_workflows()
    assert workflows, (
        "no workflow reports any required check by a literal name, so this "
        "assertion would examine nothing.")
    roles = {
        (path.name, job_id): role
        for path in workflows
        for job_id, role in _job_roles(
            path, _ALL_REQUIRED_NAMES, _ADVISORY_NAMES).items()}
    # Both anchors must occur, or one half of the rule compared nothing.
    assert {"required", "advisory"} <= set(roles.values()), set(roles.values())
    problems = _unanchored_job_problems(roles)
    for path in workflows:
        problems += _advisory_stowaway_problems(
            path, _ALL_REQUIRED_NAMES, _ADVISORY_NAMES)
    assert not problems, (
        "\n  ".join([""] + problems)
        + "\n⛔ If the job is a test leg: put it back in its gate — `needs:`, "
          "an env binding of its `.result`, and the loop. A leg outside every "
          "gate can fail on every PR with the required check green.\nIf its "
          "verdict is really meant to block nothing, give it a check name of "
          "its own and register that name under `advisory:` in "
          ".github/required-checks.yaml with the reason — the only place "
          "such a reason is kept. A Go test leg cannot take that exit "
          "(tests/ops/test_go_modules_have_a_test_leg.py).")


_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_MATRIX_REF = re.compile(r"^matrix\.([\w-]+)$")


def _rendered_names(job_id: str, job: dict) -> list[str] | None:
    """Every check-run name this job can report. None = not modellable.

    ⛔ Comparing the RAW `name:` is not enough, and this is the bypass that
    forced the function: GitHub expands `${{ }}` into the check-run name.
    Renaming a leg to `Go ${{ matrix.word }}` with
    `word: ["Tests"]` renders to exactly the required context `Go Tests`,
    while a raw-string comparison sees two different strings. (The Go legs
    used to carry `(${{ matrix.go }})`, rendering to the then-required
    `Go Tests (1.26)`.)
    """
    raw = str(job.get("name") or job_id)
    refs = _EXPRESSION.findall(raw)
    if not refs:
        return [raw]
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    options = []
    for ref in refs:
        matched = _MATRIX_REF.match(ref)
        if not matched or matched.group(1) not in matrix:
            return None
        values = matrix[matched.group(1)]
        options.append([str(v) for v in
                        (values if isinstance(values, list) else [values])])
    names = []
    for combo in itertools.product(*options):
        pending = list(combo)
        names.append(_EXPRESSION.sub(lambda _m: pending.pop(0), raw))
    return names


def _could_render_to(raw_name: str, target: str) -> bool:
    """Could `raw_name`, with every `${{ }}` free, come out as `target`?

    Each expression becomes `.*` and the literal fragments must still line up.
    `scan ${{ matrix.name }}` cannot become `Portal Tests`; `Go
    ${{ matrix.word }}` can become `Go Tests`.
    """
    # `re.split` with one capture group interleaves literals and captures, so
    # the literal fragments are the even indices.
    pattern = ".*".join(re.escape(part)
                        for part in _EXPRESSION.split(raw_name)[::2])
    return re.fullmatch(pattern, target, flags=re.S) is not None


def test_the_required_check_names_belong_to_the_gates_and_nothing_else() -> None:
    """Both directions: the gates own these names, and only the gates do.

    The first half catches a gate being renamed out of the contract. The
    second catches the dangerous inverse — a leg, or any
    other job, adopting one of these names. Branch protection matches on the
    check NAME, so whichever job carries it is the job whose verdict counts,
    and a skipped job reports Success.
    """
    gates = _gates_or_fail()
    problems = []
    for gate in gates:
        if _EXPRESSION.search(gate.check_name):
            problems.append(
                f"{gate}: the gate's `name:` contains an expression "
                f"({gate.check_name!r}). A required check's identity must not "
                "depend on run-time context.")
    owned = {gate.check_name for gate in gates}
    for missing in sorted(REQUIRED_CHECK_NAMES - owned):
        problems.append(
            f"no aggregate gate reports the required check {missing!r} any "
            "more. Branch protection still asks for it, so either a gate was "
            "renamed (update GitHub settings in the same breath) or the check "
            "is now reported by something that is not a gate. Re-check with "
            "the `gh api` command above the set, whose comment dates the pin.")

    # ⛔ Deliberately ONE direction. An earlier version also refused a gate
    # whose name was not yet in the pinned set — which makes adding a fourth
    # gate impossible, because this repo's documented order is: the gate
    # merges, reports once, and only THEN can the owner mark it required
    # (`docs/internal/iac-lint-baseline.md`, and ci.yml's own note that a new
    # required name sits permanently pending). The only way to satisfy that
    # rule was to write a name into the "branch-protection mirror" that
    # branch protection did not have — which would have made the mirror a
    # claim nobody had verified. A gate reporting a check nobody requires is
    # harmless; a required check nobody reports blocks the merge.
    gate_ids = {(gate.workflow, gate.job_id) for gate in gates}
    unrenderable: list[str] = []
    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        for job_id, job in _load(path)["jobs"].items():
            if (path.name, job_id) in gate_ids:
                continue
            rendered = _rendered_names(job_id, job)
            if rendered is None:
                # ⚠️ Unrenderable does not mean dangerous. Six existing jobs
                # across the other workflows build a name from a matrix key
                # or a `workflow_dispatch` input; refusing all of them was a
                # false red on legitimate, long-standing jobs. What matters is
                # only whether the name COULD come out as a required check, so
                # each expression is treated as a wildcard and the literal
                # parts have to be compatible.
                reachable = sorted(
                    name for name in REQUIRED_CHECK_NAMES
                    if _could_render_to(str(job.get("name") or job_id), name))
                if reachable:
                    unrenderable.append(
                        f"{path.name}::{job_id} ({job.get('name')!r}) could "
                        f"render to {reachable}")
                continue
            for name in sorted(set(rendered) & REQUIRED_CHECK_NAMES):
                problems.append(
                    f"{path.name}::{job_id} reports the required-check name "
                    f"{name!r} but is not an aggregate gate. Branch "
                    "protection matches on the NAME, across every workflow — "
                    "so whatever that job does when it skips is what it will "
                    "accept, and a skipped job reports Success.")
    if unrenderable:
        problems.append(
            "these jobs have a `name:` expression this module cannot render, "
            "so a collision with a required-check name cannot be ruled out: "
            + ", ".join(sorted(unrenderable))
            + ".\nThe cheap and safe answer is to RENAME the job so its "
            "literal fragments cannot line up with any required check. "
            "Teaching `_rendered_names` the shape also works, but only for "
            "expressions with an enumerable domain — `matrix.*` has one, a "
            "`workflow_dispatch` input does not. ⛔ Widening `_could_render_"
            "to` weakens collision detection for EVERY job.")
    assert not problems, "\n  ".join([""] + problems)


def test_every_job_a_gate_waits_for_is_also_weighed() -> None:
    """`needs:` and the gate's env bindings must name the same legs.

    ci.yml's portal gate tells the next contributor "New portal test jobs
    should join HERE (add to needs + the loop)". Adding a leg to `needs:` and
    forgetting the second half is a job the required check waits for and then
    ignores: it can fail, or skip while it was needed, and the gate never
    looks. This turns that comment into a machine check.

    ⛔ Exact equality, with nothing subtracted (#3756). There used to be a
    detect job in every gate's `needs:` that was not a leg; a `needs:`
    entry the gate does not weigh is now always a mistake.
    """
    problems = []
    for gate in _gates_or_fail():
        jobs = _load(_workflow_of(gate))["jobs"]
        awaited = set(_needs(jobs[gate.job_id]))
        weighed = {job_id for _name, job_id in gate.bindings.legs}
        for job_id in sorted(awaited - weighed):
            problems.append(
                f"{gate} waits for {gate.workflow}::{job_id} but never reads its "
                "`.result` — the gate blocks on it and then ignores its "
                "verdict.")
        for job_id in sorted(weighed - awaited):
            problems.append(
                f"{gate} reads {gate.workflow}::{job_id}'s `.result` but does not "
                "`needs:` it, so the expression renders empty regardless of "
                "what that job did.")
    assert not problems, "\n  ".join([""] + problems)


def test_every_job_that_reads_a_result_is_a_modelled_gate() -> None:
    """A job that consults another job's verdict must not be invisible.

    Reading `needs.<job>.result` is the whole shape of "a check that decides
    what another job's outcome means". In a workflow that reports a gate
    required check, a job doing that which discovery did NOT pick up is either
    a gate written in a spelling this module cannot read (the result
    interpolated into `run:` or consulted in `if:` instead of bound in a
    step's `env:`), or a real gate somebody forgot to wire — both must be a
    decision recorded here rather than silence.

    ⚠️ Simpler than its predecessor, which additionally required `always()`
    and a path-gated `needs:` before a job counted as gate-shaped. Both
    preconditions left with the path gates (#3756); what remains is the one
    that carried the meaning.
    """
    detected: set[tuple[str, str]] = set()
    modelled: set[tuple[str, str]] = set()
    for path in _gate_workflows():
        modelled |= {(path.name, gate.job_id) for gate in _gates(path)}
        for job_id, job in _load(path)["jobs"].items():
            if _READS_A_RESULT.search(json.dumps(job)):
                detected.add((path.name, job_id))

    unmodelled = detected - modelled
    expected = set(UNMODELLED_GATE_SHAPED_JOBS)
    assert unmodelled == expected, (
        "jobs that read a `.result` but that this module cannot model "
        "changed.\n"
        f"  newly unmodelled: {sorted(unmodelled - expected)}\n"
        f"  ledgered but now modelled or gone: {sorted(expected - unmodelled)}\n"
        "⛔ FIRST CHOICE: make the job modellable — bind its `${{ needs.* }}` "
        "references through the step's `env:` so discovery sees it. Only if "
        "that is genuinely impossible, add it to UNMODELLED_GATE_SHAPED_JOBS "
        "with a reason and a ticket, and understand the price: a ledgered gate "
        "is covered by NOTHING in this file — not the truth table, not the "
        "`always()` rule, not the continue-on-error ban. The ledger is an "
        "admission of blindness, not a waiver. Deleting an entry without "
        "changing the job is how a known hole becomes an unknown one.")
    assert modelled, (
        "no aggregate gate was discovered in ANY workflow that reports a "
        "`kind: gate` required check — discovery is broken, not the tree.")


# ⛔ Every spelling that READS a variable, not just `$X` and `${X}`. The first
# version matched `\$\{?(\w+)\}?` and was blind to `${#X}`, `${!X}` and
# `$((X))` — blind review walked a runner-supplied `GITHUB_RUN_ATTEMPT`
# straight through the free-variable check with `[ "$((GITHUB_RUN_ATTEMPT))"
# -gt 1 ]`, which is empty here and non-empty on every job re-run in CI.
# `test_the_free_variable_scanner_sees_the_spellings_that_defeated_it`
# pins the list.
# Everything a verdict step is allowed to declare. An accept-set, because the
# dangerous keys are the ones nobody has thought of yet.
_GATE_STEP_KEYS = frozenset({"name", "id", "env", "run", "shell", "if"})

# ⛔ `\d` is in there for `$0`. The runner passes the gate a script path under
# `/home/runner/work/_temp/…`; `[ "${0#/home/runner}" != "$0" ]` is a
# harness/CI discriminator with no variable NAME in it anywhere, and it
# survived the differential run too — both of that run's script paths are
# temp dirs, so the probe answers the same way twice. Positional parameters
# are runner-supplied inputs like any other, and a verdict has no business
# reading them.
_SHELL_VAR = re.compile(r"\$\{[#!]?([A-Za-z_0-9]\w*)|\$([A-Za-z_0-9]\w*)")
_ARITH = re.compile(r"\$\(\((.*?)\)\)", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*")
# Command substitution, excluding `$((`. A gate that shells out can read
# anything the runner exported without naming it in `env:` at all.
_COMMAND_SUB = re.compile(r"\$\((?!\()|`")
# ⛔ The shell spellings of `continue-on-error`. Enforced on the GATE SCRIPT
# only, and the narrowness is deliberate rather than lazy: `python-tests-run`
# has a live, legitimate `|| true` on its "Install amtool" step (grep guard +
# explicit empty check; the mtail one retired in #1278 D1), so the same
# rule applied across the decision path would red an existing best-effort
# install. Narrowing it to "steps that run tests" was tried twice and
# failed both times — `pytest tests/` resolves to no
# tracked file, so the obvious predicate excludes every leg whose primary
# test step names a directory. A gate job has no best-effort work by
# construction, so the rule is exact there and the leg half is recorded as a
# gap instead of shipped as a guess. → the `|| true` entry in the module
# docstring's gap list.
_EXIT_SWALLOW = re.compile(r"\|\|\s*(?:true|:)(?=\s|$|;|&)"
    r"|(?m:^\s*set\s+\+[a-z]*e)"
    r"|(?m:^\s*set\s+\+o\s+errexit)")

# Shell keywords carry no external state; they are not commands to allowlist.
_SHELL_KEYWORDS = frozenset({
    "if", "then", "elif", "else", "fi", "for", "in", "do", "done", "while",
    "until", "case", "esac", "{", "}", "!",
})
# ⛔ Everything a verdict may EXECUTE. Builtins that read only their arguments
# and the environment the harness itself supplies. Nothing that can reach the
# runner: no `printenv`, no `env`, no `grep`, no `curl`, no `jq`.
_GATE_COMMANDS = frozenset({
    "echo", "printf", "exit", "continue", "break", "return", "true", "false",
    ":", "[", "[[", "test", "set", "shift",
})
# ⛔ `test` operators a verdict may use: string and arithmetic comparison
# only. This one IS a closed enumeration and legitimately so — `man test`
# defines the whole set, unlike "ways to read the environment", which is what
# four previous enumerations tried and failed to close. Refused here:
# `-v NAME` (reads any variable with no `$` in sight) and the file tests
# `-d -f -e -r -w -x -s -h -L -p -S -b -c -g -u -k -t -N -O -G -ef -nt -ot`
# (read a filesystem the harness does not reproduce, which is the one channel
# the differential run above cannot see).
_ALLOWED_TEST_OPERATORS = frozenset({
    "-z", "-n", "-eq", "-ne", "-gt", "-ge", "-lt", "-le", "-a", "-o",
})
_TEST_OPERATORS = re.compile(r"(?<![\w-])(-[A-Za-z]{1,2})(?![\w-])")
# ⛔ A REDIRECTION reads the filesystem with no `$`, no command and no test
# operator: `if : 2>/dev/null < /home/runner/.bashrc; then exit 0; fi` was
# green against every other check here — `:` is allowlisted, the differential
# runs both passes on the same filesystem, and putting `2>/dev/null` first
# silences the stderr report that would otherwise have caught it. Fifth
# recurrence of "enumerating the ways to reach runner state has no last
# member", so this is an accept-set over TARGETS, which is closed: a redirect
# target is a file descriptor or a word, and the only word a verdict has any
# business naming is one of its own modelled bindings.
_REDIRECT = re.compile(r"(?:^|[\s;&|])\d*(?:<<?|>>?)\s*(\S+)")
def _strip_comments(script: str) -> str:
    """Drop bash comments — and ONLY bash comments.

    ⛔ This was `re.sub(r"(?m)#.*$", "", script)`, which also eats a `#` inside
    a quoted string. bash does not. One extra character in an existing echo —
    `echo "… #"; if printenv CI; then :; fi` — blinded BOTH scanners that run
    on the stripped text (`_TEST_OPERATORS` and `_command_words`) and put
    every rejected spelling back on the table: `[ -v CI ]`, `printenv`, the
    file tests. The command accept-set was introduced as the answer to "the
    ways to reach runner state have no last member"; it was bypassable with
    one character. Quote state is tracked here rather than pattern-matched,
    because that is the thing the previous version guessed at.
    """
    out, quote = [], ""
    for line in script.splitlines(keepends=True):
        kept = []
        for index, char in enumerate(line):
            if quote:
                if char == quote and (index == 0 or line[index - 1] != "\\"):
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == "#" and (not kept or kept[-1].isspace()):
                break
            kept.append(char)
        out.append("".join(kept))
        if not line.endswith("\\\n"):
            quote = ""
    return "".join(out)
_FRAGMENT = re.compile(r"\|\||&&|[;|&]|\n")


def _gate_script_problems(script: str, bound: set[str],
                          label: str) -> list[str]:
    """PURE. Everything wrong with a gate's shell TEXT.

    ⛔ Extracted because its control had re-implemented it. Blind review
    weakened the production copy four ways — `operators = []`, the strangers
    branch made dead, and so on — and the sample table stayed green every
    time, because the table was exercising a second copy of the same idea
    rather than this code. A control that does not call the thing it guards
    certifies it as watched without watching it. Same split, and the same
    reason, as `_matrix_violations` and `_unwatched_ledger_problems`.
    """
    problems: list[str] = []
    stripped = _strip_comments(script)

    redirects = sorted({
        target for target in _REDIRECT.findall(script)
        if not (target.startswith("&") or _redirect_word_is_bound(target, bound))
    })
    if redirects:
        problems.append(
            f"{label}: the gate script redirects to/from {redirects}. A "
            "redirection reads or writes the filesystem without naming a "
            "variable, running a command or using a test operator — the "
            "channel that got past every other check here — and both passes "
            "of the differential see the same filesystem, so it cannot see "
            "this either. ⚠️ A READ redirection is the dangerous half: the "
            "gate job has no `actions/checkout`, so `: < README.md || exit 0` "
            "succeeds under the harness (which runs in a checkout) and fails "
            "on CI (which does not), making the gate exit 0 for every input. "
            "A verdict may only redirect to an expansion of one of its own "
            "bindings.")

    operators = sorted(set(_TEST_OPERATORS.findall(stripped))
                       - _ALLOWED_TEST_OPERATORS)
    if operators:
        problems.append(
            f"{label}: the gate script uses the test operator(s) {operators}. "
            "`-v` reads any environment variable without a `$`, and the file "
            "tests read a filesystem this module does not reproduce — both "
            "are channels the exit-code differential cannot see, because they "
            "answer the same way in both runs. A verdict compares the results "
            "it was given; it does not probe the machine.")

    strangers = sorted(_command_words(stripped) - _GATE_COMMANDS)
    if strangers:
        problems.append(
            f"{label}: the gate script runs {strangers}. ⛔ This is an "
            "accept-set over COMMANDS, and it is the third fix for the same "
            "premise: first `$VAR` spellings, then command substitution, then "
            "`printenv GITHUB_RUN_ATTEMPT | grep -q '^[2-9]'` — no `$` "
            "anywhere, both earlier defences blind, and the gate returns 0 on "
            "every workflow re-run with a failing leg. Enumerating the ways "
            "to reach runner state has no last member; the set of commands a "
            "verdict needs does.")

    swallowed = _EXIT_SWALLOW.findall(script)
    if swallowed:
        problems.append(
            f"{label}: the gate script swallows an exit status ({swallowed}). "
            "`|| true`, `|| :` and `set +e` are the shell spellings of "
            "`continue-on-error`, which this module bans elsewhere — banning "
            "one and not the other protects the spelling rather than the "
            "property.")

    if _COMMAND_SUB.search(script):
        problems.append(
            f"{label}: the gate script uses command substitution. It can then "
            "read runner state that appears in no `env:` block, which is "
            "exactly what the free-variable check exists to prevent.")

    free = sorted({
        name for name in _shell_reads(script)
        if name not in bound and name not in _script_local_names(script)
    })
    if free:
        problems.append(
            f"{label}: the script reads {free}, which the step neither binds "
            "in `env:` nor supplies itself. The matrix leaves those empty "
            "while the runner supplies a value, so that input decides the "
            "required check and nothing tests it.\n"
            "⛔ `NAME=\"${NAME:-…}\"` does NOT answer this — it still reads "
            "the runner's value. Nor does adding the name to the step's "
            "`env:`: this module only varies the `needs.*` bindings it "
            "models. The two real answers are to teach "
            "`_simulated_runner_vars` to reproduce the value, or to move "
            "whatever needs it into a job that owns no verdict.")
    return problems


def _redirect_word_is_bound(target: str, bound: set[str]) -> bool:
    """A redirect target is acceptable only if it expands a modelled name."""
    names = _shell_reads(target)
    stripped = target.strip('"\'')
    return bool(names) and names <= bound and stripped in {
        f"${name}" for name in names} | {f"${{{name}}}" for name in names}


def _command_words(script: str) -> set[str]:
    """The first word of every command in the script.

    Deliberately crude and inclusive: split on every separator, drop comments,
    drop assignments and keywords, and treat whatever is left as a command. An
    over-read reds and gets looked at; an under-read is how the last three
    holes happened.
    """
    words: set[str] = set()
    for fragment in _FRAGMENT.split(_strip_comments(script)):
        tokens = fragment.split()
        # ⛔ `for X in …` and `while …` are NOT the same shape, and treating
        # them as one skipped a whole fragment: `while printenv CI; do exit 0;
        # done` reached the runner with nothing at the script level seeing it.
        # In `for X in list`, X is a binding and the list is words — skip the
        # header. In `while <command>` the rest IS a command — drop only the
        # keyword. (Caught by the control that this comment now justifies, on
        # the first run after it was written.)
        # ⛔ A redirection remnant is not a command. `_FRAGMENT` splits on
        # `&`, so `echo x >&2` leaves a fragment whose first word is `2`, and
        # the stranger check demanded the maintainer justify a command that
        # does not exist — while `test_gate_exit_codes_match_the_contract`
        # was, for the same edit, telling them the `>&2` diagnostic is
        # legitimate and how to register it. Two assertions in one file
        # contradicting each other, with the wrong one naming a remedy
        # (`_GATE_COMMANDS += "2"`) that would allow every fragment starting
        # with a digit.
        if tokens and tokens[0].isdigit():
            continue
        if tokens and tokens[0] == "for":
            continue
        if tokens and tokens[0] in ("while", "until"):
            tokens = tokens[1:]
        for token in tokens:
            if _SHELL_ASSIGN.match(token) or token in _SHELL_KEYWORDS:
                continue
            words.add(token)
            break
    return words
_SHELL_LOOP_VAR = re.compile(r"^\s*for\s+([A-Za-z_]\w*)\s+in\b", re.M)
_SHELL_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)=(.*)$", re.M)


def _shell_reads(script: str) -> set[str]:
    """Every variable name the script reads, across all expansion spellings.

    Inside `$(( … ))` a bare identifier is a read, so the arithmetic bodies
    are scanned separately rather than through the `$`-anchored pattern.
    """
    names = {
        match.group(1) or match.group(2)
        for match in _SHELL_VAR.finditer(script)
    }
    for body in _ARITH.findall(script):
        names |= set(_IDENT.findall(body))
    return names


def _script_local_names(script: str) -> set[str]:
    """Names the script itself supplies, so a read of them is not a free read.

    ⛔ An assignment whose right-hand side reads the SAME name does not count.
    Blind review defeated the first version with one line:

        GITHUB_EVENT_NAME="${GITHUB_EVENT_NAME:-push}"

    which looks like "the script sets this" to a rule that only matched
    `NAME=` at the start of a line — while at runtime it still reads whatever
    the runner exported, and the default is precisely what makes the harness
    take the safe branch and CI take the other one. The same edit WITHOUT the
    defaulting line was correctly caught, which is what makes this a laundering
    hole rather than a missing rule.
    """
    local = set(_SHELL_LOOP_VAR.findall(script))
    for name, rhs in _SHELL_ASSIGN.findall(script):
        if name not in _shell_reads(rhs):
            local.add(name)
    return local


def test_gate_scripts_are_executable_the_way_github_runs_them() -> None:
    """The execution model has to be true, or the matrix proves nothing.

    The simulation is `bash -e <file>` with an env dict. Everything that could
    make that unlike CI is refused here, and each item below is a bypass blind
    review actually demonstrated on this module:

      * a `shell:` override — GitHub's default for a `run:` step whose shell
        is unspecified is `bash -e {0}`, and any other value changes both the
        flags and the failure semantics (spelling `shell: bash` explicitly
        gets `bash --noprofile --norc -eo pipefail {0}`, a different command).
        ⛔ It has FOUR inputs, not one: the step, `defaults.run` on the job,
        `defaults.run` on the workflow, and the platform — `runs-on:
        windows-*` hands the block to PowerShell, and a `container:` without
        bash falls back to `sh`. `defaults.run` goes through an accept-set —
        only `shell` is modelled, so any other key including
        `working-directory` is refused, and so do the verdict step's own
        keys;
      * a `${{ }}` expression inside `run:` — the runner substitutes those
        before bash starts, so a matrix that only sets env would execute a
        different script than CI does;
      * a `$VAR` the step neither binds nor supplies itself — the matrix
        leaves it empty while the runner supplies a value;
      * ⛔ EXTRA `env:` anywhere in the job. This is an accept-set, not a list
        of dangerous names: the step's `env:` must be exactly the bindings
        this module models plus what `_simulated_runner_vars` reproduces, and
        the job and workflow must declare nothing beyond that set. The
        bypass that forced it was `BASH_ENV`, which bash sources before the
        script runs — measured, a `BASH_ENV` file containing `exit 0` makes
        the gate return 0 for a failing input with no output at all. A
        denylist of `BASH_ENV`/`ENV`/`SHELLOPTS` would have been the usual
        mistake; the accepted set is the modelled bindings and nothing else;
      * ⛔ any step BEFORE the verdict step. It can write to `$GITHUB_ENV`
        and change what the verdict reads; the harness runs that step in a
        vacuum, CI runs it inside a job. Steps AFTER the verdict are allowed —
        they can fail the job but never un-fail it;
      * command substitution, which can read the runner's environment without
        naming anything in `env:`.
    """
    problems = []
    for gate in _gates_or_fail():
        workflow = _load(_workflow_of(gate))
        job = workflow["jobs"][gate.job_id]
        steps = job.get("steps") or []
        step = steps[gate.step]
        bound_for_redirects = {n for n, _job in gate.bindings.legs}
        bound_for_redirects |= set(_simulated_runner_vars(""))
        if gate.step != 0:
            problems.append(
                f"{gate}: the verdict is step {gate.step}, not the first. A "
                "step that runs BEFORE it can rewrite what it reads through "
                "`$GITHUB_ENV`, and this module executes the verdict step in "
                "isolation. Steps AFTER the verdict are fine — they can only "
                "fail the job, never un-fail it — so move the earlier work "
                "down, or into a job that owns no required check.")
        for scope, holder in (("job", job), ("workflow", workflow)):
            extra_defaults = sorted(
                set((holder.get("defaults") or {}).get("run") or {}) - {"shell"})
            if extra_defaults:
                problems.append(
                    f"{gate}: {scope}-level `defaults.run` sets "
                    f"{extra_defaults}. `shell` is modelled; "
                    "`working-directory` moves the script somewhere this "
                    "module does not simulate.")
        # ⛔ And the same keys on the STEP. Checking only the two `defaults`
        # scopes left `working-directory:` on the verdict step itself green —
        # found by blind review, and the reason this is an accept-set over
        # step keys rather than another list of keys to refuse.
        unknown_keys = sorted(set(step) - _GATE_STEP_KEYS)
        if unknown_keys:
            problems.append(
                f"{gate}: the verdict step sets {unknown_keys}, which this "
                "module does not model. `working-directory` moves the script, "
                "`timeout-minutes` can end it without a verdict, `uses` "
                "replaces it outright. Model the key here before using it on "
                "the step that owns a required check.")
        modelled = {name for name, _job in gate.bindings.legs}
        for scope, holder in (("the verdict step", step), ("the job", job),
                              ("the workflow", workflow)):
            extra = sorted(set(holder.get("env") or {}) - modelled
                           - set(_simulated_runner_vars("")))
            if extra:
                problems.append(
                    f"{gate}: {scope} binds env {extra}, which this module "
                    "neither models nor varies — so they are untested inputs "
                    "to a required check. ⛔ `BASH_ENV` in particular is "
                    "sourced by bash before the script's first line, which is "
                    "why this is an accept-set and not a list of dangerous "
                    "names. Either the value belongs in a job that owns no "
                    "verdict, or `_simulated_runner_vars` has to learn to "
                    "reproduce it.")
        if job.get("container"):
            problems.append(
                f"{gate}: the gate job sets `container: {job['container']!r}`. "
                "GitHub falls back to `sh` when bash is absent from the image, "
                "which is not the shell this module simulates.")
        runs_on = str(job.get("runs-on", ""))
        if not runs_on.startswith("ubuntu-"):
            problems.append(
                f"{gate}: the gate job sets `runs-on: {runs_on!r}`. The "
                "default shell is platform-dependent — Windows runners get "
                "PowerShell, where this script is not even valid — so the "
                "whole exit-code matrix would be asserting about a shell that "
                "never runs it.")
        problems += _gate_script_problems(
            gate.script, bound_for_redirects, str(gate))
        if "${{" in gate.script:
            problems.append(
                f"{gate}: the gate script interpolates `${{{{ }}}}` directly "
                "instead of binding through `env:`. The runner substitutes "
                "that before bash starts, so this module would execute a "
                "different script than CI runs — and an interpolated result "
                "string is unquoted shell input besides.")
        bound = {name for name, _job in gate.bindings.legs}
        bound |= set(_simulated_runner_vars(""))
        local = _script_local_names(gate.script)
        free = sorted({
            name for name in _shell_reads(gate.script)
            if name not in bound and name not in local
        })
        if free:
            problems.append(
                f"{gate}: the script reads {free}, which the step neither "
                "binds in `env:` nor supplies itself. The matrix leaves those "
                "empty while the runner supplies a value, so that input "
                "decides the required check and nothing tests it.\n"
                "⛔ `NAME=\"${NAME:-…}\"` does NOT answer this — it still "
                "reads the runner's value.\n"
                "⛔ Nor does adding the name to the step's `env:`: this module "
                "only varies the `needs.*` bindings it models, so an extra "
                "`env:` key is refused by the accept-set above. The two real "
                "answers are to teach `_simulated_runner_vars` to reproduce "
                "the value (only honest for something the harness can "
                "actually supply, like the step summary path), or to move "
                "whatever needs it into a job that owns no verdict.")
    assert not problems, "\n  ".join([""] + problems)


_WRAPPED_EXPRESSION = re.compile(r"^\$\{\{\s*(.*?)\s*\}\}$", re.S)


def _normalised_condition(condition: str) -> str:
    """`${{ always() }}` and `always()` are the same condition.

    GitHub makes the `${{ }}` wrapper optional in `if:`, and writing it is a
    common house style. A raw string comparison called the wrapped spelling a
    violation and printed a message about branch protection — pointing the
    maintainer at the workflow when the fault was in the comparison, whose
    most obvious "fix" is deleting the condition entirely.
    """
    stripped = condition.strip()
    wrapped = _WRAPPED_EXPRESSION.match(stripped)
    return wrapped.group(1).strip() if wrapped else stripped


def test_the_gate_verdict_cannot_be_skipped() -> None:
    """Neither the gate JOB nor any of its steps may carry a real condition.

    GitHub's troubleshooting page for required status checks lists the cause
    "A job depends on a failed job" with the result "The dependent job is
    skipped and may not block merging", and its stated remedy is "Use
    `always()` with `needs` for required checks that depend on other jobs" —
    because a job skipped by a conditional "will report its status as
    'Success'".

    ⛔ Exact equality, not `"always()" in condition`. `always() && github
    .event_name != 'schedule'` contains it and still lets the gate skip itself
    into a green required check. The accepted set is one string; a richer
    condition must be modelled here first.

    ⛔ The VERDICT STEP too, and that half was missing until blind review put
    it back. Give the step that owns the verdict an `if:` and the step skips,
    the job has nothing left to fail on, and the required check reports
    success having decided nothing. The matrix below cannot see it — it
    executes the `run:` block and never looks at the condition that governs
    whether the block runs at all.

    ⚠️ ONLY the verdict step, and only conditions, and this narrowing is the
    correction of an over-reach. An earlier version refused a condition on
    ANY step of the gate job, which reds `if: failure()` on a diagnostic
    upload — a step that by definition can only run when the job has already
    failed, and cannot turn it green. Constraining what a gate job may
    contain is done in
    `test_gate_scripts_are_executable_the_way_github_runs_them`, on the one
    axis that does matter: nothing may run BEFORE the
    verdict step, because a prior step can rewrite the verdict's environment
    through `$GITHUB_ENV`.
    """
    problems = []
    for gate in _gates_or_fail():
        if _normalised_condition(gate.condition) != "always()":
            problems.append(
                f"{gate}: job `if: {gate.condition!r}` (required check "
                f"{gate.check_name!r}). ⚠️ If this looks equivalent to "
                "`always()`, it is the COMPARISON that needs teaching, not "
                "the workflow: `_normalised_condition` strips one `${{ }}` "
                "wrapper and nothing else. Do not delete the condition.")
        step = (_load(_workflow_of(gate))["jobs"][gate.job_id]["steps"])[gate.step]
        condition = _normalised_condition(str(step.get("if", "")))
        if condition and condition != "always()":
            problems.append(
                f"{gate}: the verdict step has `if: {step['if']!r}`. A skipped "
                "step is not a failing step: the job still concludes success "
                "and the required check goes green with the gate never having "
                "decided.")
    assert not problems, (
        "an aggregate gate's verdict can be skipped:\n  "
        + "\n  ".join(problems)
        + "\nA gate that skips reports Success to branch protection, which is "
          "precisely the outcome it exists to prevent. ⛔ Do not answer this "
          "by deleting the condition's PURPOSE — if a step genuinely must be "
          "conditional, it does not belong in the job that owns the verdict.")


def test_nothing_in_a_gate_decision_path_may_continue_on_error() -> None:
    """Ban the key on the gate and on every job whose result it depends on.

    Three reasons, only the first fully documented — they are kept separate on
    purpose, because collapsing them into one confident sentence is how a
    guard acquires a load-bearing lie:

      1. STEP level, documented: "When a `continue-on-error` step fails, the
         `outcome` is `failure`, but the final `conclusion` is `success`" —
         verbatim, in the CONTEXTS reference under `steps.<step_id>`, where it
         appears on both the `.conclusion` and `.outcome` rows. ⚠️ Not in
         workflow-syntax: that page's `continue-on-error` section says only
         "Prevents a job from failing when a step fails" and never mentions
         either field. The gate's `exit 1` is discarded and the job — the
         required check — concludes success.
      2. JOB level on a job the gate READS: `needs.<job>.result` is reported
         as `success` for a failed continue-on-error job. Community-reported,
         not in the docs (actions/toolkit#1739 is open about exactly this). On
         a leg that turns a failed test run into a passing required check.
      3. JOB level on the gate ITSELF: documented only as "Prevents a workflow
         run from failing when a job fails". ⛔ Whether the job's OWN check run
         then reports success is NOT documented — verified by grepping the
         published docs source, not just the rendered page: across contexts,
         workflow-syntax, status-checks, troubleshooting-required-status-checks,
         about-protected-branches and use-jobs, `continue-on-error` appears
         nowhere in the descriptions of `needs.<job>.result`, `jobs.<job>
         .result` or `job.status`, and there is no job-level equivalent of the
         step's outcome/conclusion pair. Community reports say it stays red — so do not rewrite this reason as "the required check goes
         green". The shape is refused because its effect is unknown, which is
         the fail-closed stance, and because one in-repo consumer is known:
         `scripts/tools/dx/pr_preflight.py` reads the key off every workflow
         and downgrades a red required check from FAIL to WARN when it finds
         it (`_soft_fail_check_names`).

    ⛔ Absence of the key, not falsiness. PyYAML resolves the YAML 1.1 bool
    family (`off`/`no`) to `False` while GitHub Actions does not treat those
    as booleans at all, so a truthiness test reads `continue-on-error: off` as
    disabled here and possibly enabled there. Same reasoning as
    `tests/ops/test_secret_scan_gate.py`.

    ⚠️ The decision path is the gate, the legs it weighs, and everything those
    legs transitively `needs:` (`_decision_path`). Until #3756 a sibling
    module refused the key on every path-gated leg as well; that module's
    gate-shaped half is retired, so this is the only place the rule lives.
    """
    problems: list[str] = []
    checked: set[tuple[str, str]] = set()
    for gate in _gates_or_fail():
        jobs = _load(_workflow_of(gate))["jobs"]
        in_path = {gate.job_id} | _decision_path(jobs, gate)
        for job_id in sorted(in_path):
            checked.add((gate.workflow, job_id))
            job = jobs[job_id]
            if "continue-on-error" in job:
                problems.append(
                    f"{gate.workflow}::{job_id} (decision path of {gate}) sets "
                    f"job-level `continue-on-error: "
                    f"{job['continue-on-error']!r}`")
            for step in (job.get("steps") or []):
                if "continue-on-error" in step:
                    problems.append(
                        f"{gate.workflow}::{job_id} step "
                        f"{step.get('name', step.get('uses'))!r} (decision "
                        f"path of {gate}) sets `continue-on-error: "
                        f"{step['continue-on-error']!r}`")
    assert not problems, "\n  ".join([""] + problems)

    # Anti-vacuity, scoped to what this assertion is actually about: every
    # gate and every leg it weighs must have been examined.
    missing = ({(gate.workflow, gate.job_id) for gate in _gates_or_fail()}
               | {(gate.workflow, job_id) for gate in _gates_or_fail()
                  for _name, job_id in gate.bindings.legs}) - checked
    assert not missing, (
        f"{sorted(missing)} never entered any gate's decision path, so this "
        "assertion never looked at them.")


@needs_bash
@pytest.mark.parametrize("gate", _ci_gates(), ids=str)
def test_gate_exit_codes_match_the_contract(gate: Gate) -> None:
    """Run the real gate script over the leg axis: every documented
    `needs.<job>.result` value, and the unresolved (empty) one."""
    cells = _run_matrix(gate.script, gate)
    problems = _matrix_violations(gate, cells)
    # ⛔ Counted separately. The stderr finding is about the gate, not about a
    # cell, and folding it into the cell count printed "1 of 75 cells
    # disagree" when zero cells disagreed — sending the reader to look for an
    # exit code that was never wrong.
    per_cell = [line for line in problems if "contract says" in line]
    whole_gate = [line for line in problems if line not in per_cell]
    assert not problems, (
        f"{len(per_cell)} of {len(cells)} cells disagree with the "
        f"success-only contract for {gate} (required check {gate.check_name!r})"
        + (f", plus {len(whole_gate)} whole-gate finding(s)"
           if whole_gate else "")
        + ":\n  " + "\n  ".join((per_cell[:20] + whole_gate))
        + ("\n  …" if len(per_cell) > 20 else ""))
    seen = {cell.rc for cell in cells}
    assert seen == {0, 1}, (
        f"{gate} produced exit codes {sorted(seen)} across {len(cells)} "
        "cells. Both 0 and 1 must occur — anything else means the script did "
        "not run, or the gate stopped deciding anything.")


_TICKET = re.compile(r"#\d{3,}")


def _ledger_row_problems(rows: list[tuple[str, str]]) -> list[str]:
    """PURE. A ledger row must state a substantive reason and a ticket.

    Split out because the one ledger left (`UNMODELLED_GATE_SHAPED_JOBS`) is
    empty today, so the rule would otherwise never execute —
    `test_the_ledger_row_rules_can_actually_fail` feeds it input.
    ⚠️ It checks length and a ticket reference, not whether the reason is
    TRUE — that is what a reviewer is for.
    """
    problems = []
    for label, reason in rows:
        if len(reason) < 80:
            problems.append(
                f"{label}: reason is {len(reason)} chars. A one-liner is a "
                "waiver wearing a ledger's clothes — state what the shape is, "
                "why it cannot be modelled, and what closing it would take.")
        if not _TICKET.search(reason):
            problems.append(
                f"{label}: reason names no issue (#NNNN). An entry nobody is "
                "tracking never leaves.")
    return problems


def test_every_ledger_row_states_a_reason_and_a_ticket() -> None:
    """The reason strings are the only thing standing between a ledger and a
    waiver. The other assertion on the ledger compares KEYS; this reads the
    prose."""
    rows = [(f"UNMODELLED_GATE_SHAPED_JOBS[{key!r}]", reason)
            for key, reason in UNMODELLED_GATE_SHAPED_JOBS.items()]
    problems = _ledger_row_problems(rows)
    assert not problems, "\n  ".join([""] + problems)


# ═══════════════════════════════════════════════════════════════════════════
# negative controls: prove each mechanism can say NO
# ═══════════════════════════════════════════════════════════════════════════

_SYNTHETIC_GATE = """\
echo "leg: $ZQ_LEG"
if [ "$ZQ_LEG" = "success" ]; then
  exit 0
fi
exit 1
"""


def _synthetic_workflow(directory: Path, *, leg_env: str | None,
                        leg_extra: dict | None = None,
                        upstream: dict | None = None,
                        gate_env_extra: dict | None = None) -> Path:
    """A minimal workflow with the gate shape, written under `directory`.

    Every call gets its OWN directory so the `_load` cache cannot serve a
    stale parse — the cheap alternative, calling `cache_clear()`, would also
    throw away the ci.yml parse the rest of the module shares.
    """
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(gate_env_extra or {})
    if leg_env:
        env[leg_env] = "${{ needs.zq-leg.result }}"
    leg = {"steps": [{"run": "true"}], **(leg_extra or {})}
    jobs = {"zq-leg": leg}
    if upstream is not None:
        jobs["zq-upstream"] = upstream
        leg["needs"] = "zq-upstream"
    jobs["zq-gate"] = {
        "needs": ["zq-leg"],
        "if": "always()",
        "steps": [{"name": "verify", "env": env, "run": _SYNTHETIC_GATE}],
    }
    path = directory / "zq.yml"
    path.write_text(yaml.safe_dump({"name": "zq", "jobs": jobs}, sort_keys=False),
                    encoding="utf-8", newline="\n")
    return path


def test_discovery_reads_the_env_names_instead_of_knowing_them(
        tmp_path: Path) -> None:
    """Same gate, two spellings, plus one shape that is not a gate at all.

    ⛔ The third case is the point: an implementation that simply returned
    every `if: always()` job would pass the first two. Requiring the same
    input shape to be answered DIFFERENTLY once the binding is removed is what
    a hard-coded list cannot fake.
    """
    for index, leg_env in enumerate(("RUN_RESULT", "ZQ_LEG")):
        path = _synthetic_workflow(tmp_path / f"case{index}", leg_env=leg_env)
        gates = _gates(path)
        assert [gate.job_id for gate in gates] == ["zq-gate"], (leg_env, gates)
        assert gates[0].bindings.legs == ((leg_env, "zq-leg"),)

    without_leg = _synthetic_workflow(tmp_path / "noleg", leg_env=None)
    assert _gates(without_leg) == (), (
        "a step that binds no job's result is not an aggregate gate — it "
        "decides nothing about a leg — yet discovery reported one.")


def test_a_gate_that_binds_a_job_output_is_refused(tmp_path: Path) -> None:
    """The other half of the retired mechanism cannot be re-added quietly.

    A gate forgave a skipped leg by also binding the detect job's
    `*_changed` output. Putting that binding back is refused at discovery —
    before the matrix ever runs — because the matrix varies leg results only
    and would otherwise be asserting about a script with an input it never
    moves (#3756).
    """
    path = _synthetic_workflow(
        tmp_path / "output", leg_env="ZQ_LEG",
        gate_env_extra={"ZQ_CHANGED": "${{ needs.zq-detect.outputs.zq_changed }}"})
    with pytest.raises(AssertionError, match="outputs"):
        _gates(path)


# Every spelling the step-level rule must refuse. Moved here with the rule
# from the retired path-filter guard (#3756), unchanged.
# ⛔ Pinning one of them pins a denylist containing that one: `"needs." in
# step_if` kept the suite green while a real `steps.probe.outputs.python`
# switch-off went undetected (blind review). All of them, or the pin means
# nothing.
STEP_SWITCH_OFF_SPELLINGS = (
    "steps.probe.outputs.python == 'true'",
    "needs.detect-changes.outputs.python_changed == 'true'",
    "env.X == 'true'",
    "github.event_name == 'schedule'",
    "vars.FOO == '1'",
    # ⛔ A namespace no denylist would list, because it does not exist.
    # Real spellings only ever pin a denylist that happens to contain them:
    # `startswith(("steps.","needs.","env.","github.","vars."))` refuses all
    # five above while silently accepting nine other namespaces.
    "zznever.zfield == 'zvalue'",
    # ⛔ …and these three exist because that was STILL not arbitrary enough.
    # Every member above — the invented one included — is shaped
    # `<expr> == '<literal>'`, so a predicate keyed on `"==" in step_if`
    # refuses all six and accepts these. The set has to vary along BOTH axes
    # (namespace AND syntax) or it pins a shape, not the accept-set rule.
    "contains(fromJSON(needs.detect-changes.outputs.changes), 'python')",
    "success()",
    "needs.detect-changes.outputs.python_changed",
    # ⛔ …and these two exist because THAT was still not arbitrary enough. All
    # nine above are >= 9 characters while `always()` is 8, so a predicate of
    # `len(step_if.strip()) >= 9` refused every one of them and accepted
    # `env.X`, `vars.Q` and — most total switch-off there is — `if: false`.
    # A set that varies on namespace and syntax but not LENGTH pins length.
    "false",
    "env.X",
)


def test_the_path_gate_guards_can_actually_fail(tmp_path: Path) -> None:
    """Both #3756 "it must not come back" rules, each given an input.

    Every one of these is the exact edit that restores the retired mechanism:
    a path `if:` on a leg, the same `if:` one `needs:` hop upstream, and the
    dorny step itself. And the other direction — the clean shape must not
    trip any of them, or the guards get deleted for noise.
    """
    clean = _synthetic_workflow(tmp_path / "clean", leg_env="ZQ_LEG")
    assert _conditional_jobs_in_decision_paths(clean, _gates(clean)) == []
    assert _path_filter_steps(clean) == []

    on_leg = _synthetic_workflow(
        tmp_path / "onleg", leg_env="ZQ_LEG",
        leg_extra={"if": "needs.zq-detect.outputs.zq_changed == 'true'"})
    found = _conditional_jobs_in_decision_paths(on_leg, _gates(on_leg))
    assert len(found) == 1 and "zq-leg" in found[0], found

    # ⛔ Not a path-looking condition, on purpose: the rule is the KEY, so an
    # event condition one hop upstream must be found the same way.
    upstream = _synthetic_workflow(
        tmp_path / "upstream", leg_env="ZQ_LEG",
        upstream={"if": "github.event_name == 'push'",
                  "steps": [{"run": "true"}]})
    found = _conditional_jobs_in_decision_paths(upstream, _gates(upstream))
    assert len(found) == 1 and "zq-upstream" in found[0], found

    # A job OUTSIDE every decision path may carry a condition.
    aside = _synthetic_workflow(tmp_path / "aside", leg_env="ZQ_LEG")
    doc = yaml.safe_load(aside.read_text(encoding="utf-8"))
    doc["jobs"]["zq-aside"] = {"if": "github.event_name == 'push'",
                               "steps": [{"run": "true"}]}
    aside_dir = tmp_path / "aside2"
    aside_dir.mkdir()
    aside = aside_dir / "zq.yml"
    aside.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8",
                     newline="\n")
    assert _conditional_jobs_in_decision_paths(aside, _gates(aside)) == []

    # ── step-level conditions: the accept-set is `always()` and nothing else
    # ⛔ Vacuity tripwires on the spellings themselves (carried over with the
    # rule): DISTINCT, at least one SHORTER than `always()`, and not all one
    # syntactic shape — or the loop pins a namespace, a length or `==`
    # instead of the accept-set.
    assert len(set(STEP_SWITCH_OFF_SPELLINGS)) >= 11
    assert min(len(s) for s in STEP_SWITCH_OFF_SPELLINGS) < len("always()")
    assert sum("==" not in s for s in STEP_SWITCH_OFF_SPELLINGS) >= 3
    for index, spelling in enumerate(STEP_SWITCH_OFF_SPELLINGS):
        gated_step = _synthetic_workflow(
            tmp_path / f"step{index}", leg_env="ZQ_LEG",
            leg_extra={"steps": [{"name": "zq-test", "if": spelling,
                                  "run": "true"}]})
        found = _conditional_jobs_in_decision_paths(gated_step,
                                                    _gates(gated_step))
        assert len(found) == 1 and "zq-test" in found[0], (
            f"a step gated on {spelling!r} was accepted — the predicate has "
            f"reverted to a denylist, and a denylist has no last member: "
            f"{found}")
    # YAML reads a bare `false` as a boolean, the most total switch-off there
    # is; it must be refused in that form too, not only as the string.
    as_bool = _synthetic_workflow(
        tmp_path / "stepbool", leg_env="ZQ_LEG",
        leg_extra={"steps": [{"name": "zq-test", "if": False, "run": "true"}]})
    assert _conditional_jobs_in_decision_paths(as_bool, _gates(as_bool))
    # ⛔ …and `always()` must still be ACCEPTED, in both spellings. It only
    # WIDENS; refusing it would red every honest workflow (the legs upload
    # their artifacts under it).
    for index, spelling in enumerate(("always()", "${{ always() }}")):
        widened = _synthetic_workflow(
            tmp_path / f"always{index}", leg_env="ZQ_LEG",
            leg_extra={"steps": [{"name": "zq-upload", "if": spelling,
                                  "run": "true"}]})
        assert _conditional_jobs_in_decision_paths(
            widened, _gates(widened)) == [], spelling

    for index, uses in enumerate((
            "dorny/paths-filter@0e4a8c6effa4802afeda77dc8d303f8176d7dfad",
            "Dorny/Paths-Filter@v3", "tj-actions/changed-files@v45")):
        directory = tmp_path / f"filter{index}"
        directory.mkdir()
        path = directory / "zq.yml"
        path.write_text(yaml.safe_dump({"jobs": {
            "zq-detect": {"steps": [{"name": "Filter", "uses": uses}]},
            # A MENTION in a run script or a comment-like string is not a step.
            "zq-talk": {"steps": [{"run": "echo dorny/paths-filter is gone"}]},
        }}), encoding="utf-8", newline="\n")
        hits = _path_filter_steps(path)
        assert len(hits) == 1 and "zq-detect" in hits[0], (uses, hits)


def test_the_path_filter_list_is_the_tools_and_not_a_copy(tmp_path: Path) -> None:
    """One list of path-filter actions, read from the tool (#3756).

    This module used to keep its own, and the two disagreed: a
    `tj-actions/changed-files` step was red here and rc 0 for
    `workflow_path_filter.py generate --check`; `Dorny/Paths-Filter` — which
    GitHub runs — was red here and rc 0 there. A second list reintroduced in
    this file fails the first assertion; the loop holds that the two callers
    reach one conclusion per spelling, hits and misses alike.
    """
    assert "_PATH_FILTER_ACTIONS" not in globals(), (
        "this module has its own path-filter action list again; read "
        "`workflow_path_filter.PATH_FILTER_ACTIONS` through "
        "`_WPF.path_filter_steps` instead")
    assert _WPF.PATH_FILTER_ACTIONS, "the tool's list is empty"
    spellings = [f"{action}@v1" for action in _WPF.PATH_FILTER_ACTIONS]
    spellings += [s.upper() for s in spellings]
    spellings += [f"docker://ghcr.io/{_WPF.PATH_FILTER_ACTIONS[0]}:v3",
                  "actions/checkout@v6", "zq/paths-filter-not@v1", ""]
    for index, uses in enumerate(spellings):
        directory = tmp_path / f"spelling{index}"
        directory.mkdir()
        path = directory / "zq.yml"
        step = {"name": "zq-step", "run": "true"}
        if uses:
            step = {"name": "zq-step", "uses": uses}
        path.write_text(yaml.safe_dump({"jobs": {"zq-job": {"steps": [step]}}}),
                        encoding="utf-8", newline="\n")
        tool = _WPF.path_filter_steps(_load(path))
        assert bool(_path_filter_steps(path)) == bool(tool), (uses, tool)
        expected = any(a in uses.lower() for a in _WPF.PATH_FILTER_ACTIONS)
        assert bool(tool) == expected, (uses, tool)
    # Both outcomes occurred, or the loop compared nothing with nothing.
    assert sum(any(a in s.lower() for a in _WPF.PATH_FILTER_ACTIONS)
               for s in spellings) >= 4
    assert "actions/checkout@v6" in spellings


def test_the_accounting_and_reusable_call_guards_can_actually_fail(
        tmp_path: Path) -> None:
    """`_job_roles`, `_unanchored_job_problems`, `_advisory_stowaway_problems`
    and `_reusable_calls_in_decision_paths`, each given the input it must
    refuse and the one it must leave alone."""
    required = frozenset({"ZQ Gate", "ZQ Required"})
    advisory = frozenset({"ZQ Advisory"})
    doc = yaml.safe_load(_synthetic_workflow(
        tmp_path / "base", leg_env="ZQ_LEG",
        upstream={"steps": [{"run": "true"}]}).read_text(encoding="utf-8"))
    doc["jobs"]["zq-gate"]["name"] = "ZQ Gate"
    doc["jobs"]["zq-direct"] = {"name": "ZQ Required",
                                "steps": [{"run": "true"}]}
    doc["jobs"]["zq-matrix"] = {
        "name": "ZQ ${{ matrix.word }}",
        "strategy": {"matrix": {"word": ["Required"]}},
        "steps": [{"run": "true"}]}
    # An advisory check and its shards: the shards' rendered names
    # (`ZQ Advisory (1/2)`) are not in the file — only `needs:` anchors them.
    doc["jobs"]["zq-shard"] = {
        "name": "ZQ Advisory (${{ matrix.shard }}/2)",
        "strategy": {"matrix": {"shard": [1, 2]}},
        "steps": [{"run": "true"}]}
    doc["jobs"]["zq-combine"] = {"name": "ZQ Advisory", "needs": "zq-shard",
                                 "steps": [{"run": "true"}]}
    doc["jobs"]["zq-stray"] = {"steps": [{"run": "true"}]}

    def written(label: str) -> Path:
        directory = tmp_path / label
        directory.mkdir()
        path = directory / "zq.yml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8",
                        newline="\n")
        return path

    path = written("roles")
    found = _job_roles(path, required, advisory)
    assert found == {"zq-leg": "required", "zq-upstream": "required",
                     "zq-gate": "required", "zq-direct": "required",
                     "zq-matrix": "required", "zq-shard": "advisory",
                     "zq-combine": "advisory", "zq-stray": None}, found
    assert _advisory_stowaway_problems(path, required, advisory) == []
    roles = {("zq.yml", job_id): role for job_id, role in found.items()}
    missing = _unanchored_job_problems(roles)
    assert len(missing) == 1 and "zq-stray" in missing[0], missing
    del roles[("zq.yml", "zq-stray")]
    assert _unanchored_job_problems(roles) == []
    # The file is what anchors: with the advisory name gone from it, the
    # combine job and its shards are anchored to nothing.
    unlisted = _job_roles(path, required, frozenset())
    assert unlisted["zq-combine"] is None and unlisted["zq-shard"] is None

    # ⛔ The edit this exists for: a leg taken out of its gate entirely. Every
    # job is still there, the gate still weighs all it waits for — and the
    # dropped leg is anchored to nothing.
    doc["jobs"]["zq-second"] = {"name": "ZQ Second",
                                "steps": [{"run": "true"}]}
    doc["jobs"]["zq-gate"]["needs"] = ["zq-leg", "zq-second"]
    doc["jobs"]["zq-gate"]["steps"][0]["env"]["ZQ_SECOND"] = (
        "${{ needs.zq-second.result }}")
    assert _job_roles(written("second-kept"), required,
                      advisory)["zq-second"] == "required"
    doc["jobs"]["zq-gate"]["needs"] = ["zq-leg"]
    del doc["jobs"]["zq-gate"]["steps"][0]["env"]["ZQ_SECOND"]
    assert _job_roles(written("second-dropped"), required,
                      advisory)["zq-second"] is None
    # ⛔ …and the same leg hung on the advisory job instead. The closure
    # anchors it; the stowaway rule is what refuses it, by name.
    doc["jobs"]["zq-combine"]["needs"] = ["zq-shard", "zq-second"]
    path = written("second-stowed")
    assert _job_roles(path, required, advisory)["zq-second"] == "advisory"
    stowed = _advisory_stowaway_problems(path, required, advisory)
    assert len(stowed) == 1 and "zq-second" in stowed[0], stowed
    # A job the gate ALSO waits for is no stowaway: its verdict blocks.
    doc["jobs"]["zq-combine"]["needs"] = ["zq-shard", "zq-leg"]
    assert _advisory_stowaway_problems(written("shared"), required,
                                       advisory) == []
    # A name this module cannot render is refused rather than assumed.
    doc["jobs"]["zq-combine"]["needs"] = ["zq-shard"]
    doc["jobs"]["zq-shard"]["name"] = "ZQ Advisory ${{ inputs.zq }}"
    unrendered = _advisory_stowaway_problems(written("unrendered"), required,
                                             advisory)
    assert len(unrendered) == 1 and "zq-shard" in unrendered[0], unrendered

    # ── reusable-workflow calls in a decision path
    clean = _synthetic_workflow(tmp_path / "clean", leg_env="ZQ_LEG")
    assert _reusable_calls_in_decision_paths(clean, _gates(clean)) == []
    for index, (where, uses) in enumerate((
            ("leg", "./.github/workflows/zq-callee.yaml"),
            ("leg", "zq-owner/zq-repo/.github/workflows/zq.yml@v1"),
            ("upstream", "./.github/workflows/zq-callee.yaml"))):
        if where == "leg":
            called = _synthetic_workflow(tmp_path / f"call{index}",
                                         leg_env="ZQ_LEG")
            doc = yaml.safe_load(called.read_text(encoding="utf-8"))
            doc["jobs"]["zq-leg"] = {"uses": uses}
            expect = "zq-leg"
        else:
            called = _synthetic_workflow(tmp_path / f"call{index}",
                                         leg_env="ZQ_LEG",
                                         upstream={"uses": uses})
            doc = yaml.safe_load(called.read_text(encoding="utf-8"))
            expect = "zq-upstream"
        directory = tmp_path / f"called{index}"
        directory.mkdir()
        path = directory / "zq.yml"
        path.write_text(yaml.safe_dump(doc, sort_keys=False),
                        encoding="utf-8", newline="\n")
        found_calls = _reusable_calls_in_decision_paths(path, _gates(path))
        assert len(found_calls) == 1 and expect in found_calls[0], found_calls
        assert uses in found_calls[0], found_calls
    # A call OUTSIDE every decision path is not this rule's business, and a
    # STEP-level `uses:` (an action) is not a reusable-workflow call.
    aside = _synthetic_workflow(
        tmp_path / "aside-call", leg_env="ZQ_LEG",
        leg_extra={"steps": [{"uses": "actions/checkout@v6"}]})
    doc = yaml.safe_load(aside.read_text(encoding="utf-8"))
    doc["jobs"]["zq-aside"] = {"uses": "./.github/workflows/zq-callee.yaml"}
    directory = tmp_path / "aside-call2"
    directory.mkdir()
    path = directory / "zq.yml"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8",
                    newline="\n")
    assert _reusable_calls_in_decision_paths(path, _gates(path)) == []


def test_the_ledger_row_rules_can_actually_fail() -> None:
    """Both rules, on synthetic rows, because the real ledger is empty."""
    good = ("row", "the job interpolates a matrix-indexed result that cannot "
                   "be bound in env without a rewrite; tracked in #1234, "
                   "closes when the matrix is flattened")
    assert _ledger_row_problems([good]) == []
    short = _ledger_row_problems([("row", "see #1234")])
    assert short and "chars" in short[0], short
    no_ticket = _ledger_row_problems([("row", "x" * 100)])
    assert no_ticket and "names no issue" in no_ticket[0], no_ticket


def test_the_free_variable_scanner_sees_the_spellings_that_defeated_it() -> None:
    """Samples in both directions, because the first version had neither.

    The free-variable check is the only thing standing between the exit-code
    matrix and a gate that consults runner state the matrix never varies, and
    it was defeated twice — once by `NAME="${NAME:-x}"` (an assignment that
    still reads the runner's value) and once by `$((NAME))` (a spelling the
    pattern could not see). Both were found by blind review reading ci.yml,
    not by anything here, because nothing here fed the scanner an input.
    """
    reads = [
        ('echo "$ZQA"', "ZQA"),
        ('echo "${ZQB}"', "ZQB"),
        ('echo "${ZQC:-fallback}"', "ZQC"),
        ('echo "${ZQD##*/}"', "ZQD"),
        ('echo "${#ZQE}"', "ZQE"),
        ('echo "${!ZQF}"', "ZQF"),
        ('if [ "$((ZQG))" -gt 1 ]; then :; fi', "ZQG"),
        ('echo "$(( ZQH + 1 ))"', "ZQH"),
    ]
    missed = [name for script, name in reads
              if name not in _shell_reads(script)]
    assert not missed, (
        f"_shell_reads does not see {missed}. Every miss is a variable a gate "
        "can consult while the exit-code matrix leaves it empty.")

    # And the other direction: it must not invent reads, or every gate reds.
    quiet = ['echo "plain text"', "echo '$notinsinglequotes'",
             'echo "100 dollars"']
    # (single quotes are not modelled — bash would not expand there — so this
    # case documents a known over-read rather than asserting it away.)
    assert _shell_reads(quiet[0]) == set(), _shell_reads(quiet[0])
    assert _shell_reads(quiet[2]) == set(), _shell_reads(quiet[2])
    assert _shell_reads(quiet[1]) == {"notinsinglequotes"}, (
        "the scanner is expected to over-read inside single quotes; if that "
        "changed, the comment above needs to change with it.")

    # The laundering shape, end to end.
    laundered = 'ZQI="${ZQI:-push}"\nif [ "$ZQI" = "x" ]; then exit 0; fi\n'
    assert "ZQI" not in _script_local_names(laundered), (
        "an assignment whose right-hand side reads the same name was accepted "
        "as the script supplying it.")
    honest = 'ZQJ=0\nif [ "$ZQJ" -eq 0 ]; then exit 0; fi\n'
    assert "ZQJ" in _script_local_names(honest), (
        "a genuine local assignment was rejected, which would red every gate "
        "that uses a counter.")


def test_an_unparsable_needs_binding_refuses_rather_than_absorbs(
        tmp_path: Path) -> None:
    """Fail-closed on env shapes the parser does not model.

    `${{ needs.zq-leg.result == 'success' }}` is a perfectly ordinary thing to
    write and this module cannot evaluate it. Silently ignoring it would drop
    the gate out of every assertion above while the job still looks like a
    gate to a reader.
    """
    directory = tmp_path / "weird"
    directory.mkdir()
    workflow = {
        "jobs": {
            "zq-gate": {
                "if": "always()",
                "steps": [{"env": {
                    "L": "${{ needs.zq-leg.result == 'success' }}",
                }, "run": "true"}],
            },
        },
    }
    path = directory / "zq.yml"
    path.write_text(yaml.safe_dump(workflow), encoding="utf-8", newline="\n")
    with pytest.raises(AssertionError, match="does not parse"):
        _gates(path)


@needs_bash
def test_the_matrix_comparison_can_actually_fail(tmp_path: Path) -> None:
    """Feed the real runner a gate that is wrong, and a gate that is right.

    Both directions. An only-negative control stays green when a checker
    starts rejecting everything; an only-positive control stays green when it
    stops rejecting anything.
    """
    gate, = _gates(_synthetic_workflow(tmp_path / "ok", leg_env="ZQ_LEG"))

    assert _matrix_violations(gate, _run_matrix(_SYNTHETIC_GATE, gate)) == [], (
        "the reference gate script does not satisfy the contract this module "
        "asserts — the oracle, not the tree, is wrong.")

    # ⛔ The #3756 revert, spelled the way it would be written: forgive a
    # skipped leg again. With no `*_changed` binding left to consult (that is
    # refused at discovery), this is the whole of what "the aggregate accepts
    # `skipped` again" can look like.
    lenient = _SYNTHETIC_GATE.replace(
        'exit 1\n',
        'if [ "$ZQ_LEG" = "skipped" ]; then\n  exit 0\nfi\nexit 1\n')
    assert lenient != _SYNTHETIC_GATE, "the mutation did not apply"
    problems = _matrix_violations(gate, _run_matrix(lenient, gate))
    assert problems and all("'skipped'" in line for line in problems), (
        "a gate that passes a SKIPPED leg must violate the contract, on "
        f"exactly the skipped cell — skip-as-green would go unnoticed: "
        f"{problems}")

    # And the unresolved result, which a `!= failure` style gate lets through.
    denylist = ('if [ "$ZQ_LEG" = "failure" ] || [ "$ZQ_LEG" = "cancelled" ]; '
                'then\n  exit 1\nfi\nexit 0\n')
    assert _matrix_violations(gate, _run_matrix(denylist, gate)), (
        "a gate written as a denylist of bad results passes `skipped` and "
        "the empty string, and produced no violation.")

    assert _matrix_violations(gate, _run_matrix("exit 0\n", gate)), (
        "a gate that always exits 0 produced no violation.")


@needs_bash
def test_a_script_that_never_ran_is_not_mistaken_for_a_verdict(
        tmp_path: Path) -> None:
    """The harness must not read "bash could not start it" as a decision.

    An unparsable script exits non-zero, which is the expected value for most
    rows of this table — the failure mode the #1397 prototype hit. The defence
    is that the contract only ever expects 0 or 1 while bash answers 2 for a
    parse error and 127 for a missing file, so every cell mismatches at once.
    Asserted here rather than assumed.
    """
    gate, = _gates(_synthetic_workflow(tmp_path / "syntax", leg_env="ZQ_LEG"))

    cells = _run_matrix('if [ "$ZQ_LEG" = "success" ; then exit 0; fi\n',
                        gate)
    # ⚠️ This script PARSES. `[ "$X" = "y"` with no `]` fails at run time, and
    # a failing command in an `if` condition is not an error under `set -e`,
    # so the `if` is simply false and the script exits 0 — the exit-code
    # superset argument does NOT catch it. Measured while writing this test,
    # which is why the stderr report was kept.
    assert {cell.rc for cell in cells} == {0}, (
        "the premise of this control changed; re-derive what it demonstrates "
        f"before trusting it (saw {sorted({c.rc for c in cells})}).")
    assert _matrix_violations(gate, cells), (
        "a gate that fell over at run time and exited 0 was accepted as a "
        "verdict — the stderr report is the only thing that sees this.")

    # And the other shape: a genuine parse error, where the exit code alone
    # is enough because 2 is outside the contract's range.
    broken = _run_matrix('if [ "$ZQ_LEG" = "success" ]; then\n', gate)
    assert not ({cell.rc for cell in broken} & {0, 1}), (
        f"a parse error answered {sorted({c.rc for c in broken})}; if bash "
        "ever reports 0 or 1 for a script it refused to run, the exit-code "
        "defence stops separating 'never ran' from 'decided'.")
    assert len(_matrix_violations(gate, broken)) > len(broken), (
        "every cell of an unrunnable script should mismatch, plus the stderr "
        "report.")


# ⛔ THE CONTROLS BELOW EXIST BECAUSE 29 OF 41 HELPER WEAKENINGS SURVIVED.
#
# Round-4 blind review weakened one helper at a time and re-ran: `_RUNNER_ENV
# = {}`, `rc_runner = rc`, `_TEST_OPERATORS` never matching, `_command_words`
# returning the empty set, `_normalised_condition` returning `"always()"`,
# `_leg_tuples` collapsing to two values, `_rendered_names` returning the raw
# string — all green. Four of them were then shown EXPLOITABLE: with the
# helper weakened, a real ci.yml edit that the pristine guard reds went green
# (a gate passing a FAILED leg; a leg taking a required check's name; a step
# inserted before the verdict; `always() && …`).
#
# Every one of those mechanisms had been added in response to a finding, and
# not one had an input that made it say NO. This module's own words, written
# about `_matrix_violations`: "a tripwire that only ever sees the real,
# passing gate is a tripwire nobody has watched fire."
#
# ⚠️ STILL UNCONTROLLED, stated rather than left to be discovered. The
# fail-closed REFUSAL clauses can each be made permissive with the suite
# green: the multi-step and unknown-shell raises in `_gates`,
# `_resolved_shell`, and the `_TICKET` pattern's shape. (The `_MENTIONS_NEEDS`
# raise IS controlled, by
# `test_an_unparsable_needs_binding_refuses_rather_than_absorbs` and
# `test_a_gate_that_binds_a_job_output_is_refused`.) A refusal that stops
# refusing hides a shape rather than passing a failing test, and the controls
# below are the template for closing them.
#
# ⚠️ And `_synthetic_workflow` can only build a ONE-leg gate, so the multi-leg
# loop that `go-tests` actually uses is exercised only against the real tree.
# That is why the leg-domain weakening below had to be caught by asserting on
# `_leg_tuples` directly rather than through a synthetic gate.

def test_the_environment_differential_can_actually_fail(tmp_path: Path) -> None:
    """The whole point of running every cell twice, given an input that diverges.

    ⛔ Nothing exercised this. `rc_runner` appeared five times in the module
    and zero times below the negative-controls divider, so `_RUNNER_ENV = {}`,
    `rc_runner = rc`, and "run the second pass with the same env and the same
    path" were all silent no-ops. The mechanism was correct and unattended,
    which is the harder failure to see: it is dead code that reads as defence.

    ⚠️ On today's tree every spelling that reaches runner state is ALSO caught
    by the free-variable, test-operator or command checks — blind review could
    not construct an attack only the differential sees. Its value is against
    the spelling nobody has thought of yet, which is exactly the case no other
    assertion can cover, and exactly why it needs a control of its own.
    """
    gate, = _gates(_synthetic_workflow(tmp_path / "diff", leg_env="ZQ_LEG"))

    # Reads CI, which the harness never binds and `_RUNNER_ENV` sets.
    peeking = 'if [ -n "$CI" ]; then exit 0; fi\n' + _SYNTHETIC_GATE
    problems = _matrix_violations(gate, _run_matrix(peeking, gate))
    assert any("CHANGES with the environment" in line for line in problems), (
        "a gate whose verdict flips when runner variables are present produced "
        "no environment finding — the second pass is not varying the "
        "ENVIRONMENT.\n"
        "⚠️ Scope, because an earlier version of this message claimed more "
        "than it checks: this control probes `$CI` only, so it holds the env "
        "half. The second script PATH is varied too, but both paths are temp "
        "directories, so a probe for a literal `/home/runner` prefix answers "
        "the same way twice — that side is held by `_shell_reads` seeing "
        "`$0`, not by this differential.")

    # And the other direction: the reference gate must NOT trip it, or every
    # gate reds and the mechanism gets deleted for noise.
    clean = _matrix_violations(gate, _run_matrix(_SYNTHETIC_GATE, gate))
    assert not any("CHANGES with the environment" in line for line in clean), (
        f"the reference gate tripped the environment differential: {clean}")


# (script, must the script-level checks reject it?) — the enumeration layers
# that four consecutive rounds of review each defeated, each with an input.
_SCRIPT_SAMPLES = (
    ('if [ -v CI ]; then exit 0; fi\n', True, "test operator reads env"),
    ('if [ -d /home/runner/work ]; then exit 0; fi\n', True, "file test"),
    ('if [ "${0#/home/runner}" != "$0" ]; then exit 0; fi\n', True, "$0"),
    ('if [ "$((GITHUB_RUN_ATTEMPT))" -gt 1 ]; then exit 0; fi\n', True,
     "arithmetic expansion"),
    ('if [ "${#GITHUB_TOKEN}" -gt 0 ]; then exit 0; fi\n', True, "length"),
    ('if [ "${!ZQ_IND}" = "x" ]; then exit 0; fi\n', True, "indirection"),
    ('if printenv CI; then exit 0; fi\n', True, "stranger command"),
    ('while printenv CI; do exit 0; done\n', True, "stranger in a header"),
    ('if [ "$(printenv CI)" = "true" ]; then exit 0; fi\n', True,
     "command substitution"),
    ('ZQ_X="${ZQ_X:-push}"\nif [ "$ZQ_X" = "y" ]; then exit 0; fi\n', True,
     "self-referencing assignment launders a runner variable"),
    (': < README.md || exit 0\n', True,
     "READ redirection: succeeds in a checkout, fails in the gate job"),
    ('echo "x #"; if printenv CI; then exit 0; fi\n', True,
     "a # inside quotes used to blind the comment-stripping scanners"),
    ('echo "x #"; [ -v CI ] && exit 0\n', True, "same, hiding an operator"),
    ('if : 2>/dev/null < /home/runner/.bashrc; then exit 0; fi\n', True,
     "redirection probes the filesystem with no $, command or operator"),
    ('echo hi > /tmp/zq\n', True, "redirect to a literal path"),
    ('exit 0 || true\n', True, "|| true"),
    ('set +ex\nexit 0\n', True, "set +ex"),
    ('set +o errexit\nexit 0\n', True, "set +o errexit"),
    # Legitimate shapes: rejecting these is how a guard gets deleted.
    ('echo "detect: $ZQ_DETECT"\nexit 0\n', False, "plain echo"),
    ('if [ "$ZQ_LEG" = "success" ]; then exit 0; fi\n', False, "bound read"),
    ('if [ -z "$ZQ_LEG" ]; then exit 1; fi\n', False, "-z is comparison"),
    ('bad=0\nfor r in "$ZQ_LEG"; do bad=1; done\nexit "$bad"\n', False,
     "loop variable and a genuine local"),
    ('echo "hi" >> "$GITHUB_STEP_SUMMARY"\nexit 0\n', False,
     "step summary is reproduced by the harness"),
    ('echo "gate evaluating" >&2\nexit 0\n', False,
     "an fd dup is not a command named 2 (the only redirection sample used to "
     "be `>>`, which has no `&`)"),
)


def test_the_script_level_checks_reject_what_defeated_them(
        tmp_path: Path) -> None:
    """Both directions over every spelling that has ever got through.

    ⛔ `_TEST_OPERATORS`, `_command_words`, `_COMMAND_SUB`, `_EXIT_SWALLOW` and
    the `$0` half of `_shell_reads` appeared ZERO times below the
    negative-controls divider. Each could be made to never match with the
    whole suite green — including the `0-9` in `_shell_reads`, added one
    commit earlier precisely so `$0` would be seen, whose eight existing
    samples contain no `$0`. A pattern widened without widening its samples
    is a pattern nobody is holding.
    """
    gate, = _gates(_synthetic_workflow(tmp_path / "scripts", leg_env="ZQ_LEG"))
    # `ZQ_DETECT` is no gate binding any more; it stays bound here only so the
    # "plain echo" sample keeps testing an echo of a BOUND name.
    bound = {"ZQ_DETECT", *(name for name, _job in gate.bindings.legs),
             *_simulated_runner_vars("")}

    problems = []
    for script, must_reject, why in _SCRIPT_SAMPLES:
        rejected = bool(_gate_script_problems(script, bound, "zq"))
        if rejected != must_reject:
            problems.append(
                f"{'accepted' if must_reject else 'rejected'} "
                f"{script!r} ({why})")
    assert not problems, (
        "the script-level checks disagree with their samples:\n  "
        + "\n  ".join(problems)
        + "\nA sample that flips here is a spelling that either reaches the "
          "runner unseen, or reds a maintainer doing something ordinary.")


def test_every_simulated_runner_var_is_visible_to_the_differential() -> None:
    """A value the harness supplies but never varies is a blind channel.

    ⛔ Adding one constant key to `_simulated_runner_vars` — say
    `GITHUB_BASE_REF: ""` — makes `if [ "$GITHUB_BASE_REF" = "main" ]; then
    exit 0; fi` bind cleanly AND read the same in both passes of the
    differential, so the gate returns 0 for every pull request against main
    with the whole suite green. Measured. That is the same "helper weakened,
    a red ci.yml edit goes green" shape as the four exploitable ones, not the
    lesser tier an earlier version of the comment above claimed.

    The invariant: anything the harness supplies must either be varied by
    `_RUNNER_ENV`, or take a different value on the two passes (which is why
    the step summary path is per-cell and per-script rather than shared).
    """
    first = _simulated_runner_vars("/zq/a")
    second = _simulated_runner_vars("/zq/b")
    blind = sorted(
        name for name, value in first.items()
        if name not in _RUNNER_ENV and second.get(name) == value)
    assert not blind, (
        f"{blind} are supplied to the gate but identical on both passes of "
        "the differential and absent from _RUNNER_ENV, so a gate branching "
        "on them is invisible. Either vary the value per pass, or add the "
        "name to _RUNNER_ENV.")


@pytest.mark.parametrize("leg_count", [4, 5, 6, 7],
                         ids=lambda n: f"{n}-legs")
def test_leg_tuples_are_a_strength_3_covering_array(leg_count: int) -> None:
    """Brute-force the two properties `_leg_tuples` promises past three legs.

    ⛔ The first covering design died because its blind region was asserted
    in prose and measured only after the fact (60 of 125 tuples, not the
    claimed 24). This is the measurement, run every time: every choice of
    three positions must see every value triple — which is precisely the
    3-leg product the old design missed, at every position — and the two
    families of extremes must be present verbatim. 7 legs exercises the
    greedy extension past Bush's 6-column limit.
    """
    tuples = set(_leg_tuples(leg_count))
    q = len(_RESULT_DOMAIN)
    all_triples = set(itertools.product(_RESULT_DOMAIN, repeat=3))
    for positions in itertools.combinations(range(leg_count), 3):
        seen = {tuple(t[i] for i in positions) for t in tuples}
        assert seen == all_triples, (
            f"legs {positions} miss {sorted(all_triples - seen)[:5]}… "
            f"({len(all_triples - seen)} of {len(all_triples)} triples)")
    for value in _RESULT_DOMAIN:
        assert (value,) * leg_count in tuples, value
    for pos in range(leg_count):
        for value in _RESULT_DOMAIN:
            row = ["success"] * leg_count
            row[pos] = value
            assert tuple(row) in tuples, (pos, value)
    # The point of the exercise: a fraction of the product, not all of it.
    assert len(tuples) < q ** leg_count, len(tuples)
    # Nothing outside the domain leaked in from the integer construction.
    assert {v for t in tuples for v in t} <= set(_RESULT_DOMAIN)


def test_the_derivation_helpers_can_actually_fail(tmp_path: Path) -> None:
    """The helpers whose weakening was shown to let a real attack land.

    Each line below is the exact property that, when removed, turned a red
    ci.yml edit green: a gate passing a FAILED leg (`_leg_tuples` collapsed to
    two values), a leg adopting a required check's name (`_rendered_names`
    not expanding), a step inserted before the verdict (`_gates` pinning
    `step=0`), and `always() && …` (`_normalised_condition` answering
    `always()` regardless).
    """
    # `_expected_exit`: success and nothing else. ⛔ `skipped` is the cell the
    # whole of #3756 turns on — an oracle that still forgave it would make
    # the matrix agree with a gate that does.
    assert _expected_exit(("success",)) == 0
    assert _expected_exit(("success", "success", "success")) == 0
    for bad in _RESULT_DOMAIN:
        if bad != "success":
            assert _expected_exit((bad,)) == 1, bad
            assert _expected_exit(("success", bad, "success")) == 1, bad

    # `_leg_tuples`: the full product up to three legs, and it must contain a
    # MIXED tuple — collapsing the domain to success/skipped kept every
    # existing assertion. (Beyond three legs the covering array has its own
    # control, `test_leg_tuples_are_a_strength_3_covering_array`.)
    assert len(_leg_tuples(3)) == len(_RESULT_DOMAIN) ** 3, len(_leg_tuples(3))
    assert ("failure", "skipped", "success") in _leg_tuples(3)
    assert len(_leg_tuples(1)) == len(_RESULT_DOMAIN)

    # `_normalised_condition`: strips ONE wrapper and nothing else.
    assert _normalised_condition("always()") == "always()"
    assert _normalised_condition("${{ always() }}") == "always()"
    assert _normalised_condition("always() && github.event_name != 'x'") \
        != "always()", "a narrowed condition normalised to bare always()"

    # `_rendered_names`: expands a matrix expression, refuses what it cannot.
    matrix_job = {"name": "ZQ (${{ matrix.go }})",
                  "strategy": {"matrix": {"go": ["1.26", "1.27"]}}}
    assert _rendered_names("zq", matrix_job) == ["ZQ (1.26)", "ZQ (1.27)"]
    assert _rendered_names("zq", {"name": "ZQ (${{ inputs.x }})"}) is None
    assert _rendered_names("zq", {"name": "ZQ"}) == ["ZQ"]
    assert _could_render_to("ZQ (${{ matrix.go }})", "ZQ (1.26)")
    assert not _could_render_to("scan ${{ matrix.name }}", "Portal Tests")

    # `_gates`: the verdict step's INDEX, not a constant.
    directory = tmp_path / "twostep"
    directory.mkdir(parents=True)
    workflow = {"jobs": {
        "zq-leg": {"steps": [{"run": "true"}]},
        "zq-gate": {"if": "always()", "needs": ["zq-leg"],
                    "steps": [{"name": "warm", "run": "echo warm"},
                              {"name": "verify", "env": {
                                  "L": "${{ needs.zq-leg.result }}"},
                               "run": "exit 0\n"}]}}}
    path = directory / "zq.yml"
    path.write_text(yaml.safe_dump(workflow), encoding="utf-8", newline="\n")
    gate, = _gates(path)
    assert gate.step == 1, (
        f"the verdict step was reported at index {gate.step}; pinning it to 0 "
        "makes the 'nothing may run before the verdict' rule unreachable.")

    # `_decision_path`: legs AND what they transitively need, nothing else.
    jobs = {"zq-a": {"needs": "zq-b"}, "zq-b": {"needs": ["zq-c"]},
            "zq-c": {}, "zq-other": {}}
    chained = Gate("zq.yml", "zq-gate", "ZQ", "always()", 0, "",
                   _Bindings(legs=(("L", "zq-a"),)), None)
    assert _decision_path(jobs, chained) == {"zq-a", "zq-b", "zq-c"}
