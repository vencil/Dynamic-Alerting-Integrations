r"""The three ci.yml aggregate gates are required checks with no assertions.

#1397. `ci.yml` reports three REQUIRED status checks — `Python Tests (3.13)`,
`Go Tests (1.26)`, `Portal Tests` — from three jobs that run no tests at all.
Each is a pure aggregation gate: `if: always()`, a handful of
`${{ needs.*.result }}` bindings in one step's `env:`, and ~10 lines of shell
that decide pass/fail. Those ten lines are the whole reason a path-SKIPPED
test leg does not silently satisfy branch protection, and until this module
nothing asserted anything about them.

Measured against the ci.yml of `17a05bb5` (byte-identical in `72fdaf56` and
in this commit), one mutation at a time, baseline green, each restored and
byte-compared. The counterfactual surface is the union of

    git grep -l ".github/workflows" -- 'tests/**.py'
    git grep -l "ci\.yml" -- 'tests/**.py'

— 34 modules on the tree without this file (35 with it), 949 passed / 126
skipped. ⚠️
Reproduce the SET with those two commands rather than trusting the counts:
they move with the tree, and one grep instead of the union gives a different
answer (an earlier draft of this paragraph described a hand-narrowed 22-module
subset as if it were the union, which is why the whole sweep was re-run
against the union before this sentence was written). FOURTEEN single-edit
mutations left all 949 green:

  * deleting a gate's `if [ "$DETECT_RESULT" != "success" ]` block (×3) — a
    broken `detect-changes` then skips every leg it gates, the gate reads
    `RUN_RESULT=skipped` with `*_changed` empty, and exits 0;
  * deleting the `&& [ "$X_CHANGED" != "true" ]` half (×3) — skip-as-green,
    the defect #1368 was opened for, restored in one edit;
  * `continue-on-error: true` on `detect-changes`, on a gate job, or on a
    gate STEP;
  * narrowing `if: always()` to `always() && github.event_name != 'schedule'`;
  * dropping one leg from the `go-tests` loop, or dropping both its loop entry
    and its `env:` binding;
  * swapping a gate's detect branch after its success branch;
  * interpolating `${{ needs.*.result }}` into `run:` instead of binding it.

⚠️ Two neighbouring mutations were ALREADY red before this module, and it
would be dishonest to bank them here: `continue-on-error` on a gated LEG, and
deleting a gate's `always()`. `tests/ops/test_ci_path_filter_coverage.py`
catches both — the first through `_job_step_files`, the second because a gate
without `always()` starts inheriting the gate through `needs:` and joins that
module's exact `GATED_LEGS` set. What this module adds there is reach (the
gate job and the detect job are outside the sibling's view by construction:
it skips `always()` jobs and detect jobs) and strictness (key absence rather
than truthiness — see below).

⚠️ What this module does NOT cover.

It cannot check that these three names are actually configured as required
checks — that lives in GitHub settings. `REQUIRED_CHECK_NAMES` below mirrors
them, with the `gh api` command that produced the list.
`docs/internal/iac-lint-baseline.md` has an in-repo checklist, but do not
treat it as the list: its authority is line 66, the `gh api` invocation, not
the tick-boxes at line 46, which name six checks out of the nineteen that are
actually required and do not include `Portal Tests`.

⛔ The biggest thing it does NOT cover, named because it is adjacent enough to
be mistaken for covered: `run: pytest … || true` on a test leg is the shell
spelling of `continue-on-error`, achieves exactly the same thing, sits in the
same decision path, and NOTHING in this repo forbids it. It is banned in the
gate scripts here (see `_EXIT_SWALLOW`) and deliberately not on the legs,
because `python-tests-run` has a live and legitimate `|| true` on its "Install
mtail" step and the narrowing that would separate the two is the one the
sibling module documents two failed attempts at. Candidate ticket, not a
silent omission.

⛔ What a green run does NOT prove, stated because a green guard is read as a
guarantee. It does not prove the gate script runs as bash at all beyond the
inputs checked here (`runs-on`, `container`, `shell` and `defaults` are
asserted, but the runner's own behaviour is not observed); it does not prove
the required-check list is still these three (`REQUIRED_CHECK_NAMES` is a
mirrored constant, correct on the date stamped beside it and stale the moment
GitHub settings change); and it says nothing about 16 of the 19 required
checks.

⛔ Its scope is `ci.yml`, with two exceptions:
`test_gate_shaped_jobs_are_either_modelled_or_ledgered` asks whether a
gate-shaped job in any dorny workflow is modelled or ledgered, and
`test_the_required_check_names_belong_to_the_gates_and_nothing_else` sweeps
every workflow, because branch protection matches a check NAME regardless of
which file produced it. Everything else is bound to `CI_WORKFLOW`. That leaves the LARGER half of the problem untouched
and it is worth naming precisely, because #1398's summary is easy to read as
"docs-ci's `all-checks` is weak": of the 19 required checks, **ten are
path-gated jobs registered as required directly, with no aggregate layer at
all** — `Go Lint`, `Version Consistency`, `Validate Tenant Config & Routes`
(validate.yaml), and `Check Documentation Links` / `Front Matter` / `Coverage`
/ `MkDocs Build Verification` / `Validate Mermaid Diagrams` / `Documentation
Line Count Monitor` / `Drift Detection (validate_all.py)` (docs-ci.yaml). A
broken detect job in either workflow skips all of them, and a skipped required
check reports Success. `All Documentation Checks` is NOT itself required
(verified against the branch-protection API), so repairing that one job closes
none of this. That is #1398's real subject.

## How a gate is FOUND (derived, not a list of three job ids)

A gate is any job with a step whose `env:` binds, through `${{ }}`:
  * the `.result` of a job that runs `dorny/paths-filter` (the detect job),
  * a `*_changed` entry of that same detect job's `.outputs`,
  * and the `.result` of at least one job that is NOT a detect job.

The env NAMES are read out of that mapping rather than known: the three gates
spell their leg bindings `RUN_RESULT`, `R_TE`/`R_TA`/`R_AM`, and `R_PORTAL`
(the detect binding happens to be `DETECT_RESULT` in all three today, which is
exactly the kind of coincidence a hard-coded name would enshrine). A fourth
gate written tomorrow inherits every assertion here without editing this file.
⛔ Pinning the three job ids instead
would have been "a denylist that happens to contain today's members", the
shape this repo keeps re-learning — see the ⛔ block above
`_condition_triggers` in the sibling module
`tests/ops/test_ci_path_filter_coverage.py`, where two successive spellings of
the same idea were defeated in opposite directions.

Discovery is cross-checked against an INDEPENDENT route through the same
workflow — the set of jobs that path-skip, derived from their own `if:` and
propagated through `needs:` — so a discovery that quietly finds nothing
cannot pass.

## How the shell is CHECKED (execute it, do not grep it)

`ci.yml`'s gate steps declare no `shell:`, so GitHub runs them as
`bash -e {0}` — a temp FILE, not stdin (workflow-syntax reference). The oracle
here is the same: extract the `run:` block, drop it in a temp file, run
`bash -e` on it once per cell of the full env cross product, compare the exit
code against the CI-ROI 6C contract those jobs' own comments state:

    exit 0  ⟺  detect succeeded  ∧  every guarded leg either succeeded, or
                skipped while detect said its filter did not match

⚠️ The contract is transcribed from the DESIGN (the `# CI-ROI 6C semantics`
comment block in ci.yml, and #1368), not from the script — a truth table read
off the implementation would only restate whatever the implementation does,
including its bugs.

The value domains are complete rather than sampled. `needs.<job>.result` has
exactly four documented values (success / failure / cancelled / skipped —
contexts reference), plus the empty string for "the expression did not
resolve", which is the state #1397's failure scenario is actually about.
dorny emits `true` or `false`, plus the same empty string. Detect result and
`*_changed` are taken exhaustively; the LEG axis uses a covering design
(`_leg_tuples`) rather than the full product, because the full product is
exponential in the number of legs and adding a leg is the growth path ci.yml
explicitly invites. Today: 75 cells for a one-leg gate, 975 for `go-tests`,
1125 in total.

⚠️ Cost. The whole module runs in ~3 s in the dev container and ~5 s pinned to
four vCPUs, which is what a GitHub-hosted runner has. On a Windows dev host it
is a few minutes — four independent measurements of the pre-covering-design
version spanned 137–246 s — because a `bash` spawn costs ~150 ms there against
~3 ms on Linux, and every cell is a spawn. CI is Linux, so the figure that
matters is the small one; the Windows number is given as a range because it is
dominated by process-creation noise and no two measurements agreed.
"""

from __future__ import annotations

import functools
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
# ⛔ ONE literal path string each — a `/ ".github" / "workflows"` split form
# registers this module against nothing in verify_diff_map.json.
CI_WORKFLOW = ROOT / ".github/workflows/ci.yml"
WORKFLOW_DIR = ROOT / ".github/workflows"

BASH = shutil.which("bash")
# ⛔ Per-test, never `pytestmark`: everything except the two matrix tests is
# pure YAML and must keep running on the Windows dev host. A module-level skip
# silently takes those with it.
needs_bash = pytest.mark.skipif(
    BASH is None, reason="needs bash to execute the gate script")


# ── loading ────────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _load(path: Path) -> dict:
    """ci.yml is ~1.9k lines and every helper here wants it; parse it once."""
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def _paths_filter_workflows() -> tuple[Path, ...]:
    """Every workflow that owns a `dorny/paths-filter` detect job.

    Derived from the tree rather than listed. ⚠️ But "scanned" is a narrow
    word: a workflow entering this set is only asked whether a gate-shaped
    job in it is modelled or ledgered. It does NOT bring its jobs under the
    rest of the assertions here — the required-check NAME sweep covers every
    workflow separately, and everything else is `ci.yml` only. The sibling module carries the same
    correction for the same reason — a workflow being scanned says nothing
    about its jobs being seen. `sorted` keeps failure messages stable.
    """
    return tuple(
        path for path in sorted(WORKFLOW_DIR.glob("*.y*ml"))
        if "dorny/paths-filter" in path.read_text(encoding="utf-8")
    )


def _detect_jobs(path: Path) -> frozenset[str]:
    """Job ids that RUN the dorny step, i.e. whose outputs ARE the gate."""
    return frozenset(
        job_id
        for job_id, job in _load(path)["jobs"].items()
        if any("dorny/paths-filter" in str(step.get("uses", ""))
               for step in (job.get("steps") or []))
    )


# ── reading `${{ needs.X.… }}` out of a step's env ─────────────────────────

_RESULT_EXPR = re.compile(r"^\$\{\{\s*needs\.([\w-]+)\.result\s*\}\}$")
_OUTPUT_EXPR = re.compile(
    r"^\$\{\{\s*needs\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}$")
_MENTIONS_NEEDS = re.compile(r"needs\.[\w-]+")


@dataclass(frozen=True)
class _Bindings:
    detect_result: tuple[str, str]            # (env name, detect job id)
    changed: tuple[tuple[str, str, str], ...]  # ((env, detect job, output), …)
    legs: tuple[tuple[str, str], ...]         # ((env name, job id), …) sorted

    @property
    def changed_names(self) -> tuple[str, ...]:
        return tuple(name for name, _job, _output in self.changed)

    @property
    def changed_outputs(self) -> frozenset[str]:
        return frozenset(output for _name, _job, output in self.changed)


def _step_bindings(path: Path, job_id: str, step: dict) -> _Bindings | None:
    """Classify one step's `env:` block. None means "not a gate step".

    ⛔ Fail-closed on shapes this module cannot read: an env value that
    mentions `needs.` but is neither a bare `.result` nor a bare
    `.outputs.<name>` RAISES. Absorbing it would drop the job out of every
    assertion below while it still reads as a gate to a human — the exact
    failure mode `_gated_jobs` documents for `if:` in the sibling module.
    """
    detect = _detect_jobs(path)
    results: dict[str, str] = {}
    outputs: dict[str, tuple[str, str]] = {}
    for name, raw in (step.get("env") or {}).items():
        value = str(raw).strip()
        if matched := _RESULT_EXPR.match(value):
            results[name] = matched.group(1)
        elif matched := _OUTPUT_EXPR.match(value):
            outputs[name] = (matched.group(1), matched.group(2))
        elif _MENTIONS_NEEDS.search(value):
            raise AssertionError(
                f"{path.name}::{job_id} binds env {name}={value!r}, which "
                "references `needs.` in a shape this module does not parse. "
                "Every assertion in this file keys off these bindings, so an "
                "unparsed one removes the job from the gate set silently. "
                "⛔ Teach the parser the shape. Moving the reference out of "
                "`env:` also makes this message go away — and it is the "
                "anti-pattern this module already has to ledger for "
                "`docs-ci.yaml::all-checks`: the job then leaves the gate set "
                "entirely and no assertion here covers that required check at "
                "all.")

    detect_results = {n: j for n, j in results.items() if j in detect}
    leg_results = {n: j for n, j in results.items() if j not in detect}
    detect_outputs = {
        n: (j, o) for n, (j, o) in outputs.items()
        if j in detect and o.endswith("_changed")
    }
    if not (detect_results and leg_results and detect_outputs):
        return None

    if len(detect_results) != 1:
        raise AssertionError(
            f"{path.name}::{job_id} binds {len(detect_results)} detect "
            "results. The oracle is written for one detect job deciding "
            "whether its own verdict is trustworthy; two of them is a shape "
            "this module has not modelled.")

    # ⚠️ SEVERAL `*_changed` bindings are supported on purpose. Blind review
    # tried the fix that `KNOWN_SKIP_TOLERANCE_GAPS` itself recommends —
    # weighing `go_changed` AND `docs_changed`, strictly narrower than today —
    # and the earlier "exactly one" rule turned it into a collection ERROR
    # that took all assertions in this file down with it. A guard that
    # refuses the repair it asks for is worse than the gap.
    (dr_name, dr_job), = detect_results.items()
    return _Bindings(
        detect_result=(dr_name, dr_job),
        changed=tuple(sorted(
            (name, job, output) for name, (job, output) in detect_outputs.items()
        )),
        legs=tuple(sorted(leg_results.items())),
    )


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
        return _gates(CI_WORKFLOW), None
    except AssertionError as refusal:  # noqa: PERF203 - one call, one catch
        return (), refusal


_CI_GATES, _CI_GATE_REFUSAL = _discover_gates_or_fail()


def _ci_gates() -> tuple[Gate, ...]:
    return _CI_GATES


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


# ── the independent route: which jobs are path-gated at all ────────────────

_IF_GATE_REF = re.compile(r"needs\.([\w-]+)\.outputs\.(\w+_changed)")
# "This job consults some other job's verdict", wherever it is written —
# `env:`, `run:`, `with:`. Matched against the job's JSON dump so no spelling
# location is privileged.
_READS_A_RESULT = re.compile(r"needs\.[\w-]+\.result")


def _path_gated_jobs(path: Path) -> dict[str, frozenset[str]]:
    """job id -> the `*_changed` outputs that decide whether it runs.

    This reads `if:` and `needs:`; `_gates` reads a step's `env:`. Two
    different keys over the same file, which is what makes the cross-check
    below say something — a discovery bug in one route cannot hide in the
    other.

    ⛔ TWO ways in, and the second one is not decoration. GitHub SKIPS a job
    whose dependency was skipped, so a job that merely `needs:` a gated leg
    and does not write `always()` is path-gated in fact while carrying no
    `if:` of its own. Blind review demonstrated the hole: adding such a job
    (`needs: portal-tests-run`, no `if:`) left every assertion here green
    while the job could path-skip with nothing weighing it. The sibling
    `_gated_jobs` in `tests/ops/test_ci_path_filter_coverage.py` already had
    this fixpoint; only the `if:` half had been carried over here — "a rule
    present in one guard file and missing in the other" is this repo's most
    repeated defect, so it is worth saying that this is the same rule, not a
    new one.

    Jobs carrying `always()` opt out, which is exactly how the gates
    themselves stay out of this set.
    """
    detect = _detect_jobs(path)
    jobs = _load(path)["jobs"]
    gated: dict[str, frozenset[str]] = {}
    for job_id, job in jobs.items():
        names = frozenset(
            output
            for job_ref, output in _IF_GATE_REF.findall(str(job.get("if", "")))
            if job_ref in detect
        )
        if names:
            gated[job_id] = names

    changed = True
    while changed:
        changed = False
        for job_id, job in jobs.items():
            if job_id in gated or "always()" in str(job.get("if", "")):
                continue
            inherited = frozenset(
                name for dep in _needs(job) if dep in gated
                for name in gated[dep]
            )
            if inherited:
                gated[job_id] = inherited
                changed = True
    return gated


# ⛔ Jobs that LOOK like an aggregate gate — `if: always()` over at least one
# path-gated leg — but that this module cannot model, with the reason and the
# ticket. Asserted as an exact set, so a new one cannot join a silent majority
# and a fixed one cannot leave a stale excuse behind.
UNMODELLED_GATE_SHAPED_JOBS = {
    ("docs-ci.yaml", "all-checks"):
        "interpolates `${{ needs.*.result }}` straight into `run:` instead of "
        "binding it through `env:`, and consults no `*_changed` output at all "
        "— so it cannot express the contract asserted here: it tolerates "
        "EVERY `skipped`, including one caused by a broken detect job. "
        "⚠️ Do not read this entry as 'the docs-ci exposure lives here'. "
        "`All Documentation Checks` is not itself a required check (checked "
        "against the branch-protection API on 2026-08-13); the seven docs-ci "
        "legs it waits for are required INDIVIDUALLY, so fixing this job "
        "closes none of the exposure. Tracked as #1398, whose real subject is "
        "those ten directly-required path-gated jobs across docs-ci.yaml and "
        "validate.yaml — see the module docstring. Deliberately out of scope "
        "for #1397. ⛔ Do not silence this entry by widening discovery — "
        "widening it without also changing the job would make this module "
        "assert a contract that job does not implement, and the assertion "
        "would then have to be weakened to fit.",
}


# ⛔ The branch-protection contract, mirrored. This is the one thing here that
# CANNOT be derived from the tree: the required-check list lives in GitHub
# settings. Verified 2026-08-13 with
#   gh api repos/vencil/Dynamic-Alerting-Integrations/branches/main/protection\
#     /required_status_checks --jq '.contexts[]'
# which returned 19 contexts including exactly these three.
#
# ⚠️ It is pinned because a rename is NOT symmetric. Renaming a gate to a name
# nobody requires leaves the required check waiting forever — that blocks the
# merge, so it needs no guard. But MOVING one of these names onto a job that
# can path-skip hands the required check to a job whose skip reports Success.
# Blind review did it by swapping `name:` between `portal-tests` and
# `portal-tests-run`; every assertion here stayed green.
REQUIRED_CHECK_NAMES = frozenset({
    "Python Tests (3.13)",
    "Go Tests (1.26)",
    "Portal Tests",
})


# ── the exit-code contract ─────────────────────────────────────────────────

# `needs.<job_id>.result`: "Possible values are success, failure, cancelled,
# or skipped" (contexts reference). The empty string is this module's own
# addition — it is what an unresolvable expression renders as, and it is the
# exact state #1397 describes (a failed detect job's outputs read empty). It
# is NOT documented; including it only makes the matrix stricter.
_RESULT_DOMAIN = ("success", "failure", "cancelled", "skipped", "")
# dorny emits the string "true" or "false"; empty for the same reason.
_CHANGED_DOMAIN = ("true", "false", "")


def _expected_exit(detect: str, legs: tuple[str, ...],
                   changed: tuple[str, ...]) -> int:
    """The CI-ROI 6C contract, transcribed from ci.yml's own comment block.

        success                                   -> PASS
        skipped AND detect said not needed        -> PASS
        anything else, or detect itself not green -> FAIL

    ⛔ Read this as a SPECIFICATION. It was written from the design note, not
    from the shell — an oracle copied off the implementation asserts only that
    the implementation still does what it does.

    ⚠️ One honest exception, found in review: the design note says "skipped
    AND detect said not needed", and is SILENT on what an EMPTY `*_changed`
    means. `changed != "true"` — treating empty as "not needed" — is therefore
    read off the shell, not off the spec, and it is the one place where this
    oracle could be restating a bug. It is kept deliberately: with a healthy
    detect job the outputs are always populated, and with a broken one
    `detect != "success"` decides first. The remaining way to reach
    `detect == success` with an empty `*_changed` is a MISSPELLED output name,
    which `test_every_changed_output_a_gate_or_a_leg_consults_is_declared`
    closes separately rather than by tightening this table.
    """
    if detect != "success":
        return 1
    needed = any(value == "true" for value in changed)
    for result in legs:
        if result == "success":
            continue
        if result == "skipped" and not needed:
            continue
        return 1
    return 0


def _leg_tuples(leg_count: int) -> list[tuple[str, ...]]:
    """A covering design over the leg axis, not the full cross product.

    ⛔ The full product is `5^legs`, and adding a leg is the growth path
    `ci.yml` explicitly invites ("New split jobs join HERE rather than getting
    their own required check"). Measured on a Windows dev host: 3 legs = 158 s
    for this one test, 4 legs = **911 s**. Exponential cost on the one axis
    that is designed to grow is a guard that gets deleted.

    What is generated instead, and why each part is needed:
      * every ALL-EQUAL tuple — catches a backdoor keyed on "all legs skipped",
        which pairwise coverage alone would miss;
      * every PAIR of positions taking every pair of values, others `success`
        — the `go-tests` loop is the only cross-leg structure in the tree, and
        any two-leg interaction shows up here. It subsumes one-at-a-time
        (the partner value `success` IS the one-at-a-time case).

    ⚠️ Stated blind spot: a backdoor that needs THREE OR MORE legs to hold
    mutually distinct values simultaneously is not generated. Nothing in the
    contract or in the shipped scripts has that shape, and buying it back
    costs the exponential. Growth is now quadratic: 3 legs 65 tuples (vs
    125), 4 legs 117 (vs 625), 5 legs 185 (vs 3125).
    """
    tuples = {(value,) * leg_count for value in _RESULT_DOMAIN}
    for i in range(leg_count):
        for j in range(leg_count):
            if i == j:
                continue
            for a in _RESULT_DOMAIN:
                for b in _RESULT_DOMAIN:
                    row = ["success"] * leg_count
                    row[i], row[j] = a, b
                    tuples.add(tuple(row))
    return sorted(tuples)


def _cases(leg_count: int, filter_count: int):
    """Every documented detect/changed value against the leg covering set."""
    for detect in _RESULT_DOMAIN:
        for changed in itertools.product(_CHANGED_DOMAIN, repeat=filter_count):
            for legs in _leg_tuples(leg_count):
                yield detect, legs, changed


def _env_for(gate: Gate, detect: str, legs: tuple[str, ...],
             changed: tuple[str, ...], summary: str) -> dict[str, str]:
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
    env[gate.bindings.detect_result[0]] = detect
    for name, value in zip(gate.bindings.changed_names, changed):
        env[name] = value
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
    detect: str
    legs: tuple[str, ...]
    changed: tuple[str, ...]
    rc: int
    stderr: str


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
    argv = list(gate.shell_argv)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gate.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        summary = Path(tmp) / "step-summary.md"
        summary.write_text("", encoding="utf-8", newline="\n")
        argv.append(path.as_posix())
        cells = list(_cases(len(gate.bindings.legs),
                            len(gate.bindings.changed)))

        def run(case) -> _Cell:
            detect, legs, changed = case
            env = _env_for(gate, detect, legs, changed, summary.as_posix())
            # ⛔ Retry once when the answer falls outside the contract's range.
            # The scripts are deterministic, so a repeat cannot mask a real
            # defect — but a `bash` that failed to START answers 126/127, and
            # under load on a Windows host that happens: blind review measured
            # one run of this module at `5 failed` and the next, unchanged, at
            # `19 passed`, with every flaky cell a spawn failure being read as
            # the gate's verdict. Instrument noise, not a finding — and the
            # noise was in the FALSE-RED direction, which is how a guard gets
            # deleted.
            for attempt in (1, 2):
                done = subprocess.run(
                    argv, env=env, capture_output=True, encoding="utf-8",
                    errors="replace", timeout=60)
                if done.returncode in (0, 1) or attempt == 2:
                    break
            return _Cell(detect, legs, changed, done.returncode,
                         done.stderr)

        workers = min(64, (os.cpu_count() or 4) * 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return tuple(pool.map(run, cells))


# ⛔ Exact stderr text a gate is allowed to emit. Empty, and it is the seam the
# stderr message points at — an assertion message that names a mechanism must
# name one that exists. Add the literal text, never a pattern: the point is
# that a NEW noise source has to be looked at once.
ALLOWED_GATE_STDERR: frozenset[str] = frozenset()


def _matrix_violations(gate: Gate, cells: tuple[_Cell, ...]) -> list[str]:
    """PURE. Compare observed exit codes against the contract.

    Split out from the runner so a degenerate input can prove the comparison
    is able to say NO — a tripwire that only ever sees the real, passing gate
    is a tripwire nobody has watched fire.
    """
    problems: list[str] = []
    for cell in cells:
        want = _expected_exit(cell.detect, cell.legs, cell.changed)
        if cell.rc == want:
            continue
        bound = ", ".join(
            f"{name}={value!r}" for name, value in
            list(zip(gate.bindings.changed_names, cell.changed))
            + [(name, value) for (name, _job), value
               in zip(gate.bindings.legs, cell.legs)])
        problems.append(
            f"{gate}: {gate.bindings.detect_result[0]}={cell.detect!r}, "
            f"{bound} -> exit {cell.rc} (contract says {want})")

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


# ⛔ Path-gated jobs deliberately NOT weighed by any gate, with the argument
# for why their skip cannot mislead a required check. Empty today. It exists
# because the alternative was a hard red on an ordinary downstream job (a
# coverage-delta step that merely `needs:` a leg is path-gated by inheritance
# and backs no required check) — a guard with no legitimate exit turns into a
# guard people delete.
UNWATCHED_PATH_GATED_JOBS: dict[str, str] = {}


def test_every_path_gated_leg_is_watched_by_a_gate() -> None:
    """Discovery is not allowed to quietly find nothing.

    ⛔ The anti-vacuity floor is NOT a count of gates — that would be taken
    from the very thing it protects. It is the set of path-gated jobs, derived
    through the other route (`if:` rather than `env:`): every job that can
    path-skip must have its `.result` weighed by some gate, and every leg a
    gate weighs must really be path-gated. Both directions — a gate watching a
    job that stopped being gated has a skip-tolerance branch that became
    unreachable and now only reads as protection.
    """
    gates = _gates_or_fail()
    # ⛔ Said out loud because the matrix test is parametrised over this set:
    # if it collapses to empty, pytest reports zero tests as a pass. Blind
    # review reached that state by renaming the `dorny/paths-filter` action.
    assert gates, (
        "no aggregate gate was discovered in ci.yml at all. Every assertion "
        "parametrised over the gate set silently becomes zero tests, which "
        "reads as green.")
    watched = {
        job_id: gate.job_id
        for gate in gates for _name, job_id in gate.bindings.legs
    }
    gated = _path_gated_jobs(CI_WORKFLOW)

    jobs = _load(CI_WORKFLOW)["jobs"]
    unwatched = set(gated) - set(watched)
    problems = [
        f"ci.yml::{job_id} is path-gated on {sorted(gated[job_id])} but no "
        "aggregate gate weighs its `.result`. When it path-skips, nothing "
        "decides whether that skip was legitimate.\nAsk first: SHOULD this "
        "job's outcome decide a required check? If yes, add it to a gate's "
        "`needs:` AND to that gate's env bindings — both halves, or the gate "
        "waits for it and then ignores it. If no — it backs no required "
        "check and blocks nothing — record that in UNWATCHED_PATH_GATED_JOBS "
        "with the argument. The two are not interchangeable: doing the first "
        "to a job that is not a test leg makes a required check start "
        "blocking on it."
        for job_id in sorted(unwatched - set(UNWATCHED_PATH_GATED_JOBS))
    ] + [
        f"ci.yml::{job_id} is weighed by the {watched[job_id]} gate but is no "
        "longer path-gated. Either the gate watches the wrong job, or the leg "
        "lost its `if:` and the gate's skip-tolerance branch is now dead code."
        for job_id in sorted(set(watched) - set(gated))
    ]
    # ⛔ The ledger is not a place to park a gate. Blind review laundered a
    # gate-shaped job to invisibility in three moves: add a job that reads a
    # leg's `.result`, delete its `always()` so it stops being reported as an
    # unmodelled gate, then write a ledger row. Two structural answers, both
    # here: a ledgered job may not read anyone's `.result`, and a row must
    # still name a job that is actually unwatched today.
    for job_id in sorted(UNWATCHED_PATH_GATED_JOBS):
        if job_id not in unwatched:
            problems.append(
                f"UNWATCHED_PATH_GATED_JOBS names ci.yml::{job_id}, which is "
                "not an unwatched path-gated job today. A row that outlives "
                "its job keeps whatever id it holds outside the check above, "
                "silently and forever — delete it.")
        elif _READS_A_RESULT.search(json.dumps(jobs.get(job_id, {}))):
            problems.append(
                f"ci.yml::{job_id} is in UNWATCHED_PATH_GATED_JOBS but reads "
                "another job's `.result`. A job that consults a verdict is a "
                "gate, not a downstream consumer, and this ledger is the "
                "wrong answer for it — ⛔ particularly if it reached this "
                "state by having its `always()` removed, which is strictly "
                "worse: a gate that skips reports Success to branch "
                "protection.")
    assert not problems, (
        f"{len(problems)} gate/leg mismatch(es) in ci.yml:\n  "
        + "\n  ".join(problems)
        + f"\n(gates discovered: {sorted(g.job_id for g in _gates_or_fail())}; "
          f"path-gated jobs: {sorted(gated)})")


_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_MATRIX_REF = re.compile(r"^matrix\.([\w-]+)$")


def _rendered_names(job_id: str, job: dict) -> list[str] | None:
    """Every check-run name this job can report. None = not modellable.

    ⛔ Comparing the RAW `name:` is not enough, and this is the bypass that
    forced the function: GitHub expands `${{ }}` into the check-run name, and
    three jobs in this workflow already carry an expression there. Renaming a
    path-gated leg to `Go Tests (${{ matrix.go }})` renders to exactly the
    required context `Go Tests (1.26)` — one character from the name it has —
    while a raw-string comparison sees two different strings.
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
    `scan ${{ matrix.name }}` cannot become `Portal Tests`; `Go Tests
    (${{ matrix.go }})` can become `Go Tests (1.26)`.
    """
    # `re.split` with one capture group interleaves literals and captures, so
    # the literal fragments are the even indices.
    pattern = ".*".join(re.escape(part)
                        for part in _EXPRESSION.split(raw_name)[::2])
    return re.fullmatch(pattern, target, flags=re.S) is not None


def test_the_required_check_names_belong_to_the_gates_and_nothing_else() -> None:
    """Both directions: the gates own these names, and only the gates do.

    The first half catches a gate being renamed out of the contract. The
    second catches the dangerous inverse — a job that can path-skip, or any
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
            "the `gh api` command above; the pin was taken on 2026-08-13.")

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
                # across the other workflows build a name from `matrix.name`
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


def test_every_changed_output_a_gate_or_a_leg_consults_is_declared() -> None:
    """A typo in a `*_changed` name renders as the empty string, not an error.

    GitHub does not fail a workflow for reading an output that was never
    declared; the expression simply evaluates to "". For a leg's `if:` that
    means it never runs. For a gate's `env:` it means the skip-tolerance
    branch reads "" — which `!= 'true'` — so the gate forgives every skip,
    forever, and the required check is green because a name was misspelled.

    ⛔ Neither this module's other assertions nor the sibling filter guard
    catch it: both check that the FILTER exists in the dorny block, not that
    the OUTPUT is declared on the detect job.
    """
    workflow = _load(CI_WORKFLOW)
    jobs = workflow["jobs"]
    declared = {
        detect: set((jobs[detect].get("outputs") or {}))
        for detect in _detect_jobs(CI_WORKFLOW)
    }
    problems = []
    for gate in _gates_or_fail():
        for _name, detect, output in gate.bindings.changed:
            if output not in declared.get(detect, set()):
                problems.append(
                    f"{gate} binds `needs.{detect}.outputs.{output}`, which "
                    f"{detect} does not declare. It renders as the empty "
                    "string, so the gate tolerates every skip.")
    for job_id, job in jobs.items():
        for detect, output in _IF_GATE_REF.findall(str(job.get("if", ""))):
            if detect in declared and output not in declared[detect]:
                problems.append(
                    f"ci.yml::{job_id} gates on "
                    f"`needs.{detect}.outputs.{output}`, which {detect} does "
                    "not declare — the job can never run.")
    assert not problems, "\n  ".join([""] + problems)
    assert declared and all(declared.values()), (
        f"no detect job declares any outputs ({declared!r}) — this assertion "
        "would pass on an empty tree.")


def test_every_job_a_gate_waits_for_is_also_weighed() -> None:
    """`needs:` and the gate's env bindings must name the same legs.

    ci.yml's portal gate tells the next contributor "New portal test jobs
    should join HERE (add to needs + the loop)". Adding a leg to `needs:` and
    forgetting the second half is a job the required check waits for and then
    ignores: it can fail, or skip while it was needed, and the gate never
    looks. This turns that comment into a machine check.
    """
    jobs = _load(CI_WORKFLOW)["jobs"]
    detect = _detect_jobs(CI_WORKFLOW)
    problems = []
    for gate in _gates_or_fail():
        awaited = set(_needs(jobs[gate.job_id])) - detect
        weighed = {job_id for _name, job_id in gate.bindings.legs}
        for job_id in sorted(awaited - weighed):
            problems.append(
                f"{gate} waits for ci.yml::{job_id} but never reads its "
                "`.result` — the gate blocks on it and then ignores its "
                "verdict.")
        for job_id in sorted(weighed - awaited):
            problems.append(
                f"{gate} reads ci.yml::{job_id}'s `.result` but does not "
                "`needs:` it, so the expression renders empty regardless of "
                "what that job did.")
    assert not problems, "\n  ".join([""] + problems)


def test_gate_shaped_jobs_are_either_modelled_or_ledgered() -> None:
    """A job that looks like a required-check gate must not be invisible.

    `if: always()` over a path-gated leg is the whole shape of "a required
    check that decides what a skip means". Any job wearing it that discovery
    did NOT pick up is either a gate written in a spelling this module cannot
    read, or a real gate somebody forgot to wire — both must be a decision
    recorded here rather than silence.
    """
    detected: set[tuple[str, str]] = set()
    modelled: set[tuple[str, str]] = set()
    for path in _paths_filter_workflows():
        gated = set(_path_gated_jobs(path))
        modelled |= {(path.name, gate.job_id) for gate in _gates(path)}
        for job_id, job in _load(path)["jobs"].items():
            if "always()" not in str(job.get("if", "")):
                continue
            if not gated.intersection(_needs(job)):
                continue
            # ⛔ And it must actually READ a result. Without this clause an
            # informational `notify` job — `always()`, `needs:` a gated leg,
            # reads nothing, decides nothing — was reported as an unmodelled
            # gate, and blind review measured that the cheapest way to make
            # the message go away was to DELETE its `always()`. A guard that
            # rewards removing `always()` is arguing against its own thesis.
            if _READS_A_RESULT.search(json.dumps(job)):
                detected.add((path.name, job_id))

    unmodelled = detected - modelled
    expected = set(UNMODELLED_GATE_SHAPED_JOBS)
    assert unmodelled == expected, (
        "gate-shaped jobs this module cannot model changed.\n"
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
        "no aggregate gate was discovered in ANY workflow that owns a "
        "dorny/paths-filter job — discovery is broken, not the tree.")


# ⛔ Every spelling that READS a variable, not just `$X` and `${X}`. The first
# version matched `\$\{?(\w+)\}?` and was blind to `${#X}`, `${!X}` and
# `$((X))` — blind review walked a runner-supplied `GITHUB_RUN_ATTEMPT`
# straight through the free-variable check with `[ "$((GITHUB_RUN_ATTEMPT))"
# -gt 1 ]`, which is empty here and non-empty on every job re-run in CI.
# `test_the_free_variable_scanner_sees_every_expansion_spelling` pins the list.
# Everything a verdict step is allowed to declare. An accept-set, because the
# dangerous keys are the ones nobody has thought of yet.
_GATE_STEP_KEYS = frozenset({"name", "id", "env", "run", "shell", "if"})

_SHELL_VAR = re.compile(r"\$\{[#!]?([A-Za-z_]\w*)|\$([A-Za-z_]\w*)")
_ARITH = re.compile(r"\$\(\((.*?)\)\)", re.S)
_IDENT = re.compile(r"[A-Za-z_]\w*")
# Command substitution, excluding `$((`. A gate that shells out can read
# anything the runner exported without naming it in `env:` at all.
_COMMAND_SUB = re.compile(r"\$\((?!\()|`")
# ⛔ The shell spellings of `continue-on-error`. Enforced on the GATE SCRIPT
# only, and the narrowness is deliberate rather than lazy: `python-tests-run`
# has a live, legitimate `|| true` on its "Install mtail" step, so the same
# rule applied across the decision path would red an existing best-effort
# install. Narrowing it to "steps that run tests" is the axis the sibling
# module documents two failed attempts at — `pytest tests/` resolves to no
# tracked file, so the obvious predicate excludes 3 of the 5 legs' primary
# test steps. A gate job has no best-effort work by construction, so the rule
# is exact there and the leg half is recorded as a gap instead of shipped as
# a guess. → the `|| true` entry in the module docstring's gap list.
_EXIT_SWALLOW = re.compile(r"\|\|\s*(?:true|:)(?=\s|$|;|&)|(?m:^\s*set\s+\+e\b)")
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
        bash falls back to `sh`. `defaults:` is refused wholesale rather than
        key by key, which also covers `working-directory` (the sibling module
        burned on exactly that key);
      * a `${{ }}` expression inside `run:` — the runner substitutes those
        before bash starts, so a matrix that only sets env would execute a
        different script than CI does;
      * a `$VAR` the step neither binds nor supplies itself — the matrix
        leaves it empty while the runner supplies a value;
      * ⛔ EXTRA `env:` anywhere in the job. This is an accept-set, not a list
        of dangerous names: the step's `env:` must be exactly the bindings
        this module models, and the job and workflow must declare none. The
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
    workflow = _load(CI_WORKFLOW)
    jobs = workflow["jobs"]
    problems = []
    for gate in _gates_or_fail():
        job = jobs[gate.job_id]
        steps = job.get("steps") or []
        step = steps[gate.step]
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
                    "module does not simulate. (The sibling filter guard "
                    "burned on that same key.)")
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
        modelled = {gate.bindings.detect_result[0]}
        modelled |= set(gate.bindings.changed_names)
        modelled |= {name for name, _job in gate.bindings.legs}
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
        swallowed = _EXIT_SWALLOW.findall(gate.script)
        if swallowed:
            problems.append(
                f"{gate}: the gate script swallows an exit status "
                f"({swallowed}). `|| true`, `|| :` and `set +e` are the shell "
                "spellings of `continue-on-error`, which this module bans "
                "three lines up — banning one and not the other protects the "
                "spelling rather than the property.")
        if _COMMAND_SUB.search(gate.script):
            problems.append(
                f"{gate}: the gate script uses command substitution. It can "
                "then read runner state that appears in no `env:` block, which "
                "is exactly what the free-variable check exists to prevent.")
        if "${{" in gate.script:
            problems.append(
                f"{gate}: the gate script interpolates `${{{{ }}}}` directly "
                "instead of binding through `env:`. The runner substitutes "
                "that before bash starts, so this module would execute a "
                "different script than CI runs — and an interpolated result "
                "string is unquoted shell input besides.")
        bound = {gate.bindings.detect_result[0]}
        bound |= set(gate.bindings.changed_names)
        bound |= {name for name, _job in gate.bindings.legs}
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
    whether the block runs at all. The sibling
    `tests/ops/test_ci_path_filter_coverage.py` has the same predicate for
    gated legs and structurally never reaches a gate job (it skips `always()`
    jobs by construction).

    ⚠️ ONLY the verdict step, and only conditions, and this narrowing is the
    correction of an over-reach. An earlier version refused a condition on
    ANY step of the gate job, which reds `if: failure()` on a diagnostic
    upload — a step that by definition can only run when the job has already
    failed, and cannot turn it green. Constraining what a gate job may
    contain is done in `test_gate_scripts_are_executable_the_way_github_runs
    _them`, on the one axis that does matter: nothing may run BEFORE the
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
        step = (_load(CI_WORKFLOW)["jobs"][gate.job_id]["steps"])[gate.step]
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
    """Ban the key on the gate and on every job whose result it reads.

    Three reasons, only the first fully documented — they are kept separate on
    purpose, because collapsing them into one confident sentence is how a
    guard acquires a load-bearing lie:

      1. STEP level, documented: "When a `continue-on-error` step fails, the
         `outcome` is `failure`, but the final `conclusion` is `success`". The
         gate's `exit 1` is discarded and the job — the required check —
         concludes success.
      2. JOB level on a job the gate READS: `needs.<job>.result` is reported
         as `success` for a failed continue-on-error job. Community-reported,
         not in the docs (actions/toolkit#1739 is open about exactly this). On
         `detect-changes` that reinstates skip-as-green in one line: the legs
         skip, the gate's `!= "success"` branch never fires, and the
         `*_changed` outputs are empty so the skip reads as "not needed".
      3. JOB level on the gate ITSELF: documented only as "allow a workflow
         run to pass when this job fails". ⛔ Whether the job's OWN check run
         then reports success is NOT documented, and community reports say it
         stays red — so do not rewrite this reason as "the required check goes
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

    ⚠️ Overlap, stated rather than discovered later. `_job_step_files` in
    `tests/ops/test_ci_path_filter_coverage.py` already refuses the key on
    every PATH-GATED leg, with reason (2) spelled out. The two are keyed
    differently and neither subsumes the other: that one asks "is this job
    path-gated", which by construction excludes both the gate (it carries
    `always()`) and the detect job; this one asks "does a gate read this job's
    result", which stops covering a leg the moment a gate stops weighing it.
    Where they do overlap, this one is strictly stronger — the sibling tests
    `job.get("continue-on-error")` for truthiness.

    ⚠️ That sibling check landed in #1402. While this module was being
    written, a grep for the key name over the MAIN repo's working tree
    returned nothing and the check read as absent — because that tree was
    still checked out at `2e61b20a`, three commits behind the `origin/main`
    the worktrees were cut from. A grep answers a question about whatever
    commit the tree it ran in is on, and during worktree-based work that is
    routinely not the commit under discussion. Cite `git -C <the tree you
    mean>` or a blob, not "I grepped it".
    """
    jobs = _load(CI_WORKFLOW)["jobs"]
    problems: list[str] = []
    checked: set[str] = set()
    for gate in _gates_or_fail():
        in_path = {gate.job_id, gate.bindings.detect_result[1],
                   *(job for _n, job, _o in gate.bindings.changed)}
        in_path |= {job_id for _name, job_id in gate.bindings.legs}
        for job_id in sorted(in_path):
            checked.add(job_id)
            job = jobs[job_id]
            if "continue-on-error" in job:
                problems.append(
                    f"ci.yml::{job_id} (decision path of {gate}) sets "
                    f"job-level `continue-on-error: "
                    f"{job['continue-on-error']!r}`")
            for step in (job.get("steps") or []):
                if "continue-on-error" in step:
                    problems.append(
                        f"ci.yml::{job_id} step "
                        f"{step.get('name', step.get('uses'))!r} (decision "
                        f"path of {gate}) sets `continue-on-error: "
                        f"{step['continue-on-error']!r}`")
    assert not problems, "\n  ".join([""] + problems)

    # Anti-vacuity, scoped to what this assertion is actually about: every
    # gate and every detect job must have been examined. ⚠️ It deliberately
    # does NOT demand every path-gated job — an earlier version did, and an
    # ordinary new downstream job then produced a second failure, inside a
    # test named after `continue-on-error`, whose message talked about
    # decision paths. Whether every path-gated job is weighed is asserted by
    # `test_every_path_gated_leg_is_watched_by_a_gate`, and saying it twice
    # in two voices helps nobody.
    missing = ({gate.job_id for gate in _gates_or_fail()}
               | set(_detect_jobs(CI_WORKFLOW))) - checked
    assert not missing, (
        f"{sorted(missing)} never entered any gate's decision path, so this "
        "assertion never looked at them.")


@needs_bash
@pytest.mark.parametrize("gate", _ci_gates(), ids=lambda gate: gate.job_id)
def test_gate_exit_codes_match_the_contract(gate: Gate) -> None:
    """Run the real gate script over every documented detect and `*_changed`
    value, against the leg covering set — see `_leg_tuples` for what that
    design buys and what it gives up."""
    cells = _run_matrix(gate.script, gate)
    problems = _matrix_violations(gate, cells)
    # ⛔ Counted separately. The stderr finding is about the gate, not about a
    # cell, and folding it into the cell count printed "1 of 75 cells
    # disagree" when zero cells disagreed — sending the reader to look for an
    # exit code that was never wrong.
    per_cell = [line for line in problems if "contract says" in line]
    whole_gate = [line for line in problems if line not in per_cell]
    assert not problems, (
        f"{len(per_cell)} of {len(cells)} cells disagree with the CI-ROI 6C "
        f"contract for {gate} (required check {gate.check_name!r})"
        + (f", plus {len(whole_gate)} whole-gate finding(s)"
           if whole_gate else "")
        + ":\n  " + "\n  ".join((per_cell[:20] + whole_gate))
        + ("\n  …" if len(per_cell) > 20 else ""))
    seen = {cell.rc for cell in cells}
    assert seen == {0, 1}, (
        f"{gate} produced exit codes {sorted(seen)} across {len(cells)} "
        "cells. Both 0 and 1 must occur — anything else means the script did "
        "not run, or the gate stopped deciding anything.")


# ── skip tolerance vs. what the legs are actually gated on ─────────────────

# ⛔ Legs whose `if:` consults a filter the gate does NOT weigh, each with the
# reachability argument for why the gap is latent. Exact set: new drift reds,
# and a closed gap must be removed rather than left as a stale excuse.
KNOWN_SKIP_TOLERANCE_GAPS = {
    ("ci.yml", "go-tests", "go-tests-threshold-exporter"): (
        frozenset({"docs_changed"}),
        "the leg runs on `go_changed || docs_changed` (it decodes every "
        "documented `_defaults.yaml` sample), while the gate forgives a skip "
        "whenever `go_changed != 'true'`. A docs-only PR whose leg skipped for "
        "a NON-path reason would be waved through. Latent, not live: that "
        "leg's only `needs:` is detect-changes, and a broken detect job is "
        "already caught by the gate's first branch, so there is no other way "
        "for it to skip while its own `if:` was true. Closing it needs per-leg "
        "tolerance predicates — the shared `for` loop cannot express them — "
        "and getting that wrong reds every docs-only PR, so #1397 does not "
        "attempt it.",
    ),
}


def test_gate_skip_tolerance_weighs_every_filter_its_legs_gate_on() -> None:
    """A gate may only forgive a skip it can prove was legitimate.

    A leg skips exactly when its own `if:` is false. The gate approximates
    that with the one `*_changed` output it binds. Where a leg consults a
    filter the gate does not, there is an assignment in which the gate
    forgives a skip the leg's condition did not cause — so the approximation
    has to be recorded, with why it is unreachable, rather than assumed.
    """
    gated = _path_gated_jobs(CI_WORKFLOW)
    found = {}
    for gate in _gates_or_fail():
        for _name, job_id in gate.bindings.legs:
            missing = gated.get(job_id, frozenset()) - gate.bindings.changed_outputs
            if missing:
                found[("ci.yml", gate.job_id, job_id)] = missing

    ledgered = {key: value[0] for key, value in KNOWN_SKIP_TOLERANCE_GAPS.items()}
    new_gaps = {k: sorted(v) for k, v in found.items() if ledgered.get(k) != v}
    stale = {k: sorted(v) for k, v in ledgered.items() if found.get(k) != v}
    assert found == ledgered, (
        "the gates' skip tolerance drifted from what their legs gate on.\n"
        f"  new gaps: {new_gaps}\n"
        f"  closed or stale ledger entries: {stale}\n"
        "Each entry needs the filter names AND the argument for why a leg "
        "cannot skip while its own condition was true.")


_TICKET = re.compile(r"#\d{3,}")


def test_every_ledger_row_states_a_reason_and_a_ticket() -> None:
    """The reason strings are the only thing standing between a ledger and a
    waiver, and until this test nothing read them.

    All three ledger assertions elsewhere compare KEYS (and, for skip
    tolerance, the filter set) — the prose was never touched by any assertion,
    so "with a reason and a ticket" was an instruction with no enforcement.
    This repo's other exit-locked ledgers require a substantive justification
    per row; so does this one. ⚠️ It checks length and a ticket reference, not
    whether the reason is TRUE — that is what a reviewer is for.
    """
    rows = [(f"UNMODELLED_GATE_SHAPED_JOBS[{key!r}]", reason)
            for key, reason in UNMODELLED_GATE_SHAPED_JOBS.items()]
    rows += [(f"KNOWN_SKIP_TOLERANCE_GAPS[{key!r}]", value[1])
             for key, value in KNOWN_SKIP_TOLERANCE_GAPS.items()]
    rows += [(f"UNWATCHED_PATH_GATED_JOBS[{key!r}]", reason)
             for key, reason in UNWATCHED_PATH_GATED_JOBS.items()]
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
    assert not problems, "\n  ".join([""] + problems)
    assert rows, (
        "all three ledgers are empty, so this assertion checked nothing. If the "
        "last entry was genuinely closed, delete this test with it.")


# ═══════════════════════════════════════════════════════════════════════════
# negative controls: prove each mechanism can say NO
# ═══════════════════════════════════════════════════════════════════════════

_SYNTHETIC_GATE = """\
echo "detect: $ZQ_DETECT"
echo "leg: $ZQ_LEG"
if [ "$ZQ_DETECT" != "success" ]; then
  exit 1
fi
if [ "$ZQ_LEG" = "success" ]; then
  exit 0
fi
if [ "$ZQ_LEG" = "skipped" ] && [ "$ZQ_ANY_CHANGED" != "true" ]; then
  exit 0
fi
exit 1
"""


def _synthetic_workflow(directory: Path, *, detect_env: str, changed_env: str,
                        leg_env: str | None, changed_output: str) -> Path:
    """A minimal workflow with the gate shape, written under `directory`.

    Every call gets its OWN directory so the `_load` cache cannot serve a
    stale parse — the cheap alternative, calling `cache_clear()`, would also
    throw away the ci.yml parse the rest of the module shares.
    """
    directory.mkdir(parents=True, exist_ok=True)
    env = {
        detect_env: "${{ needs.zq-detect.result }}",
        changed_env: "${{ needs.zq-detect.outputs.%s }}" % changed_output,
    }
    if leg_env:
        env[leg_env] = "${{ needs.zq-leg.result }}"
    workflow = {
        "name": "zq",
        "jobs": {
            "zq-detect": {"steps": [{"uses": "dorny/paths-filter@v3"}]},
            "zq-leg": {
                "needs": "zq-detect",
                "if": "needs.zq-detect.outputs.%s == 'true'" % changed_output,
                "steps": [{"run": "true"}],
            },
            "zq-gate": {
                "needs": ["zq-detect", "zq-leg"],
                "if": "always()",
                "steps": [{"name": "verify", "env": env,
                           "run": _SYNTHETIC_GATE}],
            },
        },
    }
    path = directory / "zq.yml"
    path.write_text(yaml.safe_dump(workflow, sort_keys=False),
                    encoding="utf-8", newline="\n")
    return path


def test_discovery_reads_the_env_names_instead_of_knowing_them(
        tmp_path: Path) -> None:
    """Same gate, two spellings, plus one shape that is not a gate at all.

    ⛔ The third case is the point: an implementation that simply returned
    every `if: always()` job would pass the first two. Requiring the same
    input shape to be answered DIFFERENTLY once a binding is removed is what a
    hard-coded list cannot fake.

    The filter name `zznever_changed` exists nowhere in this repo — discovery
    that consulted a list of known filter names would miss it.
    """
    spellings = (
        ("DETECT_RESULT", "PY_CHANGED", "RUN_RESULT"),
        ("ZQ_DETECT", "ZQ_ANY_CHANGED", "ZQ_LEG"),
    )
    for index, (detect_env, changed_env, leg_env) in enumerate(spellings):
        path = _synthetic_workflow(
            tmp_path / f"case{index}", detect_env=detect_env,
            changed_env=changed_env, leg_env=leg_env,
            changed_output="zznever_changed")
        gates = _gates(path)
        assert [gate.job_id for gate in gates] == ["zq-gate"], (detect_env,
                                                               gates)
        assert gates[0].bindings.detect_result == (detect_env, "zq-detect")
        assert gates[0].bindings.changed == ((changed_env, "zq-detect",
                                              "zznever_changed"),)
        assert gates[0].bindings.legs == ((leg_env, "zq-leg"),)

    without_leg = _synthetic_workflow(
        tmp_path / "noleg", detect_env="DETECT_RESULT",
        changed_env="PY_CHANGED", leg_env=None,
        changed_output="zznever_changed")
    assert _gates(without_leg) == (), (
        "a step that binds only the detect job's own result and output is not "
        "an aggregate gate — it decides nothing about a leg — yet discovery "
        "reported one.")


def test_the_free_variable_scanner_sees_every_expansion_spelling() -> None:
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
            "zq-detect": {"steps": [{"uses": "dorny/paths-filter@v3"}]},
            "zq-gate": {
                "if": "always()",
                "steps": [{"env": {
                    "D": "${{ needs.zq-detect.result }}",
                    "C": "${{ needs.zq-detect.outputs.zq_changed }}",
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
    gate, = _gates(_synthetic_workflow(
        tmp_path / "ok", detect_env="ZQ_DETECT", changed_env="ZQ_ANY_CHANGED",
        leg_env="ZQ_LEG", changed_output="zq_changed"))

    assert _matrix_violations(gate, _run_matrix(_SYNTHETIC_GATE, gate)) == [], (
        "the reference gate script does not satisfy the contract this module "
        "asserts — the oracle, not the tree, is wrong.")

    broken = _SYNTHETIC_GATE.replace(
        'if [ "$ZQ_DETECT" != "success" ]; then\n  exit 1\nfi\n', "")
    assert broken != _SYNTHETIC_GATE, "the mutation did not apply"
    assert _matrix_violations(gate, _run_matrix(broken, gate)), (
        "dropping the detect check produced no violation — the comparison "
        "cannot tell a fail-closed gate from a fail-open one.")

    lenient = _SYNTHETIC_GATE.replace(
        'if [ "$ZQ_LEG" = "skipped" ] && [ "$ZQ_ANY_CHANGED" != "true" ]; then',
        'if [ "$ZQ_LEG" = "skipped" ]; then')
    assert lenient != _SYNTHETIC_GATE, "the mutation did not apply"
    assert _matrix_violations(gate, _run_matrix(lenient, gate)), (
        "dropping the `*_changed` half produced no violation — skip-as-green "
        "would go unnoticed.")

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
    gate, = _gates(_synthetic_workflow(
        tmp_path / "syntax", detect_env="ZQ_DETECT",
        changed_env="ZQ_ANY_CHANGED", leg_env="ZQ_LEG",
        changed_output="zq_changed"))

    cells = _run_matrix('if [ "$ZQ_DETECT" = "success" ; then exit 0; fi\n',
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
    broken = _run_matrix('if [ "$ZQ_DETECT" = "success" ]; then\n', gate)
    assert not ({cell.rc for cell in broken} & {0, 1}), (
        f"a parse error answered {sorted({c.rc for c in broken})}; if bash "
        "ever reports 0 or 1 for a script it refused to run, the exit-code "
        "defence stops separating 'never ran' from 'decided'.")
    assert len(_matrix_violations(gate, broken)) > len(broken), (
        "every cell of an unrunnable script should mismatch, plus the stderr "
        "report.")
