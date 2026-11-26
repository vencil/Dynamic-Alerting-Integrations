#!/usr/bin/env python3
"""workflow_path_filter.py — a workflow's `on.pull_request.paths` follows what its jobs read.

#3756. A workflow gated by `on.pull_request.paths` starts only for a PR that
touches a listed path. The lists were written by hand, and a file reached
through an import was nobody's to add: image-ref-resolve.yaml ran
check_image_refs_resolve.py without `scripts/tools/_lib_input.py` (imported by
a module it imports) in its list, so a PR changing only that file never
started the workflow.

The list is now measured, not chased, and measured by the kernel: access
times (owner ruling on #3756). Three pieces:

  * scripts/ops/atime_trace.py — `reset` (the job's second step, right after
    the checkout) sets the access time of every tracked file to a moment in
    the past; `finish` (in the job's last `run:` step) names the files whose
    access time moved, checks its own canaries, and marks the state file. It
    sees a read by ANY process — make, the Go toolchain, an action, Python —
    which a per-interpreter hook did not.
  * `assert` (same last `run:` step, after `finish`) — every tracked file the
    job read must be covered by the workflow's `on.pull_request.paths`. Each
    one that is not is named, and the job fails. `--out` writes the job's
    trace as JSON, in the shape `record` commits.
  * `generate` — writes three kinds of `# BEGIN GENERATED …` / `# END GENERATED
    …` regions of a traced workflow; `generate --check` (pre-commit hook
    `workflow-path-filter-check`, `make workflow-path-filters-check`) fails when
    a committed region differs. Nothing outside the regions is touched.
      - `path-trace`, inside `on.pull_request.paths` — the union of:
          · the committed trace, `.github/path-traces/<workflow>.json`
            (`record` writes it from a finished atime state file): the files
            the job read. A `.go` file is written as `<its directory>/*.go` —
            wider than the build (`_test.go`, other platforms), and so a new
            source file of a package already built is covered before any run;
          · literal `import` statements, at any depth, in the `.py` files of
            the trace, looked up in the directories holding those files and
            followed to a fixpoint — what a branch the CI arguments do not
            reach would load;
          · the workflow file, its trace file, and MACHINERY (this tool and
            atime_trace.py) with their imports.
        An entry whose every file a hand-written pattern already covers is
        left out.
      - `path-trace-env job=<id>` and `path-trace-assert job=<id>`, inside each
        job's `steps:` — the step that runs `reset` and the step that runs
        `finish` and `assert`. Their text is fixed, so "is the job wired" is a
        comparison of the parsed step plus a check of WHERE it sits (see
        `Traced.wiring_errors`), not a search for a command name.

A MATRIX job is one job id with several legs, each reading its own part
(component-docker-build.yaml: eight images). Every leg runs the same two
generated steps and is asserted on its own reads; the committed record of the
job is the UNION of the legs — `merge`, given every leg's `--out` file. Each
file says which leg of how many wrote it, so `merge` refuses a set that is not
all of them, each once; `record` (one measurement) refuses a job with a
`strategy:`.

`generate --check` also holds, for EVERY workflow (traced or not):

  * no dead entry — each positive pattern of each `on.<event>.paths` list
    matches at least one tracked file, unless it
    is in RESERVED_PATTERNS with its reason (checked both ways);
  * no pattern the matcher (scripts/ops/paths_filter_glob.py) does not model.
    That is reported as UNMODELLED, never as dead: deleting it would remove a
    trigger that may be working;
  * every workflow that gates on paths for a pull request
    (`on.pull_request.paths`) either carries
    the generated regions or is named in ADVISORY_PATH_GATES, with its kind
    (what its `paths` list is) and the reason;
  * no workflow gates a JOB on paths with a path-filter action
    (PATH_FILTER_ACTIONS: dorny/paths-filter and its like, in any letter
    case). ci.yml, docs-ci.yaml and validate.yaml did until #3756 retired
    those gates, and with them the modelling of a dorny filter here; a new
    one is reported as UNMODELLED rather than left unchecked.

Why a new read cannot slip past once a list has converged: it is written in a
file that is already listed, so the PR adding it starts the workflow, and
`assert` names the new file.

⚠️ Boundaries, stated because a green run is read as a guarantee. An access
time that did not move is not proof that a file was not used
(scripts/ops/atime_trace.py lists these with the measurements behind them):
  * a file opened with `O_NOATIME` is read without a trace;
  * a module whose `.pyc` is current is loaded from the cache and its `.py`
    is never opened. A fresh checkout has no cache, so a job's FIRST import of
    a module is seen; a job that restores `__pycache__` from a cache would
    hide it;
  * content taken from a git object (`git show`, `git archive`, a second
    checkout elsewhere) never touches the working tree;
  * what runs before `reset` — the checkout — and after `finish` — an
    action's post step, an upload — is outside the measurement. So is a step
    that did not run (an earlier step failed): it read nothing;
  * a directory listing is not a read: a file ADDED to a directory a job scans
    starts the workflow only if a pattern covers the directory (the `.go`
    widening above is the one place this tool adds such a pattern itself);
  * a tracked file the job REPLACED (a new inode at the path) or deleted is
    reported by atime_trace.py as replaced / gone. Whether the old content
    was read first is unknown, so both are COUNTED as read here — a job that
    rewrites a tracked file must have it in its list;
  * a measurement that names nearly the whole tree is taken for what it
    almost always is — a git command comparing change times
    (`git -c core.trustctime=true`, `GIT_CONFIG_*` in a step's `env:`) — and
    is exit 2, not a list of thousands of uncovered files to record;
  * atime_trace.py leaves its own source out of every segment (each run of it
    reads it), and `finish` ends the measurement before `assert` runs, so this
    tool's reads are not in a job's trace either. Both are listed through
    MACHINERY, with their imports, instead of being measured;
  * the measurement is of ONE run. A file read only on a branch that run did
    not take is not in it; for Python the literal-import closure covers the
    statically written part of that, and nothing covers a module loaded by a
    computed path, or the equivalent in another language;
  * the other direction: anything that ran on the machine between `reset` and
    `finish` counts, the job's or not, and `mmap`, `execve` and `cp` are
    reads. A list can be wider than the job needs, never narrower for that
    reason;
  * `reset` sets `core.trustctime=false` in the checkout's git config until
    `finish` (without it the first git command after `reset` reads every
    tracked file). A step that sets it back would flood the measurement;
    `wiring_errors` refuses a `run:` that mentions the setting, and `finish`
    exits 2 when the value is not the one `reset` wrote.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 nothing to fix; 1 a finding in
the repository (an uncovered file, a stale region, a dead or unmodelled
pattern, a mis-wired job); 2 no answer (a workflow that does not parse, markers
missing or misplaced, no atime state, one that was not finished or names no
tracked file, a trace file in another format, `git` failing) — never read as
a pass.

Usage:
    python3 -I scripts/ops/atime_trace.py reset --state "$RUNNER_TEMP/atime-trace.json" \\
        --canary-dir "$GITHUB_WORKSPACE/.git/atime-canary"
    python3 -I scripts/ops/atime_trace.py finish --state "$RUNNER_TEMP/atime-trace.json"
    python3 scripts/ops/workflow_path_filter.py assert --workflow image-ref-resolve.yaml \\
        --job resolve --state "$RUNNER_TEMP/atime-trace.json" --out "$RUNNER_TEMP/path-trace.json"
    python3 scripts/ops/workflow_path_filter.py record --workflow image-ref-resolve.yaml \\
        --job resolve --state /var/tmp/atime-trace.json
    python3 scripts/ops/workflow_path_filter.py merge --workflow component-docker-build.yaml \\
        --job build leg-1/path-trace.json leg-2/path-trace.json …
    python3 scripts/ops/workflow_path_filter.py generate [--check]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

import _lib_exitcodes  # noqa: E402
from _lib_output import (  # noqa: E402  (#3981: the artifact entry)
    check_repo_artifact, ensure_dir, exit_on_output_write_error, is_special_file,
    write_repo_artifact)
from paths_filter_glob import (  # noqa: E402
    Unmodelled,
    covers,
    on_paths_covers,
    unmodelled,
)

EXIT_OK, EXIT_VIOLATION = 0, 1

REPO_ROOT = Path(os.path.realpath(__file__)).parents[2]
WORKFLOWS = ".github/workflows"
TRACES = ".github/path-traces"
ATIME_TOOL = "scripts/ops/atime_trace.py"

# This tool and the one that measures, with what they import, listed in every
# traced workflow: a change to how the list is measured or checked must start
# the workflows it checks. Neither is in a job's measurement (see the module
# docstring), so both are named here. Resolved through the literal-import
# closure, from these seeds and these directories.
MACHINERY = ("scripts/ops/workflow_path_filter.py", ATIME_TOOL)
MACHINERY_IMPORT_DIRS = ("scripts/ops", "scripts/tools")

REGION_KINDS = ("path-trace", "path-trace-env", "path-trace-assert")
BEGIN = re.compile(
    r"^(?P<indent>[ \t]*)# BEGIN GENERATED (?P<kind>path-trace(?:-env|-assert)?)"
    r"(?![\w-])(?P<rest>[^\r\n]*)")
END = re.compile(
    r"^[ \t]*# END GENERATED (?P<kind>path-trace(?:-env|-assert)?)(?![\w-])")
JOB_ARG = re.compile(r"\bjob=(?P<job>[\w-]+)")
ENTRY = re.compile(r"""^[ \t]*-[ \t]+(?P<q>["'])(?P<v>[^"']+)(?P=q)[ \t]*$""")
SENTINEL = "zz-path-trace-position-probe/zz"

ENV_STEP_NAME = "Set access times back, to measure what this job reads (#3756)"
ASSERT_STEP_NAME = "Path filter covers everything this job read (#3756)"
ASSERT_STEP_IF = "${{ !cancelled() }}"
STATE_ARG = '--state "$RUNNER_TEMP/atime-trace.json"'
ENV_STEP_RUN = (f"python3 -I {ATIME_TOOL} reset {STATE_ARG} "
                '--canary-dir "$GITHUB_WORKSPACE/.git/atime-canary"')
CHECKOUT_ACTION = "actions/checkout@"
# What `reset` sets and `finish` puts back. Compared as a STRING with every
# other `run:` of a traced job — a shell script is not parsed here, so a
# mention in a comment is refused as well.
TRUSTCTIME = "core.trustctime"


def assert_step_run(workflow: str, job: str) -> str:
    """One `run:`, joined by `&&`: when `finish` fails (the canaries do not
    hold) `assert` does not run and the step fails with `finish`'s exit code."""
    return (f"python3 -I {ATIME_TOOL} finish {STATE_ARG} && "
            "python3 scripts/ops/workflow_path_filter.py assert "
            f"--workflow {workflow} --job {job} "
            f'{STATE_ARG} --out "$RUNNER_TEMP/{TRACE_FILE}" --leg "{LEG_EXPR}"')


# The file the assert step writes, and the path the upload step after it must
# give: the same file, in the expression syntax `with:` takes.
TRACE_FILE = "path-trace.json"
UPLOAD_PATH = "${{ runner.temp }}/" + TRACE_FILE
# Environment variables that change what git or the assert step's Python do
# without appearing in any `run:` text: `GIT_CONFIG_*` / `GIT_CONFIG_PARAMETERS`
# set `core.trustctime` (then the next git command reads every tracked file),
# `PYTHON*` changes what `python3` imports.
GIT_CONFIG_ENV_PREFIX = "GIT_CONFIG"
PYTHON_ENV_PREFIX = "PYTHON"


# Which run of the job this is, as GitHub numbers it: `<index>/<total>`, `0/1`
# for a job with no matrix. In the generated assert step so that each `--out`
# file says which leg wrote it and how many there are — `merge` takes nothing
# less than all of them, and counts them from here, not from a reading of
# `strategy.matrix` (whose `include`/`exclude`/expression forms this tool does
# not model).
LEG_EXPR = "${{ strategy.job-index }}/${{ strategy.job-total }}"
UPLOAD_ACTION = "actions/upload-artifact@"


def parse_leg(text: object) -> tuple[int, int]:
    """`<index>/<total>` as two ints; anything else is no answer."""
    m = re.fullmatch(r"(\d+)/(\d+)", text) if isinstance(text, str) else None
    if not m or not int(m[1]) < int(m[2]):
        raise ValueError(f"leg {text!r} is not `<index>/<total>` with index < total")
    return int(m[1]), int(m[2])


# The advisory workflows that gate on paths for a pull request and do not carry
# the generated regions, by KIND (#3756, owner ruling 2026-11-14). The kind
# says what the workflow's `paths` list IS, and so what may be asked of it:
#
#   DIFF_DRIVEN   the list is meant to be the workflow's SUBJECT — the files
#                 whose change is what the job reports on (a defaults file, a
#                 conf.d tree, a benchmark's own inputs) — and is never
#                 compared with what the job reads: no read assertion, now or
#                 later. A change to the tool's own code is held by the pytest
#                 legs, which run on every PR.
#                 ⚠️ Not there yet: the ruling (point 2) has the list
#                 GENERATED from the glob the tool picks its work items with.
#                 Today the lists of guard-defaults-impact and
#                 config-diff-advisory are hand-written and still name tool
#                 code beside the subject; nothing here generates or checks
#                 them. That step is open on #3756.
#   AWAITS_READ_ASSERTION
#                 the list is meant to be everything the job reads, and the
#                 job is too expensive to run on every PR. It gets the read
#                 assertion (atime, ruling point 4) in the order of
#                 READ_ASSERTION_ORDER, and leaves this ledger when it does.
#
# There is no third kind (tests/ops/test_workflow_path_filter.py pins the two). A cheap verification job carries no `paths` at all
# (security-audit, required-checks-drift and the E2E spec lint dropped theirs
# with this ledger's kinds), and neither does a job that scans the whole tree;
# such a workflow is not here, and gaining a `paths` list without an entry is
# the UNTRACED finding below.
#
# `generate --check` fails when a gated workflow is in neither state, when an
# entry names a workflow that has since gained the regions (or lost its gate)
# — a stale entry would excuse the same workflow if it regressed — when a kind
# is not one of the two, and when the AWAITS_READ_ASSERTION entries and
# READ_ASSERTION_ORDER are not the same set. Dead and unmodelled patterns of
# these workflows are checked all the same.
DIFF_DRIVEN = "diff-driven"
AWAITS_READ_ASSERTION = "awaits-read-assertion"
ADVISORY_KINDS = (DIFF_DRIVEN, AWAITS_READ_ASSERTION)

ADVISORY_PATH_GATES: dict[str, tuple[str, str]] = {
    "bench-gate-pr.yaml": (
        DIFF_DRIVEN,
        "the benchmark's inputs: non-test Go of the exporter and the scripts "
        "that interleave and compare. The short list is deliberate"),
    "bench-probe-write-latency.yaml": (
        DIFF_DRIVEN,
        "a self-test of the probe: the probe's test file and its analyser"),
    "bench-workload-effect.yaml": (
        DIFF_DRIVEN,
        "a self-test of the diagnostic: the two scripts it runs"),
    "bench-workload-record.yaml": (
        DIFF_DRIVEN,
        "the benchmark test files whose workload it records"),
    "config-diff-advisory.yml": (
        DIFF_DRIVEN,
        "the conf.d tree whose base/head pair it diffs; what it loads depends "
        "on that pair, so one run's reads say nothing about the next"),
    "guard-defaults-impact.yml": (
        DIFF_DRIVEN,
        "the defaults files whose blast radius it reports; which scopes it "
        "runs depends on the files the PR changes"),
}

# The order the AWAITS_READ_ASSERTION workflows are wired in (ruling point 4).
# Empty: the four it named — image-ref-resolve, component-docker-build,
# backtest, forge-e2e-github — are traced. Kept beside the ledger and compared
# with it both ways, so the queue cannot name a workflow the ledger has dropped
# or miss one it gains.
READ_ASSERTION_ORDER: tuple[str, ...] = ()

# A pattern that matches no tracked file ON PURPOSE: (workflow, list label as
# `generate --check` prints it, pattern) → why it stays. Checked both ways: a
# dead pattern not listed here is DEAD, and an entry here whose pattern matches
# a tracked file (or is no longer in that list) is STALE.
# Empty since #3756: its one entry was a pattern of ci.yml's `portal` dorny
# filter, which is gone with the gate.
RESERVED_PATTERNS: dict[tuple[str, str, str], str] = {}


def _caller_error(message: str) -> _lib_exitcodes.CallerError:
    return _lib_exitcodes.CallerError(message)


# ── the repository ──────────────────────────────────────────────────────────

def tracked_files(root: Path) -> tuple[str, ...]:
    """Every tracked path. `-z` and the NUL split, so a quoted name is one path."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, capture_output=True,
            stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="surrogateescape", check=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise _caller_error(
            f"`git ls-files -z` failed in {root}, so which files are first-party "
            f"cannot be decided: {exc!r}") from exc
    files = tuple(sorted(p for p in out.split("\0") if p))
    if not files:
        raise _caller_error(
            f"`git ls-files -z` listed no file in {root}: every check here "
            "would pass by examining nothing.")
    return files


def parse_workflow(text: str, where: str) -> dict:
    import yaml
    from _lib_yaml_strict import strict_safe_load  # noqa: E402,PLC0415  (#4111: duplicate keys refused)
    try:
        doc = strict_safe_load(text)
    except yaml.YAMLError as exc:
        raise _caller_error(
            f"{where} does not parse ({type(exc).__name__}), so it was NOT "
            "checked — fix it, do not skip it.") from exc
    if not isinstance(doc, dict):
        raise _caller_error(f"{where} is not a mapping at the top level.")
    return doc


def load_workflow(path: Path) -> dict:
    if is_special_file(os.fspath(path)):
        # A FIFO here would block the read below — and `generate` rewrites
        # this file (#3981).
        raise _caller_error(
            f"{path.name} is not a regular file, so it was NOT checked — "
            "fix it, do not skip it.")
    try:
        text = path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise _caller_error(
            f"{path.name} cannot be read ({type(exc).__name__}), so it was NOT "
            "checked — fix it, do not skip it.") from exc
    return parse_workflow(text, path.name)


def _on_block(workflow: dict) -> object:
    # YAML 1.1 reads a bare `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def pull_request_paths(workflow: object, where: str) -> list[str] | None:
    """The workflow's `on.pull_request.paths`, or None when it has none.

    None means "not in scope": a workflow without a pull_request path gate
    starts on every PR, so nothing it runs can be skipped by this mechanism.
    Shapes this does not model raise `Unmodelled` (an AssertionError) —
    tests/ops/test_ci_path_filter_coverage.py's invariant 4 reads the same
    function, so the two cannot disagree on what a workflow's gate is.
    """
    if not isinstance(workflow, dict):
        return None
    on = _on_block(workflow)
    if not isinstance(on, dict):
        return None
    gates = [on[e] for e in ("pull_request", "pull_request_target")
             if isinstance(on.get(e), dict)
             and ("paths" in on[e] or "paths-ignore" in on[e])]
    if not gates:
        return None
    if len(gates) > 1:
        raise Unmodelled(
            f"{where}: both `pull_request` and `pull_request_target` carry a "
            "path gate — not modelled (the scripts would need covering by "
            "each). Model it before using it.")
    pr = gates[0]
    if "paths-ignore" in pr:
        raise Unmodelled(
            f"{where}: `paths-ignore` on a pull_request event is not modelled — the "
            "covered set is its complement, which an OR over positive patterns "
            "cannot express. Model it before using it; do not drop it from the "
            "scan.")
    paths = pr.get("paths")
    if paths is None:
        return None
    if not (isinstance(paths, list) and paths
            and all(isinstance(p, str) for p in paths)):
        raise Unmodelled(
            f"{where}: `on.pull_request.paths` is {paths!r}, not a non-empty "
            "list of strings — refusing to grade a shape this guard does not "
            "understand.")
    if all(p.startswith("!") for p in paths):
        raise Unmodelled(
            f"{where}: `on.pull_request.paths` holds only `!` patterns, which "
            "match nothing — GitHub needs a positive pattern first.")
    return paths


def _gate_or_caller_error(workflow: dict, where: str) -> list[str] | None:
    try:
        return pull_request_paths(workflow, where)
    except Unmodelled as exc:
        raise _caller_error(str(exc)) from exc


def _steps(job: object) -> list[dict]:
    steps = job.get("steps") if isinstance(job, dict) else None
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


# Actions whose whole purpose is "which paths did this PR change", lower-case.
# The ONE list: tests/ops/test_ci_aggregate_gate_contract.py reads the steps
# it refuses in a required-check workflow from `path_filter_steps` below, so
# this tool and that guard cannot disagree about what a path-filter step is.
#
# ⚠️ A closed list of names, and so the weaker half of the guard. Not on it,
# and not addable: a fork of one of these published under another name, a
# local composite action (`uses: ./.github/actions/…`), a hand-written
# `git diff --name-only … >> "$GITHUB_OUTPUT"` step. Accepted, because such a
# step only changes what runs once its output is read by an `if:`, and that
# is refused without reference to any name — on every job and step a required
# check waits for, by the contract test above. No heuristic is added for the
# unnamed spellings: it would be a second list with no last member.
PATH_FILTER_ACTIONS = ("dorny/paths-filter", "tj-actions/changed-files")


def path_filter_steps(workflow: dict) -> list[tuple[str, object, str]]:
    """(job id, step name or index, `uses:` as written) of every step that
    runs one of PATH_FILTER_ACTIONS.

    Compared lower-cased: GitHub resolves `owner/repo` without regard to
    case, so `Dorny/Paths-Filter@v3` runs the same action."""
    jobs = workflow.get("jobs")
    hits: list[tuple[str, object, str]] = []
    for job_id, job in (jobs.items() if isinstance(jobs, dict) else ()):
        for index, step in enumerate(_steps(job)):
            uses = str(step.get("uses", ""))
            if any(action in uses.lower() for action in PATH_FILTER_ACTIONS):
                hits.append((str(job_id), step.get("name", index), uses))
    return hits


# ── the generated regions (text, never a YAML round-trip) ───────────────────

class Region:
    def __init__(self, kind: str, job: str | None, begin: int, end: int, indent: str):
        self.kind, self.job, self.begin, self.end, self.indent = kind, job, begin, end, indent


def find_regions(lines: list[str], where: str) -> list[Region]:
    """Every `# BEGIN GENERATED path-trace…` / `# END GENERATED …` pair, in
    order. A stray, nested or unpaired marker is no answer (exit 2)."""
    regions: list[Region] = []
    open_at: tuple[int, re.Match[str]] | None = None
    for i, line in enumerate(lines):
        begin, end = BEGIN.match(line), END.match(line)
        if begin:
            if open_at is not None:
                raise _caller_error(
                    f"{where}:{i + 1}: `# BEGIN GENERATED {begin.group('kind')}` "
                    "before the previous region's END.")
            open_at = (i, begin)
        elif end:
            if open_at is None or open_at[1].group("kind") != end.group("kind"):
                raise _caller_error(
                    f"{where}:{i + 1}: `# END GENERATED {end.group('kind')}` "
                    "without a matching BEGIN line before it.")
            start, match = open_at
            job = JOB_ARG.search(match.group("rest"))
            regions.append(Region(match.group("kind"), job.group("job") if job else None,
                                  start, i, match.group("indent")))
            open_at = None
    if open_at is not None:
        raise _caller_error(
            f"{where}:{open_at[0] + 1}: `# BEGIN GENERATED "
            f"{open_at[1].group('kind')}` has no END line.")
    return regions


def _region_lines(region: Region, workflow: str, block: list[str]) -> list[str]:
    if region.kind == "path-trace":
        return [f'- "{entry}"' for entry in block]
    if region.kind == "path-trace-env":
        return [f"- name: {ENV_STEP_NAME}", f"  run: {ENV_STEP_RUN}"]
    return [f"- name: {ASSERT_STEP_NAME}", f"  if: {ASSERT_STEP_IF}",
            f"  run: {assert_step_run(workflow, region.job or '')}"]


def render(lines: list[str], regions: list[Region], workflow: str,
           block: list[str]) -> str:
    out: list[str] = []
    cursor = 0
    for region in regions:
        eol = "\r\n" if lines[region.begin].endswith("\r\n") else "\n"
        out += lines[cursor:region.begin + 1]
        out += [f"{region.indent}{text}{eol}"
                for text in _region_lines(region, workflow, block)]
        cursor = region.end
    return "".join(out + lines[cursor:])


# ── traces ──────────────────────────────────────────────────────────────────

#: Below this many tracked files "nearly all of them" says nothing (a
#: fixture repository of five files, all read).
WHOLE_TREE_FLOOR = 200


def read_state(root: Path, state_path: Path, tracked: frozenset[str]) -> set[str]:
    """The tracked files a FINISHED atime measurement of this checkout names
    as read: the union of every segment's `files`. Anything short of that —
    no file, another tool's file, another checkout's, no segment, no
    `finish`, or no tracked file read — is no answer."""
    how = ("The job must run `atime_trace.py reset --state …` as its second "
           "step and `atime_trace.py finish --state …` before this.")
    try:
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError as exc:
        raise _caller_error(
            f"the atime state file {state_path} does not exist: nothing was "
            f"measured. {how}") from exc
    except (OSError, ValueError) as exc:
        raise _caller_error(
            f"the atime state file {state_path} cannot be read "
            f"({type(exc).__name__}: {exc}): nothing was measured.") from exc
    try:
        measured_root = os.path.realpath(state["root"])
        segments = state["segments"]
        finished = state.get("finished")
        # REPLACED (the inode changed: an in-place rewrite, a `mv` over it)
        # and GONE (deleted) count as read. Whether the old content was read
        # first is not known, and "not known" must not come out as covered:
        # `sed -i` reads the file it replaces, and so does a formatter.
        for segment in segments:
            for key in ("files", "replaced", "gone"):
                if not (isinstance(segment[key], list)
                        and all(isinstance(f, str) for f in segment[key])):
                    raise TypeError(f"a segment's `{key}` is not a list of paths")
        read = {f for segment in segments
                for key in ("files", "replaced", "gone") for f in segment[key]}
        measured = state["files"]
        if not isinstance(measured, dict):
            raise TypeError("`files` is not the table of measured paths")
    except (KeyError, TypeError, AttributeError) as exc:
        raise _caller_error(
            f"{state_path} is not a state file `atime_trace.py` wrote "
            f"({type(exc).__name__}: {exc}): nothing was measured.") from exc
    if measured_root != str(root):
        raise _caller_error(
            f"{state_path} measured {measured_root}, not this checkout "
            f"({root}): it says nothing about the files here.")
    if not segments:
        raise _caller_error(
            f"{state_path} holds no segment: `atime_trace.py reset` ran and "
            f"nothing was collected. {how}")
    if not (isinstance(finished, dict) and finished.get("canary") == "ok"):
        raise _caller_error(
            f"{state_path} is not marked finished: `atime_trace.py finish` did "
            "not run after the last segment, or its canaries did not hold — "
            "whether access times record reads here is unknown, so the "
            "segments are not a measurement.")
    # The files tracked when `reset` ran, not only those tracked now: a job
    # that reads a file and then `git rm`s it has still read it.
    tracked = tracked | frozenset(measured)
    read &= tracked
    if len(tracked) >= WHOLE_TREE_FLOOR and len(read) * 10 >= len(tracked) * 9:
        raise _caller_error(
            f"{state_path} names {len(read)} of {len(tracked)} tracked files as "
            "read. That is what one git command does when it compares change "
            "times after the reset (`core.trustctime` in effect as true: `git "
            "-c`, a `GIT_CONFIG_*` variable, a global config), not what a job "
            "reads — the segments are not a measurement of the job. A job "
            "that really reads the whole tree has no use for a path gate.")
    if not read:
        raise _caller_error(
            f"{state_path} names no tracked file as read over {len(segments)} "
            "segment(s): the job's steps ran before `reset`, or read nothing "
            "from this checkout. Nothing was measured.")
    return read


def job_record(read: set[str], manual: list[str]) -> dict:
    """The committed form of one job's trace.

    A file a hand-written pattern covers is left out — a job that reads a
    whole tree (`k8s/**`) would otherwise make every file added there a change
    to the trace — EXCEPT a `.py` file (the literal imports written in it are
    looked up from the trace, covered or not) and a `.go` file (its package
    is listed as `<dir>/*.go`, which must stay when a sibling the job did not
    read is not covered by hand). `import_dirs` is where the import lookups
    go: the directories holding the `.py` files the job read; the checkout
    root is always looked in as well (`required_entries`)."""
    python = {f for f in read if f.endswith((".py", ".go"))}
    return {
        "files": sorted(python | {f for f in read if not on_paths_covers(manual, f)}),
        "import_dirs": sorted({f.rpartition("/")[0] or "." for f in python
                               if f.endswith(".py")}),
    }


def trace_path(root: Path, workflow: str) -> Path:
    return root / TRACES / f"{workflow}.json"


TRACE_JOB_KEYS = ("files", "import_dirs")


def _check_job_record(job: object) -> None:
    """Raises unless *job* is one job's record as this tool writes it."""
    if not isinstance(job, dict):
        raise TypeError("a job's record is not a mapping")
    other = sorted(set(job) - set(TRACE_JOB_KEYS))
    if other:
        raise TypeError(
            f"a job holds {', '.join(f'`{k}`' for k in other)}, which this tool "
            "does not write (a `go` key is the pre-atime format)")
    for key in TRACE_JOB_KEYS:
        if not (isinstance(job[key], list) and all(isinstance(v, str) for v in job[key])):
            raise TypeError(f"`{key}` is not a list of strings")


def load_trace(root: Path, workflow: str) -> dict:
    """The committed trace; an absent file is an empty one (a workflow being
    wired for the first time), a malformed one is no answer. So is one in the
    format the retired Python tracer wrote (a `go` list per job): its `files`
    were what Python loaded, not what the job read."""
    path = trace_path(root, workflow)
    if is_special_file(os.fspath(path)):
        raise _caller_error(
            f"{path.relative_to(root).as_posix()} is not a regular file, so it "
            "is not a trace file `workflow_path_filter.py record` wrote. Delete "
            "it, then `record` each of its jobs again.")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        jobs = doc["jobs"]
        for job in jobs.values():
            _check_job_record(job)
    except FileNotFoundError:
        return {"workflow": workflow, "jobs": {}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _caller_error(
            f"{path.relative_to(root).as_posix()} is not a trace file "
            f"`workflow_path_filter.py record` wrote ({type(exc).__name__}: "
            f"{exc}). Delete it, then `record` each of its jobs again from an "
            "atime measurement (`record` reads this file first, so it cannot "
            "replace one it refuses); do not edit it.") from exc
    return doc


def dump_trace(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ── literal imports ─────────────────────────────────────────────────────────

def literal_import_closure(root: Path, seeds: set[str], import_dirs: list[str],
                           tracked: frozenset[str]) -> set[str]:
    """`seeds` plus every tracked file a literal `import` in them (at any
    depth, followed transitively) would load from `import_dirs`.

    Deliberately not a resolver: a name is looked up as `<dir>/<a>/<b>.py` or
    `<dir>/<a>/<b>/__init__.py` in every directory of `import_dirs`, and a
    relative import beside the importing file. A module loaded by a computed
    path is the measurement's job, not this function's. A file that does not
    parse is an error — a file whose imports cannot be read would otherwise
    contribute nothing and look complete.
    """
    def lookup(base: str, dotted: str) -> set[str]:
        parts = [p for p in dotted.split(".") if p]
        found: set[str] = set()
        for i in range(1, len(parts) + 1):
            stem = "/".join(([] if base in ("", ".") else [base]) + parts[:i])
            for cand in (f"{stem}.py", f"{stem}/__init__.py"):
                if cand in tracked:
                    found.add(cand)
        return found

    done: set[str] = set()
    todo = sorted(f for f in seeds if f.endswith(".py"))
    while todo:
        current = todo.pop()
        if current in done:
            continue
        done.add(current)
        try:
            tree = ast.parse((root / current).read_bytes(), filename=current)
        except FileNotFoundError:
            # Tracked and not in the working tree (a job deleted it after
            # reading it): the path is still a requirement, and there is no
            # text to read imports from.
            continue
        except (OSError, SyntaxError, ValueError) as exc:
            raise _caller_error(
                f"{current} cannot be parsed ({type(exc).__name__}), so the "
                "imports written in it are unknown.") from exc
        names: list[tuple[str, str]] = []  # (directory to look in, dotted name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [(d, a.name) for a in node.names for d in import_dirs]
            elif isinstance(node, ast.ImportFrom):
                targets = [node.module or ""] + [
                    f"{node.module}.{a.name}" if node.module else a.name
                    for a in node.names if a.name != "*"]
                if node.level:
                    here = current.split("/")[:-1]
                    here = here[:len(here) - (node.level - 1)]
                    names += [("/".join(here), t) for t in targets if t]
                else:
                    names += [(d, t) for t in targets if t for d in import_dirs]
        for base, dotted in names:
            todo.extend(sorted(lookup(base, dotted) - done))
    return seeds | done


# ── what a workflow's list must hold ────────────────────────────────────────

def matching(entry: str, tracked: tuple[str, ...]) -> list[str]:
    return [f for f in tracked if covers(entry, f)]


def go_package_glob(path: str) -> str:
    """`<dir>/*.go` for a `.go` file: every source of its package directory.
    A superset of what a build reads (tests, other platforms) — the safe
    direction, and the one that covers a file added to the package."""
    directory = path.rpartition("/")[0]
    return f"{directory}/*.go" if directory else "*.go"


def required_entries(root: Path, workflow: str, trace: dict,
                     tracked: frozenset[str]) -> set[str]:
    entries = {f"{WORKFLOWS}/{workflow}",
               trace_path(root, workflow).relative_to(root).as_posix()}
    entries |= literal_import_closure(
        root, {f for f in MACHINERY if f in tracked},
        list(MACHINERY_IMPORT_DIRS), tracked)
    for job in trace["jobs"].values():
        files = {f for f in job["files"] if f in tracked}
        # The root too: a job started as `python -m pkg.main` resolves
        # `import pkg.lazy` from it, whichever directories it read files in.
        closure = literal_import_closure(
            root, files, sorted({".", *job["import_dirs"]}), tracked)
        entries |= {go_package_glob(f) if f.endswith(".go") else f for f in closure}
    return entries


def generated_block(entries: set[str], manual: list[str],
                    tracked: tuple[str, ...]) -> list[str]:
    """`entries` minus those whose every tracked file a hand-written pattern
    already covers. An entry matching no tracked file is dropped: nothing is
    left for it to start the workflow on."""
    keep = []
    for entry in sorted(entries):
        files = matching(entry, tracked)
        if files and not all(on_paths_covers(manual, f) for f in files):
            keep.append(entry)
    return keep


class Traced:
    """One workflow that carries the generated regions."""

    def __init__(self, root: Path, path: Path, tracked: tuple[str, ...]):
        self.root, self.path, self.name = root, path, path.name
        self.text = path.read_bytes().decode("utf-8")
        self.lines = self.text.splitlines(keepends=True)
        self.workflow = parse_workflow(self.text, self.name)
        self.regions = find_regions(self.lines, self.name)
        self.tracked = tracked
        paths_regions = [r for r in self.regions if r.kind == "path-trace"]
        if len(paths_regions) != 1:
            raise _caller_error(
                f"{self.name}: expected exactly one `# BEGIN GENERATED "
                f"path-trace` region, found {len(paths_regions)}.")
        self.block = self._block_entries(paths_regions[0])
        self.manual, self.at = self._paths_position()
        self._check_job_regions()
        self.trace = load_trace(root, self.name)

    def _block_entries(self, region: Region) -> list[str]:
        entries = []
        for line in self.lines[region.begin + 1:region.end]:
            m = ENTRY.match(line.rstrip("\r\n"))
            if not m:
                raise _caller_error(
                    f"{self.name}: a line inside the generated path-trace "
                    f"region is not a quoted list entry: {line.rstrip()!r}. The "
                    "region is written by `workflow_path_filter.py generate`; "
                    "do not edit it.")
            entries.append(m.group("v"))
        return entries

    def _paths_position(self) -> tuple[list[str], int]:
        """Where the `path-trace` region sits, found by rendering it with one
        probe entry and reading the YAML back: the probe must come out in
        `on.pull_request.paths` and nowhere else. Asked of an EMPTY region too
        — that is the state a workflow is in when `generate` first writes it,
        and a region under `push.paths` would otherwise be filled in there."""
        probe = parse_workflow(
            render(self.lines, self.regions, self.name, [SENTINEL]), self.name)
        paths = _gate_or_caller_error(probe, self.name)
        if paths is None or paths.count(SENTINEL) != 1 or \
                json.dumps(probe, default=str).count(SENTINEL) != 1:
            raise _caller_error(
                f"{self.name}: the `# BEGIN/END GENERATED path-trace` lines do "
                "not sit inside the `on.pull_request.paths` list (and only "
                "there). Nothing was written.")
        at = paths.index(SENTINEL)
        return paths[:at] + paths[at + 1:], at

    def _check_job_regions(self) -> None:
        """One env region and one assert region per job, each inside the job
        it names — read back from the rendered YAML, like the paths region."""
        jobs = self.workflow.get("jobs")
        jobs = jobs if isinstance(jobs, dict) else {}
        rendered = parse_workflow(self.rendered(self.block), self.name).get("jobs") or {}
        for kind, name in (("path-trace-env", ENV_STEP_NAME),
                           ("path-trace-assert", ASSERT_STEP_NAME)):
            named = [r.job for r in self.regions if r.kind == kind]
            for job_id in named:
                if job_id not in jobs:
                    raise _caller_error(
                        f"{self.name}: a `# BEGIN GENERATED {kind}` region "
                        f"names job `{job_id}`, which the workflow does not "
                        "define (write `job=<id>` on the BEGIN line).")
            for job_id in jobs:
                if named.count(job_id) != 1:
                    raise _caller_error(
                        f"{self.name}: job `{job_id}` needs exactly one "
                        f"`# BEGIN GENERATED {kind} job={job_id}` / `# END "
                        f"GENERATED {kind}` pair among its steps; found "
                        f"{named.count(job_id)}.")
                found = [j for j, job in rendered.items()
                         if any(s.get("name") == name for s in _steps(job))]
                if job_id not in found:
                    raise _caller_error(
                        f"{self.name}: the `{kind} job={job_id}` region is not "
                        f"inside job `{job_id}`'s `steps:` list. Nothing was "
                        "written.")

    def expected_block(self) -> list[str]:
        entries = required_entries(self.root, self.name, self.trace,
                                   frozenset(self.tracked))
        return generated_block(entries, self.manual, self.tracked)

    def full_patterns(self, block: list[str]) -> list[str]:
        return self.manual[:self.at] + block + self.manual[self.at:]

    def paths_now(self) -> list[str]:
        """`on.pull_request.paths` as committed."""
        return self.full_patterns(self.block)

    def rendered(self, block: list[str]) -> str:
        return render(self.lines, self.regions, self.name, block)

    def job_ids(self) -> list[str]:
        jobs = self.workflow.get("jobs")
        return list(jobs) if isinstance(jobs, dict) else []

    def wiring_errors(self, workflow: dict | None = None) -> list[str]:
        """Is each job of `workflow` (default: the file as committed) measured
        from the step after its checkout to its last command?

        Read from the PARSED workflow, so it holds whatever the text looks
        like. The checkout is the job's first step (atime_trace.py does not
        exist before it); the generated reset step is the SECOND, so every
        later step — a `uses:` one included: setup-python reading
        `.python-version` — is inside the measurement; the generated
        finish-and-assert step is the last `run:` step, followed by nothing
        but artifact uploads (a `uses:` step after it reads unmeasured, and
        moving the assert above one drops that step's reads from the next
        recording); both are exactly the generated steps; no `continue-on-error`
        and no `defaults.run.shell` stands between their exit code and the
        job's; and no other `run:` mentions the git setting `reset` holds.
        Whether the job runs on a PR at all (#3756): no job-level `if:`;
        `on.pull_request` holds `paths` only (`branches`/`types` that never
        match a PR mean no run, so no assertion) and there is no
        `pull_request_target` (it runs on the BASE, so it measures the wrong
        tree); a matrix is a literal, non-empty `include` list with no
        `exclude`. The `--out` file is uploaded: exactly one step follows the
        assert step, an upload of that file under the same `if:`. No
        `GIT_CONFIG*` key in any `env:` (it sets `core.trustctime` where no
        `run:` text shows it) and no `PYTHON*` key in the workflow's or the
        job's `env:` (the assert step's `python3` would import under it).
        NOT checked: a step skipped by its own `if:` (a legitimate branch of
        the job: it reads nothing, and that is what is recorded); what a
        `uses:` step does to git config.
        """
        workflow = self.workflow if workflow is None else workflow
        errors = []
        events = workflow.get("on", workflow.get(True))
        events = events if isinstance(events, dict) else {}
        if "pull_request_target" in events:
            errors.append(
                f"{self.name}: triggers on `pull_request_target`, which runs on "
                "the base branch — the job would measure the base, not the PR")
        pull_request = events.get("pull_request")
        if isinstance(pull_request, dict):
            narrowing = sorted(k for k in pull_request if k != "paths")
            if narrowing:
                errors.append(
                    f"{self.name}: `on.pull_request` narrows the trigger by "
                    f"{', '.join(narrowing)}; a PR it does not match runs no job, "
                    "so nothing is asserted — the path gate is `paths` alone")
        errors += _env_errors(self.name, "the workflow", workflow.get("env"))
        jobs = workflow.get("jobs")
        jobs = jobs if isinstance(jobs, dict) else {}
        for job_id, job in jobs.items():
            where = f"{self.name}: job `{job_id}`"
            job = job if isinstance(job, dict) else {}
            steps = _steps(job)
            if "if" in job:
                errors.append(f"{where} has a job-level `if:`; when it does not "
                              "hold, the job is skipped and nothing is asserted")
            errors += _matrix_errors(where, job.get("strategy"))
            errors += _env_errors(where, "the job", job.get("env"))
            for i, step in enumerate(steps):
                errors += _env_errors(f"{where}: step {i + 1}", "the step",
                                      step.get("env"), python=False)
            if job.get("continue-on-error"):
                errors.append(f"{where} sets `continue-on-error`, so an "
                              "uncovered file would not fail it")
            if "container" in job:
                errors.append(f"{where} runs in a `container:`, where the "
                              "checkout the access times are set on is another "
                              "mount and `$RUNNER_TEMP` another path")
            env_at = [i for i, s in enumerate(steps)
                      if s == {"name": ENV_STEP_NAME, "run": ENV_STEP_RUN}]
            want = {"name": ASSERT_STEP_NAME, "if": ASSERT_STEP_IF,
                    "run": assert_step_run(self.name, job_id)}
            assert_at = [i for i, s in enumerate(steps) if s == want]
            runs = [i for i, s in enumerate(steps) if "run" in s]
            first_uses = str(steps[0].get("uses", "")) if steps else ""
            if not first_uses.lower().startswith(CHECKOUT_ACTION):
                errors.append(
                    f"{where}: its first step must be `uses: {CHECKOUT_ACTION}…` "
                    "— the reset step runs a file of the checkout, and a step "
                    "before the checkout is not measured")
            if len(env_at) != 1:
                errors.append(
                    f"{where} must hold the generated reset step exactly once, "
                    f"unchanged (found {len(env_at)}) — nothing it reads is "
                    "measured otherwise")
            elif env_at[0] != 1:
                errors.append(
                    f"{where}: the generated reset step is step {env_at[0] + 1}; "
                    "it must be the second step, right after the checkout — "
                    "what a step between the two reads is not measured")
            if len(assert_at) != 1:
                errors.append(
                    f"{where} must hold the generated assert step exactly "
                    f"once, unchanged (found {len(assert_at)})")
            elif runs and runs[-1] != assert_at[0]:
                errors.append(
                    f"{where}: step {runs[-1] + 1} runs a command after the "
                    "assert step, so what it reads is not checked. The assert "
                    "step must be the job's last `run:` step")
            else:
                for i in range(assert_at[0] + 1, len(steps)):
                    if not str(steps[i].get("uses", "")).lower().startswith(UPLOAD_ACTION):
                        errors.append(
                            f"{where}: step {i + 1} comes after the assert step "
                            "and what it reads is not checked. Only "
                            f"`uses: {UPLOAD_ACTION}…` steps may follow it")
                errors += _upload_errors(where, steps[assert_at[0] + 1:])
            for scope, holder in (("the workflow", workflow), ("the job", job)):
                defaults = holder.get("defaults")
                run_defaults = defaults.get("run") if isinstance(defaults, dict) else None
                if isinstance(run_defaults, dict) and "shell" in run_defaults:
                    errors.append(
                        f"{where}: {scope} sets `defaults.run.shell`, which the "
                        "assert step would run under — a shell that does not "
                        "pass the exit code on would make its failure not count")
            for i, step in enumerate(steps):
                if i in env_at or i in assert_at:
                    continue
                if TRUSTCTIME in str(step.get("run") or "").lower():
                    errors.append(
                        f"{where}: step {i + 1}'s `run:` mentions `{TRUSTCTIME}` "
                        "— the reset step sets it to false until the assert "
                        "step; with any other value the next git command reads "
                        "every tracked file")
            if job_id not in self.trace["jobs"]:
                errors.append(
                    f"{where} has no recorded trace in {TRACES}/{self.name}.json "
                    "(run its steps between `atime_trace.py reset` and "
                    "`finish`, then `workflow_path_filter.py record`)")
        for job_id in self.trace["jobs"]:
            if job_id not in jobs:
                errors.append(
                    f"{TRACES}/{self.name}.json holds a trace for job "
                    f"`{job_id}`, which {self.name} does not define")
        return errors


def _env_errors(where: str, scope: str, env: object, *, python: bool = True) -> list[str]:
    """`GIT_CONFIG*` keys anywhere, and `PYTHON*` keys where the assert step
    inherits them (the workflow's and the job's `env:`)."""
    keys = [str(k) for k in env] if isinstance(env, dict) else []
    errors = [f"{where}: {scope}'s `env:` sets `{k}`, which can set "
              f"`{TRUSTCTIME}` where no `run:` text shows it"
              for k in keys if k.upper().startswith(GIT_CONFIG_ENV_PREFIX)]
    if python:
        errors += [f"{where}: {scope}'s `env:` sets `{k}`, which changes what "
                   "the assert step's `python3` runs" for k in keys
                   if k.upper().startswith(PYTHON_ENV_PREFIX)]
    return errors


def _matrix_errors(where: str, strategy: object) -> list[str]:
    """A matrix is a literal, non-empty `include` list: an expression, an
    `exclude`, or an empty list can leave no leg, and no leg asserts."""
    if strategy is None:
        return []
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if matrix is None:
        return []
    include = matrix.get("include") if isinstance(matrix, dict) else None
    if (not isinstance(matrix, dict) or set(matrix) != {"include"}
            or not isinstance(include, list) or not include):
        return [f"{where}: its matrix must be a literal, non-empty `include` "
                "list (no `exclude`, no other axis, no expression) — a matrix "
                "that leaves no leg runs no assertion"]
    return []


def _upload_errors(where: str, after: list[dict]) -> list[str]:
    """Exactly one step after the assert step: the upload of its `--out` file,
    under the assert step's own `if:` (so a failed assertion still uploads
    the trace a re-record needs)."""
    if len(after) != 1:
        return [f"{where}: exactly one step must follow the assert step — the "
                f"upload of `{UPLOAD_PATH}` (found {len(after)})"]
    step = after[0]
    with_ = step.get("with") if isinstance(step.get("with"), dict) else {}
    errors = []
    if step.get("if") != ASSERT_STEP_IF:
        errors.append(f"{where}: the upload step's `if:` must be `{ASSERT_STEP_IF}`, "
                      "as the assert step's — the trace of a failed assertion is "
                      "the one a re-record needs")
    if with_.get("path") != UPLOAD_PATH:
        errors.append(f"{where}: the upload step must upload `{UPLOAD_PATH}` — the "
                      f"assert step's `--out` (it uploads {with_.get('path')!r})")
    return errors


# ── dead and unmodelled patterns, every workflow ────────────────────────────

def _filter_rule_glob(rule: object) -> str | None:
    """An entry of an `on.<event>.paths` list is a glob string; nothing else."""
    return rule if isinstance(rule, str) else None


def pattern_lists(workflow: dict, where: str) -> list[tuple[str, list[object]]]:
    """(label, patterns) for every `on.<event>.paths` / `paths-ignore` list in
    a workflow."""
    found: list[tuple[str, list[object]]] = []
    on = _on_block(workflow)
    if isinstance(on, dict):
        for event, body in on.items():
            if not isinstance(body, dict):
                continue
            for key in ("paths", "paths-ignore"):
                if key in body:
                    value = body[key]
                    if not isinstance(value, list):
                        raise _caller_error(
                            f"{where}: `on.{event}.{key}` is not a list.")
                    found.append((f"on.{event}.{key}", value))
    return found


def pattern_findings(workflow: dict, where: str, tracked: tuple[str, ...],
                     seen: set[tuple[str, str, str]] | None = None) -> list[str]:
    """DEAD and UNMODELLED patterns of one workflow, as report lines. `seen`
    collects every (workflow, label, pattern) met, for the reserved ledger."""
    findings = []
    for label, patterns in pattern_lists(workflow, where):
        for rule in patterns:
            glob = _filter_rule_glob(rule)
            if glob is None:
                findings.append(
                    f"UNMODELLED  {where} {label}: {rule!r} — not a glob "
                    "string; it was NOT checked")
                continue
            key = (where, label, glob)
            if seen is not None:
                seen.add(key)
            positive = glob[1:] if glob.startswith("!") else glob
            why = unmodelled(positive)
            if why is not None:
                findings.append(
                    f"UNMODELLED  {where} {label}: {glob!r} — {why}. It was NOT "
                    "judged dead or alive: rewrite it in the modelled alphabet "
                    "or teach scripts/ops/paths_filter_glob.py the shape")
            elif glob.startswith("!"):
                continue
            elif not any(covers(glob, f) for f in tracked):
                if key not in RESERVED_PATTERNS:
                    findings.append(
                        f"DEAD        {where} {label}: {glob!r} matches no "
                        "tracked file, so it can never start anything (a "
                        "renamed or removed file leaves its real trigger "
                        "unlisted). If it is kept on purpose, say why in "
                        "RESERVED_PATTERNS (scripts/ops/workflow_path_filter.py)")
            elif key in RESERVED_PATTERNS:
                findings.append(
                    f"STALE       RESERVED_PATTERNS names {where} {label}: "
                    f"{glob!r}, which matches a tracked file now — delete the "
                    "entry")
    return findings


# ── commands ────────────────────────────────────────────────────────────────

def _workflow_files(root: Path) -> list[Path]:
    directory = root / WORKFLOWS
    files = sorted(directory.glob("*.y*ml")) if directory.is_dir() else []
    if not files:
        raise _caller_error(f"no workflow in {directory}.")
    return files


def _job_trace(args: argparse.Namespace) -> tuple[Traced, set[str], dict]:
    tracked = tracked_files(args.root)
    path = args.root / WORKFLOWS / args.workflow
    if not path.is_file():
        raise _caller_error(f"{WORKFLOWS}/{args.workflow} does not exist.")
    traced = Traced(args.root, path, tracked)
    if args.job not in traced.job_ids():
        raise _caller_error(
            f"{args.workflow} has no job `{args.job}` (it has: "
            f"{', '.join(traced.job_ids()) or 'none'}).")
    read = read_state(args.root, Path(args.state), frozenset(tracked))
    return traced, read, job_record(read, traced.manual)


def cmd_assert(args: argparse.Namespace) -> int:
    try:
        _, legs = parse_leg(args.leg)
    except ValueError as exc:
        raise _caller_error(f"--leg: {exc}.") from exc
    traced, read, record = _job_trace(args)
    if args.out:
        # #3981: `merge` loads this file whole (each leg's), so it is the
        # artifact entry: an unusable --out is rc 2 and one line naming it.
        out = Path(args.out)
        ensure_dir(out.parent, flag="--out")
        write_repo_artifact(out, dump_trace(
            {"workflow": traced.name, "leg": args.leg, "jobs": {args.job: record}}),
            flag="--out")
    uncovered = sorted(f for f in read if not on_paths_covers(traced.paths_now(), f))
    print(f"{traced.name} job `{args.job}`: {len(read)} tracked file(s) read "
          "(by access time).")
    if not uncovered:
        print("Every one is covered by `on.pull_request.paths`.")
        return EXIT_OK
    print(f"\n{len(uncovered)} file(s) this job read are NOT covered by "
          f"{traced.name}'s `on.pull_request.paths` — a PR changing only one of "
          "them would not start this workflow:", file=sys.stderr)
    for f in uncovered:
        print(f"  UNCOVERED  {f}", file=sys.stderr)
    if legs > 1:
        how = (f"this is leg {args.leg} of a matrix job, and the job's entry is "
               f"the union of all {legs}: download every leg's `--out` file "
               "(each uploads one as an artifact, red or green) and `python3 "
               "scripts/ops/workflow_path_filter.py merge --workflow "
               f"{traced.name} --job {args.job} <the {legs} files>`. One leg's "
               "file alone is refused — it would drop what the others read")
    else:
        how = ("the `--out` file (uploaded as an artifact) is this run's trace: "
               f"`python3 scripts/ops/workflow_path_filter.py merge --workflow "
               f"{traced.name} --job {args.job} <that file>`; or run the job's "
               "steps locally between `atime_trace.py reset` and "
               "`atime_trace.py finish` and `record` that state")
    print(
        f"\nFix: re-record the `{args.job}` entry of {TRACES}/{traced.name}.json "
        f"— {how} — then `python3 scripts/ops/workflow_path_filter.py generate` "
        "and commit both files. The measurement is by access time: whichever "
        "process read the file, it is in the list above.",
        file=sys.stderr)
    return EXIT_VIOLATION


def _write_job_record(root: Path, traced: "Traced", job: str, record: dict) -> Path:
    """Put *record* in as *job*'s entry of the workflow's trace file, the other
    jobs' entries as they were — what `record` and `merge` both end in.
    (`generate` writes the file too: it prunes names that are no longer
    tracked, through `write_if_changed`.)"""
    doc = traced.trace
    doc["workflow"] = traced.name
    doc["jobs"][job] = record
    path = trace_path(root, traced.name)
    ensure_dir(path.parent)
    write_repo_artifact(path, dump_trace(doc))
    return path


def cmd_record(args: argparse.Namespace) -> int:
    traced, read, record = _job_trace(args)
    job = traced.workflow["jobs"][args.job]
    if isinstance(job, dict) and "strategy" in job:
        raise _caller_error(
            f"{traced.name} job `{args.job}` has a `strategy:` — its entry is "
            "the union of its legs, and one state file is one leg. `merge` "
            "every leg's `assert --out` file instead. Nothing was written.")
    path = _write_job_record(args.root, traced, args.job, record)
    print(f"recorded {len(record['files'])} file(s) of {len(read)} read for job "
          f"`{args.job}` in {path.relative_to(args.root).as_posix()}; "
          "now run `workflow_path_filter.py generate`.")
    return EXIT_OK


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def cmd_merge(args: argparse.Namespace) -> int:
    """One job's record from several measurements of it — the legs of a
    matrix job, each of which read its own part. REPLACES the job's entry
    with the union of the given `--out` files, so a re-recording still
    shrinks the list. Every leg, each once: an `--out` file says which leg
    of how many wrote it (`LEG_EXPR`), and a set that is not all of them is
    refused — a left-out leg whose reads hand-written patterns cover would
    never fail its own `assert`, while the entries its `.py` files brought in
    by import went missing. Names that are not tracked files of this checkout
    are refused as well: `assert --out` writes none, so such a file was edited
    or belongs to another commit."""
    tracked = tracked_files(args.root)
    path = args.root / WORKFLOWS / args.workflow
    if not path.is_file():
        raise _caller_error(f"{WORKFLOWS}/{args.workflow} does not exist.")
    traced = Traced(args.root, path, tracked)
    if args.job not in traced.job_ids():
        raise _caller_error(
            f"{args.workflow} has no job `{args.job}` (it has: "
            f"{', '.join(traced.job_ids()) or 'none'}).")
    files: set[str] = set()
    dirs: set[str] = set()
    seen: dict[int, str] = {}
    totals: set[int] = set()
    known = frozenset(tracked)
    known_dirs = {"."} | {d for f in tracked for d in _parents(f)}
    for name in args.out_files:
        try:
            doc = json.loads(Path(name).read_text(encoding="utf-8"))
            if doc.get("workflow") != traced.name:
                raise ValueError(f"it is a trace of `{doc.get('workflow')}`")
            index, total = parse_leg(doc.get("leg"))
            if index in seen:
                raise ValueError(f"it is leg {index}, and so is {seen[index]}")
            seen[index] = name
            totals.add(total)
            job = doc["jobs"][args.job]
            _check_job_record(job)
            strange = sorted(set(job["files"]) - known)[:3] + sorted(
                set(job["import_dirs"]) - known_dirs)[:3]
            if strange:
                raise ValueError(
                    f"it names {', '.join(strange)}, not tracked in this checkout")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _caller_error(
                f"{name} is not a trace `assert --out` wrote for {traced.name} "
                f"job `{args.job}` ({type(exc).__name__}: {exc}). Nothing was "
                "written.") from exc
        files |= set(job["files"])
        dirs |= set(job["import_dirs"])
    if len(totals) != 1 or len(seen) != next(iter(totals)):
        raise _caller_error(
            f"`merge` needs every leg of job `{args.job}`, each once: got "
            f"leg(s) {', '.join(str(i) for i in sorted(seen))} of "
            f"{' or '.join(str(t) for t in sorted(totals))}. Nothing was written.")
    out = _write_job_record(args.root, traced, args.job,
                            {"files": sorted(files), "import_dirs": sorted(dirs)})
    print(f"merged {len(args.out_files)} measurement(s) of job `{args.job}`: "
          f"{len(files)} file(s) in {out.relative_to(args.root).as_posix()}; now "
          "run `workflow_path_filter.py generate`.")
    return EXIT_OK


def _ledger_findings(traced_names: set[str], gated: set[str],
                     seen: set[tuple[str, str, str]]) -> list[str]:
    findings = []
    for name in sorted(gated - traced_names - set(ADVISORY_PATH_GATES)):
        findings.append(
            f"UNTRACED    {name} gates on paths for a pull request but has "
            "neither the generated path-trace regions nor an entry in "
            "ADVISORY_PATH_GATES (scripts/ops/workflow_path_filter.py). A "
            "cheap job drops its `paths`; otherwise trace it, or add an entry "
            "with its kind")
    for name in sorted(set(ADVISORY_PATH_GATES) & traced_names
                       | set(ADVISORY_PATH_GATES) - gated):
        findings.append(
            f"STALE       ADVISORY_PATH_GATES names {name}, which "
            + ("carries the generated regions now" if name in traced_names
               else "has no path gate (or no longer exists)")
            + " — delete the entry")
    for name, entry in sorted(ADVISORY_PATH_GATES.items()):
        pair = entry if isinstance(entry, tuple) and len(entry) == 2 else (None, None)
        if not (pair[0] in ADVISORY_KINDS and isinstance(pair[1], str) and pair[1].strip()):
            findings.append(
                f"UNCLASSIFIED ADVISORY_PATH_GATES[{name!r}] is not a "
                f"(kind, why) tuple with a kind in {ADVISORY_KINDS} and a "
                f"reason written down: {entry!r}")
    awaiting = {name for name, entry in ADVISORY_PATH_GATES.items()
                if isinstance(entry, tuple) and entry[:1] == (AWAITS_READ_ASSERTION,)}
    for name in sorted(awaiting ^ set(READ_ASSERTION_ORDER)):
        findings.append(
            f"UNORDERED   {name} is "
            + ("an awaits-read-assertion entry that READ_ASSERTION_ORDER does not place"
               if name in awaiting else
               "in READ_ASSERTION_ORDER without an awaits-read-assertion entry")
            + " (scripts/ops/workflow_path_filter.py)")
    if len(set(READ_ASSERTION_ORDER)) != len(READ_ASSERTION_ORDER):
        findings.append("UNORDERED   READ_ASSERTION_ORDER names a workflow twice")
    for key in sorted(set(RESERVED_PATTERNS) - seen):
        findings.append(
            f"STALE       RESERVED_PATTERNS names {key[0]} {key[1]}: {key[2]!r}, "
            "which is not a pattern of that list — delete the entry")
    return findings


def cmd_generate(args: argparse.Namespace) -> int:
    root = args.root
    tracked = tracked_files(root)
    findings: list[str] = []
    writes: list[tuple[Path, bytes, str]] = []
    traced_names: set[str] = set()
    gated: set[str] = set()
    seen: set[tuple[str, str, str]] = set()
    for path in _workflow_files(root):
        workflow = load_workflow(path)
        if _gate_or_caller_error(workflow, path.name) is not None:
            gated.add(path.name)
        for job_id, _step, uses in path_filter_steps(workflow):
            findings.append(
                f"UNMODELLED  {path.name}: a job uses {uses} ({job_id}). A "
                "job-level path gate is not modelled here since #3756 retired "
                "the last of them (its patterns are not checked for dead "
                "entries and what the gated jobs load is not traced). Gate "
                "the workflow with `on.pull_request.paths`, or do not gate it")
        text = path.read_bytes().decode("utf-8")
        if not find_regions(text.splitlines(keepends=True), path.name):
            findings += pattern_findings(workflow, path.name, tracked, seen)
            continue
        traced_names.add(path.name)
        traced = Traced(root, path, tracked)
        # Trace entries for files that are gone: `generate` prunes them.
        stale = {job_id: sorted(set(job["files"]) - set(tracked))
                 for job_id, job in traced.trace["jobs"].items()}
        stale = {j: s for j, s in stale.items() if s}
        if stale and not args.check:
            for job_id, files in stale.items():
                job = traced.trace["jobs"][job_id]
                job["files"] = sorted(set(job["files"]) - set(files))
            writes.append((trace_path(root, path.name),
                           dump_trace(traced.trace).encode("utf-8"),
                           f"{TRACES}/{path.name}.json"))
        expected = traced.expected_block()
        new_text = traced.rendered(expected)
        # Parsed BEFORE anything is written: a region that renders into
        # something that is not this workflow any more is no answer.
        result = parse_workflow(new_text, f"{path.name} (as generated)")
        excluded = sorted(
            f for e in expected for f in matching(e, tracked)
            if not on_paths_covers(traced.full_patterns(expected), f))
        findings += [
            f"EXCLUDED    {path.name}: {f} is in the generated region but a "
            "later `!` pattern excludes it again" for f in excluded]
        if args.check:
            findings += [f"WIRING      {e}" for e in traced.wiring_errors()]
            if new_text != traced.text:
                missing = sorted(set(expected) - set(traced.block))
                extra = sorted(set(traced.block) - set(expected))
                findings.append(
                    f"DRIFT       {path.name}: a generated path-trace region "
                    "is not what the committed trace and the literal "
                    "imports of its files give"
                    + "".join(f"\n              missing: {m}" for m in missing)
                    + "".join(f"\n              not derived: {x}" for x in extra)
                    + ("" if missing or extra else
                       "\n              (the env / assert step text or the "
                       "entry order differs)"))
            findings += [
                f"DRIFT       {TRACES}/{path.name}.json: job `{j}` names "
                f"{f}, which is no longer a tracked file"
                for j, files in stale.items() for f in files]
            findings += pattern_findings(workflow, path.name, tracked, seen)
        else:
            findings += [f"WIRING      {e}" for e in traced.wiring_errors(result)]
            if new_text != traced.text:
                writes.append((path, new_text.encode("utf-8"),
                               f"{WORKFLOWS}/{path.name}"))
            # Of the text about to be written, so an entry this run drops is
            # not reported as dead by the same run.
            findings += pattern_findings(result, path.name, tracked, seen)
    findings += _ledger_findings(traced_names, gated, seen)
    # Every workflow has been read and every region placed by now: a caller
    # error above leaves the tree as it was. Every target is judged before the
    # first write (#3981), and each is named as it is written, so a write
    # refused part-way leaves the earlier ones named.
    for path, _data, _label in writes:
        check_repo_artifact(path)
    for path, data, label in writes:
        write_repo_artifact(path, data)
        print(f"wrote {label}")
    if findings:
        print(f"{len(findings)} finding(s):", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        if args.check and any(f.startswith("DRIFT") for f in findings):
            print("\nRegenerate with `make workflow-path-filters` "
                  "(python3 scripts/ops/workflow_path_filter.py generate).",
                  file=sys.stderr)
        return EXIT_VIOLATION
    if not writes:
        print(f"workflow path filters: {len(traced_names)} traced workflow(s) in "
              f"sync; no dead or unmodelled pattern in {len(_workflow_files(root))} "
              "workflow file(s).")
    return EXIT_OK


@exit_on_output_write_error
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep a workflow's on.pull_request.paths in step with what its jobs read.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, func, text in (
            ("assert", cmd_assert, "fail when the job read a file its workflow's paths do not cover"),
            ("record", cmd_record, "write a job's trace into .github/path-traces/")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--workflow", required=True, help="workflow file name")
        p.add_argument("--job", required=True, help="job id")
        p.add_argument("--state", required=True,
                       help="the state file `atime_trace.py finish` marked")
        if name == "assert":
            p.add_argument("--out", help="write this job's trace here (JSON)")
            p.add_argument("--leg", default="0/1", metavar="INDEX/TOTAL",
                           help="which run of the job this is (default: 0/1, "
                                "a job with no matrix)")
        p.set_defaults(func=func)

    p = sub.add_parser("merge", help="replace a job's record with the union of several "
                                     "`assert --out` files (the legs of a matrix job)")
    p.add_argument("--workflow", required=True, help="workflow file name")
    p.add_argument("--job", required=True, help="job id")
    p.add_argument("out_files", nargs="+", metavar="OUT",
                   help="a file `assert --out` wrote for this job")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("generate", help="write the generated regions of every traced workflow")
    p.add_argument("--check", action="store_true",
                   help="write nothing; exit 1 when a region or a pattern needs fixing")
    p.set_defaults(func=cmd_generate)

    args = parser.parse_args(argv)
    args.root = Path(os.path.realpath(args.root))
    return args.func(args)


if __name__ == "__main__":
    _lib_exitcodes.run(main)
