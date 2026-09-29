#!/usr/bin/env python3
"""commit_helper.py — UTF-8 safety layer for win_git_escape.bat commit-file.

Motivation:
  PR #42 discovered that even commit-file can corrupt CJK / em-dash / etc.
  under default Windows configurations: `git commit -F msg.txt` reads the
  file via the active codepage (CP950 / CP1252 / etc.) rather than UTF-8,
  even with `chcp 65001` set earlier. The bytes arrive as mojibake.

  This helper sidesteps that by reading the file ourselves in Python and
  piping the raw UTF-8 bytes to `git commit -F -` via stdin. Python's
  subprocess passes bytes directly without any codepage translation.

Modes:
  commit-file <path>  — read UTF-8 file, validate it with the same check the
                        commit-msg hook runs, then pipe bytes to
                        `git commit -F -`. Preserves non-ASCII reliably.

  (There is no `check-ascii` any more: it guarded the .bat's `commit "msg"`,
  which #2249 removed — commit-file covers every message.)

commit-msg gate (#1914):
  The commit itself runs with --no-verify (windows-mcp-playbook trap #36:
  pre-commit's generated hook hardcodes a Linux python path), and that flag
  skips commit-msg too. So commit-file runs the commit-msg validator itself
  (`pr_preflight.py --check-commit-msg`, via sys.executable — the interpreter
  the .bat already proved works) and refuses to commit when it fails.

Called from scripts/ops/win_git_escape.bat. Standalone usage is supported
for testing but not the primary entry point.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path


EXTRA_GIT_ARGS = ["--no-verify"]

PREFLIGHT = Path(__file__).resolve().parents[2] / "scripts" / "tools" / "dx" / "pr_preflight.py"


def check_commit_msg(data: bytes) -> int:
    """Run the commit-msg hook's validator on DATA; return its exit code.

    Mirrors scripts/hooks/commit-msg: a missing validator does not block
    (but says so, instead of passing silently).
    """
    if not PREFLIGHT.exists():
        print(
            f"WARN: {PREFLIGHT} not found; commit message NOT validated locally "
            "(CI commitlint still checks it).",
            file=sys.stderr,
        )
        return 0
    fd, tmp = tempfile.mkstemp(prefix="commit_msg_", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            result = subprocess.run(
                [sys.executable, str(PREFLIGHT), "--check-commit-msg", tmp],
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            print("ERROR: commit-msg validation timed out", file=sys.stderr)
            return 1
        return result.returncode
    finally:
        os.unlink(tmp)


def commit_file(path_str: str) -> int:
    p = Path(path_str)
    if not p.exists():
        print(f"ERROR: file not found: {p}", file=sys.stderr)
        return 1
    data = p.read_bytes()
    # Strip UTF-8 BOM if present — git's -F interprets BOM as part of the message.
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(f"ERROR: {p} is not valid UTF-8: {exc}", file=sys.stderr)
        return 1
    rc = check_commit_msg(data)
    if rc != 0:
        print(
            "ERROR: commit message failed the commit-msg check above; "
            "nothing was committed.",
            file=sys.stderr,
        )
        return rc
    try:
        result = subprocess.run(
            ["git", "commit", *EXTRA_GIT_ARGS, "-F", "-"],
            input=data,
            check=False,
            timeout=120,
        )
        return result.returncode
    except FileNotFoundError:
        print("ERROR: git not found on PATH", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    p_commit = sub.add_parser(
        "commit-file",
        help="Pipe UTF-8 file contents to `git commit -F -`.",
    )
    p_commit.add_argument("path")

    args = parser.parse_args(argv)
    if args.mode == "commit-file":
        return commit_file(args.path)
    parser.error(f"unknown mode: {args.mode}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
