"""The three ci.yml aggregate gates are required checks with no assertions.

#1397. `ci.yml` reports three REQUIRED status checks — `Python Tests (3.13)`,
`Go Tests (1.26)`, `Portal Tests` — from three jobs that run no tests at all.
Each is a pure aggregation gate: `if: always()`, a handful of
`${{ needs.*.result }}` bindings in one step's `env:`, and ~10 lines of shell
that decide pass/fail. Those ten lines are the whole reason a path-SKIPPED
test leg does not silently satisfy branch protection, and until this module
nothing asserted anything about them.

Measured against the ci.yml of `17a05bb5` (byte-identical in `72fdaf56`), one
mutation at a time, baseline green, each restored and byte-compared. The
counterfactual surface was grepped out of `tests/` rather than picked: every
module mentioning `.github/workflows` — 22 modules / 697 tests for the first
batch. Only `test_ci_path_filter_coverage.py` ever reacted, so the second
batch ran the 7 workflow-parsing modules (368 tests) that could plausibly
have. FOURTEEN single-edit mutations left all of it green:

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

⚠️ What this module does NOT cover. It cannot check that these three names
are actually configured as required checks — that lives in GitHub settings,
and the only in-repo trace is the unenforced checklist in
`docs/internal/iac-lint-baseline.md`. A gate RENAME is deliberately not
asserted either: branch protection then waits forever for a check that never
reports, which blocks the merge — fail-closed, so it needs no guard here.

## How a gate is FOUND (derived, not a list of three job ids)

A gate is any job with a step whose `env:` binds, through `${{ }}`:
  * the `.result` of a job that runs `dorny/paths-filter` (the detect job),
  * a `*_changed` entry of that same detect job's `.outputs`,
  * and the `.result` of at least one job that is NOT a detect job.

The env NAMES are read out of that mapping (`RUN_RESULT`, `R_AM`, `R_PORTAL`
— all different today), so a fourth gate written tomorrow inherits every
assertion here without editing this file. ⛔ Pinning the three job ids instead
would have been "a denylist that happens to contain today's members", the
shape this repo keeps re-learning — see the ⛔ block above
`_condition_triggers` in the sibling module
`tests/ops/test_ci_path_filter_coverage.py`, where two successive spellings of
the same idea were defeated in opposite directions.

Discovery is cross-checked against an INDEPENDENT route through the same
workflow — the set of jobs whose own `if:` consults a detect output — so a
discovery that quietly finds nothing cannot pass.

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

The domain is complete rather than sampled. `needs.<job>.result` has exactly
four documented values (success / failure / cancelled / skipped — contexts
reference), plus the empty string for "the expression did not resolve", which
is the state #1397's failure scenario is actually about. dorny emits `true` or
`false`, plus the same empty string. So the matrix is `5^(1+legs) x 3` cells:
75 for a one-leg gate, 1875 for `go-tests`.

⚠️ Cost, measured: ~2 s on Linux (where a `bash` spawn is ~3 ms), ~80 s on a
Windows dev host (~150 ms). That is the price of asserting the accepted set
exactly rather than spot-checking five rows.
"""

from __future__ import annotations

import functools
import itertools
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

    Derived from the tree, so a NEW workflow adopting the pattern is scanned
    the day it lands. `sorted` keeps failure messages stable.
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
    changed: tuple[str, str, str]             # (env name, detect job, output)
    legs: tuple[tuple[str, str], ...]         # ((env name, job id), …) sorted


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
                "Teach the parser the shape, or move the reference out of "
                "`env:` deliberately.")

    detect_results = {n: j for n, j in results.items() if j in detect}
    leg_results = {n: j for n, j in results.items() if j not in detect}
    detect_outputs = {
        n: (j, o) for n, (j, o) in outputs.items()
        if j in detect and o.endswith("_changed")
    }
    if not (detect_results and leg_results and detect_outputs):
        return None

    if len(detect_results) != 1 or len(detect_outputs) != 1:
        raise AssertionError(
            f"{path.name}::{job_id} binds {len(detect_results)} detect "
            f"result(s) and {len(detect_outputs)} `*_changed` output(s). The "
            "exit-code oracle here is written for exactly one of each; a gate "
            "that weighs several filters has a skip-tolerance rule this "
            "module has not modelled. Model it before adding the binding.")

    (dr_name, dr_job), = detect_results.items()
    (ch_name, (ch_job, ch_out)), = detect_outputs.items()
    return _Bindings(
        detect_result=(dr_name, dr_job),
        changed=(ch_name, ch_job, ch_out),
        legs=tuple(sorted(leg_results.items())),
    )


@dataclass(frozen=True)
class Gate:
    workflow: str
    job_id: str
    check_name: str
    condition: str
    step: int
    script: str
    bindings: _Bindings

    def __str__(self) -> str:
        return f"{self.workflow}::{self.job_id}"


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
        gates.append(Gate(
            workflow=path.name,
            job_id=job_id,
            check_name=str(job.get("name") or job_id),
            condition=str(job.get("if", "")),
            step=index,
            script=str(step.get("run") or "").replace("\r\n", "\n"),
            bindings=bindings,
        ))
    return tuple(gates)


def _ci_gates() -> tuple[Gate, ...]:
    return _gates(CI_WORKFLOW)


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


# ── the independent route: which jobs are path-gated at all ────────────────

_IF_GATE_REF = re.compile(r"needs\.([\w-]+)\.outputs\.(\w+_changed)")


def _path_gated_jobs(path: Path) -> dict[str, frozenset[str]]:
    """job id -> the `*_changed` outputs its OWN `if:` consults.

    This reads the job's `if:`; `_gates` reads a step's `env:`. Two different
    keys over the same file, which is what makes the cross-check below say
    something — a discovery bug in one route cannot hide in the other.
    """
    detect = _detect_jobs(path)
    gated: dict[str, frozenset[str]] = {}
    for job_id, job in _load(path)["jobs"].items():
        names = frozenset(
            output
            for job_ref, output in _IF_GATE_REF.findall(str(job.get("if", "")))
            if job_ref in detect
        )
        if names:
            gated[job_id] = names
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
        "EVERY `skipped`, including one caused by a broken detect job. That "
        "is #1398, deliberately out of scope for #1397. ⛔ Do not silence "
        "this entry by widening discovery — widening it without also changing "
        "the job would make this module assert a contract that job does not "
        "implement, and the assertion would then have to be weakened to fit.",
}


# ── the exit-code contract ─────────────────────────────────────────────────

# `needs.<job_id>.result`: "Possible values are success, failure, cancelled,
# or skipped" (contexts reference). The empty string is this module's own
# addition — it is what an unresolvable expression renders as, and it is the
# exact state #1397 describes (a failed detect job's outputs read empty). It
# is NOT documented; including it only makes the matrix stricter.
_RESULT_DOMAIN = ("success", "failure", "cancelled", "skipped", "")
# dorny emits the string "true" or "false"; empty for the same reason.
_CHANGED_DOMAIN = ("true", "false", "")


def _expected_exit(detect: str, legs: tuple[str, ...], changed: str) -> int:
    """The CI-ROI 6C contract, transcribed from ci.yml's own comment block.

        success                                   -> PASS
        skipped AND detect said not needed        -> PASS
        anything else, or detect itself not green -> FAIL

    ⛔ Read this as a SPECIFICATION. It was written from the design note, not
    from the shell — an oracle copied off the implementation asserts only that
    the implementation still does what it does.
    """
    if detect != "success":
        return 1
    for result in legs:
        if result == "success":
            continue
        if result == "skipped" and changed != "true":
            continue
        return 1
    return 0


def _cases(leg_count: int):
    """The full cross product of the documented domains."""
    for detect in _RESULT_DOMAIN:
        for changed in _CHANGED_DOMAIN:
            for legs in itertools.product(_RESULT_DOMAIN, repeat=leg_count):
                yield detect, legs, changed


def _env_for(gate: Gate, detect: str, legs: tuple[str, ...],
             changed: str) -> dict[str, str]:
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
    env[gate.bindings.changed[0]] = changed
    for (name, _job), value in zip(gate.bindings.legs, legs):
        env[name] = value
    return env


@dataclass(frozen=True)
class _Cell:
    detect: str
    legs: tuple[str, ...]
    changed: str
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
    mismatches at once. `_matrix_violations` additionally refuses any cell
    that wrote to stderr — that one is about noise, not about execution, and a
    gate that deliberately writes to `>&2` would have to be modelled here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "gate.sh"
        path.write_text(script, encoding="utf-8", newline="\n")
        target = path.as_posix()
        cells = list(_cases(len(gate.bindings.legs)))

        def run(case: tuple[str, tuple[str, ...], str]) -> _Cell:
            detect, legs, changed = case
            done = subprocess.run(
                [BASH, "-e", target],
                env=_env_for(gate, detect, legs, changed),
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=60)
            return _Cell(detect, legs, changed, done.returncode, done.stderr)

        workers = min(64, (os.cpu_count() or 4) * 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return tuple(pool.map(run, cells))


def _matrix_violations(gate: Gate, cells: tuple[_Cell, ...]) -> list[str]:
    """PURE. Compare observed exit codes against the contract.

    Split out from the runner so a degenerate input can prove the comparison
    is able to say NO — a tripwire that only ever sees the real, passing gate
    is a tripwire nobody has watched fire.
    """
    problems: list[str] = []
    for cell in cells:
        want = _expected_exit(cell.detect, cell.legs, cell.changed)
        noise = cell.stderr.strip()
        if cell.rc == want and not noise:
            continue
        legs = ", ".join(
            f"{name}={value!r}"
            for (name, _job), value in zip(gate.bindings.legs, cell.legs))
        problems.append(
            f"{gate}: {gate.bindings.detect_result[0]}={cell.detect!r}, "
            f"{gate.bindings.changed[0]}={cell.changed!r}, {legs} "
            f"-> exit {cell.rc} (contract says {want})"
            + (f"; stderr={noise!r}" if noise else ""))
    return problems


# ═══════════════════════════════════════════════════════════════════════════
# assertions on the tree
# ═══════════════════════════════════════════════════════════════════════════

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
    watched = {
        job_id: gate.job_id
        for gate in _ci_gates() for _name, job_id in gate.bindings.legs
    }
    gated = _path_gated_jobs(CI_WORKFLOW)

    problems = [
        f"ci.yml::{job_id} is path-gated on {sorted(gated[job_id])} but no "
        "aggregate gate weighs its `.result`. When it path-skips, nothing "
        "decides whether that skip was legitimate."
        for job_id in sorted(set(gated) - set(watched))
    ] + [
        f"ci.yml::{job_id} is weighed by the {watched[job_id]} gate but is no "
        "longer path-gated. Either the gate watches the wrong job, or the leg "
        "lost its `if:` and the gate's skip-tolerance branch is now dead code."
        for job_id in sorted(set(watched) - set(gated))
    ]
    assert not problems, (
        f"{len(problems)} gate/leg mismatch(es) in ci.yml:\n  "
        + "\n  ".join(problems)
        + f"\n(gates discovered: {sorted(g.job_id for g in _ci_gates())}; "
          f"path-gated jobs: {sorted(gated)})")


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
    for gate in _ci_gates():
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
            if gated.intersection(_needs(job)):
                detected.add((path.name, job_id))

    unmodelled = detected - modelled
    expected = set(UNMODELLED_GATE_SHAPED_JOBS)
    assert unmodelled == expected, (
        "gate-shaped jobs this module cannot model changed.\n"
        f"  newly unmodelled: {sorted(unmodelled - expected)}\n"
        f"  ledgered but now modelled or gone: {sorted(expected - unmodelled)}\n"
        "Add the job to UNMODELLED_GATE_SHAPED_JOBS with a reason and a "
        "ticket, or make it modellable. Deleting an entry without changing "
        "the job is how a known hole becomes an unknown one.")
    assert modelled, (
        "no aggregate gate was discovered in ANY workflow that owns a "
        "dorny/paths-filter job — discovery is broken, not the tree.")


_SHELL_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_SHELL_ASSIGNED = re.compile(
    r"^\s*(?:for\s+([A-Za-z_]\w*)\s+in\b|([A-Za-z_]\w*)=)", re.M)


def test_gate_scripts_are_executable_the_way_github_runs_them() -> None:
    """The execution model has to be true, or the matrix proves nothing.

    Three ways it could quietly stop being true, all fail-closed here:
      * a `shell:` override — GitHub's default for a `run:` step is
        `bash -e {0}`, and any other value changes both the flags and the
        failure semantics (`shell: bash` alone adds `-o pipefail`);
      * a `${{ }}` expression inside `run:` — the runner substitutes those
        before bash starts, so a matrix that only sets env would execute a
        different script than CI does;
      * a `$VAR` the step neither binds nor assigns — the matrix leaves it
        empty while CI supplies a value, so that input is untested.
    """
    jobs = _load(CI_WORKFLOW)["jobs"]
    problems = []
    for gate in _ci_gates():
        step = (jobs[gate.job_id].get("steps") or [])[gate.step]
        if "shell" in step:
            problems.append(
                f"{gate}: the gate step sets `shell: {step['shell']!r}`. This "
                "module runs it as `bash -e <file>`, GitHub's documented "
                "default; model the override before using it.")
        if "${{" in gate.script:
            problems.append(
                f"{gate}: the gate script interpolates `${{{{ }}}}` directly "
                "instead of binding through `env:`. The runner substitutes "
                "that before bash starts, so this module would execute a "
                "different script than CI runs — and an interpolated result "
                "string is unquoted shell input besides.")
        bound = {gate.bindings.detect_result[0], gate.bindings.changed[0]}
        bound |= {name for name, _job in gate.bindings.legs}
        assigned = {
            name for pair in _SHELL_ASSIGNED.findall(gate.script)
            for name in pair if name
        }
        free = sorted({
            name for name in _SHELL_VAR.findall(gate.script)
            if name not in bound and name not in assigned
        })
        if free:
            problems.append(
                f"{gate}: the script reads {free}, which the step neither "
                "binds in `env:` nor assigns. The matrix leaves those empty, "
                "so whatever CI puts there is untested.")
    assert not problems, "\n  ".join([""] + problems)


def test_gate_jobs_run_unconditionally() -> None:
    """`if:` must be exactly `always()`.

    GitHub's troubleshooting page for required status checks lists "the job
    depended on a job that failed" with the result "The dependent job is
    skipped and may not block merging", and its stated remedy is to use
    `always()` with `needs` for required checks that depend on other jobs —
    because a job skipped by a conditional "will report its status as
    'Success'".

    ⛔ Exact equality, not `"always()" in condition`. `always() && github
    .event_name != 'schedule'` contains it and still lets the gate skip itself
    into a green required check. The accepted set is one string; a richer
    condition must be modelled here first.
    """
    problems = [
        f"{gate}: `if: {gate.condition!r}` (required check {gate.check_name!r})"
        for gate in _ci_gates() if gate.condition.strip() != "always()"
    ]
    assert not problems, (
        "an aggregate gate no longer runs unconditionally:\n  "
        + "\n  ".join(problems)
        + "\nA gate that skips reports Success to branch protection, which is "
          "precisely the outcome it exists to prevent.")


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

    ⛔ Do not go looking for that sibling check by grepping the key name:
    the string is wrapped across source lines there, so the grep returns
    nothing and the check reads as absent. This module's first draft
    "corrected" a true claim into a false one on exactly that evidence — the
    same class of trap as #1383, in the file that is about to gain a guard.
    """
    jobs = _load(CI_WORKFLOW)["jobs"]
    problems: list[str] = []
    checked: set[str] = set()
    for gate in _ci_gates():
        in_path = {gate.job_id, gate.bindings.detect_result[1],
                   gate.bindings.changed[1]}
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

    # Anti-vacuity from an independent route: the decision path must contain
    # every path-gated job and every detect job in the workflow. A floor typed
    # as a number here would shrink together with whatever broke discovery.
    missing = (set(_path_gated_jobs(CI_WORKFLOW)) | set(_detect_jobs(CI_WORKFLOW))
               ) - checked
    assert not missing, (
        f"{sorted(missing)} never entered any gate's decision path, so this "
        "assertion never looked at them.")


@needs_bash
@pytest.mark.parametrize("gate", _ci_gates(), ids=lambda gate: gate.job_id)
def test_gate_exit_codes_match_the_contract(gate: Gate) -> None:
    """Run the real gate script over the complete env cross product."""
    cells = _run_matrix(gate.script, gate)
    problems = _matrix_violations(gate, cells)
    assert not problems, (
        f"{len(problems)} of {len(cells)} cells disagree with the CI-ROI 6C "
        f"contract for {gate} (required check {gate.check_name!r}):\n  "
        + "\n  ".join(problems[:20])
        + ("\n  …" if len(problems) > 20 else ""))
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
    for gate in _ci_gates():
        for _name, job_id in gate.bindings.legs:
            missing = gated.get(job_id, frozenset()) - {gate.bindings.changed[2]}
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
        assert gates[0].bindings.changed == (changed_env, "zq-detect",
                                             "zznever_changed")
        assert gates[0].bindings.legs == ((leg_env, "zq-leg"),)

    without_leg = _synthetic_workflow(
        tmp_path / "noleg", detect_env="DETECT_RESULT",
        changed_env="PY_CHANGED", leg_env=None,
        changed_output="zznever_changed")
    assert _gates(without_leg) == (), (
        "a step that binds only the detect job's own result and output is not "
        "an aggregate gate — it decides nothing about a leg — yet discovery "
        "reported one.")


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
    rows of this table — the failure mode the #1397 prototype hit. Both
    defences are asserted here rather than assumed.
    """
    gate, = _gates(_synthetic_workflow(
        tmp_path / "syntax", detect_env="ZQ_DETECT",
        changed_env="ZQ_ANY_CHANGED", leg_env="ZQ_LEG",
        changed_output="zq_changed"))

    cells = _run_matrix('if [ "$ZQ_DETECT" = "success" ; then exit 0; fi\n',
                        gate)
    assert {cell.rc for cell in cells} != {0, 1}, (
        "an unparsable script produced exactly the two exit codes a working "
        "gate produces — the witness assertion in the matrix test would not "
        "have noticed.")
    assert any(cell.stderr.strip() for cell in cells), (
        "bash reported no stderr for a syntax error, so the stderr half of "
        "_matrix_violations is measuring nothing.")
    assert _matrix_violations(gate, cells), (
        "a script bash refused to run was accepted as a verdict.")
