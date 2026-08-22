#!/usr/bin/env python3
"""Tests for blast_radius.py — Blast Radius diff engine for CI bot."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "dx"))

import blast_radius as br  # noqa: E402


sys.path.insert(0, str(Path(__file__).resolve().parent))

# ⛔ ONE implementation of "which interpreter does GitHub run this `run:`
# under", imported rather than rewritten. This file used to answer it with
# `_ACTIONS_SHELL_ARGV.get(step.get("shell"))` — a second copy that read ONE of
# the three places the answer lives. GitHub resolves `shell:` as
# step > job `defaults.run` > workflow `defaults.run` > platform default, so
# adding `defaults: {run: {shell: bash}}` at the top of the workflow makes CI
# run `bash --noprofile --norc -eo pipefail` while this harness went on running
# `bash -e`. Measured: the whole module stayed green while every behavioural
# test in it was measuring a different interpreter than CI.
# ⚠️ That is the same "which carrier does my predicate read" failure this file
# fixes elsewhere for shell text — the shell's own configuration has three
# carriers and the old helper read one.
from test_ci_aggregate_gate_contract import (  # noqa: E402
    _SHELL_ARGV,
    BASH,
    _resolved_shell,
)

# Environment variables the harness's own interpreter consumes BEFORE it runs
# the script. A step that sets one of these changes the interpreter's behaviour
# from outside the `run:` text, where no assertion in this file can see it —
# `BASH_ENV` names a file bash sources first, which can set `pipefail`, an
# alias or a `git()` function.
#
# ⛔ Every member is BEHAVIOURALLY VERIFIED, by
# `test_control_every_startup_variable_actually_changes_the_shell` below: for
# each name, the same script runs twice under the harness's own argv, once with
# the variable set and once without, and the outputs must differ. Membership is
# therefore a measurement, not a list.
#
# ⚠️ That control exists because the enumerated version of this set was WRONG
# and said so with an appeal to authority ("the authority is bash(1), not me").
# It carried `ENV` and `IFS`, and measured in this container neither is consumed
# by the argv this harness runs: `env ENV=probe.sh bash -e -c …` does not source
# the file (bash reads ENV only in posix mode or when invoked as `sh`), and bash
# RESETS `IFS` at startup rather than inheriting it. A control test even pinned
# the false premise for `IFS`, so the mistake had a witness testifying for it.
# ⚠️ `ENV` stays out even though posix mode would read it: reaching posix mode
# through the environment needs `POSIXLY_CORRECT`, which is refused below, so
# the compound cannot arise. If that ever changes, the control above is where
# it becomes visible.
_SHELL_STARTUP_ENV = frozenset({
    "BASH_ENV", "SHELLOPTS", "BASHOPTS", "POSIXLY_CORRECT",
})

# CANDIDATES — every variable anyone has proposed for the set above, whether or
# not it belongs there, each with a value and a probe script whose OUTPUT
# differs if and only if the variable took effect. `BASH_ENV` and `ENV` need a
# real file, so their value is filled in per-test.
#
# ⛔ This pool is HARDCODED and independent of `_SHELL_STARTUP_ENV`, because the
# control below compares the two. Deriving it from the set would make removing
# a member remove its own test — the floor taken from the thing it protects,
# which is the failure this file refuses elsewhere.
# ⚠️ `ENV` and `IFS` are here precisely because they were once IN the set and
# are measurably inert under this argv. They are the anti-regression half: the
# control requires them to stay out.
_STARTUP_CANDIDATES = {
    "BASH_ENV": (None, 'echo BODY\n'),
    "SHELLOPTS": ("pipefail", 'set -o | grep "^pipefail" \n'),
    "BASHOPTS": ("xpg_echo", 'shopt xpg_echo || true\n'),
    "POSIXLY_CORRECT": ("1", 'set -o | grep "^posix" \n'),
    "ENV": (None, 'echo BODY\n'),
    "IFS": (";", 'v="a;b"; set -- $v; echo "count=$#"\n'),
}


def _actions_shell_argv(shell: str | None) -> tuple[str, ...] | None:
    """Argv for a resolved `shell:` value, or None if we cannot reproduce it.

    ⚠️ Fail-closed on an unmodelled value (`sh`, `pwsh`, a custom template):
    refusing is right, because reproducing it from a guess would measure a
    program CI does not run.
    """
    if shell not in _SHELL_ARGV:
        return None
    return (BASH,) + _SHELL_ARGV[shell]


# ⛔ ONE anchor for the header-listing block, matched as an ASSIGNMENT to a
# named variable rather than as a spelling of the right-hand side.
#
# It used to be the literal `changed_raw=$(mktemp)`, written out in two places
# — a substring test that finds the step, and an equality test that finds the
# first line of the slice. Measured: quoting it as `changed_raw="$(mktemp)"`,
# which shellcheck-minded reviewers ask for and which changes nothing bash
# does, missed BOTH. Eight tests went red; the message said `found []` and
# named neither `mktemp` nor the other anchor, and repairing only the one the
# message pointed at left the same eight red — the second miss then surfacing
# as a bare `StopIteration` with no message at all.
#
# ⚠️ The variable NAME is still a pin, and deliberately: something has to
# identify this block, and one identifier in one place is a one-line review
# trigger. What is no longer pinned is how its value is written.
#
# ⚠️ DISCLOSED ASYMMETRY with the sibling PR: `test_ci_baseline_substitution.py`
# locates its steps by NAME, so renaming a step is red there and green here.
# The difference is not that one design is better — an anchor is an identifier
# too, and renaming `changed_raw` reds this file (measured) — it is that this
# module locates ONE step, which happens to own a distinctive assignment, while
# that module locates seven across three files, most without one. A contributor
# who renames a step in both files gets two different answers, and the reason
# is here rather than left to look arbitrary.
# ⚠️ Declaration keywords are part of "assigns this name". `export changed_raw=…`
# is the same assignment with a scope keyword in front, and blind review found
# that the bare form of this regex missed it — eight tests red, on an edit that
# changes nothing bash does. The alternation is closed and externally defined
# (these are the POSIX/bash declaration utilities), which is what makes listing
# them a derivation rather than a guess.
_HEADER_ANCHOR = re.compile(
    r"^\s*(?:(?:export|readonly|local|typeset)\s+|declare(?:\s+-\w+)*\s+)?"
    r"changed_raw=")
_ANCHOR_HELP = (
    "The anchor is `_HEADER_ANCHOR` in this file: the line that ASSIGNS "
    "`changed_raw` — any right-hand side, optionally behind a declaration "
    "keyword (`export`/`declare`/`readonly`/`local`/`typeset`). If the "
    "variable was renamed, change that one regex; do not relax it to match any "
    "assignment, and do not delete the tests that depend on it — they are the "
    "only thing running the shipped statement.")

_BLAST_WORKFLOW = Path(REPO_ROOT) / ".github/workflows/blast-radius.yml"

# ── what "the harness runs what CI runs" does and does not cover ────────────
#
# ⛔ This is a DISCLOSURE, not a to-do list. The claim above the anchor rule —
# that the measured region has to be what CI runs — is made over a surface with
# many carriers, and closing them one per review round was measurably not
# converging (each round closed one and the next found another). The stopping
# rule this repo uses elsewhere applies: a guard earns its place when its silent
# failure lets the ORIGINAL defect back, and none of the unguarded rows below
# does that. #1419 is about a comparison baseline being silently substituted in
# the shipped workflow; these rows are about how faithfully this harness
# reproduces the shipped workflow. Losing fidelity costs confidence in the
# tests, not correctness of the thing shipped.
#
#   carrier                                   | status
#   ------------------------------------------|--------------------------------
#   `run:` text above the anchor               | GUARDED (anchor rule)
#   `shell:` at step / job / workflow          | GUARDED (resolved through all
#                                              | three, each row probed by
#                                              | behaviour)
#   `env:` at step / job / workflow            | GUARDED (startup variables
#                                              | refused; membership measured
#                                              | in both directions)
#   `$GITHUB_ENV` written by an EARLIER step   | NOT guarded. Measured: the
#                                              | module stays green. No step in
#                                              | this job writes it today.
#   the anchor step's own `if:`                | NOT guarded. Measured: `if:
#                                              | false` keeps the module green
#                                              | while CI skips the step
#                                              | entirely.
#   `runs-on:` (the default shell is platform  | NOT guarded. Measured: green.
#   dependent)                                 | ⚠️ The premise that a Windows
#                                              | runner defaults to `pwsh` was
#                                              | NOT verified here.
#   composite action / reusable workflow       | NOT measured. Reasoned only:
#                                              | moving the step out makes the
#                                              | owner list empty, which fails
#                                              | closed above.
#   `container:` / `services:` /               | NOT measured.
#   `working-directory:`                       |
#
# ⚠️ Every "measured" above is a run of this module against an edited copy of
# the workflow, not a run on a real runner. Nothing here was observed on
# GitHub's infrastructure.


def _header_listing_step(workflow: Path = _BLAST_WORKFLOW) -> dict:
    """The one step that builds the comment header's changed-files listing.

    ⛔ One lookup, used by both the slice-builder and the runner. Two lookups
    would let the script and the interpreter come from different places, and
    "the harness runs a different program than CI" is the exact failure this
    file is meant to detect rather than commit.

    ⚠️ `workflow` is a PARAMETER so the fail-closed arity below has something
    to be tested against. With the path hardcoded inside the body there was no
    way to hand it a workflow with zero or two owners, so `== 1` could be
    weakened to `>= 0` with the module still green — an assertion in a
    structurally unverifiable state. The sibling module learned this same
    lesson for `_one_verdict_pair`; this file had not.
    """
    doc = yaml.safe_load(Path(workflow).read_text(encoding="utf-8"))
    # ⛔ Named, not `doc["jobs"]`. A workflow with no `jobs:` key, or with
    # `jobs:` present but empty, raised a bare `KeyError`/`AttributeError`
    # here — a traceback naming a dict lookup rather than the thing that was
    # wrong. The synthetic fixtures below all carry a well-formed `jobs:`, so
    # nothing would ever have reached it.
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    assert isinstance(jobs, dict) and jobs, (
        f"{Path(workflow).name} declares no usable `jobs:` mapping, so there "
        f"is nothing to search for the header-listing step")
    owners = [(job, s) for job in jobs.values()
              for s in (job.get("steps") or []) if isinstance(s, dict)
              if any(_HEADER_ANCHOR.match(ln)
                     for ln in (s.get("run") or "").split("\n"))]
    assert len(owners) == 1, (
        f"expected exactly one step assigning `changed_raw`, found "
        f"{[s.get('name') for _job, s in owners]}.\n" + _ANCHOR_HELP)
    job, step = owners[0]
    # ⛔ The interpreter is resolved through GitHub's documented precedence, in
    # the SAME lookup that found the step — two lookups would let the script
    # and the interpreter come from different places.
    shell = _resolved_shell(doc, job, step)
    # ⛔ …and the other carrier: variables bash reads before it runs anything.
    # `BASH_ENV` names a file bash sources first, so a step can set `pipefail`,
    # an alias or a `git()` function without a single character of it appearing
    # in `run:` — where every assertion in this file looks.
    #
    # ⛔ ALL THREE LEVELS. `env:` merges workflow → job → step exactly as
    # `shell:` resolves through three carriers, and the first version of this
    # check read only the step — in the SAME edit that fixed `shell:` for all
    # three. Measured: `env: {BASH_ENV: …}` on the JOB left the module green.
    # Two adjacent attributes, the same three carriers, one of them fixed.
    declared = {}
    for holder in (doc, job, step):
        declared.update((holder.get("env") or {}) if isinstance(holder, dict)
                        else {})
    startup = sorted(set(declared) & _SHELL_STARTUP_ENV)
    assert not startup, (
        f"{step.get('name')!r} runs with {startup} set in `env:` (workflow, "
        f"job or step level). The interpreter consumes those before it runs "
        f"the script — measured, not assumed: see "
        f"`test_control_every_startup_variable_actually_changes_the_shell` — "
        f"so they change what the statements below the anchor do while being "
        f"invisible to every assertion here, which is the same blind spot the "
        f"anchor rule closes for `run:` text. If CI genuinely needs one of "
        f"them, the harness has to apply it too; do not delete this assertion.")
    return step, shell


_ERREXIT_PROBE = "echo BEFORE\nfalse\necho REACHED\n"
_PIPEFAIL_PROBE = "set +e\nfalse | true\necho \"RC=$?\"\n"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("shell, script, must_contain, must_not_contain", [
    # ── the row the shipped workflow actually uses (no `shell:` declared) ──
    # `-e` is on: a failing command aborts, so the line after it never runs.
    # ⛔ Both directions in one row. An absence-only assertion passes when the
    # probe produces NOTHING — measured: degrade the argv so the script never
    # runs and this row goes green on its own. `BEFORE` is the positive half
    # that says the probe reached the shell at all.
    (None, _ERREXIT_PROBE, "BEFORE", "REACHED"),
    # …and pipefail is OFF: a pipeline reports its LAST command's status.
    (None, _PIPEFAIL_PROBE, "RC=0", "RC=1"),
    # ── and the row a declared `shell: bash` selects ──────────────────────
    # ⛔ EVERY row, not just the one in use today. Collapsing this file's own
    # argv table into the shared one left the `bash` row consumed by lookup
    # only, in both modules: measured, dropping `pipefail` from it kept BOTH
    # modules green. A shared table with one behavioural witness guards one
    # row and reads as if it guards the table.
    ("bash", _ERREXIT_PROBE, "BEFORE", "REACHED"),
    # …and here pipefail IS on, which is the whole difference between the rows.
    ("bash", _PIPEFAIL_PROBE, "RC=1", "RC=0"),
], ids=["default-errexit-on", "default-pipefail-off",
        "bash-errexit-on", "bash-pipefail-on"])
def test_control_each_shell_row_behaves_the_way_actions_runs_it(
        shell, script, must_contain, must_not_contain):
    """⛔ The measurement BASE, checked by behaviour rather than by its literal.

    The row for an undeclared `shell:` decides which interpreter every
    behavioural test in this file runs under, and the shipped
    `blast-radius.yml` declares none — so that row is not a fallback, it is the
    row in use. It had no control at all: blind review changed it to
    `("-eo", "pipefail")` — the exact mistake the comment above the table calls
    "the easiest to get wrong" — and then to a bare `()`, and neither weakening
    turned anything red. An assertion comparing the tuple to itself would be a
    tautology, so this runs a probe that can only answer one way per property.

    ⚠️ This is also the round's one "a weakening that looks like an
    improvement" case: adding pipefail is what a shellcheck-minded reviewer
    would call stricter, and the suite could not tell "matches CI" from "does
    not match CI" in either direction.
    """
    argv = _actions_shell_argv(shell)
    assert argv, f"the {shell!r} row disappeared from _SHELL_ARGV"
    proc = subprocess.run(  # subprocess-timeout: ignore
        [shutil.which(argv[0]), *argv[1:], "-c", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = proc.stdout
    assert must_contain in out, (
        f"the harness shell for `shell: {shell!r}` does not behave the way "
        f"Actions runs it; got {out!r} from {argv}")
    assert must_not_contain not in out, (
        f"the harness shell for `shell: {shell!r}` does not behave the way "
        f"Actions runs it: {must_not_contain!r} appeared in {out!r} under "
        f"{argv}. An undeclared shell is `bash -e` with NO pipefail; a declared "
        f"`shell: bash` is `--noprofile --norc -eo pipefail`. A harness that "
        f"adds or drops a flag measures a different program than CI runs")


def _synthetic_workflow(tmp_path: Path, steps: str, *, top: str = "",
                        job: str = "") -> Path:
    """A minimal workflow file, for the fail-closed controls below.

    `top` and `job` inject workflow-level and job-level blocks, so the controls
    can reach the two `shell:` carriers that are not the step.
    """
    path = tmp_path / "synthetic.yml"
    path.write_text(
        "name: Synthetic\non: [push]\n" + top
        + "jobs:\n  j:\n    runs-on: ubuntu-latest\n" + job
        + "    steps:\n" + steps, encoding="utf-8", newline="\n")
    return path


def _tenant(report: dict, tenant_id: str) -> dict:
    """The report row for *tenant_id*, or a failure that says what is missing.

    ⛔ Named, not `next()`. A bare `next()` on a miss raises `StopIteration`
    with no message: a traceback pointing at a generator expression, saying
    nothing about which tenant was expected or what the report did contain.
    Three sites in this file were fixed for that reason and these two were not
    — the cited instance was not the class.
    """
    rows = [t for t in report.get("tenants") or [] if t["tenant_id"] == tenant_id]
    assert len(rows) == 1, (
        f"expected exactly one report row for {tenant_id!r}, found "
        f"{[t['tenant_id'] for t in report.get('tenants') or []]}")
    return rows[0]


def test_control_the_tenant_helper_selects_by_id(tmp_path):
    """⛔ The helper's SELECTION, not just its message.

    Both call sites happen to want a row that is first (or tied) in their
    fixture, so replacing the filter with `[:1]` — ignoring the id entirely —
    left the module green. The message-quality half of this helper was the only
    half anything watched.
    """
    report = {"tenants": [{"tenant_id": "a", "highest_tier": "C"},
                          {"tenant_id": "b", "highest_tier": "A"}]}
    assert _tenant(report, "b")["highest_tier"] == "A"
    assert _tenant(report, "a")["highest_tier"] == "C"
    with pytest.raises(AssertionError, match="exactly one report row"):
        _tenant(report, "missing")


# ⚠️ DISCLOSED, once, for every `parametrize` control in this file: emptying a
# sample table does not turn anything red. pytest renders a zero-row
# parametrize as `1 skipped` with rc=0, so a merge conflict resolved by
# deleting both sides, or a tidy-up of "stale" rows, silently removes the
# control while CI stays green. Written down rather than guarded, because a
# guard for it would be a floor — either a number that expires or one derived
# from the table it is protecting.
_OWNER_STEP = """\
      - name: {name}
        run: |
          changed_raw=$(mktemp)
          echo x > "$changed_raw"
"""


@pytest.mark.parametrize("steps", [
    "      - name: Nothing\n        run: echo hi\n",
    _OWNER_STEP.format(name="A") + _OWNER_STEP.format(name="B"),
], ids=["no-owner", "two-owners"])
def test_control_the_header_step_lookup_is_fail_closed(tmp_path, steps):
    """Zero and two must both raise, or `== 1` is decoration.

    Measured before this control existed: weakening it to `>= 0` left the
    module green, because the shipped workflow has exactly one owner
    and nothing could hand the function anything else.
    """
    with pytest.raises(AssertionError, match="exactly one step assigning"):
        _header_listing_step(_synthetic_workflow(tmp_path, steps))


def test_control_the_header_step_lookup_accepts_a_declared_assignment(tmp_path):
    """…and the refusal must not be "refuse everything".

    `export changed_raw=…` is the same assignment; the bare regex missed it and
    turned eight tests red on an edit bash cannot tell apart.
    """
    steps = ("      - name: Owner\n        run: |\n"
             "          export changed_raw=$(mktemp)\n")
    step, _shell = _header_listing_step(_synthetic_workflow(tmp_path, steps))
    assert step["name"] == "Owner"


@pytest.mark.parametrize("top, job, step_shell, expected", [
    # no declaration anywhere — Actions runs `bash -e`, no pipefail
    ("", "", "", None),
    # ⛔ the workflow-level carrier. Adding this is exactly what someone does to
    # get pipefail repo-wide; it changes the interpreter for EVERY `run:` in
    # the file. Measured before this resolution existed: the whole module
    # stayed green while CI would have been running a different shell.
    ("defaults:\n  run:\n    shell: bash\n", "", "", "bash"),
    # …and the job-level one, which overrides the workflow's.
    ("", "    defaults:\n      run:\n        shell: bash\n", "", "bash"),
    # the step wins over both
    ("defaults:\n  run:\n    shell: bash\n", "", "        shell: sh\n", "sh"),
], ids=["unspecified", "workflow-defaults", "job-defaults", "step-wins"])
def test_control_the_shell_is_resolved_through_every_carrier(
        tmp_path, top, job, step_shell, expected):
    """GitHub's documented precedence: step > job defaults > workflow defaults.

    ⛔ Reading only the step mapping is what let a one-block edit at the top of
    the file change the interpreter CI uses with nothing here noticing.
    """
    steps = ("      - name: Owner\n" + step_shell
             + "        run: |\n          changed_raw=$(mktemp)\n")
    _step, shell = _header_listing_step(
        _synthetic_workflow(tmp_path, steps, top=top, job=job))
    assert shell == expected


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("var", sorted(_STARTUP_CANDIDATES))
def test_control_the_startup_set_matches_what_the_shell_actually_consumes(
        tmp_path, var):
    """⛔ Membership in `_SHELL_STARTUP_ENV` is MEASURED, in both directions.

    Three independent things are compared here and none is derived from
    another: a hardcoded candidate pool, what the harness's own interpreter
    actually does with each candidate, and what the production set claims.

    The enumerated version of that set carried two names bash does not consume
    under this argv — `ENV` (read only in posix mode or when invoked as `sh`)
    and `IFS` (bash RESETS it at startup rather than inheriting it) — wrapped
    in a claim that its authority was bash(1). A control test even pinned the
    false premise for `IFS`, so the mistake had a witness testifying for it.

    ⚠️ Parametrising over the SET instead of over this pool would make removing
    a member remove its own test: measured, dropping `POSIXLY_CORRECT` from the
    set left the module green. The floor cannot come from the thing it guards.
    """
    argv = _actions_shell_argv(None)
    assert argv, "the default row disappeared from _SHELL_ARGV"
    value, script = _STARTUP_CANDIDATES[var]
    if value is None:                      # the two that name a file to source
        sourced = tmp_path / "startup.sh"
        sourced.write_text("echo STARTUP_FILE_SOURCED\n", encoding="utf-8",
                           newline="\n")
        value = str(sourced.as_posix())

    def run(env_extra):
        return subprocess.run(  # subprocess-timeout: ignore
            [shutil.which(argv[0]), *argv[1:], "-c", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, **env_extra}).stdout

    consumed = run({var: value}) != run({})
    assert consumed is (var in _SHELL_STARTUP_ENV), (
        f"{var} is {'' if consumed else 'NOT '}consumed by the interpreter "
        f"this harness runs ({argv}), but it is "
        f"{'in' if var in _SHELL_STARTUP_ENV else 'not in'} "
        f"`_SHELL_STARTUP_ENV`. Refusing an inert variable rejects a legitimate "
        f"`env:` key for no reason; failing to refuse a consumed one leaves the "
        f"blind spot this set exists to close. Fix the SET, not this control.")


@pytest.mark.parametrize("var", sorted(_SHELL_STARTUP_ENV))
def test_control_a_shell_startup_variable_in_env_is_refused(tmp_path, var):
    """The other carrier: what bash reads before it reads the script.

    ⛔ `BASH_ENV` names a file bash sources first, so `set -o pipefail`, an
    `IFS`, an alias or a `git()` function can be injected without one character
    appearing in `run:` — where the anchor rule and every other assertion in
    this file look. Refusing is the only honest answer: the harness does not
    apply the step's `env:`, so it would be measuring a different program.
    """
    steps = (f"      - name: Owner\n        env:\n          {var}: x\n"
             "        run: |\n          changed_raw=$(mktemp)\n")
    with pytest.raises(AssertionError, match="consumes those before it runs"):
        _header_listing_step(_synthetic_workflow(tmp_path, steps))


@pytest.mark.parametrize("level", ["workflow", "job"])
def test_control_a_startup_variable_is_refused_at_every_level(tmp_path, level):
    """⛔ `env:` merges workflow → job → step, exactly like `shell:` resolves.

    Reading only the step is the mistake this file made in the same edit that
    fixed `shell:` for all three carriers — measured: a job-level
    `env: {BASH_ENV: …}` left the module green.
    """
    steps = ("      - name: Owner\n        run: |\n"
             "          changed_raw=$(mktemp)\n")
    top = "env:\n  BASH_ENV: x\n" if level == "workflow" else ""
    job = "    env:\n      BASH_ENV: x\n" if level == "job" else ""
    with pytest.raises(AssertionError, match="consumes those before it runs"):
        _header_listing_step(
            _synthetic_workflow(tmp_path, steps, top=top, job=job))


def test_control_an_ordinary_env_key_is_not_refused(tmp_path):
    """…and the refusal must not be "refuse every `env:`"."""
    steps = ("      - name: Owner\n        env:\n          BASE_REF: main\n"
             "        run: |\n          changed_raw=$(mktemp)\n")
    step, _shell = _header_listing_step(_synthetic_workflow(tmp_path, steps))
    assert step["name"] == "Owner"


# ---------------------------------------------------------------------------
# Test: flatten_dict()
# ---------------------------------------------------------------------------

class TestFlattenDict:
    """Tests for flatten_dict()."""

    def test_flat_simple(self):
        assert br.flatten_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_flat_nested(self):
        d = {"a": {"b": {"c": 3}}, "x": 1}
        assert br.flatten_dict(d) == {"a.b.c": 3, "x": 1}

    def test_flat_empty(self):
        assert br.flatten_dict({}) == {}

    def test_flat_mixed_types(self):
        d = {"a": [1, 2], "b": {"c": "hello"}}
        result = br.flatten_dict(d)
        assert result == {"a": [1, 2], "b.c": "hello"}


# ---------------------------------------------------------------------------
# Test: classify_field()
# ---------------------------------------------------------------------------

class TestClassifyField:
    """Tests for classify_field() — Tier A/B/C classification."""

    def test_tier_a_threshold(self):
        assert br.classify_field("alerts.threshold.MariaDBHighConnections") == "A"

    def test_tier_a_thresholds(self):
        assert br.classify_field("alerts.thresholds.DiskUsageHigh") == "A"

    def test_tier_a_receiver(self):
        assert br.classify_field("_routing.receiver.type") == "A"

    def test_tier_a_receivers(self):
        assert br.classify_field("receivers") == "A"

    def test_tier_a_routing_receivers(self):
        assert br.classify_field("_routing.receivers") == "A"

    def test_tier_b_alerts_generic(self):
        assert br.classify_field("alerts.enabled") == "B"

    def test_tier_b_routing_generic(self):
        assert br.classify_field("_routing.some_other_field") == "B"

    def test_tier_b_rules(self):
        assert br.classify_field("rules.cpu_high") == "B"

    def test_tier_b_severity(self):
        assert br.classify_field("severity.default") == "B"

    def test_tier_c_metadata(self):
        assert br.classify_field("_metadata.domain") == "C"

    def test_tier_c_comment(self):
        assert br.classify_field("_comment") == "C"

    def test_tier_c_is_only_for_provable_format_changes(self):
        """Tier C renders as "no threshold/routing/alerting impact".

        That is a positive claim of harmlessness, so it must be the PROVABLE
        category, never the bucket that catches whatever nobody classified.
        The fallback is Tier B: listed, not declared safe.
        """
        assert br.classify_field("_metadata.note") == "C"
        assert br.classify_field("_some_future_structural_key") == "B"

    def test_a_bare_key_is_a_threshold(self):
        """`diff_configs` is fed the CONTENTS of effective_config.

        Its bare keys are metric keys — including dimensional keys and the
        `_critical` tier. These all used to be Tier C, i.e. every real threshold
        change was reported as format-only (#1419).

        ⚠️ Not "by construction": the platform document's own structural keys
        reach effective_config through the wrapper-less `_defaults.yaml` path,
        which is what `PLATFORM_DOCUMENT_KEYS` exists for. An earlier version of
        this docstring made the stronger claim and two reviewers falsified it.
        """
        assert br.classify_field("mysql_connections") == "A"
        assert br.classify_field("es_heap_usage_percent{node=\"es-hot-01\"}") == "A"
        assert br.classify_field("mongodb_connections_current_critical") == "A"

    def test_platform_document_keys_match_the_schema(self):
        """The structural-key set is a cache of an authoritative definition.

        `describe_tenant` merges `ddata.get("defaults", ddata)`, so a
        `_defaults.yaml` with no `defaults:` wrapper merges its WHOLE document
        into effective_config — which makes "a bare key is a threshold" false
        for the platform document's own structural keys. Deriving the set here
        from the schema means adding a key to the schema without teaching the
        classifier fails, instead of silently reporting it as a threshold
        change once per inheriting tenant.
        """
        schema_path = (Path(REPO_ROOT) / "docs" / "schemas"
                       / "platform-defaults.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        structural = {k for k in schema.get("properties", {})
                      if not k.startswith("_") and k != "defaults"}
        assert br.PLATFORM_DOCUMENT_KEYS == structural, (
            f"PLATFORM_DOCUMENT_KEYS has drifted from {schema_path.name}: "
            f"only-in-code={br.PLATFORM_DOCUMENT_KEYS - structural}, "
            f"only-in-schema={structural - br.PLATFORM_DOCUMENT_KEYS}")

    def test_no_shipped_defaults_file_has_an_unclassified_bare_key(self):
        """⚠️ Honest boundary. `platform-defaults.schema.json` is
        `additionalProperties: false`, so the document cannot legally grow a
        bare structural key — but nothing validates every `_defaults.yaml`
        against it on the path this classifier runs on, and an unvalidated file
        with a new bare key would be read as a threshold. That over-reports
        rather than under-reports, so it is the safe failure; this test says
        when it starts happening.
        """
        import yaml
        offenders = []
        for path in Path(REPO_ROOT).rglob("_defaults.yaml"):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue  # malformed fixtures are another gate's problem
            if not isinstance(doc, dict) or "defaults" in doc:
                continue
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{k}" for k in doc
                if not k.startswith("_") and k not in br.PLATFORM_DOCUMENT_KEYS
            ]
        assert not offenders, (
            f"these keys will be reported as threshold changes to every "
            f"inheriting tenant: {offenders}")

    def test_an_unclassified_bare_key_would_be_read_as_a_threshold(self):
        """Positive control for the scan above, which is otherwise vacuous.

        Exactly two shipped `_defaults.yaml` are wrapper-less and neither
        contributes a candidate key, so the scan asserts nothing about today's
        tree — emptying `PLATFORM_DOCUMENT_KEYS` leaves it green. This pins the
        rule it is guarding, so the pair is a tripwire plus a demonstration
        that the tripwire is wired to something.
        """
        assert br.classify_field("some_new_platform_knob") == "A"
        for structural in br.PLATFORM_DOCUMENT_KEYS:
            assert br.classify_field(structural) == "B", structural

    @pytest.mark.parametrize("path", [
        "profiles.web.cpu_usage_percent",
        "tenants.db-b.mysql_connections",
    ])
    def test_paths_beneath_a_structural_key_are_still_thresholds(self, path):
        """The structural keys are structural AT THEIR OWN LEVEL only.

        `Profiles map[string]map[string]ScheduledValue` (`pkg/config/types.go`)
        makes `profiles.<name>.<metric>` a threshold map, and
        `tenants.<id>.<metric>` is per-tenant thresholds outright. Matching
        PLATFORM_DOCUMENT_KEYS on the TOP key demoted all of these to Tier B —
        re-creating the #1419 defect inside its own fix. Reverting to the
        top-key form left the whole suite green, so this exists because a
        per-fix counterfactual found the gap, not the batch run.
        """
        assert br.classify_field(path) == "A"

    @pytest.mark.parametrize("key", [
        "rules_engine_lag_seconds", "severity_score_p99",
        "alerts_dropped_total", "notification_queue_depth",
        "escalation_backlog", "inhibit_rules_count",
    ])
    def test_a_metric_key_colliding_with_a_tier_b_word_is_still_a_threshold(
            self, key):
        """Tier patterns name FIELDS, so they must match whole segments.

        The matcher used to be a bare prefix test, so any metric whose name
        merely began with a Tier B word was demoted out of Tier A before the
        bare-key rule ran. All six of these are legal tenant threshold keys —
        `tenant-config.schema.json` puts no name restriction on
        `additionalProperties` — and halving any of them rendered as "other
        alerting" instead of the highlight tier.
        """
        assert br.classify_field(key) == "A"

    @pytest.mark.parametrize("path", [
        "state_filters.maintenance.default_state",
        # ⚠️ Also NOT a threshold: a routing receiver field. It sat in the
        # parametrize above until a reviewer pointed out it is the same class
        # as the state filter — the first fix moved one of the two, which is
        # "fixed the cited instance, not the class" inside the edit that was
        # fixing exactly that.
        # ⛔ This path is SYNTHETIC and the honest reason is a measurement, not
        # a Go type. An earlier version of this comment argued from
        # `ScheduledValue` (`pkg/config/types.go`, Default/Overrides/Expiry, no
        # `type`) — true about the struct, but `classify_field` never sees Go
        # types; it sees whatever `describe_tenant` puts in `effective_config`.
        # Measured there instead, and by `_real_effective_keys()` below rather
        # than by a number typed here: over the tree that helper scans, every
        # dotted key the real producer emits is `_routing.*`, and none begins
        # with `profiles.` or `tenants.`. ⛔ NO total in this comment, and
        # none in the explanation of why either — the previous attempt at this
        # sentence announced "NO total" and then quoted two of them. A count
        # here is asserted by nothing; the helper below re-derives it. The
        # realistic sibling of this path is `_routing.receiver.type`, which
        # reaches Tier A by PATTERN (`_routing.receiver`) and is covered by the
        # pattern tests — so it cannot stand in for the fallthrough this test
        # is about.
        "profiles.web.receiver.type",
    ])
    def test_a_non_threshold_path_reaches_tier_a_by_falling_through(self, path):
        """Tier A is right, but NOT because these are thresholds.

        ⛔ The assertion has to be about the MECHANISM. An earlier version only
        asserted `== "A"`, so adding `"maintenance"` to TIER_A_PATTERNS made the
        docstring false while 91 tests stayed green — the name claiming more
        than the body proves, one level below the disease it was written for.
        """
        # ⚠️ Unpacked, not `+`. The `+` pinned an INCIDENTAL type: production
        # only ever iterates these two constants, so making them `frozenset`
        # — a more accurate type for a membership loop, and a refactor someone
        # will make — raised a bare `TypeError` from this line, with no message
        # naming the assumption. Measured: two tests red, and neither message named the assumption. The failure
        # class is the same one this round fixed twice elsewhere; the cited
        # instance was not the only member.
        assert not any(br._matches_field(path, p)
                       for p in [*br.TIER_A_PATTERNS, *br.TIER_B_PATTERNS]), (
            f"{path} now matches a tier pattern, so it no longer reaches the "
            f"bare-key fallthrough this test is about")
        assert br.classify_field(path) == "A"

    def test_tier_b_words_still_match_as_whole_segments(self):
        """…and the boundary fix must not stop the patterns working."""
        assert br.classify_field("severity.default") == "B"
        assert br.classify_field("alerts.enabled") == "B"
        assert br.classify_field("_routing.some_other_field") == "B"

    def test_tier_c_requires_an_exact_key_not_a_prefix(self):
        """Tier C's exact match is deliberate and the file says so.

        Its neighbours in `classify_field` use segment matching, so a future "consistency"
        edit here would silently turn Tier C back into a catch-all — the exact
        vulnerability #1419 closed. Nothing caught that mutation before.
        """
        assert br.classify_field("_metadata") == "C"
        assert br.classify_field("_metadata_but_not_really") != "C"
        assert br.classify_field("_commentary") != "C"

    def test_tier_c_description(self):
        assert br.classify_field("_description.text") == "C"

    def test_tier_a_nested_threshold(self):
        """Nested path still matches Tier A."""
        assert br.classify_field("alerts.threshold") == "A"

    def test_tier_a_custom_alerts(self):
        """_custom_alerts (ADR-024 #741) is a real alerting change → Tier A,
        never format-only. flatten_dict keeps the list opaque so the field path
        is exactly '_custom_alerts'."""
        assert br.classify_field("_custom_alerts") == "A"

    def test_tier_a_custom_alerts_not_misread_as_tier_c(self):
        """Regression: the 'alerts'/'_alerting' Tier-B patterns must NOT
        substring-match '_custom_alerts', and it must NOT fall through to C."""
        assert br.classify_field("_custom_alerts") != "C"
        assert br.classify_field("_custom_alerts") != "B"


# ---------------------------------------------------------------------------
# Test: diff_configs()
# ---------------------------------------------------------------------------

class TestDiffConfigs:
    """Tests for diff_configs() — effective config diffing."""

    def test_identical(self):
        cfg = {"a": 1, "b": {"c": 2}}
        result = br.diff_configs(cfg, cfg)
        assert result["added"] == {}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_added_key(self):
        base = {"a": 1}
        pr = {"a": 1, "b": 2}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"b": 2}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_removed_key(self):
        base = {"a": 1, "b": 2}
        pr = {"a": 1}
        result = br.diff_configs(base, pr)
        assert result["removed"] == {"b": 2}
        assert result["added"] == {}

    def test_changed_value(self):
        base = {"a": {"b": 90}}
        pr = {"a": {"b": 95}}
        result = br.diff_configs(base, pr)
        assert result["changed"] == {"a.b": {"base": 90, "pr": 95}}

    def test_mixed_changes(self):
        base = {"x": 1, "y": {"a": 10}, "z": 3}
        pr = {"x": 1, "y": {"a": 20, "b": 30}}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"y.b": 30}
        assert result["removed"] == {"z": 3}
        assert result["changed"] == {"y.a": {"base": 10, "pr": 20}}


# ---------------------------------------------------------------------------
# Test: classify_diff()
# ---------------------------------------------------------------------------

class TestClassifyDiff:
    """Tests for classify_diff() — tier classification of diffs."""

    def test_threshold_change_is_tier_a(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"alerts.threshold.MariaDBHighConnections": {"base": 90, "pr": 95}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["field"] == "alerts.threshold.MariaDBHighConnections"
        assert tiers["A"][0]["action"] == "changed"
        assert len(tiers["B"]) == 0
        assert len(tiers["C"]) == 0

    def test_receiver_added_is_tier_a(self):
        diff = {
            "added": {"_routing.receiver.type": "slack"},
            "removed": {},
            "changed": {},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["action"] == "added"

    def test_metadata_change_is_tier_c(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"_metadata.domain": {"base": "old", "pr": "new"}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["C"]) == 1
        assert len(tiers["A"]) == 0
        assert len(tiers["B"]) == 0

    def test_mixed_tiers(self):
        diff = {
            "added": {"mysql_connections": 80},
            "removed": {"_metadata.old_field": "x"},
            "changed": {
                "alerts.threshold.DiskUsage": {"base": 80, "pr": 85},
                "severity.default": {"base": "warning", "pr": "critical"},
            },
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 2  # the bare metric key + the threshold path
        assert len(tiers["B"]) == 1  # severity
        assert len(tiers["C"]) == 1  # metadata only

    def test_custom_alerts_added_is_tier_a(self):
        """Adding a _custom_alerts recipe → Tier A (ADR-024 #741)."""
        diff = {
            "added": {"_custom_alerts": [{"recipe": "threshold", "name": "x"}]},
            "removed": {},
            "changed": {},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["field"] == "_custom_alerts"
        assert tiers["A"][0]["action"] == "added"
        assert len(tiers["C"]) == 0

    def test_custom_alerts_changed_is_tier_a(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"_custom_alerts": {
                "base": [{"name": "a"}], "pr": [{"name": "a"}, {"name": "b"}],
            }},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert len(tiers["C"]) == 0

    def test_custom_alerts_pure_reorder_downgraded_to_tier_c(self):
        """N3: a reorder (same recipes, swapped order) has zero alerting impact
        → must be Tier C, not flag as substantive Tier A."""
        a = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "1"}
        b = {"recipe": "rate", "name": "b", "metric": "m2", "threshold": "2"}
        diff = {
            "added": {}, "removed": {},
            "changed": {"_custom_alerts": {"base": [a, b], "pr": [b, a]}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 0
        assert len(tiers["C"]) == 1

    def test_custom_alerts_none_to_empty_list_is_noise_not_tier_a(self):
        """Reef 6: missing key → `_custom_alerts: []` is a no-op; must not flag."""
        diff = {"added": {"_custom_alerts": []}, "removed": {}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert all(len(v) == 0 for v in tiers.values())  # no tier flagged at all

    def test_custom_alerts_removed_empty_list_is_noise(self):
        diff = {"added": {}, "removed": {"_custom_alerts": []}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert all(len(v) == 0 for v in tiers.values())

    def test_custom_alerts_added_nonempty_still_tier_a(self):
        """Guard: a real (non-empty) addition must still flag Tier A."""
        diff = {"added": {"_custom_alerts": [{"name": "a"}]}, "removed": {}, "changed": {}}
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1

    def test_custom_alerts_real_change_stays_tier_a_not_downgraded(self):
        """Guard against the reorder-downgrade hiding a real change."""
        a = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "1"}
        a2 = {"recipe": "threshold", "name": "a", "metric": "m", "threshold": "999"}
        diff = {
            "added": {}, "removed": {},
            "changed": {"_custom_alerts": {"base": [a], "pr": [a2]}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert len(tiers["C"]) == 0


# ---------------------------------------------------------------------------
# Test: _summarize_custom_alerts() — F2 recipe-level detail rendering
# ---------------------------------------------------------------------------

class TestSummarizeCustomAlerts:
    """The opaque-list diff must render WHICH recipes changed, not a raw dump."""

    def _r(self, name, **kw):
        return {"recipe": "threshold", "name": name, "metric": "m",
                "op": ">", "window": "5m", "threshold": "1:warning", **kw}

    def test_added_lists_recipe_names(self):
        entry = {"field": "_custom_alerts", "action": "added",
                 "detail": {"pr": [self._r("a"), self._r("b")]}}
        out = br._summarize_custom_alerts(entry)
        assert "2 recipe(s)" in out and "'a'" in out and "'b'" in out

    def test_removed_lists_recipe_names(self):
        entry = {"field": "_custom_alerts", "action": "removed",
                 "detail": {"base": [self._r("a")]}}
        out = br._summarize_custom_alerts(entry)
        assert "removed" in out and "'a'" in out

    def test_changed_shows_name_level_delta_not_raw_dump(self):
        base = [self._r("keep"), self._r("drop"), self._r("tune", threshold="1:warning")]
        pr = [self._r("keep"), self._r("add"), self._r("tune", threshold="999:critical")]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        out = br._summarize_custom_alerts(entry)
        assert "+['add']" in out
        assert "-['drop']" in out
        assert "~['tune']" in out
        # NOT a raw JSON dump of the whole list
        assert "'metric'" not in out

    def test_changed_flags_threshold_disable_as_silenced(self):
        """N1: blast_radius is the LIVE tool — it must flag the camouflaged
        threshold→disable, not just '~[name]'."""
        base = [self._r("x", threshold="100:critical")]
        pr = [self._r("x", threshold="disable")]
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" in out and "'x'" in out

    def test_changed_flags_mode_silent_as_silenced(self):
        base = [self._r("x", mode="page")]
        pr = [self._r("x", mode="silent")]
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" in out

    def test_silencing_detects_full_disabled_set_and_severity_suffix(self):
        """R2: mirror exporter custom_alert.go — `off`/`disabled`/`false` and
        `:severity`-suffixed disables are silencings too, not just 'disable'."""
        for disabled_val in ("off", "disabled", "false", "disable:warning", "off:critical"):
            base = [self._r("x", threshold="100:critical")]
            pr = [self._r("x", threshold=disabled_val)]
            out = br._summarize_custom_alerts(
                {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}})
            assert "SILENCED" in out, f"missed disabled form: {disabled_val!r}"

    def test_threshold_disabled_helper(self):
        assert br._threshold_disabled({"threshold": "disable"})
        assert br._threshold_disabled({"threshold": "off:warning"})
        assert not br._threshold_disabled({"threshold": "150:critical"})
        assert not br._threshold_disabled({"threshold": "150"})

    def test_benign_modify_not_flagged_silenced(self):
        base = [self._r("x", threshold="100:critical")]
        pr = [self._r("x", threshold="120:critical")]  # just a retune, still active
        out = br._summarize_custom_alerts(
            {"field": "_custom_alerts", "action": "changed",
             "detail": {"base": base, "pr": pr}})
        assert "SILENCED" not in out
        assert "~['x']" in out

    def test_summary_used_in_tenant_change_summary(self):
        """Wired through _tenant_change_summary (full-detail render path)."""
        big = [self._r(f"r{i}") for i in range(8)]
        changed = big[:7] + [self._r("r7", threshold="999:critical")]
        tenant = {"tenant_id": "t", "status": "changed", "highest_tier": "A",
                  "tiers": {"A": [{"field": "_custom_alerts", "action": "changed",
                                   "detail": {"base": big, "pr": changed}}],
                            "B": [], "C": []}}
        out = br._tenant_change_summary(tenant)
        assert "~['r7']" in out
        # The old behaviour dumped ~1.7KB; the summary must be compact
        assert len(out) < 200


# ---------------------------------------------------------------------------
# Test: compute_blast_radius()
# ---------------------------------------------------------------------------

class TestComputeBlastRadius:
    """Tests for compute_blast_radius() — full blast radius computation."""

    @pytest.fixture()
    def base_data(self):
        return {
            "tenant-a": {
                "merged_hash": "aaaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "_routing": {"receiver": {"type": "slack"}},
                    "timezone": "UTC",
                },
            },
            "tenant-b": {
                "merged_hash": "bbbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 85}},
                    "timezone": "UTC",
                },
            },
            "tenant-c": {
                "merged_hash": "cccc",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                },
            },
        }

    def test_no_changes(self, base_data):
        report = br.compute_blast_radius(base_data, base_data)
        assert report["summary"]["affected_tenants"] == 0
        assert report["summary"]["total_tenants_scanned"] == 3

    def test_threshold_change_one_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 90

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["tenants"][0]["tenant_id"] == "tenant-a"
        assert report["tenants"][0]["highest_tier"] == "A"

    def test_format_only_change(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        # A genuine format-only change. ⚠️ `_comment`, not `_metadata`: the
        # first rewrite swapped `timezone` for `_metadata`, which
        # `describe_tenant.deep_merge` drops unconditionally — one
        # never-emitted fixture for another, in the very edit that claimed to
        # stop doing that. `_comment` survives the merge and is a reachable
        # Tier C carrier.
        pr_data["tenant-b"]["effective_config"]["_comment"] = "reworded"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1

    def test_new_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-new"] = {
            "merged_hash": "newnew",
            "effective_config": {"alerts": {"threshold": {"DiskUsage": 80}}},
        }

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["new_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_removed_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        del pr_data["tenant-c"]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["removed_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_hash_match_skips_diff(self, base_data):
        """When merged_hash matches, tenant is skipped entirely."""
        import copy
        pr_data = copy.deepcopy(base_data)
        # Same hash, different config (shouldn't happen, but tests hash-first logic)
        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 0

    def test_custom_alerts_change_is_not_format_only(self, base_data):
        """Regression for the #741 ADR-024 ops-review blind spot: a tenant that
        adds a _custom_alerts recipe (PR #771) must land in Tier A, NOT be
        misclassified as Tier C format-only."""
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-ca"
        pr_data["tenant-b"]["effective_config"]["_custom_alerts"] = [
            {
                "recipe": "threshold",
                "name": "mariadb_conns_high",
                "metric": "mysql_global_status_threads_connected",
                "op": ">",
                "window": "5m",
                "threshold": "150:warning",
                "mode": "page",
            }
        ]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 0
        t = _tenant(report, "tenant-b")
        assert t["highest_tier"] == "A"

    def test_multiple_tiers(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)

        # tenant-a: Tier A (threshold change)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 95

        # tenant-b: Tier B (severity change)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        pr_data["tenant-b"]["effective_config"]["severity"] = {"default": "critical"}

        # tenant-c: Tier C (comment only — see test_format_only_change)
        pr_data["tenant-c"]["merged_hash"] = "cccc-new"
        pr_data["tenant-c"]["effective_config"]["_comment"] = "reworded"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 3
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_b_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1


# ---------------------------------------------------------------------------
# Test: generate_pr_comment()
# ---------------------------------------------------------------------------

class TestGeneratePRComment:
    """Tests for PR comment markdown generation."""

    def test_no_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 100,
                "affected_tenants": 0,
                "tier_a_tenants": 0,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [],
        }
        md = br.generate_pr_comment(report)
        assert "No effective tenant config changes" in md

    def test_with_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 347,
                "affected_tenants": 347,
                "tier_a_tenants": 12,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 335,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [
                {
                    "tenant_id": f"tenant-{i}",
                    "status": "changed",
                    "highest_tier": "A",
                    "tiers": {
                        "A": [{"field": "alerts.threshold.DiskUsage", "action": "changed",
                               "detail": {"base": 80, "pr": 85}}],
                        "B": [],
                        "C": [],
                    },
                }
                for i in range(12)
            ] + [
                {
                    "tenant_id": f"format-{i}",
                    "status": "changed",
                    "highest_tier": "C",
                    "tiers": {"A": [], "B": [], "C": [
                        {"field": "timezone", "action": "changed",
                         "detail": {"base": "UTC", "pr": "UTC+0"}}
                    ]},
                }
                for i in range(335)
            ],
        }
        md = br.generate_pr_comment(report, changed_files="finance/_defaults.yaml")
        assert "finance/_defaults.yaml" in md
        assert "347" in md
        assert "12" in md
        assert "Substantive changes" in md
        assert "Format-only changes" in md
        assert "<details>" in md

    def test_changed_files_header(self):
        report = {
            "summary": {
                "total_tenants_scanned": 10,
                "affected_tenants": 1,
                "tier_a_tenants": 1,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [{
                "tenant_id": "tenant-a",
                "status": "changed",
                "highest_tier": "A",
                "tiers": {
                    "A": [{"field": "receivers", "action": "changed",
                           "detail": {"base": "slack", "pr": "pagerduty"}}],
                    "B": [], "C": [],
                },
            }],
        }
        md = br.generate_pr_comment(report, changed_files="domain-a/_defaults.yaml")
        assert "domain-a/_defaults.yaml" in md


# ---------------------------------------------------------------------------
# Test: PR comment length-limit defences
# ---------------------------------------------------------------------------

def _make_report(
    *,
    tier_a: int = 0,
    tier_b: int = 0,
    tier_c: int = 0,
    fields_per_tenant: int = 1,
    id_prefix: str = "tenant",
) -> dict:
    """Factory helper for GitHub comment length tests."""
    tenants = []
    for i in range(tier_a):
        entries = [
            {
                "field": f"alerts.threshold.Metric{j}",
                "action": "changed",
                "detail": {"base": 80, "pr": 90},
            }
            for j in range(fields_per_tenant)
        ]
        tenants.append({
            "tenant_id": f"{id_prefix}-a-{i:04d}",
            "status": "changed",
            "highest_tier": "A",
            "tiers": {"A": entries, "B": [], "C": []},
        })
    for i in range(tier_b):
        tenants.append({
            "tenant_id": f"{id_prefix}-b-{i:04d}",
            "status": "changed",
            "highest_tier": "B",
            "tiers": {
                "A": [],
                "B": [{"field": "severity.default", "action": "changed",
                       "detail": {"base": "warning", "pr": "critical"}}],
                "C": [],
            },
        })
    for i in range(tier_c):
        tenants.append({
            "tenant_id": f"{id_prefix}-c-{i:04d}",
            "status": "changed",
            "highest_tier": "C",
            "tiers": {"A": [], "B": [], "C": [
                {"field": "timezone", "action": "changed",
                 "detail": {"base": "UTC", "pr": "UTC+0"}}
            ]},
        })
    return {
        "summary": {
            "total_tenants_scanned": tier_a + tier_b + tier_c,
            "affected_tenants": tier_a + tier_b + tier_c,
            "tier_a_tenants": tier_a,
            "tier_b_tenants": tier_b,
            "tier_c_only_tenants": tier_c,
            "new_tenants": 0,
            "removed_tenants": 0,
        },
        "tenants": tenants,
    }


class TestPRCommentLengthGuard:
    """Defensive behaviour against GitHub's 65,536-char comment limit.

    GitHub silently rejects comments exceeding the hard limit (422 error),
    so the generator MUST keep output below it in all scenarios. These tests
    pin the three-layer guard: (1) tenant-count threshold, (2) byte-length
    safety net, (3) last-resort truncation.
    """

    def test_small_report_stays_in_full_detail_mode(self):
        report = _make_report(tier_a=5, tier_b=3)
        md = br.generate_pr_comment(report)
        # Full-detail keeps the per-field detail section inside <details>
        assert "Substantive changes:" in md
        # Threshold not triggered: no "exceeds the inline-detail threshold" warning
        assert "inline-detail threshold" not in md

    def test_many_tenants_triggers_summary_mode(self):
        # 60 Tier A tenants > SUMMARY_MODE_TENANT_THRESHOLD (50) → summary mode
        report = _make_report(tier_a=60)
        md = br.generate_pr_comment(report)
        assert "inline-detail threshold" in md
        # Per-field diffs should NOT appear in summary mode
        assert "alerts.threshold.Metric0" not in md
        # But tenant IDs should
        assert "tenant-a-0000" in md

    def test_artifact_hint_is_rendered(self):
        report = _make_report(tier_a=60)
        hint = "Full diff in the `blast-radius-report` artifact on run #123."
        md = br.generate_pr_comment(report, artifact_hint=hint)
        assert hint in md

    def test_artifact_hint_also_in_full_detail_mode(self):
        report = _make_report(tier_a=3)
        hint = "See run #456 artifact."
        md = br.generate_pr_comment(report, artifact_hint=hint)
        assert hint in md

    def test_1000_tenant_output_stays_under_github_limit(self):
        # Real-world v2.8.0 target: 1000-tenant PR must produce a valid comment.
        report = _make_report(tier_a=1000, fields_per_tenant=5)
        md = br.generate_pr_comment(
            report,
            artifact_hint="Full diff in artifact.",
        )
        assert len(md) < br.GITHUB_COMMENT_HARD_LIMIT, \
            f"Comment body is {len(md)} chars, exceeds GitHub's 65,536 limit"
        assert len(md) <= br.COMMENT_SAFETY_LIMIT, \
            f"Comment body is {len(md)} chars, exceeds COMMENT_SAFETY_LIMIT"

    def test_summary_mode_caps_listed_tenants(self):
        # 500 > SUMMARY_MODE_LIST_CAP (200) → should truncate list and show tail
        report = _make_report(tier_a=500)
        md = br.generate_pr_comment(report)
        # First tenant listed
        assert "tenant-a-0000" in md
        # Last tenant should NOT be listed individually
        assert "tenant-a-0499" not in md
        # Should have "…and N more" tail
        assert "more (see artifact)" in md

    def test_tier_c_count_only_regardless_of_mode(self):
        # Tier C never gets itemised (preserves existing behaviour)
        report = _make_report(tier_a=1, tier_c=5000)
        md = br.generate_pr_comment(report)
        assert "5000 tenants" in md
        # No individual Tier-C tenant IDs in output
        assert "tenant-c-0000" not in md

    def test_pathological_single_tenant_huge_field_diff_falls_back(self):
        # Single tenant with so many per-field changes that full-detail blows
        # past COMMENT_SAFETY_LIMIT — must auto-fall-back to summary mode.
        report = _make_report(tier_a=1, fields_per_tenant=100_000)
        md = br.generate_pr_comment(report)
        assert len(md) < br.GITHUB_COMMENT_HARD_LIMIT
        # Fall-through produced summary mode (or truncation) — field details
        # must not be in the output at that volume.
        assert md.count("alerts.threshold.Metric") < 20  # <<< 100000

    def test_no_changes_returns_stable_short_message(self):
        report = _make_report()  # all zeros
        md = br.generate_pr_comment(report)
        assert "No effective tenant config changes" in md
        assert len(md) < 200  # sanity: short and stable


# ---------------------------------------------------------------------------
# Test: CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    """CLI integration tests."""

    @pytest.fixture()
    def json_pair(self, tmp_path):
        base = {
            "tenant-a": {
                "merged_hash": "aaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "timezone": "UTC",
                },
            },
        }
        pr = {
            "tenant-a": {
                "merged_hash": "bbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                    "timezone": "UTC",
                },
            },
        }
        base_path = tmp_path / "base.json"
        pr_path = tmp_path / "pr.json"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        pr_path.write_text(json.dumps(pr), encoding="utf-8")
        return str(base_path), str(pr_path)

    def test_cli_json_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "json"],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["tier_a_tenants"] == 1

    def test_cli_markdown_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "markdown"],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert "Blast Radius" in result.stdout

    def test_cli_output_file(self, json_pair, tmp_path):
        base_path, pr_path = json_pair
        out_path = str(tmp_path / "report.json")
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--output", out_path],
            capture_output=True,
            text=True, encoding='utf-8'
        )
        assert result.returncode == 0
        assert os.path.exists(out_path)
        report = json.loads(Path(out_path).read_text(encoding="utf-8"))
        assert "summary" in report


# ---------------------------------------------------------------------------
# Test: describe_tenant → blast_radius integration contract (F3)
# ---------------------------------------------------------------------------

class TestDescribeTenantIntegration:
    """Lock the contract between describe_tenant and blast_radius for
    `_custom_alerts`. The unit tests above use hand-built effective_config
    dicts; this proves the REAL pipeline — describe_tenant must emit
    `_custom_alerts` into effective_config so blast_radius can tier it. If
    describe_tenant ever strips `_`-prefixed keys (like config_diff's flatten
    does), this fails loudly instead of the fix silently going dead (cf. #731
    synthetic-fixture false-green)."""

    def _effective(self, conf_d):
        import describe_tenant as dt
        scanner = dt.ConfDScanner(Path(conf_d))
        return {tid: scanner.source_info(tid) for tid in scanner.tenants}

    def _write_tenant(self, d, tenant, custom_alerts):
        import yaml
        body = {"mysql_connections": "100"}
        if custom_alerts is not None:
            body["_custom_alerts"] = custom_alerts
        (Path(d) / f"{tenant}.yaml").write_text(
            yaml.dump({"tenants": {tenant: body}}), encoding="utf-8"
        )

    def test_a_new_or_removed_tenant_is_tier_a(self):
        """`highest_tier` is a hardcoded literal for these two statuses.

        It alone decides which rendering bucket they land in, and `tiers` is
        always empty for them so nothing else can carry the answer. Mutating
        both sites to "C" left 86 tests green — an entirely new or entirely
        deleted tenant, arguably the two most consequential single events this
        report can show, would have rendered as "no threshold/routing/alerting
        impact".
        """
        base = {"t-old": {"merged_hash": "h1", "effective_config": {"cpu": 1}}}
        pr = {"t-new": {"merged_hash": "h2", "effective_config": {"cpu": 2}}}
        report = br.compute_blast_radius(base, pr)
        by_id = {t["tenant_id"]: t for t in report["tenants"]}
        assert by_id["t-new"]["highest_tier"] == "A", by_id["t-new"]
        assert by_id["t-old"]["highest_tier"] == "A", by_id["t-old"]

    def test_summary_mode_still_names_a_silenced_recipe(self):
        """Truncation must never be what hides a silenced pager.

        ⛔ Classifying thresholds as Tier A (correctly) had a second-order
        effect nobody checked: `substantive_count = A + B` now crosses
        SUMMARY_MODE_TENANT_THRESHOLD on any routine `_defaults.yaml` bump of a
        conf.d with more than ~50 tenants. Summary mode never calls
        `_tenant_change_summary`, the only producer of the ":warning: SILENCED"
        highlight — so a PR that bumped a default AND silenced one tenant's
        pager rendered the silencing nowhere, and past SUMMARY_MODE_LIST_CAP
        not even the tenant id.
        """
        def tenant(i, silenced=False):
            t = {"tenant_id": f"t-{i:04d}", "status": "changed",
                 "highest_tier": "A",
                 "tiers": {"A": [{"field": "mysql_connections",
                                  "action": "changed",
                                  "detail": {"base": 100, "pr": 40}}],
                           "B": [], "C": []}}
            if silenced:
                t["tiers"]["A"].append({
                    "field": "_custom_alerts", "action": "changed",
                    "detail": {
                        "base": [{"name": "order-latency-p99", "threshold": "5"}],
                        "pr": [{"name": "order-latency-p99", "threshold": "disable"}],
                    }})
            return t

        n = br.SUMMARY_MODE_LIST_CAP * 5
        canary = n - 1  # sorts past the list cap on purpose
        report = {
            "summary": {"total_tenants_scanned": n, "affected_tenants": n,
                        "tier_a_tenants": n, "tier_b_tenants": 0,
                        "tier_c_only_tenants": 0, "new_tenants": 0,
                        "removed_tenants": 0},
            "tenants": [tenant(i, silenced=(i == canary)) for i in range(n)],
        }
        markdown = br.generate_pr_comment(report)
        assert "Showing tenant list only" in markdown, "expected summary mode"
        assert "SILENCE" in markdown, markdown[:2000]
        assert f"t-{canary:04d}" in markdown, (
            "the silenced tenant sorts past the list cap and was not named")
        assert "order-latency-p99" in markdown, markdown[:2000]

    def test_deleting_a_recipe_counts_as_silencing(self):
        """The plainest way to stop a pager is to delete the recipe.

        `_is_silencing` only compares names present on BOTH sides — it was
        written to unmask "a delete wearing a modified disguise", so an actual
        delete was the one shape it never saw. In full detail that still showed
        as `-[name]`; once thresholds became Tier A and summary mode became the
        norm for defaults bumps, it showed nowhere.
        """
        report = {"tenants": [{
            "tenant_id": "t-1", "status": "changed", "highest_tier": "A",
            "tiers": {"A": [{"field": "_custom_alerts", "action": "changed",
                             "detail": {
                                 "base": [{"name": "order-latency-p99", "threshold": "5"},
                                          {"name": "keep", "threshold": "1"}],
                                 "pr": [{"name": "keep", "threshold": "1"}]}}],
                      "B": [], "C": []}}]}
        assert br._silenced_tenants(report) == [("t-1", ["order-latency-p99"])]

    def test_the_silenced_count_survives_hard_truncation(self):
        """The COUNT is the guarantee; the list is best-effort.

        `generate_pr_comment` hard-truncates the finished body with no idea what
        it is cutting, so an "uncapped, truncation-proof" list was a claim the
        code could not keep — measured: at 5000 silenced tenants, 3395 were
        dropped mid-list under a generic notice. A reader must at least learn
        the number and that an artifact exists.
        """
        n = 5000
        tenants = [{
            "tenant_id": f"t-{i:05d}", "status": "changed", "highest_tier": "A",
            "tiers": {"A": [{"field": "_custom_alerts", "action": "changed",
                             "detail": {"base": [{"name": "pager", "threshold": "5"}],
                                        "pr": [{"name": "pager", "threshold": "disable"}]}}],
                      "B": [], "C": []}} for i in range(n)]
        report = {"summary": {"total_tenants_scanned": n, "affected_tenants": n,
                              "tier_a_tenants": n, "tier_b_tenants": 0,
                              "tier_c_only_tenants": 0, "new_tenants": 0,
                              "removed_tenants": 0},
                  "tenants": tenants}
        markdown = br.generate_pr_comment(report)
        assert len(markdown) <= br.COMMENT_SAFETY_LIMIT
        assert f"**{n} tenant(s) SILENCE" in markdown, markdown[:1500]
        assert "see artifact" in markdown

    def test_a_threshold_change_flows_to_tier_a(self, tmp_path):
        """The contract this class existed for, applied to the main event.

        ⛔ Until #1419 this class pinned `_custom_alerts` only, and every unit
        test above fed the classifier `alerts.threshold.*` — a shape
        describe_tenant has never emitted. So Tier A's threshold half was
        structurally unreachable while the suite stayed green, and halving a
        limit rendered as "format-only … no threshold/routing/alerting impact".
        Running the real pipeline is the only way that gap was ever going to
        show up.
        """
        import yaml
        base_dir, pr_dir = tmp_path / "base", tmp_path / "pr"
        for d, value in ((base_dir, "100"), (pr_dir, "40")):
            d.mkdir()
            (d / "db-b.yaml").write_text(
                yaml.dump({"tenants": {"db-b": {"mysql_connections": value}}}),
                encoding="utf-8")

        report = br.compute_blast_radius(self._effective(base_dir),
                                         self._effective(pr_dir))
        assert report["summary"]["tier_a_tenants"] == 1, report["summary"]
        assert report["summary"]["tier_c_only_tenants"] == 0, report["summary"]
        tenant = report["tenants"][0]
        assert tenant["highest_tier"] == "A"
        assert any(f["field"] == "mysql_connections" for f in tenant["tiers"]["A"]), \
            tenant["tiers"]

        # …and the rendered comment must not carry the harmlessness claim.
        markdown = br.generate_pr_comment(report)
        assert "no threshold/routing/alerting impact" not in markdown, markdown

    def test_disabling_a_metric_flows_to_tier_a(self, tmp_path):
        """Switching a threshold to "disable" turns an alert off for a tenant.

        It reported as format-only too, and on the same PR config-diff called
        it `toggled` — two bots, opposite answers, and the one claiming "no
        impact" carried no red.
        """
        import yaml
        base_dir, pr_dir = tmp_path / "base", tmp_path / "pr"
        for d, value in ((base_dir, "100"), (pr_dir, "disable")):
            d.mkdir()
            (d / "db-b.yaml").write_text(
                yaml.dump({"tenants": {"db-b": {"mysql_connections": value}}}),
                encoding="utf-8")

        report = br.compute_blast_radius(self._effective(base_dir),
                                         self._effective(pr_dir))
        assert report["tenants"][0]["highest_tier"] == "A", report

    def test_custom_alert_addition_flows_to_tier_a(self, tmp_path):
        base_dir = tmp_path / "base"
        pr_dir = tmp_path / "pr"
        base_dir.mkdir()
        pr_dir.mkdir()
        self._write_tenant(base_dir, "db-b", None)
        self._write_tenant(pr_dir, "db-b", [{
            "recipe": "threshold", "name": "mariadb_conns_high",
            "metric": "mysql_global_status_threads_connected",
            "op": ">", "window": "5m", "threshold": "150:warning", "mode": "page",
        }])

        base_data = self._effective(str(base_dir))
        pr_data = self._effective(str(pr_dir))

        # Contract: _custom_alerts survives into effective_config
        assert "_custom_alerts" in pr_data["db-b"]["effective_config"]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 0
        t = _tenant(report, "db-b")
        assert t["highest_tier"] == "A"


# ── the resolution field, and what promoting it did to its neighbours ───────
#
# ⛔ Every assertion in this class covers something that shipped with no
# sample: `_custom_alerts_resolution` went into TIER_A_PATTERNS and removing
# the line again left the whole module green, while the comment next to it
# claimed a measurement ("the only A→B move across 123 real effective_config
# keys") that nothing in the tree could reproduce. The claim was true and the
# number was invented — so it is replaced here by a derivation that re-runs.

_REPO_CONF_D = Path(REPO_ROOT) / "components" / "threshold-exporter" / "config" / "conf.d"


def _real_effective_keys() -> set[str]:
    """Every flattened key the REAL producer emits for the repo's own tree."""
    import describe_tenant as dt
    scanner = dt.ConfDScanner(_REPO_CONF_D)
    keys: set[str] = set()
    for tid in scanner.tenants:
        info = scanner.source_info(tid)
        keys |= set(br.flatten_dict(info.get("effective_config") or {}))
    return keys


class TestRecipeResolutionField:

    def test_the_resolution_field_is_tier_a(self):
        """The pin the promotion never had. Removing the pattern reverts it."""
        assert br.classify_field("_custom_alerts_resolution") == "A"

    def test_the_resolution_field_is_produced_by_the_real_pipeline(self):
        """Anti-vacuity for the derivation below, and for the pin above.

        A tier assertion about a key nothing emits is decoration; this is the
        assertion that fails first if `describe_tenant` stops carrying it.
        """
        assert "_custom_alerts_resolution" in _real_effective_keys()

    def test_tier_membership_is_exhaustive_over_the_real_pipeline(self):
        """Removing the pattern must move EXACTLY this key, and no other.

        ⛔ Derived on every run instead of quoting a key count in a comment.
        ⚠️ Several versions of that count have been wrong, the last of them a
        retraction that declared the original number unreproducible when the
        number was right and only its SCOPE was missing. ⛔ So no count appears
        anywhere in this file — including in the story about the counts, which
        is where the previous attempt put several more of them while
        announcing that none would appear.
        ⚠️ The ordinal is gone on purpose. This paragraph said "five versions
        … the fifth was the retraction" while its counterpart in
        `blast_radius.py` narrated the same history as four — two comments
        disagreeing about how many times a count had been wrong, which is the
        joke telling itself. Neither number is asserted by anything, so
        neither gets to be stated; the two paragraphs now describe the same
        events without counting them. Any number written here is
        asserted by nothing and expires the next time a conf.d fixture is
        added; `_real_effective_keys()` re-derives the set on every run and
        names its own scope in code.
        """
        keys = _real_effective_keys()
        original = br.TIER_A_PATTERNS
        without = tuple(p for p in original
                        if p != "_custom_alerts_resolution")
        try:
            before = {k: br.classify_field(k) for k in keys}
            br.TIER_A_PATTERNS = without
            after = {k: br.classify_field(k) for k in keys}
        finally:
            br.TIER_A_PATTERNS = original
        moved = {k: (before[k], after[k]) for k in keys if before[k] != after[k]}
        assert moved == {"_custom_alerts_resolution": ("A", "B")}, moved

    def test_a_realistic_reorder_still_reaches_the_tier_c_downgrade(self):
        """Both recipe fields reorder together, because one producer writes both.

        ⛔ The synthetic shape (only `_custom_alerts` moves) is what the older
        test pinned, and it kept passing while the real shape stopped
        downgrading: the tenant stayed Tier A through the sibling field. A test
        that only ever sees the synthetic shape testifies for the gap.
        """
        recipes = [{"name": "a", "mode": "page"}, {"name": "b", "mode": "page"}]
        resolution = [{"name": "a", "from": "_defaults"},
                      {"name": "b", "from": "_defaults"}]
        base = {"t": {"merged_hash": "h1", "effective_config": {
            "_custom_alerts": recipes,
            "_custom_alerts_resolution": resolution}}}
        pr = {"t": {"merged_hash": "h2", "effective_config": {
            "_custom_alerts": list(reversed(recipes)),
            "_custom_alerts_resolution": list(reversed(resolution))}}}
        report = br.compute_blast_radius(base, pr)
        tenant = report["tenants"][0]
        assert tenant["highest_tier"] == "C", tenant

    def test_a_real_recipe_change_is_never_downgraded_by_that_path(self):
        """Reverse sample: the downgrade must not swallow a substantive edit."""
        base = {"t": {"merged_hash": "h1", "effective_config": {
            "_custom_alerts": [{"name": "a", "threshold": "10:warning"}],
            "_custom_alerts_resolution": [{"name": "a", "from": "_defaults"}]}}}
        pr = {"t": {"merged_hash": "h2", "effective_config": {
            "_custom_alerts": [{"name": "a", "threshold": "disable"}],
            "_custom_alerts_resolution": [{"name": "a", "from": "_defaults"}]}}}
        report = br.compute_blast_radius(base, pr)
        assert report["tenants"][0]["highest_tier"] == "A"

    def test_the_resolution_field_renders_by_name_not_as_a_blob(self):
        """Promoting it into Tier A sent it to the generic renderer.

        That dumps both full lists — the ~1.7KB blob `_summarize_custom_alerts`
        exists to avoid — and spends one of the per-tenant line slots doing it.

        ⛔ Asserted through `_tenant_change_summary`, i.e. through the WIRING,
        not by calling the summariser directly. The first version of this test
        called `_summarize_custom_alerts(entry)` itself, so reverting the router
        to `field == "_custom_alerts"` — the exact regression — left it green:
        it was guarding the helper, not the path a report actually takes.
        """
        entry = {"field": "_custom_alerts_resolution", "action": "changed",
                 "detail": {"base": [{"name": "a", "from": "_defaults"}],
                            "pr": [{"name": "a", "from": "team-x/_defaults"}]}}
        tenant = {"tenant_id": "t", "status": "changed", "highest_tier": "A",
                  "tiers": {"A": [entry], "B": [], "C": []}}
        out = br._tenant_change_summary(tenant)
        assert "`_custom_alerts_resolution`" in out, out
        assert "team-x/_defaults" not in out, (
            f"the raw recipe payload leaked into the rendered summary: {out}")

    def test_a_silencing_entry_is_recognised_before_the_cap_is_applied(self):
        """The priority split is what keeps the highlight out of the tail.

        Ordering, not room, is the mechanism: entries arrive added → removed →
        changed, so a silencing is naturally LAST and any cap reaches it first.
        A test that only checks "the line is present under a large cap" cannot
        tell the fixed order from the broken one.
        """
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": [{"name": "pager", "threshold": "9:warning"}],
                            "pr": [{"name": "pager", "threshold": "disable"}]}}
        assert br._entry_is_silencing(entry) is True
        benign = {"field": "_custom_alerts", "action": "changed",
                  "detail": {"base": [{"name": "pager", "threshold": "9:warning"}],
                             "pr": [{"name": "pager", "threshold": "8:warning"}]}}
        assert br._entry_is_silencing(benign) is False


class TestUndecodablePathsInTheHeader:
    """⛔ The consumer must not die on the paths the producer passes `-z` for."""

    _BAD = b"conf.d/\xb4\xfa-prod/_defaults.yaml".decode(
        "utf-8", errors="surrogateescape")

    def test_a_surrogate_in_changed_files_does_not_kill_the_run(self, tmp_path):
        """Measured before the fix: `UnicodeEncodeError: surrogates not allowed`
        out of `write_text(encoding="utf-8")`, rc=1, traceback blaming this
        file — on a PR whose only sin was a non-UTF-8 directory name."""
        base = {"db-a": {"merged_hash": "h1", "effective_config": {"cpu": 80}}}
        pr = {"db-a": {"merged_hash": "h2", "effective_config": {"cpu": 40}}}
        report = br.compute_blast_radius(base, pr)
        body = br.generate_pr_comment(
            report, changed_files=br._printable(self._BAD),
            artifact_hint=br._printable(self._BAD))
        out = tmp_path / "out.md"
        out.write_text(body, encoding="utf-8", newline="\n")   # the failing call
        assert "conf.d" in out.read_text(encoding="utf-8")

    def test_the_sanitiser_is_wired_into_the_cli_not_only_into_the_tests(
            self, tmp_path):
        """⛔ Every other test in this class sanitises the value ITSELF.

        They call `generate_pr_comment(..., artifact_hint=br._printable(bad))`
        — a second copy of the wiring, written by me, standing in for the
        production one. Deleting `args.artifact_hint = _printable(...)` from
        `main()` left all of them green, which is the failure the module's own
        history calls "the control proves its own copy, not the system".
        Run the real entry point instead: raw surrogates in, a file on disk out.
        """
        base = {"db-a": {"merged_hash": "h1", "effective_config": {"cpu": 80}}}
        pr = {"db-a": {"merged_hash": "h2", "effective_config": {"cpu": 40}}}
        base_path = tmp_path / "base.json"
        pr_path = tmp_path / "pr.json"
        out_path = tmp_path / "report.md"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        pr_path.write_text(json.dumps(pr), encoding="utf-8")

        argv = ["blast_radius.py",
                "--base", str(base_path), "--pr", str(pr_path),
                "--format", "markdown", "--output", str(out_path),
                "--changed-files", self._BAD,
                "--artifact-hint", f"see {self._BAD}"]
        with mock.patch.object(sys, "argv", argv):
            rc = br.main()
        assert rc in (0, None), rc
        body = out_path.read_text(encoding="utf-8")
        # ⛔ The PROPERTY, not the spelling. What `_printable` owes the caller
        # is "this survives a strict encode"; which escape it picks is an
        # implementation detail, and pinning the literal escape here would be
        # copying the expectation off the artifact. Both halves are checked
        # because both are wired separately in `main()`.
        assert not any(0xDC80 <= ord(c) <= 0xDCFF for c in body), (
            f"a lone surrogate reached the report: {body[:400]!r}")
        body.encode("utf-8")            # the call that used to raise
        assert "-prod/_defaults.yaml" in body, (
            f"the changed-files header lost the path entirely: {body[:400]!r}")
        assert "_see conf.d/" in body, (
            f"the artifact hint did not reach the report: {body[:400]!r}")

    def test_the_producer_and_the_consumer_of_this_string_land_together(self):
        """⛔ The workflow's `-z` and `_printable()` are one change.

        With `-z` alone the run dies (`UnicodeEncodeError`); without `-z` the
        reviewer reads C-quoted escapes as the list of changed files. A rule
        that pins only one half lets a future edit reintroduce either failure,
        and the workflow-side PR had to revert its own copy of this line for
        exactly that reason — so both halves are asserted here, in the PR that
        owns both.
        """
        # ⛔ Two versions of this check have now been wrong in OPPOSITE
        # directions, so it is worth naming both. The first pinned the literal
        # `tr '\0' '\n'` — too tight: it rejected a pipeline that handles NUL
        # strictly better, i.e. it sat in a position to reward the wrong edit.
        # The replacement was a "property" regex over everything after the
        # first `|`, and it was VACUOUS: `tr '\0' ','` contains `\0`, so the
        # pattern matched a line with no `-z` anywhere (measured — the old,
        # too-tight version correctly said no on the same input). An empty
        # control is worse than the over-tight one it replaced.
        # What is actually load-bearing is narrow and checkable: the PRODUCER
        # must carry `-z`. That is one clause about one command, not a claim
        # about the shape of the pipeline; everything downstream is proved
        # behaviourally by the tests below, which run the shipped statement.
        block = self._header_listing_script()
        # ⛔ Named, not `next()`. A bare `next()` on a miss raises
        # `StopIteration` — a traceback with no message, saying nothing about
        # what was being looked for. Measured: rewriting the producer as the
        # behaviour-identical `command git diff …` produced exactly that.
        producers = [ln for ln in block.splitlines()
                     if ln.strip().startswith("git diff")]
        assert len(producers) == 1, (
            f"expected exactly one line in the measured region starting with "
            f"`git diff`, found {producers}. ⚠️ This looks for the command in "
            f"the FIRST position of the line; a wrapper prefix (`command git "
            f"diff`, `env … git diff`) is a legitimate spelling this predicate "
            f"does not model — widen it here rather than dropping the check, "
            f"which is the only thing asserting the producer carries `-z`.")
        producer = producers[0]
        assert " -z " in f" {producer} ", (
            f"git will C-quote non-ASCII paths into the comment header: "
            f"{producer!r}")
        src = (Path(REPO_ROOT) / "scripts/tools/ops/blast_radius.py"
               ).read_text(encoding="utf-8")
        assert "args.changed_files = _printable(" in src, (
            "the workflow passes raw path bytes; without the sanitiser the "
            "strict UTF-8 write kills the run")

    @staticmethod
    def _header_listing_script() -> str:
        """The shipped statement, from the anchor through the CONSUMER call.

        ⛔ Located through YAML, never through text. Every earlier version of
        this helper found things by scanning lines, and every one was forged or
        mis-aimed:
          * the step boundary was "walk back to the nearest `- name:`", which a
            line inside a heredoc satisfies — a decoy there moved the boundary
            and left a real `shell: bash` outside the window (measured:
            nothing went red, with pipefail silently in play, contradicting
            the premise
            half this file's comments rest on);
          * before that the same window was a fixed sixty lines, which the
            comment block above the anchor already exceeded;
          * the end of the slice was the FIRST line mentioning
            `blast_radius.py` — the JSON invocation — while `$CHANGED_FILES` is
            read by the SECOND, so everything between them was unguarded
            (measured: `read -r CHANGED_FILES <<< "conf.d/PWNED.yaml"` in that
            gap, nothing went red);
          * the no-reassignment check matched the literal `CHANGED_FILES=`, so
            any writing syntax without an adjacent `=` left no trace.
        The step comes from `yaml.safe_load` and `shell:` is read off the step
        MAPPING, so nothing written inside `run:` can move either.
        """
        step, shell = _header_listing_step()
        # ⛔ Structural, not a text window, and RESOLVED rather than read off
        # one mapping: `shell:` may be declared on the step, on the job's
        # `defaults.run`, or on the workflow's. What the harness must not do is
        # guess — the flags below are DERIVED from that resolution rather than
        # pinned to any one carrier's absence.
        # ⚠️ This used to be `assert "shell" not in step`, which turned red for
        # an ordinary hardening: adding `shell: bash` to that step (which is
        # what you would do to GET pipefail) failed here, and — because the
        # harness is a shared fixture — it failed in EIGHT tests at once, none
        # of whose names mention `shell`. The direction was right and the shape
        # was wrong: the premise the runner needs is "which interpreter does CI
        # use", and that is readable, not assumable.
        assert _actions_shell_argv(shell), (
            f"{step.get('name')!r} resolves to `shell: {shell!r}`, which this "
            f"harness cannot reproduce, so these tests would measure an "
            f"interpreter CI does not run.\n"
            f"⛔ Adding a row to `_SHELL_ARGV` is NOT enough on its own: that "
            f"table holds FLAGS and the binary is hardcoded to bash, so a `sh` "
            f"row would make the harness simulate the runner's `dash` with "
            f"bash — green, and wrong in exactly the way this assertion "
            f"exists to prevent. Teaching another shell means teaching the "
            f"binary as well, and giving it a behavioural probe like the ones "
            f"in `test_control_each_shell_row_behaves_the_way_actions_runs_it` "
            f"so the new row is measured rather than declared.")

        run_lines = (step["run"] or "").split("\n")
        # ⛔ The SAME anchor the step lookup used, and asserted rather than
        # `next()`. A bare `next()` on a miss raises `StopIteration` with no
        # message: a traceback that says nothing about what was being looked
        # for. Both misses have the same cause, so both have to say so.
        starts = [i for i, ln in enumerate(run_lines) if _HEADER_ANCHOR.match(ln)]
        assert len(starts) == 1, (
            f"expected exactly one `changed_raw` assignment in "
            f"{step.get('name')!r}, found {[run_lines[i].strip() for i in starts]}"
            f".\n" + _ANCHOR_HELP)
        start = starts[0]
        # ⛔⛔ Everything ABOVE the anchor is run by CI and by nothing here.
        # That gap is not theoretical: blind review inserted `head() { cat; }`
        # one line above the anchor and the module stayed green, while
        # in CI the five-file cap and its `(+N more)` disclosure were both
        # gone — `head -n 5` with `head` shadowed passes everything through
        # (measured in the container: six records in, six out). The test that
        # exists to pin that cap,
        # `test_the_header_listing_really_stops_at_five_files`, was green
        # throughout, because it executes a slice that starts BELOW the shadow.
        # A `set -o pipefail`, an `IFS=`, a `git()` function and an alias all
        # sit in the same blind spot, so this is not a list of bad constructs:
        # the measured region simply has to BE what CI runs.
        # ⚠️ The shipped step has zero executable lines above the anchor (the
        # eighty-odd lines before it are comments), so this is a property of
        # the artifact rather than a coincidence being pinned. A statement that
        # legitimately belongs there — `set -o pipefail` most obviously —
        # belongs BELOW the anchor precisely so the harness executes it too;
        # that red is correct, not friction.
        above = [ln for ln in run_lines[:start]
                 if ln.strip() and not ln.strip().startswith("#")]
        assert not above, (
            f"{step.get('name')!r} runs statements ABOVE the `changed_raw` "
            f"anchor. CI executes them; every test in this class starts at the "
            f"anchor, so they are measured by nothing — a shell function, an "
            f"`IFS=`, an alias or a `set -o` there silently changes what the "
            f"statement below does. Move them BELOW the anchor so the harness "
            f"runs them too:\n" + "\n".join(ln.strip() for ln in above))
        # ⛔ The slice runs through the CONSUMER, and the consumer is the call
        # that is handed the value — not the first mention of the tool. This
        # step invokes it twice and only the second one gets `--changed-files`.
        # ⚠️ Comments in this block quote the flag while explaining it, and the
        # first quotation sits above the anchor — matching it made the slice
        # end before it began.
        consumers = [i for i, ln in enumerate(run_lines)
                     if "--changed-files" in ln
                     and not ln.strip().startswith("#") and i > start]
        assert consumers, (
            f"no `--changed-files` call after the anchor in "
            f"{step.get('name')!r}, so there is no slice to execute and these "
            f"tests would measure nothing. Either the value stopped being "
            f"passed — which is the regression this file exists to catch — or "
            f"the call moved above the anchor.")
        consumer = consumers[0]
        end = consumer
        while run_lines[end].rstrip().endswith("\\"):
            end += 1
        # ⛔ SET CONTAINMENT, not a spelling. Every line in this step that
        # mentions the variable at all must lie inside the measured region;
        # that covers `read`, `printf -v`, `mapfile` and anything else, since
        # all of them have to name it. Matching `CHANGED_FILES=` only ever
        # covered one way of writing to it.
        # ⛔ …but only BEFORE the consumer. Lines after it cannot change what
        # the consumer already received, and demanding they be inside the
        # region made a read-only diagnostic — `echo "rendered as:
        # $CHANGED_FILES"` placed after the call — turn EIGHT tests red on a
        # required check, with a message accusing the author of moving the
        # value out of the measured region. Measured.
        # ⛔ …and the cheap structural repair for that false red is the wrong
        # one. Relaxing the match to `CHANGED_FILES=` greens the diagnostic and
        # ALSO stops this rule seeing every other way of writing to the name —
        # `read`, `printf -v`, `mapfile` — which is the hole the docstring
        # above records as already burned once. Narrowing by POSITION keeps
        # every writing syntax covered; narrowing by SPELLING does not.
        # ⚠️ What the relaxation does NOT do, measured, so that nobody argues
        # it the other way: with the match relaxed and a poisoning
        # `read -r CHANGED_FILES <<< "conf.d/PWNED.yaml"` placed inside the
        # measured region, the BEHAVIOURAL tests below catch it (measured: they go red). The case for narrowing by position is the writing
        # syntaxes above, not a claim that the relaxation alone lets an attack
        # through.
        mentions = {i for i, ln in enumerate(run_lines)
                    if "CHANGED_FILES" in ln and not ln.strip().startswith("#")}
        outside = sorted(i for i in mentions if i < start)
        assert not outside, (
            "`CHANGED_FILES` is touched BEFORE the region these tests execute, "
            "so they measure a value CI does not pass. Move it below "
            f"`{run_lines[start].strip()}` or the harness is running a "
            "different program: "
            + "; ".join(run_lines[i].strip() for i in outside))
        body = "\n".join(run_lines[start:end + 1])
        # Only Actions expressions are substituted; every pipeline stage, flag
        # and separator below is the shipped text. ⛔ Fail-closed on one the
        # harness has no value for, rather than letting bash see it literally.
        known = {
            "steps.detect.outputs.conf_d": "conf.d",
            "github.server_url": "https://example.invalid",
            "github.repository": "o/r",
            "github.run_id": "1",
        }

        def _fill(match):
            expr = match.group(1).strip()
            assert expr in known, (
                f"unmapped Actions expression {expr!r}; add it to the harness "
                f"rather than letting bash see it literally")
            return known[expr]

        return re.sub(r"\$\{\{(.*?)\}\}", _fill, body)

    @staticmethod
    def _run_header_listing(repo: Path, script: str, base_ref: str = "main"):
        # ⛔ `bash -e`, and deliberately NOT `set -euo pipefail`: these three
        # workflows declare no `shell:`, so Actions runs `bash -e` with NO
        # pipefail, and a harness that adds it measures a different program
        # than CI runs.
        # ⚠️ An earlier version of this comment also claimed pipefail would
        # "invent a failure of its own (`head -z` closes the pipe, so git
        # would come back 141)". False since the merge: git redirects to a
        # file and is not in the pipeline at all (measured, 3000 records,
        # pipefail on, rc=0). The claim was copied from the pre-merge form
        # along with the code it described.
        # ⛔ What the CONSUMER receives, not what the variable holds. Printing
        # `$CHANGED_FILES` measured a variable, so editing the invocation
        # itself — `--changed-files "${CHANGED_FILES}, conf.d/PWNED.yaml"` —
        # changed what CI passes while every assertion here stayed green
        # (measured: nothing went red). The slice now runs through the real call and
        # a stub records the argument, so the measured thing is the argument.
        # ⚠️ The value is still fenced: `::warning::` goes to stdout too, and
        # reading the whole stream as "the header" once made an announcement
        # look like a header.
        # ⛔ The interpreter is read from the SAME step the script came from,
        # so a workflow that starts declaring `shell:` is measured under what
        # it declares. Absent means `bash -e` and NO pipefail — that is what
        # Actions does, not a convention of this file. Taking it as a default
        # argument instead would let the two drift apart silently, which is
        # the shape this whole class of test is about.
        _step, shell = _header_listing_step()
        argv = _actions_shell_argv(shell)
        assert argv, f"unreproducible shell: {shell!r}"
        bin_dir = repo / "_stub_bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / "python3"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "prev=\"\"\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$prev\" = \"--changed-files\" ]; then\n"
            "    printf '<<VALUE>>%s<</VALUE>>' \"$a\"\n"
            "  fi\n"
            "  prev=\"$a\"\n"
            "done\n"
            "exit 0\n",
            encoding="utf-8", newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP
                   | stat.S_IXOTH)
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        # ⛔ Flags derived from `shell:` above — see the note there.
        return subprocess.run(  # subprocess-timeout: ignore
            [shutil.which(argv[0]), *argv[1:], "-c",
             f'BASE_REF={base_ref}\n{script}'],
            cwd=repo, env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace")

    @staticmethod
    def _header_value(proc) -> str:
        assert "<<VALUE>>" in proc.stdout and "<</VALUE>>" in proc.stdout, (
            f"the statement did not reach the end:\n{proc.stdout}\n{proc.stderr}")
        return proc.stdout.split("<<VALUE>>", 1)[1].split("<</VALUE>>", 1)[0]

    @staticmethod
    def _header_repo(tmp_path: Path, names: list[str],
                     extra_on_main: str | None = None) -> Path:
        repo = tmp_path / "repo"
        (repo / "conf.d").mkdir(parents=True)
        run = lambda *a: subprocess.run(  # noqa: E731  # subprocess-timeout: ignore
            ["git", "-C", str(repo), *a], check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],  # subprocess-timeout: ignore
                       check=True, capture_output=True)
        for cfg in ("user.email", "user.name"):
            run("config", cfg, "t")
        (repo / "conf.d" / "_defaults.yaml").write_text(
            "defaults: {}\n", encoding="utf-8", newline="\n")
        run("add", "-A")
        run("commit", "-qm", "base")
        run("update-ref", "refs/remotes/origin/main", "HEAD")
        if extra_on_main is not None:
            # A commit that lands on the base branch AFTER the branch point.
            # `A...B` must not attribute it to this PR; `A..B` would.
            (repo / "conf.d" / extra_on_main).write_text(
                "moved: on\n", encoding="utf-8", newline="\n")
            run("add", "-A")
            run("commit", "-qm", "someone else's merge")
            run("update-ref", "refs/remotes/origin/main", "HEAD")
            run("reset", "-q", "--hard", "HEAD~1")
        for name in names:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("cpu: 1\n", encoding="utf-8", newline="\n")
        run("add", "-A")
        run("commit", "-qm", "pr")
        return repo

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_header_listing_really_stops_at_five_files(self, tmp_path):
        """⛔ Run the shipped statement; the cap had no witness at all.

        The limit lived in `tr '\\0' '\\n' | head -5`, i.e. in the ORDER of two
        stages that read as interchangeable. Swapping them leaves `head` a
        stream with no newlines in it — one "line" — so the cap passes
        everything and a large PR renders every path into the comment header,
        silently.
        """
        script = self._header_listing_script()
        repo = self._header_repo(
            tmp_path, [f"conf.d/t{i}.yaml" for i in range(6)])
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        listed = self._header_value(proc).split(",")
        assert len(listed) == 5, (
            f"the five-file cap did not hold: {len(listed)} entries "
            f"({proc.stdout!r})")
        # ⛔ Split WITHOUT dropping empties. The first version filtered them
        # out, which meant the trailing-comma stripper could be deleted and no
        # test moved — the assertion was eating the very artefact it should
        # have been reading.
        assert all(listed), f"an empty entry survived: {proc.stdout!r}"

    def test_the_no_changes_comment_still_says_which_files_were_touched(self):
        """⛔ The strongest positive claim was also the least informative.

        `affected_tenants == 0` returned a bare `### Blast Radius Report` and
        dropped the changed-files header, so the one comment that asserts
        "nothing changed" gave the reviewer nothing to notice that against —
        not even the name of the file the PR edited. ⚠️ This does not make the
        claim true (a platform-level `_routing_defaults` edit reaches no
        tenant's effective config and lands here), it makes the claim
        checkable by the person reading it.
        """
        same = {"db-a": {"merged_hash": "h", "effective_config": {"cpu": 80}}}
        report = br.compute_blast_radius(same, same)
        assert report["summary"]["affected_tenants"] == 0, report["summary"]
        body = br.generate_pr_comment(report, changed_files="conf.d/_defaults.yaml")
        assert "conf.d/_defaults.yaml" in body, body
        assert "No effective tenant config changes detected" in body, body
        # Reverse sample: with nothing to name, no empty backticks.
        bare = br.generate_pr_comment(report)
        assert "modifies" not in bare, bare

    def test_the_summary_fold_names_new_and_removed_tenants(self):
        """⛔ One comment, two different `Tier A` numbers.

        Grouping is by `highest_tier` and new/removed tenants carry a
        hardcoded `"A"`, while the summary table's `Tier A` row counts
        `tier_a_tenants`, which excludes them — measured, a table saying 60 sat
        directly above a fold saying 64, with nothing telling the reader which
        was real. The heading now names what the group holds; the counts feed
        the summary-mode threshold and are a separate decision.
        """
        base = {f"t{i}": {"merged_hash": "h", "effective_config": {"cpu": 80}}
                for i in range(60)}
        pr = {f"t{i}": {"merged_hash": "h2", "effective_config": {"cpu": 40}}
              for i in range(60)}
        pr["brand-new"] = {"merged_hash": "n", "effective_config": {"cpu": 10}}
        report = br.compute_blast_radius(base, pr)
        body = br.generate_pr_comment(report)
        assert "Showing tenant list only" in body or "summary" in body.lower(), (
            "expected summary mode for this size; the fixture no longer "
            f"exercises the path under test:\n{body[:400]}")
        assert "brand-new" in body, body
        # ⛔ Named, not `next()`: on a miss this raised `StopIteration` with no
        # message, so a renderer change that dropped the fold heading arrived
        # as a traceback naming a line number rather than the thing it broke.
        headings = [ln for ln in body.splitlines()
                    if "Tier A" in ln and "brand-new" not in ln
                    and "|" not in ln]
        assert headings, (
            f"summary mode rendered no non-table `Tier A` heading, so the fold "
            f"this test is about is gone:\n{body[:600]}")
        heading = headings[0]
        assert "new/removed" in heading, (
            f"the fold that lists new tenants is labelled as though it held "
            f"only Tier A ones, while the table above counts something else: "
            f"{heading!r}")

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_cap_says_that_it_capped(self, tmp_path):
        """⛔ A silent truncation reads as a complete list.

        `git diff --name-only` is sorted, so an eight-file PR rendered the
        alphabetically-first five and the header — a sentence with no
        qualifier — read as the whole set. The only contradicting signal was
        the `Affected tenants` row much further down the comment. The
        workflow's own comment worried about the opposite direction (300 paths
        in the header) and the cap closed that half; this is the other half,
        and nothing pinned it: asserting "exactly five entries" is true with
        or without the disclosure.
        """
        script = self._header_listing_script()
        repo = self._header_repo(
            tmp_path, [f"conf.d/t{i}.yaml" for i in range(8)])
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        value = self._header_value(proc)
        assert "(+3 more)" in value, (
            f"eight changed files rendered as a complete-looking list of five: "
            f"{value!r}")

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_cap_stays_quiet_when_there_is_nothing_to_disclose(
            self, tmp_path):
        """Reverse sample: exactly at the cap, no disclosure — a `(+0 more)`
        would be its own small lie."""
        script = self._header_listing_script()
        repo = self._header_repo(
            tmp_path, [f"conf.d/t{i}.yaml" for i in range(5)])
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert "more)" not in self._header_value(proc), self._header_value(proc)

    @pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt",
                        reason="needs bash and a filesystem that allows \\n")
    def test_a_filename_containing_a_newline_stays_one_entry(self, tmp_path):
        """⛔ THE case that tells the new pipeline from the old one.

        With ordinary names `tr '\\0' '\\n' | head -5` and `head -z -n 5` agree,
        so a cap test built only from ordinary names passes for both — measured,
        the whole statement could be reverted with the suite green. A name
        carrying a literal newline is where they diverge: the old form split it
        and spent two of the five slots on one file, so the header both
        under-reported and displayed two paths that do not exist.
        """
        script = self._header_listing_script()
        names = ["conf.d/aa\nbb.yaml"] + [f"conf.d/t{i}.yaml" for i in range(5)]
        repo = self._header_repo(tmp_path, names)
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        listed = self._header_value(proc).split(",")
        assert len(listed) == 5, (
            f"the newline split a name into extra entries: {proc.stdout!r}")
        assert "conf.d/aa bb.yaml" in listed, (
            f"the name did not survive the cap intact: {proc.stdout!r}")

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_header_listing_is_limited_to_the_detected_tree(self, tmp_path):
        """The pathspec had no witness: every fixture file lived under conf.d,
        so deleting `-- "$conf_d"` changed nothing."""
        script = self._header_listing_script()
        repo = self._header_repo(
            tmp_path, ["conf.d/a.yaml", "docs/unrelated.md"])
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert self._header_value(proc) == "conf.d/a.yaml", (
            f"a file outside the detected tree reached the header: "
            f"{proc.stdout!r}")

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_the_header_listing_uses_the_merge_base_not_the_base_tip(
            self, tmp_path):
        """⛔ `A...B`, not `A..B` — the ticket's own species.

        Two dots list everything on the base branch since the branch point too,
        so an unrelated PR that merged while this one was open gets attributed
        to this one in the header. The first fixture was a linear repo, where
        the two forms are identical by construction.
        """
        script = self._header_listing_script()
        repo = self._header_repo(tmp_path, ["conf.d/mine.yaml"],
                                 extra_on_main="moved_on.yaml")
        proc = self._run_header_listing(repo, script)
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
        assert self._header_value(proc) == "conf.d/mine.yaml", (
            f"a file from someone else's merge was attributed to this PR: "
            f"{proc.stdout!r}")

    @pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
    def test_a_failed_listing_is_announced_rather_than_rendered_as_empty(
            self, tmp_path):
        """⛔ The control for the merged rc check.

        Without it the assignment inherits `sed`'s success: a bad base ref
        printed `fatal: bad revision`, left the value empty and returned 0, and
        the header line simply vanished from a report that otherwise rendered
        normally — "git could not tell me what changed" displayed as "nothing
        changed".
        """
        script = self._header_listing_script()
        repo = self._header_repo(tmp_path, ["conf.d/a.yaml"])
        proc = self._run_header_listing(repo, script, base_ref="no-such-ref")
        value = self._header_value(proc)
        assert "::warning::" in proc.stderr + proc.stdout, (
            f"the failure was silent in the job log:\n{proc.stdout}\n"
            f"{proc.stderr}")
        # ⛔ …and it must reach the READER, not just the log. An empty value
        # renders the identical header to never passing `--changed-files` at
        # all (measured: both `### Blast Radius Report`), so on the PR comment
        # "git could not tell me" and "nobody asked" were the same bytes. The
        # `::warning::` above lives in the job log, which nobody reading the
        # comment sees. Asserting emptiness here was pinning that.
        assert value and "could not" in value, (
            f"the reviewer cannot tell a failed listing from an absent one: "
            f"{value!r}")
        # ⛔ The NUMBER, not the word. Asserting `"exited" in value` said only
        # that the sentence contains a word; hardcoding a status that git never
        # returned (`exited 999`) passed. Both this sentence and the
        # `::warning::` interpolate `$diff_rc`, so requiring them to agree
        # cannot be satisfied by inventing either one.
        warned = re.search(r"git diff exited (\d+) listing", proc.stdout)
        assert warned, f"no ::warning:: carrying git's status:\n{proc.stdout}"
        assert f"exited {warned.group(1)}" in value, (
            f"the header says a different status than the warning does — one "
            f"of them is not git's: warning={warned.group(1)!r} value={value!r}")

    def test_the_sanitiser_is_lossless_for_ordinary_text(self):
        """Reverse sample — it must not mangle the normal case."""
        assert br._printable("conf.d/db-a.yaml, conf.d/db-b.yaml") == (
            "conf.d/db-a.yaml, conf.d/db-b.yaml")
        assert br._printable("conf.d/租戶-a/_defaults.yaml") == (
            "conf.d/租戶-a/_defaults.yaml")
        assert br._printable(None) is None


class TestFullDetailTruncation:
    """The per-tenant line cap used to be able to drop the loudest signal."""

    def _tenant(self, n_thresholds: int, silencing: bool) -> dict:
        entries = [{"field": f"metric_{i:02d}", "action": "added",
                    "detail": {"pr": 100 + i}} for i in range(n_thresholds)]
        if silencing:
            entries.append({
                "field": "_custom_alerts", "action": "changed",
                "detail": {
                    "base": [{"name": "pager", "threshold": "150:warning"}],
                    "pr": [{"name": "pager", "threshold": "disable"}]}})
        return {"tenant_id": "t", "status": "changed", "highest_tier": "A",
                "tiers": {"A": entries, "B": [], "C": []}}

    def test_the_silencing_highlight_is_never_the_line_that_is_dropped(self):
        """Entry order puts a silencing LAST, so the cap reached it first."""
        out = br._tenant_change_summary(
            self._tenant(br.PER_TENANT_LINE_CAP + 2, silencing=True))
        assert "SILENCED" in out, out

    def test_truncation_is_disclosed(self):
        """A partial list must not read as a complete one."""
        out = br._tenant_change_summary(
            self._tenant(br.PER_TENANT_LINE_CAP + 2, silencing=True))
        assert "not shown" in out, out

    def test_a_short_list_is_not_labelled_as_truncated(self):
        """Reverse sample — the marker must not fire when nothing was cut."""
        out = br._tenant_change_summary(self._tenant(2, silencing=True))
        assert "not shown" not in out, out
        assert "SILENCED" in out, out

    def test_the_cap_still_bounds_the_output(self):
        """…and the fix must not turn the cap off."""
        out = br._tenant_change_summary(self._tenant(50, silencing=False))
        bullets = [ln for ln in out.splitlines() if ln.startswith("  - ")]
        assert len(bullets) == br.PER_TENANT_LINE_CAP + 1, out


class TestTheTwoRenderersAgree:
    """One predicate, two callers — asserted by behaviour, not by a docstring."""

    def _report_with(self, action, base_recipes, pr_recipes,
                     field="_custom_alerts") -> dict:
        """⛔ `action` is a PARAMETER, because the shape is the whole point.

        The first version of this class hardcoded `action="changed"` for every
        case including the one it called "deleted" (an emptied list). The real
        producer never writes `_custom_alerts: []` — `describe_tenant` only
        sets the key when the resolver has a non-empty list — so a genuine
        deletion arrives as action="removed" with no `pr` side at all, and that
        branch returned before the shared predicate was ever called. The test
        named the defect and exercised the other branch.
        """
        detail = {"base": base_recipes} if action == "removed" else (
            {"pr": pr_recipes} if action == "added" else
            {"base": base_recipes, "pr": pr_recipes})
        return {"tenants": [{
            "tenant_id": "t", "status": "changed", "highest_tier": "A",
            "tiers": {"A": [{"field": field, "action": action,
                             "detail": detail}],
                      "B": [], "C": []}}]}

    @pytest.mark.parametrize("action, base_recipes, pr_recipes, expected", [
        # ⭐ The shape the REAL pipeline produces when a tenant drops its whole
        # `_custom_alerts:` block — the plainest way to stop a pager.
        ("removed", [{"name": "pager", "mode": "page"}], None, ["pager"]),
        # The same intent expressed as an emptied list (hand-built configs, and
        # what the previous version of this test mistook for the above).
        ("changed", [{"name": "pager", "mode": "page"}], [], ["pager"]),
        # In-place silencing.
        ("changed", [{"name": "pager", "threshold": "150:warning"}],
         [{"name": "pager", "threshold": "disable"}], ["pager"]),
        # Reverse samples: these must be silent in BOTH modes, or the agreement
        # would be satisfied by flagging everything.
        ("changed", [{"name": "pager", "threshold": "150:warning"}],
         [{"name": "pager", "threshold": "120:warning"}], []),
        ("added", None, [{"name": "pager", "mode": "page"}], []),
    ], ids=["removed-key", "emptied-list", "disabled", "benign", "added"])
    def test_both_modes_name_the_same_recipes(self, action, base_recipes,
                                              pr_recipes, expected):
        report = self._report_with(action, base_recipes, pr_recipes)
        tenant = report["tenants"][0]
        summary_mode = br._silenced_tenants(report)
        full_detail = br._tenant_change_summary(tenant)
        assert [names for _, names in summary_mode] == (
            [expected] if expected else []), summary_mode
        for name in expected:
            assert "SILENCED" in full_detail and name in full_detail, full_detail
        if not expected:
            assert "SILENCED" not in full_detail, full_detail

    def test_the_resolution_field_never_claims_a_silencing(self):
        """Control for the `field == "_custom_alerts"` gate.

        Provenance moving between `_defaults` files is not a silencing, and the
        gate that says so had no sample: dropping it left the suite green while
        `silenced_recipe_names` happily returned a name for a resolution entry.
        """
        report = self._report_with(
            "removed",
            [{"name": "pager", "origin": "db-b.yaml", "is_own": True}],
            None, field="_custom_alerts_resolution")
        assert br._silenced_tenants(report) == []
        out = br._tenant_change_summary(report["tenants"][0])
        assert "SILENCED" not in out, out

    @pytest.mark.parametrize("summariser_input, expect_marker, why", [
        # A byte-identical multiset under one duplicated name. The tiering
        # calls this "provably harmless"; the renderer must agree.
        (("dup-reorder"), False,
         "a duplicate-name reorder was rendered as a modification"),
        # Reverse sample: a real change under the same duplicated name must
        # still be rendered, or the fix would have bought silence.
        (("dup-changed"), True,
         "a real change under a duplicated name went unreported"),
    ], ids=["reorder", "changed"])
    def test_the_renderer_groups_duplicate_names_too(
            self, summariser_input, expect_marker, why):
        """⛔ The last-wins construct was fixed in two places and pinned in one.

        `silenced_recipe_names` got `_by_name` AND a test; the summariser
        `_summarize_custom_alerts` got `_by_name` and nothing. Both mutations
        that put the last-wins dict back survived the whole module — the
        class was fixed, the class was not pinned.
        """
        pair = [{"name": "a", "threshold": "disable"},
                {"name": "a", "threshold": "9:warning"}]
        if summariser_input == "dup-reorder":
            pr = list(reversed(pair))
        else:
            pr = [{"name": "a", "threshold": "disable"},
                  {"name": "a", "threshold": "8:warning"}]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": pair, "pr": pr}}
        line = br._summarize_custom_alerts(entry)
        assert ("~['a']" in line) is expect_marker, f"{why}: {line}"

    def test_duplicate_recipe_names_do_not_invent_a_silencing(self):
        """A byte-identical multiset must not raise the alarm.

        Grouping by name last-wins let the two sides keep different survivors,
        so a pure reorder — the case the tiering calls "provably harmless" —
        could simultaneously report SILENCE/DELETE.
        """
        pair = [{"name": "a", "threshold": "disable"},
                {"name": "a", "threshold": "9:warning"}]
        report = self._report_with("changed", pair, list(reversed(pair)))
        assert br._silenced_tenants(report) == []
        assert br._custom_alerts_reorder_only(pair, list(reversed(pair))) is True

    @pytest.mark.parametrize("pr_recipes, expected, why", [
        # A pure rename: same recipe, new name. The pager moved, it did not
        # stop — and in summary mode a rename and a delete render identically,
        # so flagging it cries wolf on an ordinary refactor.
        ([{"name": "new", "metric": "m", "threshold": "9:warning", "mode": "page"}],
         [], "a rename must not raise the SILENCE/DELETE alarm"),
        # …but a rename that ALSO disables it must still fire.
        ([{"name": "new", "metric": "m", "threshold": "disable", "mode": "page"}],
         ["old"], "a rename that disables is still a silencing"),
        # …and a plain delete must still fire.
        ([], ["old"], "a delete is still a silencing"),
        # A recipe that was already disabled is not a pager, so removing it
        # stops nothing.
        ([], [], "removing an already-disabled recipe silences nothing"),
    ], ids=["renamed", "renamed-and-disabled", "deleted", "deleted-but-dead"])
    def test_a_rename_is_not_a_silencing(self, pr_recipes, expected, why):
        """⛔ The fix that made deletions count traded a false negative for a
        false positive on the loudest line this report prints."""
        threshold = "disable" if why.startswith("removing") else "9:warning"
        base = [{"name": "old", "metric": "m", "threshold": threshold,
                 "mode": "page"}]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr_recipes}}
        assert br.silenced_recipe_names(entry) == expected, why

    @pytest.mark.parametrize("pr_recipes, expected, why", [
        # The shape the fix is for: the pager body moved to `beta`, and a
        # SILENT recipe was left behind under `alpha`. `alpha` is therefore on
        # BOTH sides, so it never reaches the `removed` branch that owns the
        # rename oracle — it reaches `disabled`, which asks only "did this name
        # stop paging". It did; the pager did not stop.
        ([{"name": "beta", "metric": "m", "threshold": "9:warning",
           "mode": "page"},
          {"name": "alpha", "metric": "m", "threshold": "9:warning",
           "mode": "silent"}],
         [], "a rename that leaves a silent recipe behind is still a rename"),
        # ⛔ The other direction, so the filter cannot be satisfied by never
        # firing: nothing moved, the name simply went quiet.
        ([{"name": "alpha", "metric": "m", "threshold": "9:warning",
           "mode": "silent"}],
         ["alpha"], "a name that goes silent with nowhere for its pager to "
                    "have gone is a real silencing"),
        # ⛔ And the mixed case: TWO pagers under one name, one moves away and
        # one is really silenced. Something did stop, so it must still fire.
        ([{"name": "beta", "metric": "m", "threshold": "9:warning",
           "mode": "page"},
          {"name": "alpha", "metric": "second", "threshold": "9:warning",
           "mode": "silent"}],
         ["alpha"], "one pager moving does not cover for another that stopped"),
    ], ids=["renamed-with-silent-leftover", "really-silenced", "mixed"])
    def test_a_rename_is_not_a_silencing_through_the_disabled_branch(
            self, pr_recipes, expected, why):
        """⛔ The rename oracle reached only ONE of the two branches.

        `removed` filtered vanished names through `moved`; `disabled` never
        consulted it. So the false :rotating_light: that the rename handling
        exists to remove came back through the adjacent branch, reachable
        whenever the old name still carries any recipe. Reproduced before the
        fix: this first case returned `['alpha']` with the pager live under
        `beta`. Both branches now share one named predicate, because two copies
        of the same question is how one of them got fixed and the other did not.
        """
        base = [{"name": "alpha", "metric": "m", "threshold": "9:warning",
                 "mode": "page"}]
        if why.startswith("one pager"):
            base.append({"name": "alpha", "metric": "second",
                         "threshold": "9:warning", "mode": "page"})
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr_recipes}}
        assert br.silenced_recipe_names(entry) == expected, why

    def test_a_rename_onto_an_existing_name_is_not_a_silencing(self):
        """⛔ The rename oracle only looked at names it had never seen before.

        `moved` was built from `for n in pr if n not in base`, so a rename ONTO
        a name that already exists — merging `alpha` into the `beta` entry —
        produced an empty `moved` set and announced `alpha` as SILENCED. In
        summary mode a false :rotating_light: and a real one are visually the
        same line, which is the trade the rename handling exists to undo; this
        is that same false positive, surviving the fix for it because the fix
        asked "is the name new" instead of "is the body still here".
        """
        base = [{"name": "alpha", "metric": "m", "threshold": "9:warning",
                 "mode": "page"},
                {"name": "beta", "metric": "other", "threshold": "9:warning",
                 "mode": "page"}]
        pr = [{"name": "beta", "metric": "other", "threshold": "9:warning",
               "mode": "page"},
              {"name": "beta", "metric": "m", "threshold": "9:warning",
               "mode": "page"}]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        assert br.silenced_recipe_names(entry) == [], (
            "the pager moved into an existing name; nothing stopped paging")

    def test_a_rename_onto_an_existing_name_that_disables_still_fires(self):
        """The other side of the same input: widening the oracle must not make
        it blind. Same merge, but the moved body is disabled on arrival."""
        base = [{"name": "alpha", "metric": "m", "threshold": "9:warning",
                 "mode": "page"},
                {"name": "beta", "metric": "other", "threshold": "9:warning",
                 "mode": "page"}]
        pr = [{"name": "beta", "metric": "other", "threshold": "9:warning",
               "mode": "page"},
              {"name": "beta", "metric": "m", "threshold": "disable",
               "mode": "page"}]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        assert br.silenced_recipe_names(entry) == ["alpha"]

    def test_the_rename_oracle_ignores_key_order(self):
        """⛔ `_recipe_body` canonicalises with `sort_keys=True`, and dropping
        it is invisible everywhere else.

        The oracle compares JSON text, so without the sort a rename whose
        recipe mapping happens to be built in a different key order produces a
        different body, `moved` misses it, and an ordinary refactor is
        announced as SILENCED. Nothing else in this module reads the body, so
        the flag has no other witness.
        """
        base = [{"name": "old", "metric": "m", "threshold": "9:warning",
                 "mode": "page"}]
        pr = [{"mode": "page", "threshold": "9:warning", "metric": "m",
               "name": "new"}]
        assert br._recipe_body(base[0]) == br._recipe_body(pr[0]), (
            "two spellings of the same recipe produced different bodies")
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        assert br.silenced_recipe_names(entry) == []

    _PAGE = {"name": "x", "threshold": "9:warning", "mode": "page"}
    _DEAD = {"name": "x", "threshold": "disable", "mode": "page"}
    _MUTE = {"name": "x", "threshold": "9:warning", "mode": "silent"}

    @pytest.mark.parametrize("base, pr, expected, why", [
        # ⛔ Every case has TWO members on at least one side. The first version
        # of this table compared single-element groups against the pairwise
        # predicate it replaced, and at size one `any` and `all` are the same
        # function — measured, narrowing `any(_pages(r) for r in pr_group)` to
        # `all(...)` left that table green while four unrelated tests went red.
        # A group predicate tested only on groups of one tests nothing about
        # groups.
        (["_PAGE", "_DEAD"], ["_DEAD", "_DEAD"], True,
         "the last pager in the group stopped"),
        (["_PAGE", "_DEAD"], ["_PAGE", "_DEAD"], False,
         "one member still pages — `all` would call this a silencing"),
        (["_PAGE", "_PAGE"], ["_PAGE", "_DEAD"], False,
         "losing one of two pagers is not losing the pager"),
        (["_DEAD", "_DEAD"], ["_DEAD", "_DEAD"], False,
         "nothing was reaching anyone to begin with"),
        (["_DEAD", "_MUTE"], ["_DEAD", "_DEAD"], False,
         "already silent by mode, then also disabled — the shape the pairwise "
         "predicate used to call a silencing on 12 of its 81 pairs"),
        (["_DEAD", "_DEAD"], ["_PAGE", "_DEAD"], False,
         "un-silencing is not silencing"),
        (["_MUTE", "_DEAD"], ["_PAGE", "_DEAD"], False,
         "reverse sample: the predicate must not drift into firing on any "
         "change at all"),
    ], ids=["last-pager-stops", "one-still-pages", "one-of-two",
            "already-dead", "already-mute-then-disabled", "un-silencing",
            "reverse"])
    def test_a_group_is_silenced_only_when_its_last_pager_stops(
            self, base, pr, expected, why):
        """⛔ Two-sided, and on real groups.

        The predicate the report needs is "this name used to reach a pager and
        no longer does" — a statement about the group as a state, not about any
        pair of members. Its earlier partner `_is_silencing` compared members
        pairwise, invented alarms on duplicate names, and is gone; what is left
        has to be pinned on its own terms rather than against a removed
        function.
        """
        lookup = {"_PAGE": self._PAGE, "_DEAD": self._DEAD, "_MUTE": self._MUTE}
        got = br._group_became_silent([lookup[n] for n in base],
                                      [lookup[n] for n in pr])
        assert got is expected, why

    def test_a_duplicate_name_from_union_inheritance_is_not_a_silencing(self):
        """⛔ The producer really emits duplicate names, and the cross-product
        turned one into an invented alarm.

        ADR-024 inheritance is a UNION, so a tenant carries the inherited
        recipe AND its own under one name. Reproduced end to end through
        `describe_tenant`: an inherited `disable` beside an own
        `9:warning → 8:warning` printed
        `SILENCED (disabled / deleted / mode→silent): ['shared']`
        while `_is_silencing` is False for BOTH recipes — the alarm came only
        from pairing base-own with pr-inherited. The sibling validator cannot
        exclude this either: `custom_alert.go` skips a DISABLED recipe before
        registering the name, and the disabled one is the duplicate that
        matters. Comparing the groups as states removes the pairing entirely.
        """
        base = [{"name": "shared", "metric": "m_inherited", "threshold": "disable"},
                {"name": "shared", "metric": "m_own", "threshold": "9:warning"}]
        pr = [{"name": "shared", "metric": "m_inherited", "threshold": "disable"},
              {"name": "shared", "metric": "m_own", "threshold": "8:warning"}]
        entry = {"field": "_custom_alerts", "action": "changed",
                 "detail": {"base": base, "pr": pr}}
        assert br.silenced_recipe_names(entry) == [], (
            "no recipe under this name stopped paging; the alarm is invented")
        # …and the real transition under the same shape must still fire.
        silenced_pr = [{"name": "shared", "metric": "m_inherited",
                        "threshold": "disable"},
                       {"name": "shared", "metric": "m_own",
                        "threshold": "disable"}]
        entry2 = {"field": "_custom_alerts", "action": "changed",
                  "detail": {"base": base, "pr": silenced_pr}}
        assert br.silenced_recipe_names(entry2) == ["shared"], (
            "the last paging recipe under this name was disabled and the "
            "highlight did not fire")

    @pytest.mark.parametrize("base_recipes, pr_recipes", [
        # in-place silencing, plain delete, benign edit, pure reorder, and a
        # reorder of DUPLICATE names — the shape that used to be the only way
        # a silencing entry could have been downgraded out of Tier A.
        ([{"name": "p", "threshold": "9:warning"}],
         [{"name": "p", "threshold": "disable"}]),
        ([{"name": "p", "mode": "page"}], []),
        ([{"name": "p", "threshold": "9:warning"}],
         [{"name": "p", "threshold": "8:warning"}]),
        ([{"name": "p", "mode": "page"}, {"name": "q", "mode": "page"}],
         [{"name": "q", "mode": "page"}, {"name": "p", "mode": "page"}]),
        ([{"name": "d", "threshold": "disable"}, {"name": "d", "threshold": "9:warning"}],
         [{"name": "d", "threshold": "9:warning"}, {"name": "d", "threshold": "disable"}]),
    ], ids=["disabled", "deleted", "benign", "reorder", "reorder-duplicate-names"])
    def test_a_silencing_entry_never_lands_outside_tier_a(self, base_recipes,
                                                          pr_recipes):
        """The bound `_silenced_tenants` relies on, asserted on real reports.

        ⛔ Replaces a test that hand-built a report and put the entry into each
        of "A"/"B"/"C" in turn. That version claimed to pin a wider reader
        contract and pinned nothing: restoring the literal `("A","B","C")` the
        production code had just removed was GREEN, because the test looped
        over the same three keys. Worse, the wider scan it was defending made
        the two renderers disagree — Tier B and C entries are rendered by paths
        that never print the highlight.

        The honest property is a bound on what the PRODUCER can emit, so it is
        asserted over `compute_blast_radius` output, not over a literal.
        """
        base = {"t": {"merged_hash": "h1",
                      "effective_config": {"_custom_alerts": base_recipes}}}
        pr = {"t": {"merged_hash": "h2",
                    "effective_config": {"_custom_alerts": pr_recipes}}}
        report = br.compute_blast_radius(base, pr)
        seen = 0
        for tenant in report["tenants"]:
            for tier, entries in (tenant.get("tiers") or {}).items():
                for entry in entries or []:
                    if entry.get("field") != "_custom_alerts":
                        continue
                    seen += 1
                    if br.silenced_recipe_names(entry):
                        assert tier == "A", (
                            f"a silencing `_custom_alerts` entry reached tier "
                            f"{tier}; `_silenced_tenants` only scans A:\n{entry}")
        assert seen, "no `_custom_alerts` entry was produced — test is vacuous"

    def test_the_reorder_predicate_refuses_non_lists(self):
        """`"abc"` vs `"cba"` iterated character-by-character and compared equal.

        That is Tier C — "no threshold/routing/alerting impact" — on a value
        that changed. Not producer-reachable, but the predicate now also runs
        against `_custom_alerts_resolution`, and "the producer happens not to
        do that" is not a property of the predicate.
        """
        assert br._custom_alerts_reorder_only("abc", "cba") is False
        assert br._custom_alerts_reorder_only({"a": 1}, {"a": 1}) is False
        assert br._custom_alerts_reorder_only([{"n": 1}], [{"n": 1}]) is True
