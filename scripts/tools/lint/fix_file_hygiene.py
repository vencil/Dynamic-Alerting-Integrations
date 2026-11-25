#!/usr/bin/env python3
"""Fix file hygiene issues: strip null bytes and ensure EOF newline.

Auto-fixer for pre-commit. Silently fixes issues in-place and exits 1
if any file was modified (so pre-commit re-stages the fixed version).

Usage:
    python3 fix_file_hygiene.py [files...]
    python3 fix_file_hygiene.py --check [files...]   # report only, no fix
"""
import os
import stat
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402
from _lib_output import (  # noqa: E402  (#3981: the artifact entry)
    check_repo_artifact, exit_on_output_write_error, is_special_file, write_repo_artifact)


#: The positional's name, as the usage line names it: a refused target is
#: reported as "check the value given to FILE" (#3981).
_FLAG = "FILE"


def plan_fix(path: str, check_only: bool) -> "tuple[bool, bytes | None]":
    """``(had issues, fixed bytes to write or None)`` for one file, nothing
    written (#3981). Under *check_only* the issues are printed instead."""
    # Skip symlinks: their "content" is the target path string; appending
    # an EOF newline would corrupt the target (e.g. turn `../README.md`
    # into `../README.md\n`, which readlink() then resolves as a broken
    # path). docs/README-root.md is the canonical example.
    try:
        if os.path.islink(path):
            return False, None
    except OSError:
        pass
    # A FIFO would block the read; like a directory, it is not a file this
    # hook fixes (#3981).
    if is_special_file(path):
        return False, None

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (OSError, IsADirectoryError):
        return False, None

    fixed = raw

    # Strip null bytes
    if b"\x00" in fixed:
        fixed = fixed.replace(b"\x00", b"")

    # Ensure file ends with exactly one newline
    if fixed and not fixed.endswith(b"\n"):
        fixed = fixed.rstrip() + b"\n"

    if fixed == raw:
        return False, None

    if check_only:
        issues = []
        if b"\x00" in raw:
            count = raw.count(b"\x00")
            issues.append(f"{count} null bytes")
        if raw and not raw.endswith(b"\n"):
            issues.append("missing EOF newline")
        print(f"  {path}: {', '.join(issues)}")
        return True, None

    return True, fixed


def write_fixes(writes: "list[tuple[str, bytes]]") -> None:
    """Write the planned fixes (#3981): every file is judged before the first
    is written, then written through the artifact entry — bytes as computed,
    the file's own permission bits kept (this hook runs on the committer's
    working copies: a 0600 file stays 0600) — and named as it is written, so
    a later refusal (rc 2) still shows which files were already fixed."""
    for path, _fixed in writes:
        check_repo_artifact(path, flag=_FLAG)
    for path, fixed in writes:
        write_repo_artifact(path, fixed, flag=_FLAG, mode=stat.S_IMODE(os.stat(path).st_mode))
        print(f"  fixed {path}")


def fix_file(path: str, check_only: bool) -> bool:
    """Return True if file had issues; fix it unless *check_only* — one file
    through the same :func:`write_fixes` as `main`."""
    had, fixed = plan_fix(path, check_only)
    if fixed is not None:
        write_fixes([(path, fixed)])
    return had


@exit_on_output_write_error
def main() -> int:
    args = sys.argv[1:]
    valid_flags = {"--check", "--help", "-h"}
    for a in args:
        if a.startswith("-") and a not in valid_flags:
            print(f"Unknown option: {a}", file=sys.stderr)
            print("Usage: fix_file_hygiene.py [--check] [files...]", file=sys.stderr)
            return EXIT_CALLER_ERROR
    if "--help" in args or "-h" in args:
        print("Usage: fix_file_hygiene.py [--check] [files...]")
        print("Fix file hygiene: strip null bytes, ensure EOF newline.")
        print("Options:")
        print("  --check    Report issues without fixing")
        return EXIT_OK
    check_only = "--check" in args
    files = [f for f in args if not f.startswith("--")]

    if not files:
        return EXIT_OK

    modified = []
    writes = []
    for f in files:
        had, fixed = plan_fix(f, check_only)
        if had:
            modified.append(f)
        if fixed is not None:
            writes.append((f, fixed))
    write_fixes(writes)

    if modified:
        action = "would fix" if check_only else "fixed"
        print(f"file-hygiene: {action} {len(modified)} file(s)")
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
