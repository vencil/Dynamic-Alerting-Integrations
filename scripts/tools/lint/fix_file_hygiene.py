#!/usr/bin/env python3
"""Fix file hygiene issues: strip null bytes and ensure EOF newline.

Auto-fixer for pre-commit. Silently fixes issues in-place and exits 1
if any file was modified (so pre-commit re-stages the fixed version).

Usage:
    python3 fix_file_hygiene.py [files...]
    python3 fix_file_hygiene.py --check [files...]   # report only, no fix
"""
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION, EXIT_CALLER_ERROR  # noqa: E402


def fix_file(path: str, check_only: bool) -> bool:
    """Return True if file had issues."""
    # Skip symlinks: their "content" is the target path string; appending
    # an EOF newline would corrupt the target (e.g. turn `../README.md`
    # into `../README.md\n`, which readlink() then resolves as a broken
    # path). docs/README-root.md is the canonical example.
    try:
        if os.path.islink(path):
            return False
    except OSError:
        pass

    try:
        raw = open(path, "rb").read()
    except (OSError, IsADirectoryError):
        return False

    fixed = raw

    # Strip null bytes
    if b"\x00" in fixed:
        fixed = fixed.replace(b"\x00", b"")

    # Ensure file ends with exactly one newline
    if fixed and not fixed.endswith(b"\n"):
        fixed = fixed.rstrip() + b"\n"

    if fixed == raw:
        return False

    if check_only:
        issues = []
        if b"\x00" in raw:
            count = raw.count(b"\x00")
            issues.append(f"{count} null bytes")
        if raw and not raw.endswith(b"\n"):
            issues.append("missing EOF newline")
        print(f"  {path}: {', '.join(issues)}")
        return True

    open(path, "wb").write(fixed)
    return True


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
    for f in files:
        if fix_file(f, check_only):
            modified.append(f)

    if modified:
        action = "would fix" if check_only else "fixed"
        print(f"file-hygiene: {action} {len(modified)} file(s)")
        return EXIT_VIOLATION
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
