#!/usr/bin/env python3
"""workflow_path_filter.py — a workflow's `on.pull_request.paths` follows what its jobs load.

#3756. A workflow gated by `on.pull_request.paths` starts only for a PR that
touches a listed path. The lists were written by hand, and a file reached
through an import was nobody's to add: image-ref-resolve.yaml ran
check_image_refs_resolve.py without `scripts/tools/_lib_input.py` (imported by
a module it imports) in its list, so a PR changing only that file never
started the workflow.

The list is now measured, not chased. Three pieces, one file each:

  * scripts/ops/path_trace/sitecustomize.py — inside every Python process of a
    job, records the files executed as modules and the files opened for reading.
    scripts/ops/go_build.sh records each Go package it builds.
  * `assert` (the job's last `run:` step) — every first-party file the job
    loaded, and every in-repo source of every Go package it built, must be
    covered by the workflow's `on.pull_request.paths`. Each one that is not is
    named, and the job fails. `--out` writes the job's trace as JSON, in the
    shape `record` commits.
  * `generate` — writes three kinds of `# BEGIN GENERATED …` / `# END GENERATED
    …` regions of a traced workflow; `generate --check` (pre-commit hook
    `workflow-path-filter-check`, `make workflow-path-filters-check`) fails when
    a committed region differs. Nothing outside the regions is touched.
      - `path-trace`, inside `on.pull_request.paths` — the union of:
          · the committed trace, `.github/path-traces/<workflow>.json`
            (`record` writes it from a trace directory);
          · the Go sources of the packages the job built (`go list -deps`,
            stored in the trace: in-repo package directories, embedded files,
            and each in-repo module's go.mod / go.sum);
          · literal `import` statements, at any depth, in the traced Python
            files, looked up in the first-party `sys.path` directories of the
            trace and followed to a fixpoint — what a branch the CI arguments
            do not reach would load;
          · BOUNDARY below — files a non-Python process of the job reads;
          · the workflow file, its trace file, and this tool with its imports.
        An entry whose every file a hand-written pattern already covers is
        left out.
      - `path-trace-env job=<id>` and `path-trace-assert job=<id>`, inside each
        job's `steps:` — the step that switches the tracer on and the step that
        runs `assert`. Their text is fixed, so "is the job wired" is a byte
        comparison plus a check of WHERE they sit (see `Traced.wiring_errors`),
        not a search for a command name.

`generate --check` also holds, for EVERY workflow (traced or not):

  * no dead entry — each positive pattern of each `on.<event>.paths` list and
    each dorny/paths-filter filter matches at least one tracked file, unless it
    is in RESERVED_PATTERNS with its reason (checked both ways);
  * no pattern the matcher (scripts/ops/paths_filter_glob.py) does not model.
    That is reported as UNMODELLED, never as dead: deleting it would remove a
    trigger that may be working;
  * every workflow that gates on paths for a pull request — an
    `on.pull_request.paths`, or a job using dorny/paths-filter — either carries
    the generated regions or is named in NOT_YET_TRACED, with the reason.

Why a new import cannot slip past once a list has converged: it is written in a
file that is already listed, so the PR adding it starts the workflow, and
`assert` names the new file.

⚠️ Boundaries, stated because a green run is read as a guarantee:
  * a file read by a non-Python process (`make` reading the Makefile, an
    action reading `.python-version`, node, a Go binary at run time) is not
    traced. Such a file goes in BOUNDARY, by name and with the reason;
  * a Python child process started with `-I`, `-E` or `-S`, or with an `env=`
    that drops `PYTHONPATH` / `DA_PATH_TRACE_DIR`, is not traced, and nothing
    marks that it ran;
  * a Python step that was not traced at all (it ran before the env step, or
    in a container) is invisible when other steps of the job were traced:
    `assert` refuses an EMPTY trace, not a partial one. The position check on
    the env step is what stands in for this;
  * a file read through a symlink is named by its target only;
  * a directory listing is not a read: a file ADDED to a directory a job scans
    starts the workflow only if a pattern covers the directory;
  * `go test` and a `go build` that does not go through go_build.sh are not
    recorded (tests/ops/test_go_build_ssot.py holds the latter);
  * a module loaded by a computed path (`spec_from_file_location(ROOT / x)`)
    in a branch the CI arguments do not reach is in neither the trace nor the
    literal-import closure;
  * steps that did not run (an earlier step failed) loaded nothing;
  * `assert` and `record` discard their OWN process's trace, so what they read
    (the workflow, the trace file) is not part of a job's trace; this tool is
    listed through MACHINERY instead.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 nothing to fix; 1 a finding in
the repository (an uncovered file, a stale region, a dead or unmodelled
pattern, a mis-wired job); 2 no answer (a workflow that does not parse, markers
missing or misplaced, no trace or an incomplete one, `git` or `go` failing) —
never read as a pass.

Usage:
    python3 scripts/ops/workflow_path_filter.py env --trace-dir "$RUNNER_TEMP/path-trace" >> "$GITHUB_ENV"
    python3 scripts/ops/workflow_path_filter.py assert --workflow image-ref-resolve.yaml \\
        --job resolve --trace-dir "$RUNNER_TEMP/path-trace" --out "$RUNNER_TEMP/path-trace.json"
    python3 scripts/ops/workflow_path_filter.py record --workflow image-ref-resolve.yaml \\
        --job resolve --trace-dir /tmp/path-trace
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
TRACER_DIR = "scripts/ops/path_trace"
TRACER = f"{TRACER_DIR}/sitecustomize.py"
GO_BUILD_LOG = "go-build.log"
TRACE_ENV_NAMES = ("PYTHONPATH", "DA_PATH_TRACE_DIR")

# This tool and what it imports, listed in every traced workflow: a change to
# how the list is checked must start the workflows it checks. Resolved through
# the literal-import closure, from these seeds and these directories.
MACHINERY = ("scripts/ops/workflow_path_filter.py", TRACER)
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

ENV_STEP_NAME = "Trace what this job loads (#3756)"
ASSERT_STEP_NAME = "Path filter covers everything this job loaded (#3756)"
ASSERT_STEP_IF = "${{ !cancelled() }}"
ENV_STEP_RUN = ('python3 scripts/ops/workflow_path_filter.py env --trace-dir '
                '"$RUNNER_TEMP/path-trace" >> "$GITHUB_ENV"')


def assert_step_run(workflow: str, job: str) -> str:
    return ("python3 scripts/ops/workflow_path_filter.py assert "
            f"--workflow {workflow} --job {job} "
            '--trace-dir "$RUNNER_TEMP/path-trace" --out "$RUNNER_TEMP/path-trace.json"')


# A `run:` script is shell, so "does it change the tracer's environment" is not
# decidable here. Conservative instead: an assignment to either name, an
# `unset` / `env -u` naming one, or `env -i`, anywhere in the text, is refused.
_TRACE_ENV_TOUCH = re.compile(
    r"\b(?:PYTHONPATH|DA_PATH_TRACE_DIR)\s*\+?="
    r"|\bunset\b[^\n;|&]*\b(?:PYTHONPATH|DA_PATH_TRACE_DIR)\b"
    r"|\benv\b[^\n;|&]*\s-u\s*(?:PYTHONPATH|DA_PATH_TRACE_DIR)\b"
    r"|\benv\b[^\n;|&]*\s(?:-i|--ignore-environment|-)(?:\s|$)")

# ⛔ Only shrinks. A workflow that gates on paths for a pull request and does
# not carry the generated regions yet, and what stands between it and a trace.
# `generate --check` fails when such a workflow is in neither state, and when
# an entry here names a workflow that has since gained the regions (or lost
# its gate) — a stale entry would excuse the same workflow if it regressed.
# Its dead and unmodelled patterns are checked all the same.
NOT_YET_TRACED: dict[str, str] = {
    "backtest.yaml":
        "its Python step queries a Prometheus; without one it returns before "
        "the comparison, so a run that can be reproduced records a partial list",
    "bench-gate-pr.yaml":
        "`go test -bench` through shell scripts — not a go_build.sh build, so "
        "its Go inputs are not recorded",
    "bench-probe-write-latency.yaml":
        "builds with `go test -c`, which go_build.sh does not see",
    "bench-workload-effect.yaml":
        "builds with `go test -c`, which go_build.sh does not see",
    "bench-workload-record.yaml":
        "`go test -bench` through bench_gate_compare.sh",
    "ci.yml":
        "gated per job by dorny/paths-filter; the assert mode for a dorny gate "
        "is not decided (#3756)",
    "component-docker-build.yaml":
        "the reads happen inside `docker build`, outside any traced process",
    "config-diff-advisory.yml":
        "traceable (Python + go_build.sh) but not traced yet: what it loads "
        "depends on the PR's base/head conf.d pair",
    "docs-ci.yaml":
        "gated per job by dorny/paths-filter; the assert mode for a dorny gate "
        "is not decided (#3756)",
    "forge-e2e-github.yaml":
        "`go test -tags forge_e2e`, which go_build.sh does not see",
    "guard-defaults-impact.yml":
        "traceable (Python + go_build.sh) but not traced yet: what it runs "
        "depends on which defaults files the PR changes",
    "playwright.yml":
        "node and a browser do the reading",
    "required-checks-drift.yml":
        "traceable but not traced yet: its input is a GitHub API response "
        "fetched with the workflow token",
    "security-audit.yaml":
        "bandit reads the trees it is pointed at; the gate is those trees",
    "validate.yaml":
        "gated per job by dorny/paths-filter; the assert mode for a dorny gate "
        "is not decided (#3756)",
}

# The boundary set: files a traced job reads through something the tracer
# cannot see, by workflow. `generate` writes them into the `path-trace` region
# like any measured file. Each entry must match a tracked file, and each key
# must be a traced workflow — both checked by `generate --check`.
BOUNDARY: dict[str, dict[str, str]] = {
    "image-ref-resolve.yaml": {
        "Makefile":
            "`make da-crdecode-build`: make reads it, not Python",
        ".python-version":
            "actions/setup-python reads it (`python-version-file`)",
    },
}

# A pattern that matches no tracked file ON PURPOSE: (workflow, list label as
# `generate --check` prints it, pattern) → why it stays. Checked both ways: a
# dead pattern not listed here is DEAD, and an entry here whose pattern matches
# a tracked file (or is no longer in that list) is STALE.
RESERVED_PATTERNS: dict[tuple[str, str, str], str] = {
    ("ci.yml", "filter `portal`", "k8s/03-monitoring/configmap-rules-*.yml"):
        "the rule-pack ConfigMap family declares `.yaml` and `.yml` as names "
        "of one member (scripts/tools/_lib_consumer_read.py RULE_SOURCES), and "
        "tests/shared/test_rule_source_globs_declared_once.py requires a "
        "filter that watches the family to match every declared name; no "
        "`.yml` file is tracked today",
}


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
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _caller_error(
            f"{where} does not parse ({type(exc).__name__}), so it was NOT "
            "checked — fix it, do not skip it.") from exc
    if not isinstance(doc, dict):
        raise _caller_error(f"{where} is not a mapping at the top level.")
    return doc


def load_workflow(path: Path) -> dict:
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


def uses_dorny(workflow: dict) -> bool:
    jobs = workflow.get("jobs")
    return any("dorny/paths-filter" in str(step.get("uses", ""))
               for job in (jobs.values() if isinstance(jobs, dict) else ())
               for step in _steps(job))


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

def _rel(root: Path, raw: str) -> str | None:
    try:
        real = os.path.realpath(raw)
    except OSError:
        return None
    try:
        rel = os.path.relpath(real, root)
    except ValueError:
        return None
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return rel.replace(os.sep, "/")


def discard_own_trace() -> None:
    """Drop the log the tracer opened for THIS process, when the tool runs
    inside a traced job. What `assert` / `record` read is not something the
    job loaded; left in, a second `record` of the same directory would differ
    from the first by exactly those reads."""
    trace_dir = os.environ.get("DA_PATH_TRACE_DIR")
    if not trace_dir:
        return
    for log in Path(trace_dir).glob(f"py-{os.getpid()}-*.log"):
        try:
            log.unlink()
        except OSError as exc:
            raise _caller_error(
                f"cannot remove this process's own trace {log}: {exc}") from exc


def read_trace_dir(root: Path, trace_dir: Path, tracked: frozenset[str]) -> dict:
    """{"code": tracked files executed as modules, "opened": tracked files
    opened for reading, "import_dirs": first-party sys.path directories,
    "go_builds": [(module dir, package)]} from a trace directory."""
    logs = sorted(trace_dir.glob("py-*.log")) if trace_dir.is_dir() else []
    if not logs:
        raise _caller_error(
            f"no Python trace in {trace_dir}: nothing was measured. The job "
            "must export the two lines `workflow_path_filter.py env` prints "
            "BEFORE its first Python step.")
    code: set[str] = set()
    opened: set[str] = set()
    dirs: set[str] = set()
    errors: set[str] = set()
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            kind, _, raw = line.partition("\t")
            if not raw:
                continue
            if kind == "error":
                errors.add(raw)
                continue
            rel = _rel(root, raw)
            if rel is None:
                continue
            if kind == "path":
                if (root / rel).is_dir():
                    dirs.add(rel)
            elif rel in tracked:
                (opened if kind == "open" else code).add(rel)
    if errors:
        raise _caller_error(
            f"the trace in {trace_dir} is incomplete: the tracer could not "
            "record " + "; ".join(sorted(errors)) + ". What those events "
            "loaded is unknown, so this is not a pass.")
    builds: list[tuple[str, str]] = []
    go_log = trace_dir / GO_BUILD_LOG
    if go_log.is_file():
        for line in go_log.read_text(encoding="utf-8").splitlines():
            module_dir, _, package = line.partition("\t")
            rel = _rel(root, module_dir) if module_dir else None
            if rel is None or not package:
                raise _caller_error(
                    f"{go_log}: a line does not name a module directory inside "
                    f"the repository and a package: {line!r}")
            if (rel, package) not in builds:
                builds.append((rel, package))
    return {"code": code, "opened": opened - code, "import_dirs": dirs,
            "go_builds": sorted(builds)}


_GO_LIST_FORMAT = (
    "{{if not .Standard}}{{.Dir}}\t{{if .Module}}{{.Module.GoMod}}{{end}}\t"
    "{{range .EmbedFiles}}{{.}}\x1f{{end}}{{end}}")


def go_sources(root: Path, module_dir: str, package: str,
               tracked: frozenset[str]) -> list[str]:
    """Path-filter entries for the in-repo sources `go build <package>` reads:
    `<dir>/*.go` per in-repo package, each embedded file, and go.mod / go.sum
    of each in-repo module."""
    try:
        out = subprocess.run(
            ["go", "list", "-deps", "-f", _GO_LIST_FORMAT, package],
            cwd=root / module_dir, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", check=True, timeout=600).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or repr(exc)
        raise _caller_error(
            f"`go list -deps {package}` failed in {module_dir}, so the Go "
            f"sources that build reads are unknown: {detail.strip()}") from exc
    entries: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        pkg_dir, gomod, embeds = (line.split("\t") + ["", ""])[:3]
        rel_dir = _rel(root, pkg_dir)
        if rel_dir is None:
            continue  # a dependency in the module cache
        entries.add(f"{rel_dir}/*.go")
        for name in filter(None, embeds.split("\x1f")):
            rel = _rel(root, os.path.join(pkg_dir, name))
            if rel in tracked:
                entries.add(rel)
        if gomod:
            for name in (gomod, gomod[:-len("mod")] + "sum"):
                rel = _rel(root, name)
                if rel in tracked:
                    entries.add(rel)
    if not entries:
        raise _caller_error(
            f"`go list -deps {package}` in {module_dir} named no package "
            "inside the repository.")
    return sorted(entries)


def job_record(root: Path, trace: dict, tracked: frozenset[str],
               manual: list[str]) -> dict:
    """The committed form of one job's trace. Opened-only files a hand-written
    pattern covers are left out: a job that reads a whole tree (`k8s/**`)
    would otherwise make every file added there a change to the trace."""
    return {
        "files": sorted(trace["code"] | {
            f for f in trace["opened"] if not on_paths_covers(manual, f)}),
        "import_dirs": sorted(trace["import_dirs"]),
        "go": [{"module_dir": m, "package": p,
                "sources": go_sources(root, m, p, tracked)}
               for m, p in trace["go_builds"]],
    }


def trace_path(root: Path, workflow: str) -> Path:
    return root / TRACES / f"{workflow}.json"


def load_trace(root: Path, workflow: str) -> dict:
    """The committed trace; an absent file is an empty one (a workflow being
    wired for the first time), a malformed one is no answer."""
    path = trace_path(root, workflow)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        jobs = doc["jobs"]
        for job in jobs.values():
            for key in ("files", "import_dirs", "go"):
                if not isinstance(job[key], list):
                    raise TypeError(f"`{key}` is not a list")
            for build in job["go"]:
                if not isinstance(build["sources"], list):
                    raise TypeError("`sources` is not a list")
    except FileNotFoundError:
        return {"workflow": workflow, "jobs": {}}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _caller_error(
            f"{path.relative_to(root).as_posix()} is not a trace file "
            f"`workflow_path_filter.py record` wrote ({type(exc).__name__}: "
            f"{exc}). Record it again; do not edit it.") from exc
    return doc


def dump_trace(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ── literal imports ─────────────────────────────────────────────────────────

def literal_import_closure(root: Path, seeds: set[str], import_dirs: list[str],
                           tracked: frozenset[str]) -> set[str]:
    """`seeds` plus every tracked file a literal `import` in them (at any
    depth, followed transitively) would load from `import_dirs`.

    Deliberately not a resolver: a name is looked up as `<dir>/<a>/<b>.py` or
    `<dir>/<a>/<b>/__init__.py` in every traced `sys.path` directory, and a
    relative import beside the importing file. A module loaded by a computed
    path is the trace's job, not this function's. A file that does not parse
    is an error — a file whose imports cannot be read would otherwise
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


def required_entries(root: Path, workflow: str, trace: dict,
                     tracked: frozenset[str]) -> set[str]:
    entries = {f"{WORKFLOWS}/{workflow}",
               trace_path(root, workflow).relative_to(root).as_posix()}
    entries |= set(BOUNDARY.get(workflow, {}))
    entries |= literal_import_closure(
        root, {f for f in MACHINERY if f in tracked},
        list(MACHINERY_IMPORT_DIRS), tracked)
    for job in trace["jobs"].values():
        files = {f for f in job["files"] if f in tracked}
        entries |= literal_import_closure(root, files, job["import_dirs"], tracked)
        for build in job["go"]:
            entries |= set(build["sources"])
            entries.add("scripts/ops/go_build.sh")
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
        """Is each job of `workflow` (default: the file as committed) traced
        from its first command to its last?

        Read from the PARSED workflow, so it holds whatever the text looks
        like: the env step is the job's first `run:` step (a `uses:` step may
        come before it — what such an action reads is BOUNDARY's), the assert
        step is its last, both are exactly the generated steps, nothing in the
        job can make a failure of theirs not count, and nothing else in the
        workflow sets the two variables the tracer lives on.
        """
        workflow = self.workflow if workflow is None else workflow
        errors = []
        jobs = workflow.get("jobs")
        jobs = jobs if isinstance(jobs, dict) else {}
        if any(k in TRACE_ENV_NAMES for k in (workflow.get("env") or {})):
            errors.append(f"{self.name}: the workflow-level `env:` sets "
                          f"{' / '.join(TRACE_ENV_NAMES)}")
        for job_id, job in jobs.items():
            where = f"{self.name}: job `{job_id}`"
            job = job if isinstance(job, dict) else {}
            steps = _steps(job)
            if job.get("continue-on-error"):
                errors.append(f"{where} sets `continue-on-error`, so an "
                              "uncovered file would not fail it")
            if any(k in TRACE_ENV_NAMES for k in (job.get("env") or {})):
                errors.append(f"{where} sets {' / '.join(TRACE_ENV_NAMES)} in "
                              "its `env:`")
            if "container" in job:
                errors.append(f"{where} runs in a `container:`, where the "
                              "exported tracer path does not exist")
            env_at = [i for i, s in enumerate(steps)
                      if s == {"name": ENV_STEP_NAME, "run": ENV_STEP_RUN}]
            want = {"name": ASSERT_STEP_NAME, "if": ASSERT_STEP_IF,
                    "run": assert_step_run(self.name, job_id)}
            assert_at = [i for i, s in enumerate(steps) if s == want]
            runs = [i for i, s in enumerate(steps) if "run" in s]
            if len(env_at) != 1:
                errors.append(
                    f"{where} must hold the generated env step exactly once, "
                    f"unchanged (found {len(env_at)}) — nothing it loads is "
                    "recorded otherwise")
            elif runs and runs[0] != env_at[0]:
                errors.append(
                    f"{where}: step {runs[0] + 1} runs a command before the "
                    "env step, so what it loads is not recorded. The env step "
                    "must be the job's first `run:` step")
            if len(assert_at) != 1:
                errors.append(
                    f"{where} must hold the generated assert step exactly "
                    f"once, unchanged (found {len(assert_at)})")
            elif runs and runs[-1] != assert_at[0]:
                errors.append(
                    f"{where}: step {runs[-1] + 1} runs a command after the "
                    "assert step, so what it loads is not checked. The assert "
                    "step must be the job's last `run:` step")
            for i, step in enumerate(steps):
                if i in env_at or i in assert_at:
                    continue
                if any(k in TRACE_ENV_NAMES for k in (step.get("env") or {})):
                    errors.append(f"{where}: step {i + 1} sets "
                                  f"{' / '.join(TRACE_ENV_NAMES)} in its `env:`")
                if _TRACE_ENV_TOUCH.search(str(step.get("run") or "")):
                    errors.append(
                        f"{where}: step {i + 1}'s `run:` assigns, unsets or "
                        f"clears {' / '.join(TRACE_ENV_NAMES)} — the tracer "
                        "lives on those two variables")
            if job_id not in self.trace["jobs"]:
                errors.append(
                    f"{where} has no recorded trace in {TRACES}/{self.name}.json "
                    "(run its steps under the tracer, then "
                    "`workflow_path_filter.py record`)")
        for job_id in self.trace["jobs"]:
            if job_id not in jobs:
                errors.append(
                    f"{TRACES}/{self.name}.json holds a trace for job "
                    f"`{job_id}`, which {self.name} does not define")
        return errors


# ── dead and unmodelled patterns, every workflow ────────────────────────────

def _filter_rule_glob(rule: object) -> str | None:
    """dorny/paths-filter accepts a bare glob or `{<change types>: <glob>}`."""
    if isinstance(rule, str):
        return rule
    if isinstance(rule, dict) and len(rule) == 1:
        value = next(iter(rule.values()))
        return value if isinstance(value, str) else None
    return None


def pattern_lists(workflow: dict, where: str) -> list[tuple[str, list[object]]]:
    """(label, patterns) for every `on.<event>.paths` / `paths-ignore` list and
    every dorny/paths-filter filter in a workflow."""
    import yaml
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
    jobs = workflow.get("jobs")
    for job_id, job in (jobs.items() if isinstance(jobs, dict) else ()):
        for step in _steps(job):
            if "dorny/paths-filter" not in str(step.get("uses", "")):
                continue
            raw = (step.get("with") or {}).get("filters")
            try:
                filters = yaml.safe_load(raw) if isinstance(raw, str) else raw
            except yaml.YAMLError as exc:
                raise _caller_error(
                    f"{where}: job `{job_id}`'s paths-filter `filters` does not "
                    f"parse ({type(exc).__name__}).") from exc
            if not isinstance(filters, dict):
                raise _caller_error(
                    f"{where}: job `{job_id}`'s paths-filter `filters` is not an "
                    "inline mapping — a filters FILE is not modelled.")
            for name, rules in filters.items():
                if not isinstance(rules, list):
                    raise _caller_error(
                        f"{where}: paths-filter `{name}` is not a list.")
                found.append((f"filter `{name}`", rules))
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
                    f"UNMODELLED  {where} {label}: {rule!r} — not a glob string "
                    "or a one-key `{change types: glob}` rule; it was NOT checked")
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


def cmd_env(args: argparse.Namespace) -> int:
    tracer = (args.root / TRACER_DIR).as_posix()
    if not (args.root / TRACER).is_file():
        raise _caller_error(f"{tracer}/sitecustomize.py is missing.")
    inherited = os.environ.get("PYTHONPATH")
    print("PYTHONPATH=" + tracer + (os.pathsep + inherited if inherited else ""))
    print(f"DA_PATH_TRACE_DIR={Path(args.trace_dir).resolve().as_posix()}")
    return EXIT_OK


def _job_trace(args: argparse.Namespace) -> tuple[Traced, dict, dict]:
    discard_own_trace()
    tracked = tracked_files(args.root)
    path = args.root / WORKFLOWS / args.workflow
    if not path.is_file():
        raise _caller_error(f"{WORKFLOWS}/{args.workflow} does not exist.")
    traced = Traced(args.root, path, tracked)
    if args.job not in traced.job_ids():
        raise _caller_error(
            f"{args.workflow} has no job `{args.job}` (it has: "
            f"{', '.join(traced.job_ids()) or 'none'}).")
    raw = read_trace_dir(args.root, Path(args.trace_dir), frozenset(tracked))
    if not (raw["code"] - {TRACER}):
        raise _caller_error(
            f"the trace in {args.trace_dir} holds no first-party file besides "
            "the tracer itself: the job's Python steps ran before the tracer "
            "was set up, or in another checkout. Nothing was measured.")
    return traced, raw, job_record(args.root, raw, frozenset(tracked), traced.manual)


def cmd_assert(args: argparse.Namespace) -> int:
    traced, raw, record = _job_trace(args)
    if args.out:
        Path(args.out).write_text(dump_trace(
            {"workflow": traced.name, "jobs": {args.job: record}}),
            encoding="utf-8", newline="\n")
    loaded = raw["code"] | raw["opened"]
    uncovered = sorted(f for f in loaded if not on_paths_covers(traced.paths_now(), f))
    go_uncovered = sorted({
        (f, f"{b['module_dir']} {b['package']}")
        for b in record["go"] for entry in b["sources"] + ["scripts/ops/go_build.sh"]
        for f in matching(entry, traced.tracked)
        if not on_paths_covers(traced.paths_now(), f)})
    print(f"{traced.name} job `{args.job}`: {len(loaded)} first-party file(s) "
          f"loaded by Python, {len(record['go'])} Go build(s).")
    if not uncovered and not go_uncovered:
        print("Every one is covered by `on.pull_request.paths`.")
        return EXIT_OK
    print(f"\n{len(uncovered) + len(go_uncovered)} file(s) this job loaded are "
          f"NOT covered by {traced.name}'s `on.pull_request.paths` — a PR "
          "changing only one of them would not start this workflow:",
          file=sys.stderr)
    for f in uncovered:
        print(f"  UNCOVERED  {f}", file=sys.stderr)
    for f, build in go_uncovered:
        print(f"  UNCOVERED  {f}  (go build {build})", file=sys.stderr)
    print(
        f"\nFix: make the `{args.job}` entry of {TRACES}/{traced.name}.json "
        "this run's trace — the `--out` file (uploaded as an artifact) holds "
        "exactly what `workflow_path_filter.py record` would write for the "
        "job; or run the job's steps locally under the tracer and `record` — "
        "then `python3 scripts/ops/workflow_path_filter.py generate` and "
        "commit both files. A file read by something other than Python "
        "belongs in BOUNDARY (scripts/ops/workflow_path_filter.py).",
        file=sys.stderr)
    return EXIT_VIOLATION


def cmd_record(args: argparse.Namespace) -> int:
    traced, _raw, record = _job_trace(args)
    doc = traced.trace
    doc["workflow"] = traced.name
    doc["jobs"][args.job] = record
    path = trace_path(args.root, traced.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_trace(doc), encoding="utf-8", newline="\n")
    print(f"recorded {len(record['files'])} file(s), {len(record['go'])} Go "
          f"build(s) for job `{args.job}` in {path.relative_to(args.root).as_posix()}; "
          "now run `workflow_path_filter.py generate`.")
    return EXIT_OK


def _ledger_findings(tracked: tuple[str, ...], traced_names: set[str],
                     gated: set[str], seen: set[tuple[str, str, str]]) -> list[str]:
    findings = []
    for name in sorted(gated - traced_names - set(NOT_YET_TRACED)):
        findings.append(
            f"UNTRACED    {name} gates on paths for a pull request but has "
            "neither the generated path-trace regions nor an entry in "
            "NOT_YET_TRACED (scripts/ops/workflow_path_filter.py). Trace it; "
            "add an entry only with what stands in the way")
    for name in sorted(set(NOT_YET_TRACED) & traced_names
                       | set(NOT_YET_TRACED) - gated):
        findings.append(
            f"STALE       NOT_YET_TRACED names {name}, which "
            + ("carries the generated regions now" if name in traced_names
               else "has no path gate (or no longer exists)")
            + " — delete the entry")
    for key in sorted(set(RESERVED_PATTERNS) - seen):
        findings.append(
            f"STALE       RESERVED_PATTERNS names {key[0]} {key[1]}: {key[2]!r}, "
            "which is not a pattern of that list — delete the entry")
    for name, entries in sorted(BOUNDARY.items()):
        if name not in traced_names:
            findings.append(
                f"STALE       BOUNDARY names {name}, which does not carry the "
                "generated regions — delete the entry")
        for entry in sorted(entries):
            why = unmodelled(entry)
            if why is not None:
                findings.append(f"UNMODELLED  BOUNDARY {name}: {entry!r} — {why}")
            elif not any(covers(entry, f) for f in tracked):
                findings.append(
                    f"DEAD        BOUNDARY {name}: {entry!r} matches no "
                    "tracked file")
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
        if _gate_or_caller_error(workflow, path.name) is not None or uses_dorny(workflow):
            gated.add(path.name)
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
                    "is not what the committed trace, the Go sources, the "
                    "literal imports and the boundary set give"
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
    findings += _ledger_findings(tracked, traced_names, gated, seen)
    # Every workflow has been read and every region placed by now: a caller
    # error above leaves the tree as it was.
    for path, data, label in writes:
        path.write_bytes(data)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep a workflow's on.pull_request.paths in step with what its jobs load.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("env", help="print the GITHUB_ENV lines that switch the tracer on")
    p.add_argument("--trace-dir", required=True)
    p.set_defaults(func=cmd_env)

    for name, func, text in (
            ("assert", cmd_assert, "fail when the job loaded a file its workflow's paths do not cover"),
            ("record", cmd_record, "write a job's trace into .github/path-traces/")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--workflow", required=True, help="workflow file name")
        p.add_argument("--job", required=True, help="job id")
        p.add_argument("--trace-dir", required=True)
        if name == "assert":
            p.add_argument("--out", help="write this job's trace here (JSON)")
        p.set_defaults(func=func)

    p = sub.add_parser("generate", help="write the generated regions of every traced workflow")
    p.add_argument("--check", action="store_true",
                   help="write nothing; exit 1 when a region or a pattern needs fixing")
    p.set_defaults(func=cmd_generate)

    args = parser.parse_args(argv)
    args.root = Path(os.path.realpath(args.root))
    return args.func(args)


if __name__ == "__main__":
    _lib_exitcodes.run(main)
