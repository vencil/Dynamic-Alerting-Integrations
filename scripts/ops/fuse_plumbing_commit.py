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
  0 — commit landed (either via normal path or plumbing fallback)
  1 — generic failure (message unreadable, file missing, etc.)
  2 — commit-tree succeeded but ref-write failed; new SHA in stderr for recovery

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
import subprocess
import sys
import tempfile


def _run(cmd: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> str:
    """Run cmd, return stripped stdout; raise on non-zero if check=True."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"cmd failed ({result.returncode}): {' '.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout.strip()


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

        # 4. commit-tree
        if amend:
            # amend: replace HEAD; parent is HEAD's parent
            parent = _run(["git", "rev-parse", "HEAD^"])
        else:
            parent = _run(["git", "rev-parse", "HEAD"])

        with tempfile.NamedTemporaryFile("w", suffix=".msg", delete=False) as mf:
            mf.write(message)
            msg_path = mf.name
        try:
            new_sha = _run(
                ["git", "commit-tree", tree, "-p", parent, "-F", msg_path],
            )
        finally:
            os.unlink(msg_path)

        # 5. Direct-write branch ref
        branch = _run(["git", "symbolic-ref", "--short", "HEAD"])
        ref_path = repo_root / ".git" / "refs" / "heads" / branch
        ref_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            ref_path.write_text(new_sha + "\n")
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


def normal_commit(
    repo_root: pathlib.Path,
    message: str,
    files: list[str],
    *,
    amend: bool = False,
) -> str:
    """Commit via normal git path (hooks run)."""
    for rel in files:
        _run(["git", "add", rel])
    cmd = ["git", "commit", "-m", message]
    if amend:
        cmd = ["git", "commit", "--amend", "-m", message]
    _run(cmd)
    return _run(["git", "rev-parse", "HEAD"])


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
        return 1

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
        message = pathlib.Path(args.msg_file).read_text()
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
        return 1

    branch = _run(["git", "symbolic-ref", "--short", "HEAD"], check=False) or "(detached)"
    print(f"committed: {sha}  (branch={branch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
