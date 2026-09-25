#!/usr/bin/env python3
"""go_test_reads.py — every repo file a Go test leg opened must be able to wake that leg.

#1399. A Go leg in ci.yml runs only when its `if:` says one of its path filters
matched. If a test reads a repo file that no such filter covers, a PR that
changes only that file skips the leg and the required `Go Tests (1.26)` still
reports green — the test that would have caught the drift never ran. Python
and portal legs have static scanners for this
(tests/ops/test_ci_path_filter_coverage.py); Go had none, and a live instance existed
when this was written (batch_body_size_test.go reading
helm/tenant-api/values.yaml).

This checks what the leg ACTUALLY opened, in CI, on the run that just
finished. The leg runs `go test -exec "sh scripts/ops/go_testlog_exec.sh"`,
which makes each test binary write the `testing` package's own action log.
Then this script, run from the repo root:

  1. reads the leg's gate from its job `if:` in the workflow, and the patterns
     of the filters it names from the detect job's dorny step;
  2. resolves every `open` in every log to a repo-relative path;
  3. fails if a tracked file was opened DIRECTLY and no gate pattern covers it.

"Directly" excludes a file opened after one of its ancestor directories was
opened in the same log — i.e. reached by listing a directory (WalkDir /
ReadDir / Glob). A walker opens everything it passes and keeps what it wants,
so its opens say nothing about what it depends on; requiring coverage for them
would demand the whole repo for the gitops tenant-config walker. That walker's
residual is already disclosed beside `**/conf.d/**` in ci.yml (#1722).
`stat` lines never count: probing for `Makefile` to find the repo root is not
depending on it.

⚠️ Blind spots, stated because a green run is read as a guarantee:
  * reads before `m.Run` (package init, the start of a TestMain) and reads by
    child processes are not in the log;
  * a test skipped on the runner opens nothing, so its reads are not checked
    (and do not need to be: it did not run);
  * a file opened directly AND also inside a directory the same binary had
    listed earlier is classified as walked, i.e. not checked;
  * compile inputs (`*.go`, go.mod, go.sum) are not opened by the test binary
    at all — tests/ops/test_go_filter_compile_inputs.py covers those.

Exit codes: 0 every direct read is covered; 1 some are not; 2 the check could
not measure (no logs, a log without the header, an unmodelled gate, a `!`
pattern) — never read as a pass.

Usage:
    python3 scripts/ops/go_test_reads.py --job go-tests-tenant-api \\
        --logs "$RUNNER_TEMP/go-testlog"
"""
from __future__ import annotations

import argparse
import posixpath
import re
import subprocess
import sys
from pathlib import Path

import yaml

from paths_filter_glob import covers

ROOT = Path(__file__).resolve().parents[2]
HEADER = "# test log"  # written by testing/internal/testdeps; cmd/go keys on it
GATE_ATOM = re.compile(r"needs\.([\w-]+)\.outputs\.(\w+)_changed\s*==\s*'true'")


class Unmeasurable(Exception):
    """The check cannot say anything; exit 2."""


def leg_gate_patterns(workflow: Path, job_id: str) -> list[str]:
    doc = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    jobs = doc.get("jobs") or {}
    if job_id not in jobs:
        raise Unmeasurable(f"{workflow.name} has no job {job_id!r}")
    condition = str(jobs[job_id].get("if", "")).strip()
    atoms = GATE_ATOM.findall(condition)
    # Only a plain OR of gate atoms is modelled: then the leg runs iff ANY of
    # the named filters matched, so the union of their patterns is its gate.
    rest = GATE_ATOM.sub("", condition).replace("||", "").strip()
    if not atoms or rest:
        raise Unmeasurable(
            f"{workflow.name}::{job_id} `if: {condition}` is not a plain OR of "
            "`needs.<detect>.outputs.<name>_changed == 'true'` atoms; model it "
            "before relying on this check")
    detects = {d for d, _ in atoms}
    if len(detects) != 1:
        raise Unmeasurable(f"{job_id} gates on more than one detect job: {sorted(detects)}")
    filters = None
    for step in (jobs.get(detects.pop()) or {}).get("steps") or []:
        raw = (step.get("with") or {}).get("filters")
        if raw is not None:
            if filters is not None:
                raise Unmeasurable("the detect job has two `filters:` steps")
            filters = yaml.safe_load(raw)
    if not isinstance(filters, dict):
        raise Unmeasurable("could not find the detect job's dorny `filters:`")
    patterns: list[str] = []
    for _, name in atoms:
        if name not in filters:
            raise Unmeasurable(f"{job_id} gates on `{name}`, which is not a filter")
        for p in filters[name]:
            if str(p).startswith("!"):
                raise Unmeasurable(f"filter `{name}` has an exclusion {p!r}; "
                                   "`covers` cannot express an override")
            patterns.append(str(p))
    return patterns


def tracked_files(root: Path) -> frozenset[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True,
                         check=True, timeout=120).stdout.decode("utf-8")
    files = frozenset(p for p in out.split("\0") if p)
    if not files:
        raise Unmeasurable("`git ls-files` listed nothing")
    return files


def direct_reads(log: Path, root: str, tracked: frozenset[str]) -> tuple[set[str], int]:
    """Tracked files the binary opened directly, and how many it reached by walking."""
    text = log.read_text(encoding="utf-8", errors="replace").splitlines()
    if not text or text[0] != HEADER:
        raise Unmeasurable(f"{log.name} does not start with {HEADER!r}")
    cwd_file = Path(f"{log}.cwd")
    if not cwd_file.is_file():
        raise Unmeasurable(f"{log.name} has no .cwd beside it")
    cwd = cwd_file.read_text(encoding="utf-8").strip()
    opened_dirs: set[str] = set()
    direct: set[str] = set()
    walked = 0
    for line in text[1:]:
        op, _, arg = line.partition(" ")
        if op == "chdir":
            cwd = posixpath.normpath(posixpath.join(cwd, arg))
            continue
        if op != "open" or not arg:
            continue
        full = posixpath.normpath(posixpath.join(cwd, arg))
        if full != root and not full.startswith(root + "/"):
            continue
        rel = full[len(root) + 1:]
        if rel not in tracked:
            opened_dirs.add(rel)  # a directory (ReadDir/WalkDir) or an untracked file
            continue
        parent = posixpath.dirname(rel)
        while True:
            if parent in opened_dirs:
                walked += 1
                break
            if not parent:
                direct.add(rel)
                break
            parent = posixpath.dirname(parent)
    return direct, walked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--job", required=True, help="ci.yml job id of the Go leg")
    ap.add_argument("--logs", required=True, type=Path, help="GO_TESTLOG_DIR of that leg")
    ap.add_argument("--workflow", type=Path, default=ROOT / ".github" / "workflows" / "ci.yml")
    ap.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    try:
        patterns = leg_gate_patterns(args.workflow, args.job)
        root = args.root.resolve().as_posix()
        tracked = tracked_files(args.root)
        logs = sorted(args.logs.glob("*.log")) if args.logs.is_dir() else []
        if not logs:
            raise Unmeasurable(
                f"no test log in {args.logs} — the leg's `go test` did not run "
                "through go_testlog_exec.sh, so nothing was measured")
        direct: set[str] = set()
        walked = 0
        for log in logs:
            d, w = direct_reads(log, root, tracked)
            direct |= d
            walked += w
    except (Unmeasurable, OSError, subprocess.SubprocessError, yaml.YAMLError) as exc:
        print(f"::error::go_test_reads: cannot measure — {exc}", file=sys.stderr)
        return 2
    uncovered = sorted(p for p in direct if not any(covers(g, p) for g in patterns))
    print(f"{args.job}: {len(logs)} test log(s), {len(direct)} tracked file(s) "
          f"opened directly, {walked} reached by listing a directory (not checked)")
    if uncovered:
        print(f"::error::{args.job} opened {len(uncovered)} repo file(s) that no "
              "filter in its gate covers. A PR changing only one of them skips "
              "this leg and the required check stays green. Add each to the "
              "gate's filter in ci.yml's detect-changes step:", file=sys.stderr)
        for p in uncovered:
            print(f"  {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
