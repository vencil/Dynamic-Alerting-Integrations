#!/usr/bin/env python3
"""fuse_plumbing_commit.py — commit through git plumbing when FUSE phantom locks block git.

Problem
-------
Under Cowork's FUSE-mounted workspace, `.git/index.lock` and `.git/HEAD.lock` can
appear as phantom files: `ls` shows them, `rm` returns EPERM, yet `git` sees them
and refuses to create its own lock. High-level `git add` / `git commit` / `git
update-ref` all fail with:

    fatal: Unable to create '.git/index.lock': File exists.

Historical workaround (LL #59, #60 in v2.7.0 planning): manual plumbing via
`hash-object -w` → temp-index `update-index` → `write-tree` → `commit-tree` →
direct write to `.git/refs/heads/<branch>`. This tool codifies that workaround
so we never hand-type it again.

Usage
-----
    # Commit specific files with a message from a file
    python scripts/ops/fuse_plumbing_commit.py --msg msg.txt file1 file2 ...

    # Commit with inline message
    python scripts/ops/fuse_plumbing_commit.py -m "feat(foo): bar" file1

    # Auto-detect (runs normal git commit when no phantom lock; falls back
    # to plumbing when locks are detected)
    python scripts/ops/fuse_plumbing_commit.py --auto --msg msg.txt file1

    # Makefile shortcut
    make fuse-commit MSG=msg.txt FILES="file1 file2"

Exit codes
----------
  0 — commit landed (either via normal path or plumbing fallback); when the
      SHA or the branch cannot be read afterwards in time, still 0, said
  2 — no commit: the normal path's `git commit` failed (a hook failed — a
      finding or a hook that could not run, which git and pre-commit both end
      at 1 and the tool cannot tell apart — or nothing to commit; read
      git's and the hooks' output above the error, which streams to stderr
      as it runs, #4231), the pre-commit hook is not installed or
      cannot start pre-commit, not in a repo, a file or the message missing, a
      git plumbing step failed, or commit-tree succeeded but the ref write
      failed (new SHA in stderr for recovery), or a git step did not finish in
      time (#3836: `git commit` gets COMMIT_TIMEOUT_S, every other step
      STEP_TIMEOUT_S; a `git commit` cut off is stopped with its hooks, and
      the commit's state is unknown — check with `git log -1`) —
      scripts/tools/_lib_exitcodes.py

  Never 1 (#3634, owner ruling 2026-11-13): 1 means "change the content",
  and the tool cannot prove a hook failure is a finding rather than a check
  that never ran. The only documented caller, `make fuse-commit`, ends any
  failure at 2 anyway.

Design notes
------------
* Skips pre-commit hooks — plumbing path cannot invoke them. Use the
  sandbox runner (`scripts/ops/run_hooks_sandbox.sh`) or `make pr-preflight`
  as the quality gate before pushing.
* Preserves executable bit by reading source file stat.
* Uses a temp index under /tmp so we never touch .git/index while locked.
* After commit, tries to sync .git/index to the new tree (best-effort).
"""
from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))


#: Seconds a git plumbing step (read-tree, hash-object, commit-tree…) may take.
STEP_TIMEOUT_S = 60
#: #3836: seconds the normal path's `git commit` may take. It runs every
#: pre-commit hook — over a hundred in this repository — which often takes
#: longer than a plumbing step's limit. Past it git and its hooks are stopped
#: as one process group (POSIX; on Windows only git is): a hook left running
#: would stash and restore unstaged changes beside the retry.
COMMIT_TIMEOUT_S = 1800
#: Seconds the stopped group gets between SIGINT and SIGKILL.
_GRACE_S = 5


class CommandFailed(RuntimeError):
    """A command `_run` ran ended non-zero; *returncode* is its status."""

    def __init__(self, returncode: int, message: str) -> None:
        super().__init__(message)
        self.returncode = returncode


class CommandTimedOut(RuntimeError):
    """A command `_run` ran did not finish within its limit (#3836); it was
    killed. A RuntimeError, so the run ends at 2 with one named line."""


#: #4231: where a streamed command's stdout and stderr go — both to this
#: process's fd 2, so they show as they are written and this tool's own
#: stdout keeps only its `committed:` line.
_STREAMED = {"stdout": 2, "stderr": 2}
_CAPTURED = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}


def _run(cmd: list[str], *, env: dict[str, str] | None = None, check: bool = True,
         timeout: float | None = None, group: bool = False,
         stream: bool = False) -> str:
    """Run cmd, return stripped stdout; raise on non-zero if check=True.
    *timeout*: seconds (STEP_TIMEOUT_S when not given); past it the command
    is killed and CommandTimedOut raised. *group*: run it as its own process
    group and stop the whole group (see COMMIT_TIMEOUT_S). *stream*: do not
    capture — its output goes to stderr as it is written (#4231: a `git
    commit` runs every hook, for up to COMMIT_TIMEOUT_S) and is not repeated
    in the error; the return value is then ``""``."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    limit = STEP_TIMEOUT_S if timeout is None else timeout
    streams = _STREAMED if stream else _CAPTURED
    try:
        if group and os.name == "posix":
            result = _run_group(cmd, merged_env, limit, streams)
        else:
            result = subprocess.run(
                cmd,
                env=merged_env,
                **streams,
                text=True, encoding="utf-8", errors="replace",
                check=False,
                timeout=limit,
            )
    except subprocess.TimeoutExpired:
        raise CommandTimedOut(
            f"`{' '.join(cmd)}` did not finish within {limit:g}s and was stopped") from None
    if check and result.returncode != 0:
        if stream:
            raise CommandFailed(
                result.returncode,
                f"cmd failed ({result.returncode}): {' '.join(cmd)} — its output is above")
        raise CommandFailed(
            result.returncode,
            f"cmd failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
            + (f"\nstdout: {result.stdout.strip()}" if result.stdout.strip() else "")
        )
    return (result.stdout or "").strip()


def _run_group(cmd: list[str], env: dict[str, str], limit: float,
               streams: dict) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a command that starts children (git's hooks):
    its own session, so a timeout — or Ctrl-C here, which the new session
    no longer passes on — stops all of them (`_stop_group`)."""
    proc = subprocess.Popen(cmd, env=env, **streams,
                            text=True, encoding="utf-8", errors="replace",
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=limit)
    except BaseException:  # the timeout, or KeyboardInterrupt: stop the group
        _stop_group(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _stop_group(proc: subprocess.Popen) -> None:
    """Stop *proc*'s process group: SIGINT first, as Ctrl-C would send it
    (pre-commit then restores the unstaged changes it stashed), SIGKILL to
    whatever is still there after _GRACE_S."""
    for sig in (signal.SIGINT, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if _group_gone(proc, _GRACE_S):
            break
    proc.wait()
    for stream in (proc.stdout, proc.stderr):
        if stream is not None:
            stream.close()


def _group_gone(proc: subprocess.Popen, within: float) -> bool:
    """Whether every process of *proc*'s group ended within *within* s."""
    deadline = time.monotonic() + within
    while True:
        proc.poll()  # reap git itself: a zombie still counts in the group
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _repo_root() -> pathlib.Path:
    root = _run(["git", "rev-parse", "--show-toplevel"])
    return pathlib.Path(root)


def detect_phantom_lock(repo_root: pathlib.Path) -> list[str]:
    """Return list of phantom lock paths that exist. Empty list = clean."""
    locks = []
    for name in ("index.lock", "HEAD.lock"):
        p = repo_root / ".git" / name
        if p.exists():
            locks.append(str(p))
    return locks


def plumbing_commit(
    repo_root: pathlib.Path,
    message: str,
    files: list[str],
    *,
    amend: bool = False,
) -> str:
    """Commit via plumbing. Returns new commit SHA. Skips hooks."""
    # 1. Build temp index seeded from HEAD
    with tempfile.NamedTemporaryFile(prefix="plumb_idx_", dir="/tmp", delete=False) as tf:
        idx_path = tf.name
    try:
        env = {"GIT_INDEX_FILE": idx_path}
        _run(["git", "read-tree", "HEAD"], env=env)

        # 2. Hash and add each file
        for rel in files:
            abs_path = repo_root / rel
            if not abs_path.exists():
                raise FileNotFoundError(f"file not found: {rel}")
            mode = "100755" if os.access(abs_path, os.X_OK) else "100644"
            blob = _run(["git", "hash-object", "-w", str(abs_path)])
            _run(
                [
                    "git",
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{mode},{blob},{rel}",
                ],
                env=env,
            )

        # 3. Write tree from temp index
        tree = _run(["git", "write-tree"], env=env)

        # The branch first: past commit-tree a failure would strand the new
        # commit object with no SHA said (#3836 review).
        branch = _run(["git", "symbolic-ref", "--short", "HEAD"])

        # 4. commit-tree
        if amend:
            # amend: replace HEAD; parent is HEAD's parent
            parent = _run(["git", "rev-parse", "HEAD^"])
        else:
            parent = _run(["git", "rev-parse", "HEAD"])

        # ⛔ newline="\n" is load-bearing: `git commit-tree -F` takes this file
        # VERBATIM as the commit message. Under a Python whose os.linesep is
        # CRLF, the platform default would put a CR on every line of a
        # PERMANENT commit object — including the trailer block that
        # `git interpret-trailers` and the Self-Review-Pass-2 gate parse.
        # Unfixable afterwards without a history rewrite. (Measured: the CRs do
        # survive verbatim into the commit object.)
        # NOTE: this module is normally driven from the Linux Cowork VM — see
        # the /tmp index above and `python3 …` in the Makefile — where
        # os.linesep is already LF. The pin is defence-in-depth for the
        # Windows-host escape-hatch path, not a live Linux defect.
        with tempfile.NamedTemporaryFile("w", suffix=".msg", delete=False,
                                         encoding="utf-8", newline="\n") as mf:
            mf.write(message)
            msg_path = mf.name
        try:
            new_sha = _run(
                ["git", "commit-tree", tree, "-p", parent, "-F", msg_path],
            )
        finally:
            os.unlink(msg_path)

        # 5. Direct-write branch ref
        ref_path = repo_root / ".git" / "refs" / "heads" / branch
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # newline="\n" so the ref file is byte-identical on every host.
            # NOTE: git itself tolerates a CRLF-terminated ref — measured:
            # `git rev-parse` / `git log` / `git checkout` all resolve it fine,
            # because the loose-ref parser strips trailing whitespace. So this
            # is consistency, not a correctness fix. (An earlier revision of
            # this comment claimed a CRLF ref is "invalid"; that was asserted
            # without testing and is wrong — do not reinstate it.)
            ref_path.write_text(new_sha + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            print(
                f"error: ref write failed ({exc}); new commit at {new_sha}",
                file=sys.stderr,
            )
            sys.exit(2)

        # 6. Best-effort sync .git/index to the new tree
        main_idx = repo_root / ".git" / "index"
        try:
            shutil.copy(idx_path, main_idx)
        except OSError:
            # .git/index locked too — new commit is fine, status may lie until lock clears
            print(
                "warn: could not sync .git/index; `git status` may show stale state "
                "until locks clear",
                file=sys.stderr,
            )

        return new_sha
    finally:
        try:
            os.unlink(idx_path)
        except OSError:
            pass


def require_pre_commit_hook() -> None:
    """The normal path runs the checks through git's pre-commit hook: refuse
    (RuntimeError → exit 2) when it would not run them, instead of committing
    unchecked with exit 0 (#3634). The hook git would run is the one
    `git rev-parse --git-path hooks/pre-commit` names (it follows
    `core.hooksPath`). It must be an executable file; a pre-commit framework
    shim must also be able to start pre-commit — its INSTALL_PYTHON with
    pre_commit importable, else `pre-commit` on PATH, as the shim itself
    tries — or it fails every commit without running a hook. The path is
    asked for absolute: plain `--git-path` is relative to the current
    directory, not the repository root."""
    hook = pathlib.Path(_run(["git", "rev-parse", "--path-format=absolute",
                              "--git-path", "hooks/pre-commit"]))
    if not hook.is_file() or not os.access(hook, os.X_OK):
        raise RuntimeError(
            f"no executable pre-commit hook at {hook}: the checks would not run; "
            "install it (`pre-commit install`, or the session-start hook) and retry")
    text = hook.read_text(encoding="utf-8", errors="replace")
    if "INSTALL_PYTHON=" not in text:
        return  # not pre-commit's shim: git runs it as it is
    python = ""
    for line in text.splitlines():
        if line.startswith("INSTALL_PYTHON="):
            python = line.split("=", 1)[1].strip().strip("'\"")
    candidates = []
    if python and os.access(python, os.X_OK):
        candidates.append([python, "-m", "pre_commit", "--version"])
    if shutil.which("pre-commit"):
        candidates.append(["pre-commit", "--version"])
    for cmd in candidates:
        try:
            if subprocess.run(cmd, capture_output=True, check=False, timeout=60).returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            continue  # cannot start it (a directory, no permission, hangs): not runnable
    raise RuntimeError(
        f"the pre-commit hook at {hook} cannot start pre-commit (INSTALL_PYTHON "
        f"{python or '(none)'} has no pre_commit, and no pre-commit on PATH): "
        "every commit would fail without running a check; reinstall pre-commit")


def normal_commit(
    repo_root: pathlib.Path,
    message: str,
    files: list[str],
    *,
    amend: bool = False,
) -> str:
    """Commit via normal git path (hooks run). Any failure is a RuntimeError
    (exit 2): git ends a hook failure at 1 whether the hook found something
    or could not run (pre-commit flattens both to 1), so it is never claimed
    to be a finding (#3634)."""
    require_pre_commit_hook()
    for rel in files:
        _run(["git", "add", rel])
    cmd = ["git", "commit", "-m", message]
    if amend:
        cmd = ["git", "commit", "--amend", "-m", message]
    try:
        _run(cmd, timeout=COMMIT_TIMEOUT_S, group=True, stream=True)
    except CommandTimedOut as exc:
        raise RuntimeError(
            f"{exc}: whether the commit landed is unknown (a hook may still be "
            "running); check with `git log -1` before committing again") from exc
    except CommandFailed as exc:
        raise RuntimeError(
            f"`git commit` failed (rc {exc.returncode}): a hook failed — "
            "a finding, or a check that could not run — or there was nothing to "
            "commit; the output above says which") from exc
    try:
        return _run(["git", "rev-parse", "HEAD"])
    except CommandTimedOut:
        # The commit landed (git commit ended 0): exit 0, say what is missing.
        print("warn: committed, but `git rev-parse HEAD` timed out: "
              "read the new SHA with `git log -1`", file=sys.stderr)
        return "(unknown)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fuse_plumbing_commit",
        description="Commit via git plumbing when FUSE phantom locks block normal git.",
    )
    # Not required at parse time: --show-locks doesn't need a message.
    # Enforced below after we know the mode.
    msg_group = parser.add_mutually_exclusive_group(required=False)
    msg_group.add_argument("-m", "--message", help="inline commit message")
    msg_group.add_argument("--msg", dest="msg_file", help="path to commit message file")

    parser.add_argument(
        "--auto",
        action="store_true",
        help="use plumbing only when phantom lock is detected; otherwise normal path",
    )
    parser.add_argument(
        "--force-plumbing",
        action="store_true",
        help="always use plumbing (skips hooks)",
    )
    parser.add_argument(
        "--amend",
        action="store_true",
        help="amend HEAD instead of creating a new commit",
    )
    parser.add_argument(
        "--show-locks",
        action="store_true",
        help="print detected phantom locks and exit",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="repo-relative paths to stage into the commit",
    )

    args = parser.parse_args(argv)

    try:
        repo_root = _repo_root()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2  # not in a repository: a caller error (#3577)

    if args.show_locks:
        locks = detect_phantom_lock(repo_root)
        if locks:
            for lk in locks:
                print(lk)
            return 0
        print("(no phantom locks)")
        return 0

    if not args.files:
        parser.error("at least one file required (unless --show-locks)")
    if not args.message and not args.msg_file:
        parser.error("commit message required: use -m MSG or --msg FILE")

    # Resolve message
    if args.msg_file:
        # encoding="utf-8" must match the temp-file write further down: commit
        # subjects in this repo are Traditional Chinese by convention, and a
        # locale-default read raises UnicodeDecodeError on a cp950 host (that
        # is a ValueError, so it would not be caught by the OSError handlers
        # around this flow either).
        message = pathlib.Path(args.msg_file).read_text(encoding="utf-8")
    else:
        message = args.message

    # Decide path
    locks = detect_phantom_lock(repo_root)
    use_plumbing = args.force_plumbing or (args.auto and bool(locks)) or (not args.auto)

    if args.auto and locks:
        print(
            f"info: phantom lock(s) detected ({', '.join(locks)}); using plumbing",
            file=sys.stderr,
        )

    try:
        if use_plumbing:
            sha = plumbing_commit(repo_root, message, args.files, amend=args.amend)
        else:
            sha = normal_commit(repo_root, message, args.files, amend=args.amend)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2  # a plumbing step or a missing file: a caller error (#3577)

    try:
        branch = _run(["git", "symbolic-ref", "--short", "HEAD"], check=False) or "(detached)"
    except CommandTimedOut:
        branch = "(unknown)"  # the commit landed; only its label is missing
    print(f"committed: {sha}  (branch={branch})")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
