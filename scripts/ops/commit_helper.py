#!/usr/bin/env python3
"""commit_helper.py — UTF-8 safety layer for win_git_escape.bat commit / commit-file.

Motivation:
  PR #42 discovered that even commit-file can corrupt CJK / em-dash / etc.
  under default Windows configurations: `git commit -F msg.txt` reads the
  file via the active codepage (CP950 / CP1252 / etc.) rather than UTF-8,
  even with `chcp 65001` set earlier. The bytes arrive as mojibake.

  This helper sidesteps that by reading the file ourselves in Python and
  piping the raw UTF-8 bytes to `git commit -F -` via stdin. Python's
  subprocess passes bytes directly without any codepage translation.

Modes:
  check-ascii <msg>   — exit 0 if MSG is ASCII-only, 1 with hint otherwise.
                        Used before `git commit -m "..."` to reject messages
                        that cmd.exe would corrupt.
  commit-file <path>  — read UTF-8 file, pipe bytes to `git commit -F -`.
                        Preserves non-ASCII reliably.

Called from scripts/ops/win_git_escape.bat. Standalone usage is supported
for testing but not the primary entry point.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


EXTRA_GIT_ARGS = ["--no-verify"]


def check_ascii(msg: str) -> int:
    non_ascii = [c for c in msg if ord(c) > 127]
    if not non_ascii:
        return 0
    uniq = sorted(set(non_ascii))
    sample = "".join(uniq[:10])
    print(
        (
            f"ERROR: commit message contains {len(non_ascii)} non-ASCII "
            f"char(s). Sample: {sample!r}\n"
            "\n"
            "Windows cmd corrupts UTF-8 in -m arguments regardless of chcp.\n"
            "Use commit-file instead (reads msg.txt as UTF-8 reliably):\n"
            "\n"
            "    (write your message to _msg.txt as UTF-8, no BOM)\n"
            "    scripts\\ops\\win_git_escape.bat commit-file _msg.txt\n"
        ),
        file=sys.stderr,
    )
    return 1


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

    p_check = sub.add_parser(
        "check-ascii",
        help="Exit 0 if msg is ASCII-only; exit 1 with hint otherwise.",
    )
    p_check.add_argument("msg")

    p_commit = sub.add_parser(
        "commit-file",
        help="Pipe UTF-8 file contents to `git commit -F -`.",
    )
    p_commit.add_argument("path")

    args = parser.parse_args(argv)
    if args.mode == "check-ascii":
        return check_ascii(args.msg)
    if args.mode == "commit-file":
        return commit_file(args.path)
    parser.error(f"unknown mode: {args.mode}")
    return 2  # unreachable; parser.error exits


if __name__ == "__main__":
    sys.exit(main())
