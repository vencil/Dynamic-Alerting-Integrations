#!/usr/bin/env python3
"""plan_race_shards.py — shard the nightly race run by last night's per-package timings.

Why this exists (#3541)
=======================
`.github/workflows/nightly-race.yaml` used to run ONE `go test -race -count=10`
per module on one 4-core runner. Every package of the module shared those cores,
so a package's wall-clock time was inflated by its neighbours: on the 11-02
nightly tenant-api's `internal/handler` reached 1718 s of the 1800 s per-binary
`-timeout`, threshold-exporter's `cmd/da-guard` 1331 s. Splitting each module
over several runners gives every slow package fewer neighbours, and assigning
packages by measured cost keeps the slow ones apart.

This script is the single place that knows:
  * which modules the nightly runs, where, and with which package patterns
    (MODULES) — the workflow's race matrix AND the report job's
    `--expected-modules` are both derived from it, so they cannot drift;
  * how many shards each module gets (SHARDS_PER_MODULE).

Subcommands
-----------
  modules         print the module names, comma-separated (report job)
  fetch-timings   download the newest unexpired `race-timings-<module>` artifact
                  of an earlier run of this workflow on main (fail-open: warn)
  plan            `go list` each module, assign packages to shards, emit the
                  race matrix and the per-module merge matrix
  merge           concatenate one module's shard streams; rc = first non-zero
  timings         per-package elapsed seconds from a merged `go test -json` stream

Fail-open vs fail-closed
------------------------
Only SCHEDULING is fail-open: no timings, unreadable timings or a failed
`go list` degrade to a worse split (round-robin, or one shard running the
module's own patterns, i.e. the pre-#3541 behaviour), logged as `::warning::`.
RESULTS are fail-closed: `merge` reports a module whose shard evidence is
incomplete as incomplete, and the workflow then withholds that module's
fragment, which `file_race_report.py` already treats as `no-result` — the same
verdict a module whose single leg never uploaded used to get. A partial stream
must never be parsed into a fragment, because a fragment built from the shards
that DID arrive can read green while the missing shard held the failure.

Stdlib only, like parse_go_test_json.py: the plan job has no install step.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import statistics
import subprocess
import sys
import zipfile
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

# Explicit, so `python3 -I` (which drops the script's own directory from
# sys.path) still finds the sibling parser whose timeout regex, -timeout value
# and package-elapsed reader are reused.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse_go_test_json import (  # noqa: E402
    GO_TEST_TIMEOUT_S,
    TIMEOUT_PANIC_RE,
    package_verdict,
)

# ── the SSOT ─────────────────────────────────────────────────────────────────

# Each nightly module: where `go test` runs and which package patterns it covers.
# Adding a module here adds its race legs, its merge leg and its report entry.
MODULES: tuple[dict, ...] = (
    {"module": "tenant-api", "workdir": "components/tenant-api",
     "packages": "./cmd/... ./internal/..."},
    {"module": "threshold-exporter", "workdir": "components/threshold-exporter/app",
     "packages": "./..."},
)

# Shards per module. A module with fewer packages gets fewer (never an empty one).
SHARDS_PER_MODULE = 4

# GO_TEST_TIMEOUT_S (imported above, defined in parse_go_test_json.py) must
# equal the workflow's `go test -timeout`. A package that timed out is recorded
# at least this long, so the planner keeps treating it as the heaviest.

TIMINGS_ARTIFACT_TMPL = "race-timings-{module}"
TIMINGS_FILE_TMPL = "race-timings-{module}.json"
RAW_FILE_TMPL = "race-raw-{module}-{shard}.jsonl"
RC_FILE_TMPL = "race-rc-{module}-{shard}"

# A timings artifact is our own output, but its numbers come from a stream that
# test code wrote. Bound it before trusting it.
MAX_TIMINGS_BYTES = 1 << 20
# How many earlier completed runs to look through for an unexpired artifact.
LOOKBACK_RUNS = 10

# Go's time.Duration.String() shape, e.g. `30m0s`, `1h0m0s`, `500ms`.
_GO_DURATION_PART_RE = re.compile(r"(\d+(?:\.\d+)?)(h|ms|us|µs|ns|m|s)")
_GO_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3,
                      "us": 1e-6, "µs": 1e-6, "ns": 1e-9}


def warn(msg: str) -> None:
    print(f"::warning title=Nightly race plan::{msg}", file=sys.stderr)


def module_names() -> list[str]:
    return [m["module"] for m in MODULES]


# ── assignment ───────────────────────────────────────────────────────────────


def assign(packages: list[str], timings: dict[str, float] | None,
           shards: int = SHARDS_PER_MODULE,
           zero_weight: frozenset[str] = frozenset()) -> tuple[list[list[str]], str]:
    """Split `packages` (in `go list` order) into at most `shards` non-empty groups.

    Returns (groups, method). Groups keep `go list` order inside; empty groups
    are never returned, so a leg never runs `go test` with no packages.

      * no timing for ANY package with tests -> round-robin in `go list` order
      * otherwise greedy longest-processing-time: heaviest first onto the
        lightest shard. A package without a timing weighs the median of the
        known ones; a package in `zero_weight` (no test files: `go test` only
        builds it) weighs 0.
    Ties break on (load, package count, shard index) so zero-weight packages
    still spread out instead of piling onto shard 1.
    """
    if shards < 1:
        raise ValueError("shards must be >= 1")
    if not packages:
        return [], "empty"
    k = min(shards, len(packages))
    known = {p: float(timings[p]) for p in packages
             if timings and p in timings and p not in zero_weight}
    if not known:
        groups = [packages[i::k] for i in range(k)]
        return [g for g in groups if g], "round-robin"

    default = statistics.median(known.values())
    order = {p: i for i, p in enumerate(packages)}

    def weight(p: str) -> float:
        if p in zero_weight:
            return 0.0
        return known.get(p, default)

    loads = [0.0] * k
    groups: list[list[str]] = [[] for _ in range(k)]
    for p in sorted(packages, key=lambda p: (-weight(p), order[p])):
        i = min(range(k), key=lambda j: (loads[j], len(groups[j]), j))
        groups[i].append(p)
        loads[i] += weight(p)
    return [sorted(g, key=order.__getitem__) for g in groups if g], "lpt"


# ── go list ──────────────────────────────────────────────────────────────────

GO_LIST_FORMAT = "{{.ImportPath}}\t{{len .TestGoFiles}}\t{{len .XTestGoFiles}}"


def _run(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, timeout=600)


def go_list(workdir: str, patterns: str, runner=_run) -> tuple[list[str], frozenset[str]] | None:
    """(packages in `go list` order, the ones without test files) or None on failure.

    Every package the patterns match is kept, test files or not: `go test`
    still BUILDS a package without test files and fails the run when it does
    not compile, so dropping them would silently narrow what the nightly checks.
    """
    try:
        cp = runner(["go", "list", "-e", "-f", GO_LIST_FORMAT, *patterns.split()], cwd=workdir)
    except (OSError, subprocess.SubprocessError) as exc:
        warn(f"go list in {workdir} could not run ({exc})")
        return None
    out = cp.stdout.decode("utf-8", "replace") if isinstance(cp.stdout, bytes) else str(cp.stdout)
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace") if isinstance(cp.stderr, bytes) else str(cp.stderr)
        warn(f"go list in {workdir} exited {cp.returncode}: {err.strip()[:500]}")
        return None
    pkgs: list[str] = []
    no_tests: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].strip():
            warn(f"go list in {workdir}: unexpected line {line!r}")
            return None
        name = parts[0].strip()
        if name in pkgs:
            continue
        pkgs.append(name)
        try:
            if int(parts[1]) + int(parts[2]) == 0:
                no_tests.add(name)
        except ValueError:
            warn(f"go list in {workdir}: unexpected line {line!r}")
            return None
    if not pkgs:
        warn(f"go list in {workdir} matched no package for {patterns!r}")
        return None
    return pkgs, frozenset(no_tests)


# ── timings ──────────────────────────────────────────────────────────────────


def go_duration_seconds(text: str) -> float | None:
    """`30m0s` -> 1800.0. None when `text` is not a Go duration."""
    pos, total = 0, 0.0
    for m in _GO_DURATION_PART_RE.finditer(text):
        if m.start() != pos:
            return None
        total += float(m.group(1)) * _GO_DURATION_UNITS[m.group(2)]
        pos = m.end()
    return total if pos == len(text) and pos else None


def validate_timings(doc) -> dict[str, float] | None:
    if not isinstance(doc, dict):
        return None
    out: dict[str, float] = {}
    for k, v in doc.items():
        if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
            return None
        if not math.isfinite(v) or v < 0:
            return None
        out[k] = float(v)
    return out


def load_timings(path: Path) -> dict[str, float] | None:
    try:
        if path.stat().st_size > MAX_TIMINGS_BYTES:
            warn(f"{path.name} exceeds {MAX_TIMINGS_BYTES} bytes; ignored")
            return None
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        warn(f"{path.name} is unreadable ({exc}); ignored")
        return None
    timings = validate_timings(doc)
    if timings is None:
        warn(f"{path.name} is not a {{package: seconds}} object; ignored")
    return timings


def stream_timings(lines) -> dict[str, float]:
    """Per package: `Elapsed` of its package-level pass/fail event (seconds).

    A package that hit the `-timeout` is recorded at least at that duration (the
    panic line names it), so it is never mistaken for a light one. Packages that
    only `skip` (no test files) are left out; `go list` already says what they are.
    """
    elapsed: dict[str, float] = {}
    timeout: dict[str, float] = {}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict) or not isinstance(ev.get("Package"), str):
            continue
        pkg = ev["Package"]
        action = ev.get("Action")
        if action == "output" and isinstance(ev.get("Output"), str):
            m = TIMEOUT_PANIC_RE.match(ev["Output"].rstrip("\n"))
            if m:
                d = go_duration_seconds(m.group(1))
                timeout[pkg] = d if d is not None else float(GO_TEST_TIMEOUT_S)
        elif (verdict := package_verdict(ev)) is not None:
            if verdict[1] is not None:
                elapsed[pkg] = verdict[1]
            elif verdict[0] == "fail":
                elapsed.setdefault(pkg, 0.0)
    for pkg, d in timeout.items():
        if pkg in elapsed:
            elapsed[pkg] = max(elapsed[pkg], d)
    return {p: round(elapsed[p], 3) for p in sorted(elapsed)}


# ── fetch (GitHub REST) ──────────────────────────────────────────────────────


def _gh_json(runner, path: str):
    cp = runner(["gh", "api", path])
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace") if isinstance(cp.stderr, bytes) else str(cp.stderr)
        raise RuntimeError(f"gh api {path} exited {cp.returncode}: {err.strip()[:300]}")
    return json.loads(cp.stdout)


def fetch_timings(repo: str, workflow: str, branch: str, out_dir: Path,
                  modules: list[str], runner=_run) -> dict[str, str]:
    """Write `race-timings-<module>.json` for each module that has one. Never raises.

    Returns {module: source run id} for what was fetched. A module without a
    usable artifact is simply absent; `plan` then round-robins it.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, str] = {}
    try:
        runs = _gh_json(runner, f"repos/{repo}/actions/workflows/{workflow}/runs"
                                f"?branch={branch}&status=completed&per_page={LOOKBACK_RUNS}")
        run_list = [r for r in (runs.get("workflow_runs") or []) if isinstance(r, dict)]
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, AttributeError) as exc:
        warn(f"could not list earlier runs ({exc}); planning without timings")
        return found
    wanted = {TIMINGS_ARTIFACT_TMPL.format(module=m): m for m in modules}
    for run in run_list:  # the API lists newest first
        if len(found) == len(modules):
            break
        run_id = run.get("id")
        try:
            arts = _gh_json(runner, f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100")
            art_list = [a for a in (arts.get("artifacts") or []) if isinstance(a, dict)]
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError, AttributeError) as exc:
            warn(f"could not list artifacts of run {run_id} ({exc})")
            continue
        for art in art_list:
            module = wanted.get(str(art.get("name")))
            if module is None or module in found or art.get("expired"):
                continue
            try:
                cp = runner(["gh", "api", f"repos/{repo}/actions/artifacts/{art.get('id')}/zip"])
                if cp.returncode != 0:
                    raise RuntimeError(f"download exited {cp.returncode}")
                with zipfile.ZipFile(io.BytesIO(cp.stdout)) as zf:
                    member = TIMINGS_FILE_TMPL.format(module=module)
                    info = zf.getinfo(member)
                    if info.file_size > MAX_TIMINGS_BYTES:
                        raise RuntimeError(f"{member} is {info.file_size} bytes")
                    timings = validate_timings(json.loads(zf.read(member)))
                if timings is None:
                    raise RuntimeError("not a {package: seconds} object")
            except (RuntimeError, ValueError, KeyError, OSError, TypeError,
                    zipfile.BadZipFile, subprocess.SubprocessError) as exc:
                warn(f"timings artifact of run {run_id} for {module} unusable ({exc})")
                continue
            (out_dir / TIMINGS_FILE_TMPL.format(module=module)).write_text(
                json.dumps(timings, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                newline="\n")
            found[module] = str(run_id)
    for m in modules:
        if m not in found:
            warn(f"no unexpired {TIMINGS_ARTIFACT_TMPL.format(module=m)} artifact in the last "
                 f"{len(run_list)} completed run(s) on {branch}; {m} is split round-robin")
    return found


# ── plan ─────────────────────────────────────────────────────────────────────


def plan(timings_dir: Path | None, shards: int = SHARDS_PER_MODULE,
         runner=_run, root: Path = Path(".")) -> dict:
    """{"race": [...legs], "merge": [...modules]} for the workflow matrices."""
    race: list[dict] = []
    merge: list[dict] = []
    for spec in MODULES:
        module, workdir, patterns = spec["module"], spec["workdir"], spec["packages"]
        listed = go_list(str(root / workdir), patterns, runner=runner)
        if listed is None:
            # Scheduling only: fall back to one leg running the module's own
            # patterns — exactly the pre-#3541 run, never a narrower one.
            warn(f"{module}: package list unavailable; running it as ONE leg")
            groups, method = [patterns.split()], "single-leg-fallback"
        else:
            pkgs, no_tests = listed
            timings = (load_timings(timings_dir / TIMINGS_FILE_TMPL.format(module=module))
                       if timings_dir else None)
            groups, method = assign(pkgs, timings, shards, zero_weight=no_tests)
        ids = [str(i + 1) for i in range(len(groups))]
        for sid, group in zip(ids, groups):
            race.append({"module": module, "workdir": workdir, "shard": sid,
                         "shards": len(groups), "packages": " ".join(group)})
        merge.append({"module": module, "workdir": workdir, "packages": patterns,
                      "shards": ",".join(ids), "method": method})
        print(f"[plan_race_shards] {module}: {len(groups)} shard(s) by {method}", file=sys.stderr)
    return {"race": race, "merge": merge}


# ── merge ────────────────────────────────────────────────────────────────────


def merge(module: str, shard_ids: list[str], raw_dir: Path, output: Path) -> dict:
    """Concatenate shard streams in shard order; report what is missing.

    A shard counts as present only with BOTH its stream and its rc file: the rc
    file is written after `go test` returns, so a stream without one is a leg
    that died mid-run, and its stream is partial.
    """
    missing: list[str] = []
    rcs: list[int] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as out:
        for sid in shard_ids:
            raw = raw_dir / RAW_FILE_TMPL.format(module=module, shard=sid)
            rc_path = raw_dir / RC_FILE_TMPL.format(module=module, shard=sid)
            try:
                rc = int(rc_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                missing.append(sid)
                continue
            if not raw.is_file():
                missing.append(sid)
                continue
            rcs.append(rc)
            data = raw.read_bytes()
            out.write(data)
            if data and not data.endswith(b"\n"):
                out.write(b"\n")
    rc = next((r for r in rcs if r != 0), 0)
    complete = bool(shard_ids) and not missing
    if missing:
        # Results are missing: never hand the parser a partial stream as if it
        # were the module's (see the module docstring).
        rc = rc or 1
    return {"module": module, "go_exit_code": rc, "complete": complete,
            "missing_shards": missing}


# ── CLI ──────────────────────────────────────────────────────────────────────


def _write_github_output(pairs: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for k, v in pairs.items():
            fh.write(f"{k}={v}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("modules", help="print module names, comma-separated")

    f = sub.add_parser("fetch-timings")
    f.add_argument("--repo", required=True)
    f.add_argument("--workflow", required=True)
    f.add_argument("--branch", default="main")
    f.add_argument("--out-dir", required=True, type=Path)

    p = sub.add_parser("plan")
    p.add_argument("--timings-dir", type=Path)
    p.add_argument("--shards", type=int, default=SHARDS_PER_MODULE)

    m = sub.add_parser("merge")
    m.add_argument("--module", required=True)
    m.add_argument("--shards", required=True, help="comma-separated shard ids")
    m.add_argument("--raw-dir", required=True, type=Path)
    m.add_argument("--output", required=True, type=Path)

    t = sub.add_parser("timings")
    t.add_argument("--input", required=True, type=Path)
    t.add_argument("--output", required=True, type=Path)

    args = ap.parse_args(argv)

    if args.cmd == "modules":
        print(",".join(module_names()))
        return 0
    if args.cmd == "fetch-timings":
        found = fetch_timings(args.repo, args.workflow, args.branch, args.out_dir, module_names())
        print(f"[plan_race_shards] timings fetched: {json.dumps(found, sort_keys=True)}")
        return 0  # fail-open: scheduling only
    if args.cmd == "plan":
        result = plan(args.timings_dir, args.shards)
        print(json.dumps(result, indent=2))
        _write_github_output({"race_matrix": json.dumps({"include": result["race"]}),
                              "merge_matrix": json.dumps({"include": result["merge"]})})
        return 0
    if args.cmd == "merge":
        ids = [s.strip() for s in args.shards.split(",") if s.strip()]
        res = merge(args.module, ids, args.raw_dir, args.output)
        print(f"[plan_race_shards] merge: {json.dumps(res, sort_keys=True)}")
        if res["missing_shards"]:
            print(f"::error title=Nightly race::{args.module}: shard(s) "
                  f"{', '.join(res['missing_shards'])} left no complete evidence; the module "
                  "is reported as no-result, not parsed from the shards that arrived")
        _write_github_output({"go_exit_code": str(res["go_exit_code"]),
                              "complete": "true" if res["complete"] else "false"})
        return 0
    if args.cmd == "timings":
        with args.input.open("r", encoding="utf-8", errors="replace") as fh:
            timings = stream_timings(fh)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(timings, indent=2, sort_keys=True) + "\n",
                               encoding="utf-8", newline="\n")
        print(f"[plan_race_shards] timings: {len(timings)} package(s)")
        return 0
    return 2


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
