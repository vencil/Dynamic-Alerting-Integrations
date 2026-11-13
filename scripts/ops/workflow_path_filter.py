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
  * `assert` (the job's last step) — every first-party file the job loaded, and
    every in-repo source of every Go package it built, must be covered by the
    workflow's `on.pull_request.paths`. Each one that is not is named, and the
    job fails. It also writes the trace as JSON (uploaded as an artifact).
  * `generate` — writes the union below between the `# BEGIN GENERATED
    path-trace` / `# END GENERATED path-trace` lines of the workflow's
    `on.pull_request.paths`; `generate --check` (pre-commit hook
    `workflow-path-filter-check`, `make workflow-path-filters-check`) fails when
    the committed block differs. The rest of the workflow file is not touched.
      - the committed trace, `.github/path-traces/<workflow>.json` (`record`
        writes it from a trace directory);
      - the Go sources of the packages the job built (`go list -deps`, stored
        in the trace: in-repo package directories, embedded files, and each
        in-repo module's go.mod / go.sum);
      - literal `import` statements, at any depth, in the traced Python files,
        looked up in the first-party `sys.path` directories of the trace and
        followed to a fixpoint. This is what a branch the CI arguments do not
        reach would load;
      - the workflow file itself and its trace file.
    An entry whose every file a hand-written pattern already covers is left out.

`generate --check` also holds, for EVERY workflow (traced or not):

  * no dead entry — each positive pattern of each `on.<event>.paths` list and
    each dorny/paths-filter filter matches at least one tracked file;
  * no pattern the matcher (scripts/ops/paths_filter_glob.py) does not model.
    That is reported as UNMODELLED, never as dead: deleting it would remove a
    trigger that may be working;
  * every workflow with a `pull_request` path gate either carries the generated
    block or is named in NOT_YET_TRACED, with the reason.

Why a new import cannot slip past once a list has converged: it is written in a
file that is already listed, so the PR adding it starts the workflow, and
`assert` names the new file.

⚠️ Boundaries, stated because a green run is read as a guarantee:
  * a file read by a non-Python process (`cat`, `make` reading the Makefile,
    node, a Go binary at run time) is not traced. Such a file is listed by
    hand, outside the generated block — those hand-written lines ARE the
    boundary set;
  * an interpreter started with `-I`, `-E` or `-S` ignores `PYTHONPATH` or
    `site` and is not traced;
  * a directory listing is not a read: a file ADDED to a directory a job scans
    starts the workflow only if a pattern covers the directory;
  * `go test` and a `go build` that does not go through go_build.sh are not
    recorded (tests/ops/test_go_build_ssot.py holds the latter);
  * a module loaded by a computed path (`spec_from_file_location(ROOT / x)`)
    in a branch the CI arguments do not reach is in neither the trace nor the
    literal-import closure;
  * steps that did not run (an earlier step failed) loaded nothing.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 nothing to fix; 1 a finding in
the repository (an uncovered file, a stale block, a dead or unmodelled
pattern); 2 no answer (a workflow that does not parse, markers missing or
misplaced, no trace, `git` or `go` failing) — never read as a pass.

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
GO_BUILD_LOG = "go-build.log"
BEGIN = re.compile(r"^(?P<indent>[ \t]*)# BEGIN GENERATED path-trace\b")
END = re.compile(r"^[ \t]*# END GENERATED path-trace\b")
ENTRY = re.compile(r"""^[ \t]*-[ \t]+(?P<q>["'])(?P<v>[^"']+)(?P=q)[ \t]*$""")

# ⛔ Only shrinks. A workflow with a path gate that does not carry the
# generated block yet, and what stands between it and a trace. `generate
# --check` fails when a path-gated workflow is in neither state, and when an
# entry here names a workflow that has since gained the block (or lost its
# gate) — a stale entry would excuse the same workflow if it regressed.
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
    "component-docker-build.yaml":
        "the reads happen inside `docker build`, outside any traced process",
    "config-diff-advisory.yml":
        "traceable (Python + go_build.sh) but not traced yet: what it loads "
        "depends on the PR's base/head conf.d pair",
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


def load_workflow(path: Path) -> dict:
    import yaml
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, OSError) as exc:
        raise _caller_error(
            f"{path.name} does not parse ({type(exc).__name__}), so it was NOT "
            "checked — fix it, do not skip it.") from exc
    if not isinstance(doc, dict):
        raise _caller_error(f"{path.name} is not a mapping at the top level.")
    return doc


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


# ── the generated block (text, never a YAML round-trip) ─────────────────────

def split_block(text: str, where: str) -> tuple[list[str], list[str], list[str], str] | None:
    """(lines before BEGIN inclusive, entries, lines from END on, indent), or
    None when the file has no markers. Lines keep their own line endings."""
    lines = text.splitlines(keepends=True)
    begins = [i for i, line in enumerate(lines) if BEGIN.match(line)]
    ends = [i for i, line in enumerate(lines) if END.match(line)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise _caller_error(
            f"{where}: expected exactly one `# BEGIN GENERATED path-trace` line "
            f"followed by one `# END GENERATED path-trace` line; found "
            f"{len(begins)} and {len(ends)}.")
    entries = []
    for line in lines[begins[0] + 1:ends[0]]:
        m = ENTRY.match(line.rstrip("\r\n"))
        if not m:
            raise _caller_error(
                f"{where}: a line inside the generated path-trace block is not "
                f"a quoted list entry: {line.rstrip()!r}. The block is written "
                "by `workflow_path_filter.py generate`; do not edit it.")
        entries.append(m.group("v"))
    return (lines[:begins[0] + 1], entries, lines[ends[0]:],
            BEGIN.match(lines[begins[0]]).group("indent"))


def render_block(before: list[str], entries: list[str], after: list[str],
                 indent: str) -> str:
    eol = "\r\n" if before[-1].endswith("\r\n") else "\n"
    return "".join(before + [f'{indent}- "{e}"{eol}' for e in entries] + after)


def manual_patterns(paths: list[str], block: list[str], where: str) -> tuple[list[str], int]:
    """The hand-written part of `paths`, and where the block sits in it."""
    n = len(block)
    for i in range(len(paths) - n + 1):
        if paths[i:i + n] == block:
            return paths[:i] + paths[i + n:], i
    raise _caller_error(
        f"{where}: the generated path-trace block is not a run of "
        "`on.pull_request.paths` entries — the marker lines must sit inside "
        "that list.")


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
    for log in logs:
        for line in log.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            kind, _, raw = line.partition("\t")
            if not raw:
                continue
            rel = _rel(root, raw)
            if rel is None:
                continue
            if kind == "path":
                if (root / rel).is_dir():
                    dirs.add(rel)
            elif rel in tracked:
                (opened if kind == "open" else code).add(rel)
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
    # This process is part of the job too, and its own log is still being
    # written — take what it has loaded directly instead of racing the file.
    for module in list(sys.modules.values()):
        rel = _rel(root, getattr(module, "__file__", None) or "")
        if rel in tracked:
            code.add(rel)
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
    path = trace_path(root, workflow)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        jobs = doc["jobs"]
        for job in jobs.values():
            assert isinstance(job["files"], list)
            assert isinstance(job["import_dirs"], list)
            assert isinstance(job["go"], list)
    except FileNotFoundError:
        return {"workflow": workflow, "jobs": {}}
    except (OSError, ValueError, KeyError, TypeError, AssertionError, AttributeError) as exc:
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
    """One workflow that carries the generated block."""

    def __init__(self, root: Path, path: Path, tracked: tuple[str, ...]):
        self.root, self.path, self.name = root, path, path.name
        self.text = path.read_bytes().decode("utf-8")
        self.workflow = load_workflow(path)
        split = split_block(self.text, self.name)
        if split is None:
            raise _caller_error(
                f"{self.name} has no `# BEGIN GENERATED path-trace` / `# END "
                "GENERATED path-trace` lines in its `on.pull_request.paths`.")
        self.before, self.block, self.after, self.indent = split
        paths = _gate_or_caller_error(self.workflow, self.name)
        if paths is None:
            raise _caller_error(
                f"{self.name} carries the generated path-trace block but has "
                "no `on.pull_request.paths`.")
        self.paths = paths
        self.manual, self.at = manual_patterns(paths, self.block, self.name)
        self.tracked = tracked
        self.trace = load_trace(root, self.name)

    def expected_block(self) -> list[str]:
        entries = required_entries(self.root, self.name, self.trace,
                                   frozenset(self.tracked))
        return generated_block(entries, self.manual, self.tracked)

    def full_patterns(self, block: list[str]) -> list[str]:
        return self.manual[:self.at] + block + self.manual[self.at:]

    def rendered(self, block: list[str]) -> str:
        return render_block(self.before, block, self.after, self.indent)

    def job_ids(self) -> list[str]:
        jobs = self.workflow.get("jobs")
        return list(jobs) if isinstance(jobs, dict) else []

    def wiring_errors(self) -> list[str]:
        """Each job must set the tracer up and end on `assert` for itself."""
        errors = []
        jobs = self.workflow.get("jobs") or {}
        for job_id, job in jobs.items():
            steps = [s for s in (job or {}).get("steps") or [] if isinstance(s, dict)]
            runs = [str(s.get("run") or "") for s in steps]
            env_at = [i for i, r in enumerate(runs)
                      if re.search(r"workflow_path_filter\.py\s+env\b", r)]
            want = re.compile(
                r"workflow_path_filter\.py\s+assert\b(?=[^\n]*--workflow\s+"
                + re.escape(self.name) + r"(\s|$))(?=[^\n]*--job\s+"
                + re.escape(job_id) + r"(\s|$))")
            assert_at = [i for i, r in enumerate(runs)
                         if want.search(r.replace("\\\n", " "))]
            if not env_at:
                errors.append(
                    f"{self.name}: job `{job_id}` never runs "
                    "`workflow_path_filter.py env`, so nothing it loads is "
                    "recorded")
            if not assert_at:
                errors.append(
                    f"{self.name}: job `{job_id}` has no step running "
                    f"`workflow_path_filter.py assert --workflow {self.name} "
                    f"--job {job_id}`")
                continue
            step = steps[assert_at[-1]]
            if step.get("continue-on-error"):
                errors.append(
                    f"{self.name}: job `{job_id}`'s assert step sets "
                    "`continue-on-error`, so an uncovered file would not fail it")
            condition = re.sub(r"\s+", "", str(step.get("if", "")))
            if condition not in ("${{!cancelled()}}", "!cancelled()"):
                errors.append(
                    f"{self.name}: job `{job_id}`'s assert step must carry "
                    "`if: ${{ !cancelled() }}` and nothing else — it has to "
                    "report what the job loaded when an earlier step failed too")
            if job_id not in self.trace["jobs"]:
                errors.append(
                    f"{self.name}: job `{job_id}` has no recorded trace in "
                    f"{TRACES}/{self.name}.json (run its steps under the "
                    "tracer, then `workflow_path_filter.py record`)")
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
        for step in (job or {}).get("steps") or []:
            if not isinstance(step, dict) or "dorny/paths-filter" not in str(step.get("uses", "")):
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


def pattern_findings(workflow: dict, where: str,
                     tracked: tuple[str, ...]) -> list[str]:
    """DEAD and UNMODELLED patterns of one workflow, as report lines."""
    findings = []
    for label, patterns in pattern_lists(workflow, where):
        for rule in patterns:
            glob = _filter_rule_glob(rule)
            if glob is None:
                findings.append(
                    f"UNMODELLED  {where} {label}: {rule!r} — not a glob string "
                    "or a one-key `{change types: glob}` rule; it was NOT checked")
                continue
            positive = glob[1:] if glob.startswith("!") else glob
            why = unmodelled(positive)
            if why is not None:
                findings.append(
                    f"UNMODELLED  {where} {label}: {glob!r} — {why}. It was NOT "
                    "judged dead or alive: rewrite it in the modelled alphabet "
                    "or teach scripts/ops/paths_filter_glob.py the shape")
            elif not glob.startswith("!") and not any(covers(glob, f) for f in tracked):
                findings.append(
                    f"DEAD        {where} {label}: {glob!r} matches no tracked "
                    "file, so it can never start anything (a renamed or "
                    "removed file leaves its real trigger unlisted)")
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
    if not (args.root / TRACER_DIR / "sitecustomize.py").is_file():
        raise _caller_error(f"{tracer}/sitecustomize.py is missing.")
    inherited = os.environ.get("PYTHONPATH")
    print("PYTHONPATH=" + tracer + (os.pathsep + inherited if inherited else ""))
    print(f"DA_PATH_TRACE_DIR={Path(args.trace_dir).resolve().as_posix()}")
    return EXIT_OK


def _job_trace(args: argparse.Namespace) -> tuple[Traced, dict, dict]:
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
    own = {f"{TRACER_DIR}/sitecustomize.py"}
    if not (raw["code"] - own - _own_modules(args.root, frozenset(tracked))):
        raise _caller_error(
            f"the trace in {args.trace_dir} holds no first-party file besides "
            "the tracer itself: the job's Python steps ran before the tracer "
            "was set up, or in another checkout. Nothing was measured.")
    return traced, raw, job_record(args.root, raw, frozenset(tracked), traced.manual)


def _own_modules(root: Path, tracked: frozenset[str]) -> set[str]:
    return {rel for rel in (
        _rel(root, getattr(m, "__file__", None) or "")
        for m in list(sys.modules.values())) if rel in tracked}


def cmd_assert(args: argparse.Namespace) -> int:
    traced, raw, record = _job_trace(args)
    if args.out:
        Path(args.out).write_text(dump_trace(
            {"workflow": traced.name, "jobs": {args.job: record}}),
            encoding="utf-8", newline="\n")
    loaded = raw["code"] | raw["opened"]
    uncovered = sorted(f for f in loaded if not on_paths_covers(traced.paths, f))
    go_uncovered = sorted({
        (f, f"{b['module_dir']} {b['package']}")
        for b in record["go"] for entry in b["sources"] + ["scripts/ops/go_build.sh"]
        for f in matching(entry, traced.tracked)
        if not on_paths_covers(traced.paths, f)})
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
        "\nFix: take this run's trace (the `--out` file, uploaded as an "
        f"artifact) as the `{args.job}` entry of {TRACES}/{traced.name}.json — "
        "or run the job's steps locally under the tracer and "
        "`workflow_path_filter.py record` — then "
        "`python3 scripts/ops/workflow_path_filter.py generate` and commit "
        "both files. A file read by something other than Python belongs in the "
        "hand-written part of the list.", file=sys.stderr)
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


def cmd_generate(args: argparse.Namespace) -> int:
    root = args.root
    tracked = tracked_files(root)
    findings: list[str] = []
    wrote: list[str] = []
    seen_traced: set[str] = set()
    gated: set[str] = set()
    for path in _workflow_files(root):
        workflow = load_workflow(path)
        if _gate_or_caller_error(workflow, path.name) is not None:
            gated.add(path.name)
        if split_block(path.read_bytes().decode("utf-8"), path.name) is None:
            findings += pattern_findings(workflow, path.name, tracked)
            continue
        seen_traced.add(path.name)
        traced = Traced(root, path, tracked)
        findings += [f"WIRING      {e}" for e in traced.wiring_errors()]
        # Trace entries for files that are gone: `generate` prunes them.
        stale = {job_id: sorted(set(job["files"]) - set(tracked))
                 for job_id, job in traced.trace["jobs"].items()}
        stale = {j: s for j, s in stale.items() if s}
        expected = traced.expected_block()
        excluded = sorted(
            f for e in expected for f in matching(e, tracked)
            if not on_paths_covers(traced.full_patterns(expected), f))
        if excluded:
            findings += [
                f"EXCLUDED    {path.name}: {f} is in the generated block but a "
                "later `!` pattern excludes it again" for f in excluded]
        if args.check:
            if expected != traced.block:
                missing = sorted(set(expected) - set(traced.block))
                extra = sorted(set(traced.block) - set(expected))
                findings.append(
                    f"DRIFT       {path.name}: the generated path-trace block "
                    "is not what the committed trace, the Go sources and the "
                    "literal imports give"
                    + "".join(f"\n              missing: {m}" for m in missing)
                    + "".join(f"\n              not derived: {x}" for x in extra)
                    + ("" if missing or extra else "\n              (order differs)"))
            findings += [
                f"DRIFT       {TRACES}/{path.name}.json: job `{j}` names "
                f"{f}, which is no longer a tracked file"
                for j, files in stale.items() for f in files]
        else:
            if stale:
                for job_id, files in stale.items():
                    job = traced.trace["jobs"][job_id]
                    job["files"] = sorted(set(job["files"]) - set(files))
                trace_path(root, path.name).write_text(
                    dump_trace(traced.trace), encoding="utf-8", newline="\n")
                wrote.append(f"{TRACES}/{path.name}.json")
                expected = traced.expected_block()
            new_text = traced.rendered(expected)
            if new_text != traced.text:
                path.write_bytes(new_text.encode("utf-8"))
                wrote.append(f"{WORKFLOWS}/{path.name}")
        # After the write, so an entry `generate` just dropped is not reported
        # as dead by the same run.
        findings += pattern_findings(load_workflow(path), path.name, tracked)
    for name in sorted(gated - seen_traced - set(NOT_YET_TRACED)):
        findings.append(
            f"UNTRACED    {name} has a path gate but neither the generated "
            "path-trace block nor an entry in NOT_YET_TRACED "
            "(scripts/ops/workflow_path_filter.py). Trace it; add an entry "
            "only with what stands in the way")
    for name in sorted(set(NOT_YET_TRACED) & seen_traced
                       | set(NOT_YET_TRACED) - gated):
        findings.append(
            f"STALE       NOT_YET_TRACED names {name}, which "
            + ("carries the generated block now" if name in seen_traced
               else "has no path gate (or no longer exists)")
            + " — delete the entry")
    for name in wrote:
        print(f"wrote {name}")
    if findings:
        print(f"{len(findings)} finding(s):", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        if args.check and any(f.startswith("DRIFT") for f in findings):
            print("\nRegenerate with `make workflow-path-filters` "
                  "(python3 scripts/ops/workflow_path_filter.py generate).",
                  file=sys.stderr)
        return EXIT_VIOLATION
    if not wrote:
        print(f"workflow path filters: {len(seen_traced)} traced workflow(s) in "
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

    p = sub.add_parser("generate", help="write the generated block of every traced workflow")
    p.add_argument("--check", action="store_true",
                   help="write nothing; exit 1 when a block or a pattern needs fixing")
    p.set_defaults(func=cmd_generate)

    args = parser.parse_args(argv)
    args.root = Path(os.path.realpath(args.root))
    return args.func(args)


if __name__ == "__main__":
    _lib_exitcodes.run(main)
