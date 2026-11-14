#!/usr/bin/env python3
"""atime_trace.py — which tracked files did a stretch of a job read, by access time.

#3756. A per-process tracer sees one kind of process: the Python audit hook
does not see what `make`, a Go binary, node or an action reads. The kernel sees
all of them. This tool sets the access time of every tracked file and directory
to a fixed moment in the past, lets the job run, and names the ones whose
access time moved.

    reset      `git update-index -q --refresh`, then set the access time of
               every tracked file and of every directory holding one to PAST,
               create three canaries, and write the state file.
    collect    name what was read (files) and listed (directories) since the
               last `reset` / `collect`, store it in the state file as one
               labelled segment, and re-arm exactly those paths.
    canary     the self-check of the measurement: the canary that was read
               moved, the one nobody touched did not, the directory that was
               listed moved. Exit 2 when any of the three does not hold.
    compare    the union of the collected segments against a trace written by
               `workflow_path_filter.py assert --out` (transitional: it goes
               when the Python tracer goes).
    semantics  what an access time means on the filesystem holding a directory:
               one cell per way of touching a file, each printed next to the
               reference (ext4 `relatime`, where the method was validated).
    cost       what the measurement itself took, and the size of each segment.

Why the order inside `reset` is fixed: on a fresh checkout the first `git
status` was measured to read most of the tree (#3756, the 2026-11-14 survey),
so the index is refreshed before the access times are set, not after.

Why resetting is not optional: under `relatime` a read updates the access time
only when it is not newer than the modification / change time (or is a day
old). Setting it changes the change time, so the next read is always recorded;
a tool that only SNAPSHOTS access times misses every second read.

⚠️ What an unchanged access time does NOT prove (measured, ext4 relatime):
  * a file opened with `O_NOATIME` is read without a trace;
  * a file opened and closed without a read is unchanged (it was not read);
  * a module whose `.pyc` is current is loaded from the cache — the `.py` is
    not opened;
  * content taken from a git object (`git show`, `git archive`) never touches
    the working tree;
  * this tool's own source is read by every run of it, so it is left out of
    every segment and named as such, never reported as "not read". (What it
    imports from scripts/tools/ is NOT left out: those access times are put
    back after the imports.) Inside a job that also runs the Python tracer,
    start it as `python3 -I`, or the interpreter reads the tracer's
    sitecustomize.py on its behalf;
  * whatever runs after the job's last step (an action's post step) is after
    the last `collect`.
And what a changed one does not prove: `mmap` without `read`, `execve` and a
`cp` all count as reads, and they are — but so does anything else that ran in
the same stretch, on a shared machine included. A path whose inode changed is
reported as REPLACED: whether the old content was read first is unknown.

Exit codes (scripts/tools/_lib_exitcodes.py): 0 measured, nothing to report;
1 a finding (`compare`: the access times missed a file the tracer recorded);
2 no answer — the canaries do not hold, an access time could not be set, the
state file or `git` is unusable, or (`semantics`) a cell the method rests on
differs from the reference. Never read as a pass.

Usage:
    python3 scripts/ops/atime_trace.py reset --state "$RUNNER_TEMP/atime.json" \\
        --canary-dir "$GITHUB_WORKSPACE/.git/atime-canary"
    cat "$GITHUB_WORKSPACE/.git/atime-canary/must-read" > /dev/null
    ls "$GITHUB_WORKSPACE/.git/atime-canary/listed-dir" > /dev/null
    python3 scripts/ops/atime_trace.py collect --state "$RUNNER_TEMP/atime.json" --label build
    python3 scripts/ops/atime_trace.py canary --state "$RUNNER_TEMP/atime.json"
    python3 scripts/ops/atime_trace.py semantics --dir "$GITHUB_WORKSPACE/.git"
"""
from __future__ import annotations

import os
import sys

# ⛔ Before any other import. Importing reads files and lists directories of
# the tree being measured: the interpreter lists this directory (sys.path[0])
# for every module it looks up, and _lib_exitcodes with what it imports lives
# in scripts/tools/. Left alone, each run of this tool would mark those as
# read — and a job that really loads `_lib_exitcodes.py` could not be told
# from one that does not. So their access times are noted here, before the
# imports, and put back after them (`_restore_own_reads`). `os` and `sys` are
# already loaded when a script starts, so these two lines touch nothing.
_HERE = os.path.dirname(os.path.realpath(__file__))
_TOOLS = os.path.normpath(os.path.join(_HERE, os.pardir, "tools"))


def _own_stamps() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for directory in (_HERE, _TOOLS):  # the directories first: listing one moves it
        try:
            st = os.lstat(directory)
        except OSError:
            continue
        out[directory] = (st.st_atime_ns, st.st_mtime_ns)
    try:
        names = os.listdir(_TOOLS)
    except OSError:
        names = []
    for name in names:
        if name.endswith(".py"):
            full = os.path.join(_TOOLS, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            out[full] = (st.st_atime_ns, st.st_mtime_ns)
    return out


_OWN_BEFORE = _own_stamps()
# A `.pyc` this tool left behind would make the JOB's import of the same module
# load the cache and never open the `.py` — a read this tool would have hidden.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import glob  # noqa: E402
import json  # noqa: E402
import mmap  # noqa: E402
import platform  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Callable  # noqa: E402

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

import _lib_exitcodes  # noqa: E402


def _restore_own_reads() -> list[str]:
    """Put back the access times this process moved while importing; the
    paths it could not put back (a read of those can no longer be told from
    this tool's own)."""
    failed = []
    for path, (atime_ns, mtime_ns) in _OWN_BEFORE.items():
        try:
            if os.lstat(path).st_atime_ns != atime_ns:
                os.utime(path, ns=(atime_ns, mtime_ns), follow_symlinks=False)
        except OSError as exc:
            failed.append(f"{path}: {exc.strerror}")
    return failed


_OWN_NOT_RESTORED = _restore_own_reads()

EXIT_OK, EXIT_VIOLATION = 0, 1

REPO_ROOT = Path(os.path.realpath(__file__)).parents[2]
STATE_VERSION = 1
# 2001-01-01T00:00:00Z. Older than any checkout, newer than the epoch (a zero
# timestamp reads as "unset" to some tools).
PAST_NS = 978307200 * 10**9
OLD_MTIME_NS = 788918400 * 10**9  # 1995-01-01: a modification time older than PAST
GIT_TIMEOUT = 600

CANARY_READ = "must-read"
CANARY_UNTOUCHED = "must-not-read"
CANARY_DIR = "listed-dir"
CANARY_INNER = "listed-dir/inner"


def _caller_error(message: str) -> _lib_exitcodes.CallerError:
    return _lib_exitcodes.CallerError(message)


# ── the filesystem ──────────────────────────────────────────────────────────

def _stamp(path: str) -> list[int] | None:
    """[access time in ns, inode] of the path itself (a symlink is not followed)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return [st.st_atime_ns, st.st_ino]


def _set_past(path: str) -> None:
    """Access time → PAST; the modification time is kept, so nothing that
    compares mtimes (make, the git index) sees a change."""
    os.utime(path, ns=(PAST_NS, os.lstat(path).st_mtime_ns), follow_symlinks=False)


def mount_of(path: str) -> str:
    """`fstype options` of the mount holding the path, from /proc/self/mountinfo;
    a sentence saying why not when it cannot be read."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return f"unknown (/proc/self/mountinfo: {exc.strerror})"
    real = os.path.realpath(path)
    best: tuple[int, str] | None = None
    for line in lines:
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 6 or len(tail) < 3:
            continue
        point = fields[4].replace("\\040", " ")
        if real == point or real.startswith(point.rstrip("/") + "/"):
            if best is None or len(point) >= best[0]:
                best = (len(point), f"{tail[0]} {fields[5]},{tail[2]} (mounted at {point})")
    return best[1] if best else "unknown (no mount covers it)"


# ── the repository ──────────────────────────────────────────────────────────

def _git(root: Path, *argv: str, accept: tuple[int, ...] = (0,)) -> str:
    try:
        proc = subprocess.run(
            ["git", *argv], cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="surrogateescape", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise _caller_error(f"`git {' '.join(argv)}` could not run in {root}: {exc!r}") from exc
    if proc.returncode not in accept:
        raise _caller_error(
            f"`git {' '.join(argv)}` exited {proc.returncode} in {root}: "
            f"{proc.stderr.strip()[:300]}")
    return proc.stdout


def tracked_paths(root: Path) -> tuple[list[str], list[str]]:
    """(tracked files, every directory holding one — the root is `.`)."""
    files = sorted(p for p in _git(root, "ls-files", "-z").split("\0") if p)
    if not files:
        raise _caller_error(
            f"`git ls-files` listed no file in {root}: there is nothing to measure.")
    dirs = {"."}
    for name in files:
        while "/" in name:
            name = name.rsplit("/", 1)[0]
            dirs.add(name)
    return files, sorted(dirs)


def _self_rel(root: Path) -> str | None:
    """This file, relative to the root, when it is inside it."""
    rel = os.path.relpath(os.path.realpath(__file__), root)
    return None if rel.startswith(os.pardir) else rel.replace(os.sep, "/")


# ── the state file ──────────────────────────────────────────────────────────

def load_state(path: Path) -> dict:
    if _OWN_NOT_RESTORED:
        raise _caller_error(
            "this tool moved the access time of files it imports and could not "
            "put them back, so its own reads would be reported as the job's: "
            + "; ".join(_OWN_NOT_RESTORED[:5]))
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        raise _caller_error(
            f"the state file {path} cannot be read ({exc}): run `reset` first. "
            "Nothing was measured.") from exc
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise _caller_error(
            f"{path} is not a version-{STATE_VERSION} state file of this tool.")
    return state


def save_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        raise _caller_error(f"the state file {path} cannot be written: {exc}") from exc


# ── reset ───────────────────────────────────────────────────────────────────

def make_canaries(directory: Path) -> dict:
    """Three paths nobody but the caller touches, with their stamps."""
    try:
        shutil.rmtree(directory, ignore_errors=True)
        (directory / CANARY_DIR).mkdir(parents=True)
        for name in (CANARY_READ, CANARY_UNTOUCHED, CANARY_INNER):
            with open(directory / name, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("atime canary (#3756)\n")
        for name in (CANARY_READ, CANARY_UNTOUCHED, CANARY_INNER, CANARY_DIR):
            _set_past(str(directory / name))
    except OSError as exc:
        raise _caller_error(f"the canaries cannot be created in {directory}: {exc}") from exc
    return {"dir": str(directory),
            "stamps": {name: _stamp(str(directory / name))
                       for name in (CANARY_READ, CANARY_UNTOUCHED, CANARY_DIR, CANARY_INNER)}}


def cmd_reset(args: argparse.Namespace) -> int:
    root: Path = args.root
    started = time.monotonic()
    # 0: clean; 1: some entry still differs from the index (a modified tree).
    _git(root, "update-index", "-q", "--refresh", accept=(0, 1))
    refreshed = time.monotonic()
    files, dirs = tracked_paths(root)
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").strip())
    canary_dir = Path(args.canary_dir).resolve() if args.canary_dir else git_dir / "atime-canary"

    absent: list[str] = []
    refused: list[str] = []
    stamps: dict[str, dict[str, list[int] | None]] = {"files": {}, "dirs": {}}
    listed = time.monotonic()
    for kind, names in (("files", files), ("dirs", dirs)):
        for name in names:
            full = os.path.join(root, name)
            try:
                _set_past(full)
            except FileNotFoundError:
                absent.append(name)
            except OSError as exc:
                refused.append(f"{name}: {exc.strerror}")
            stamps[kind][name] = _stamp(full)
    armed = time.monotonic()
    if refused:
        raise _caller_error(
            f"the access time of {len(refused)} tracked path(s) could not be set, "
            "so a read of them would go unseen — nothing is measured: "
            + "; ".join(refused[:5]) + (" …" if len(refused) > 5 else ""))

    canary = make_canaries(canary_dir)
    if os.lstat(canary_dir).st_dev != os.lstat(root).st_dev:
        raise _caller_error(
            f"the canary directory {canary_dir} is on another filesystem than "
            f"{root}: its canaries would say nothing about the tracked files.")
    state = {
        "version": STATE_VERSION, "root": str(root), "past_ns": PAST_NS,
        "self": _self_rel(root), "files": stamps["files"], "dirs": stamps["dirs"],
        "canary": canary, "segments": [],
        "cost": {"refresh_s": refreshed - started, "list_s": listed - refreshed,
                 "reset_s": armed - listed, "collect_s": []},
    }
    save_state(Path(args.state), state)
    print(f"atime-trace reset: {len(files)} tracked file(s), {len(dirs)} "
          f"directory(ies) in {root}; {len(absent)} not in the working tree.")
    print(f"atime-trace reset: mount: {mount_of(str(root))}")
    print(f"atime-trace reset: cost: git update-index --refresh {refreshed - started:.2f}s, "
          f"git ls-files {listed - refreshed:.2f}s, setting access times {armed - listed:.2f}s")
    print(f"atime-trace reset: canaries in {canary_dir} — read `{CANARY_READ}`, "
          f"list `{CANARY_DIR}`, leave `{CANARY_UNTOUCHED}` alone, then run `canary`.")
    return EXIT_OK


# ── collect ─────────────────────────────────────────────────────────────────

def _diff(root: str, before: dict[str, list[int] | None]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"read": [], "replaced": [], "gone": [], "appeared": []}
    for name, old in before.items():
        new = _stamp(os.path.join(root, name))
        if new is None:
            if old is not None:
                out["gone"].append(name)
        elif old is None:
            out["appeared"].append(name)
        elif new[1] != old[1]:
            out["replaced"].append(name)
        elif new[0] != old[0]:
            out["read"].append(name)
    return out


def _rearm(root: str, names: list[str], stamps: dict[str, list[int] | None]) -> list[str]:
    refused = []
    for name in names:
        full = os.path.join(root, name)
        try:
            _set_past(full)
        except FileNotFoundError:
            pass
        except OSError as exc:
            refused.append(f"{name}: {exc.strerror}")
        stamps[name] = _stamp(full)
    return refused


def _show(tag: str, names: list[str], limit: int) -> None:
    for name in names[:limit]:
        print(f"  {tag}  {name}")
    if len(names) > limit:
        print(f"  {tag}  … and {len(names) - limit} more")


def cmd_collect(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    root = state["root"]
    started = time.monotonic()
    files = _diff(root, state["files"])
    dirs = _diff(root, state["dirs"])
    own = state.get("self")
    own_read = own in files["read"]
    if own_read:
        files["read"].remove(own)
    # Without `-I` the interpreter looks its start-up modules up in the script's
    # directory before this file runs a line, so that listing is this tool's.
    own_dir = None
    if own and not sys.flags.isolated:
        own_dir = own.rsplit("/", 1)[0] if "/" in own else "."
    own_listed = own_dir in dirs["read"]
    if own_listed:
        dirs["read"].remove(own_dir)
    refused = _rearm(root, [n for k in ("read", "replaced", "appeared") for n in files[k]]
                     + ([own] if own_read else []), state["files"])
    refused += _rearm(root, [n for k in ("read", "replaced", "appeared") for n in dirs[k]]
                      + ([own_dir] if own_listed else []), state["dirs"])
    for name in files["gone"]:
        state["files"][name] = None
    for name in dirs["gone"]:
        state["dirs"][name] = None
    seconds = time.monotonic() - started
    state["segments"].append({
        "label": args.label, "files": files["read"], "dirs": dirs["read"],
        "replaced": files["replaced"] + files["appeared"], "gone": files["gone"]})
    state["cost"]["collect_s"].append(seconds)
    save_state(state_path, state)

    print(f"atime-trace collect [{args.label}]: {len(files['read'])} tracked file(s) read, "
          f"{len(dirs['read'])} directory(ies) listed, "
          f"{len(files['replaced']) + len(files['appeared'])} replaced, "
          f"{len(files['gone'])} gone ({seconds:.2f}s)")
    _show("READ    ", files["read"], args.show)
    _show("LISTED  ", dirs["read"], args.show)
    _show("REPLACED", files["replaced"] + files["appeared"], args.show)
    _show("GONE    ", files["gone"], args.show)
    if own:
        print(f"  (not measured: {own} — every run of this tool reads it)")
    if own_dir:
        print(f"  (not measured: the directory {own_dir} — without `python3 -I` the "
              "interpreter lists it at start-up)")
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("".join(f"{name}\n" for name in files["read"]))
        except OSError as exc:
            raise _caller_error(f"{args.out} cannot be written: {exc}") from exc
    if refused:
        raise _caller_error(
            f"{len(refused)} path(s) could not be re-armed, so the next segment "
            "cannot see a read of them: " + "; ".join(refused[:5]))
    return EXIT_OK


# ── canary ──────────────────────────────────────────────────────────────────

def canary_verdict(state: dict) -> list[str]:
    """What does not hold, one sentence each; empty when the measurement works."""
    canary = state["canary"]
    moved = {}
    for name, old in canary["stamps"].items():
        new = _stamp(os.path.join(canary["dir"], name))
        if new is None or old is None:
            return [f"the canary `{name}` is missing from {canary['dir']}: "
                    "something removed it after `reset`"]
        moved[name] = new != old
    problems = []
    if not moved[CANARY_READ]:
        problems.append(
            f"`{CANARY_READ}` was not recorded as read: either nobody read it "
            "after `reset`, or this filesystem does not record reads (noatime, "
            "or a read path that bypasses access times)")
    if moved[CANARY_UNTOUCHED]:
        problems.append(
            f"`{CANARY_UNTOUCHED}` was recorded as read though nothing should "
            "have touched it: something walks and reads this directory, or "
            "access times move without a read")
    if not moved[CANARY_DIR]:
        problems.append(
            f"`{CANARY_DIR}` was not recorded as listed: either nobody listed "
            "it after `reset`, or directory access times are not recorded")
    if moved[CANARY_INNER]:
        problems.append(
            f"`{CANARY_INNER}` was recorded as read though only its directory "
            "was to be listed: a listing cannot be told from a read here")
    return problems


def cmd_canary(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state))
    problems = canary_verdict(state)
    where = state["canary"]["dir"]
    if problems:
        raise _caller_error(
            f"CANARY-FAILED: access times in {where} do not measure reads — "
            "every segment collected with this state is unusable, in either "
            "direction:\n  - " + "\n  - ".join(problems)
            + f"\n  mount: {mount_of(where)}")
    print(f"CANARY-OK: in {where} the file that was read moved, the file nobody "
          "touched did not, the directory that was listed moved and the file "
          "inside it did not.")
    return EXIT_OK


# ── compare (transitional) ──────────────────────────────────────────────────

def _glob_regex(pattern: str) -> re.Pattern[str]:
    """`*` stays inside one path segment, `**` crosses them — the two forms a
    trace's Go source entries use."""
    out = []
    for part in re.split(r"(\*\*|\*)", pattern):
        out.append(".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part))
    return re.compile("".join(out) + r"\Z")


def cmd_compare(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state))
    try:
        with open(args.tracer_json, encoding="utf-8") as handle:
            jobs = json.load(handle)["jobs"]
        python_files = sorted({f for job in jobs.values() for f in job["files"]})
        go_patterns = sorted({s for job in jobs.values() for b in job["go"] for s in b["sources"]})
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _caller_error(
            f"{args.tracer_json} is not a trace written by `workflow_path_filter.py "
            f"assert --out` ({exc!r}): there is nothing to compare against.") from exc
    if not python_files:
        raise _caller_error(f"{args.tracer_json} names no file: the tracer measured nothing.")
    if not state["segments"]:
        raise _caller_error("no segment was collected: the access times measured nothing.")

    seen = {f for segment in state["segments"] for f in segment["files"]}
    replaced = {f for segment in state["segments"] for f in segment["replaced"]}
    tracked = sorted(state["files"])
    regexes = [_glob_regex(p) for p in go_patterns]
    go_files = sorted(f for f in tracked if any(r.match(f) for r in regexes))
    own = state.get("self")

    tracer_only = [f for f in python_files if f not in seen and f != own]
    go_only = [f for f in go_files if f not in seen]
    atime_only = sorted(seen - set(python_files) - set(go_files))
    print(f"COMPARE: access times: {len(seen)} tracked file(s) read over "
          f"{len(state['segments'])} segment(s) ({len(replaced)} replaced, not counted)")
    print(f"COMPARE: tracer (Python): {len(python_files)} first-party file(s); "
          f"both saw {len(set(python_files) & seen)}")
    print(f"COMPARE: tracer (go list): {len(go_files)} tracked file(s) under "
          f"{len(go_patterns)} source pattern(s); both saw {len(set(go_files) & seen)}")
    print(f"COMPARE: GO-LIST-ONLY ({len(go_only)}) — matched by a source pattern, not "
          "read (a pattern is wider than a build: `_test.go`, other platforms):")
    _show("GO-LIST-ONLY", go_only, args.show)
    print(f"COMPARE: ATIME-ONLY ({len(atime_only)}) — read, and not named by the tracer:")
    _show("ATIME-ONLY", atime_only, args.show)
    if own and own in python_files:
        print(f"COMPARE: (not compared: {own} — every run of this tool reads it)")
    if not tracer_only:
        print("COMPARE: TRACER-ONLY (0) — the access times saw every file the "
              "Python tracer recorded.")
        return EXIT_OK
    print(f"COMPARE: TRACER-ONLY ({len(tracer_only)}) — the tracer recorded these and "
          "their access time did not move. Each is a file the access-time method "
          "MISSED (or one that was opened and never read):", file=sys.stderr)
    for name in tracer_only:
        note = "  [replaced during the run]" if name in replaced else ""
        print(f"  TRACER-ONLY  {name}{note}", file=sys.stderr)
    return EXIT_VIOLATION


# ── semantics ───────────────────────────────────────────────────────────────

# (cell id, what is done, expected on ext4 relatime, does the method rest on it).
# The expected column is what THIS tool printed on ext4 `rw,relatime`, kernel
# 6.18, the machine the method was validated on. A cell the method rests on that
# differs is exit 2; the others are printed and named, and are context.
_CELLS: list[tuple[str, str, bool, bool]] = [
    ("reset-read", "reset to PAST, then read()", True, True),
    ("reset-read-old-mtime", "mtime 1995 < atime PAST < ctime now, then read()", True, True),
    ("recent-atime-read", "atime = now-1h (mtime 1995), then read()", True, True),
    ("fresh-first-read", "new file, no reset: first read()", True, False),
    ("fresh-second-read", "new file, no reset: second read()", False, False),
    ("reset-second-read", "reset, read(), then a second read() with no reset", False, False),
    ("o-noatime", "reset, read through O_NOATIME", False, False),
    ("mmap", "reset, open + mmap + touch a byte, no read()", True, False),
    ("open-close", "reset, open() + close(), no read", False, True),
    ("stat", "reset, stat() only", False, True),
    ("write-in-place", "reset, truncate + write in place", False, False),
    ("rename-replace", "replace by rename: the path keeps its inode (False = seen as REPLACED)", False, True),
    ("cat", "reset, `cat file > /dev/null`", True, True),
    ("cp", "reset, `cp file copy`", True, False),
    ("execve", "reset, execute as a script", True, False),
    ("symlink-target", "reset, read through a symlink: the target", True, True),
    ("symlink-link", "reset, read through a symlink: the link itself", True, False),
    ("read-file:dir", "read dir/f: the directory", False, True),
    ("read-file:file", "read dir/f: the file", True, True),
    ("listdir:dir", "os.listdir(dir): the directory", True, True),
    ("listdir:subdir", "os.listdir(dir): its subdirectory", False, True),
    ("listdir:file", "os.listdir(dir): a file in it", False, True),
    ("ls:dir", "`ls dir`: the directory", True, True),
    ("ls:file", "`ls dir`: a file in it", False, True),
    ("walk:subdir", "os.walk(dir), no read: its subdirectory", True, False),
    ("walk:file", "os.walk(dir), no read: a file in it", False, True),
    ("glob-miss:dir", "glob dir/*.x with no match: the directory", True, False),
    ("exists:dir", "os.path.exists(dir/sub/g): the directories on the way", False, True),
]


def _run_quiet(argv: list[str], cwd: str) -> None:
    """Run a command whose only purpose is to touch a file: its output is
    discarded and decides nothing (`cat`, `cp`, `ls` are the subject of a
    cell, not a judge of it)."""
    subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60, check=True)


def measure_semantics(work: str) -> dict[str, bool]:
    """Run every cell in `work` (an empty directory on the filesystem in
    question) and return cell id → did the access time move."""
    def path(name: str) -> str:
        return os.path.join(work, name)

    def make(name: str, mtime_ns: int | None = None) -> str:
        full = path(name)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("x" * 5000)
        if mtime_ns is not None:
            os.utime(full, ns=(mtime_ns, mtime_ns))
        return full

    def atime(full: str) -> int:
        return os.lstat(full).st_atime_ns

    def read(full: str) -> None:
        with open(full, "rb") as handle:
            handle.read()

    def moved(full: str, action: Callable[[], object], at_ns: int = PAST_NS) -> bool:
        os.utime(full, ns=(at_ns, os.lstat(full).st_mtime_ns), follow_symlinks=False)
        before = atime(full)
        action()
        return atime(full) != before

    out: dict[str, bool] = {}
    a = make("a")
    out["reset-read"] = moved(a, lambda: read(a))
    b = make("b", OLD_MTIME_NS)
    out["reset-read-old-mtime"] = moved(b, lambda: read(b))
    c = make("c", OLD_MTIME_NS)
    out["recent-atime-read"] = moved(c, lambda: read(c), time.time_ns() - 3600 * 10**9)

    d = make("d")
    first = atime(d)
    time.sleep(0.05)
    read(d)
    second = atime(d)
    time.sleep(0.05)
    read(d)
    out["fresh-first-read"] = second != first
    out["fresh-second-read"] = atime(d) != second

    e = make("e")
    moved(e, lambda: read(e))
    after_first = atime(e)
    time.sleep(0.05)
    read(e)
    out["reset-second-read"] = atime(e) != after_first

    def noatime() -> None:
        fd = os.open(a, os.O_RDONLY | os.O_NOATIME)
        try:
            os.read(fd, 100)
        finally:
            os.close(fd)
    out["o-noatime"] = moved(a, noatime)

    def mapped() -> None:
        fd = os.open(a, os.O_RDONLY)
        try:
            with mmap.mmap(fd, 0, prot=mmap.PROT_READ) as view:
                view[0]
        finally:
            os.close(fd)
    out["mmap"] = moved(a, mapped)
    out["open-close"] = moved(a, lambda: os.close(os.open(a, os.O_RDONLY)))
    out["stat"] = moved(a, lambda: os.stat(a))

    def in_place() -> None:
        with open(a, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("y" * 100)
    out["write-in-place"] = moved(a, in_place)

    f = make("f")
    inode = os.lstat(f).st_ino
    make("f.new")
    os.replace(path("f.new"), f)
    out["rename-replace"] = os.lstat(f).st_ino == inode

    out["cat"] = moved(a, lambda: _run_quiet(["cat", a], work))
    out["cp"] = moved(a, lambda: _run_quiet(["cp", a, path("a.copy")], work))
    script = path("s.sh")
    with open(script, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("#!/bin/sh\ntrue\n")
    os.chmod(script, 0o755)
    out["execve"] = moved(script, lambda: _run_quiet([script], work))

    link = path("link")
    os.symlink("a", link)
    os.utime(a, ns=(PAST_NS, os.lstat(a).st_mtime_ns))
    target_before, link_before = atime(a), atime(link)
    read(link)
    out["symlink-target"] = atime(a) != target_before
    out["symlink-link"] = atime(link) != link_before

    directory, sub = path("dir"), path("dir/sub")
    inner = make("dir/f")
    make("dir/sub/g")
    trio = {"dir": directory, "subdir": sub, "file": inner}

    def around(action: Callable[[], object]) -> dict[str, bool]:
        for full in trio.values():
            os.utime(full, ns=(PAST_NS, os.lstat(full).st_mtime_ns))
        before = {k: atime(v) for k, v in trio.items()}
        action()
        return {k: atime(v) != before[k] for k, v in trio.items()}

    got = around(lambda: read(inner))
    out["read-file:dir"], out["read-file:file"] = got["dir"], got["file"]
    got = around(lambda: os.listdir(directory))
    out["listdir:dir"], out["listdir:subdir"], out["listdir:file"] = (
        got["dir"], got["subdir"], got["file"])
    got = around(lambda: _run_quiet(["ls", directory], work))
    out["ls:dir"], out["ls:file"] = got["dir"], got["file"]
    got = around(lambda: list(os.walk(directory)))
    out["walk:subdir"], out["walk:file"] = got["subdir"], got["file"]
    got = around(lambda: glob.glob(os.path.join(directory, "*.x")))
    out["glob-miss:dir"] = got["dir"]
    got = around(lambda: os.path.exists(os.path.join(sub, "g")))
    out["exists:dir"] = got["dir"] or got["subdir"]
    return out


def cmd_semantics(args: argparse.Namespace) -> int:
    base = Path(args.dir).resolve() if args.dir else Path(
        _git(args.root, "rev-parse", "--absolute-git-dir").strip())
    try:
        work = tempfile.mkdtemp(prefix="atime-semantics-", dir=base)
    except OSError as exc:
        raise _caller_error(f"no scratch directory can be made in {base}: {exc}") from exc
    try:
        try:
            got = measure_semantics(work)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _caller_error(
                f"the semantics cells could not all be run in {work} ({exc!r}): "
                "what an access time means here is unknown.") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print(f"atime-trace semantics: directory {base}")
    print(f"atime-trace semantics: mount: {mount_of(str(base))}")
    print(f"atime-trace semantics: kernel {platform.release()}, python {platform.python_version()}")
    print("atime-trace semantics: reference = ext4 rw,relatime (kernel 6.18)")
    basis: list[str] = []
    context: list[str] = []
    for cell, text, expected, rests in _CELLS:
        same = got[cell] == expected
        if not same:
            (basis if rests else context).append(cell)
        print(f"  SEM {cell:<22} updated={got[cell]!s:<5} reference={expected!s:<5} "
              f"{'same   ' if same else 'DIFFERS'} {'[basis]  ' if rests else '[context]'} {text}")
    missing = sorted(set(got) ^ {c[0] for c in _CELLS})
    if missing:
        raise _caller_error(f"cells measured and cells declared differ: {missing}")
    if context:
        print("SEMANTICS-CONTEXT-DIFFERS: " + ", ".join(context)
              + " — not what the method rests on; read the docstring's list of "
              "blind spots against these.")
    if basis:
        raise _caller_error(
            "SEMANTICS-BASIS-DIFFERS: " + ", ".join(basis) + " — the method rests "
            "on these cells and this filesystem answers differently from the "
            "reference. Access times here are not a measurement of reads.")
    print(f"SEMANTICS-OK: all {sum(1 for c in _CELLS if c[3])} basis cells match the "
          f"reference; {len(context)} context cell(s) differ.")
    return EXIT_OK


# ── cost ────────────────────────────────────────────────────────────────────

def cmd_cost(args: argparse.Namespace) -> int:
    state = load_state(Path(args.state))
    cost = state["cost"]
    collects = cost["collect_s"]
    print(f"COST: {len(state['files'])} tracked file(s), {len(state['dirs'])} directory(ies)")
    print(f"COST: reset: git update-index --refresh {cost['refresh_s']:.2f}s + "
          f"git ls-files {cost['list_s']:.2f}s + setting access times {cost['reset_s']:.2f}s")
    if not collects:
        raise _caller_error("no segment was collected: there is no collect cost to report.")
    for segment in state["segments"]:
        print(f"COST: segment [{segment['label']}]: {len(segment['files'])} read, "
              f"{len(segment['dirs'])} listed, {len(segment['replaced'])} replaced, "
              f"{len(segment['gone'])} gone")
    print(f"COST: collect: {len(collects)} run(s), {sum(collects):.2f}s in total, "
          f"slowest {max(collects):.2f}s (each includes re-arming what it found)")
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Name the tracked files a stretch of a job read, by access time.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT,
                        help="repository root (default: this checkout)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("reset", help="refresh the index, set every tracked path's "
                                     "access time to the past, create the canaries")
    p.add_argument("--state", required=True, help="state file to write (JSON)")
    p.add_argument("--canary-dir", help="where the canaries go; must be on the "
                                        "filesystem of the root (default: <git dir>/atime-canary)")
    p.set_defaults(func=cmd_reset)

    p = sub.add_parser("collect", help="name what was read since the last reset/collect")
    p.add_argument("--state", required=True)
    p.add_argument("--label", required=True, help="name of this segment")
    p.add_argument("--show", type=int, default=40, help="paths printed per list (default 40)")
    p.add_argument("--out", help="also write the files read, one per line")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("canary", help="exit 2 unless the canaries show reads are measured")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_canary)

    p = sub.add_parser("compare", help="collected segments against a Python tracer's trace")
    p.add_argument("--state", required=True)
    p.add_argument("--tracer-json", required=True,
                   help="the file `workflow_path_filter.py assert --out` wrote")
    p.add_argument("--show", type=int, default=60, help="paths printed per list (default 60)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("semantics", help="what an access time means on a filesystem")
    p.add_argument("--dir", help="directory on the filesystem to probe (default: the "
                                 "root's git directory)")
    p.set_defaults(func=cmd_semantics)

    p = sub.add_parser("cost", help="what the measurement itself cost")
    p.add_argument("--state", required=True)
    p.set_defaults(func=cmd_cost)

    args = parser.parse_args(argv)
    args.root = Path(os.path.realpath(args.root))
    return args.func(args)


if __name__ == "__main__":
    _lib_exitcodes.run(main)
