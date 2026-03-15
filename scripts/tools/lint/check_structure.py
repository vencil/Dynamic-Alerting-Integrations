#!/usr/bin/env python3
"""check_structure.py — Project structure enforcement.

Pre-commit hook that validates the repository's directory layout stays
normalised.  Catches common drift scenarios:

  1. scripts/tools/ root should only contain allowed files (shared lib,
     metric-dictionary, validate_all, vendor_download)
  2. docs/ root should have no .jsx files (they belong in
     docs/interactive/tools/ or docs/getting-started/)
  3. Test files (test_*.py) should only live under tests/
  4. Test output directories should not be tracked

Usage:
    python3 scripts/tools/lint/check_structure.py          # report mode
    python3 scripts/tools/lint/check_structure.py --ci      # exit 1 on violations

Exit codes:
    0 = clean
    1 = violations found (--ci mode)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────

# Files allowed directly in scripts/tools/ (not in a subdirectory)
ALLOWED_TOOLS_ROOT = {
    "_lib_python.py",
    "metric-dictionary.yaml",
    "validate_all.py",
    "vendor_download.sh",
    "__init__.py",       # in case it's ever added
}

# Allowed .jsx locations (relative to PROJECT_ROOT)
ALLOWED_JSX_DIRS = {
    "docs/interactive/tools",
    "docs/getting-started",
}

# Directories that should never be tracked in git
BANNED_TRACKED_DIRS = [
    "tests/_test_output",
    "tests/_test_multidb_output",
]

# ── Helpers ─────────────────────────────────────────────────────────

def _git_tracked(project_root: Path) -> list[str]:
    """Return list of git-tracked file paths (relative to project root)."""
    import subprocess
    result = subprocess.run(
        ["git", "ls-files"],
        capture_output=True, text=True, cwd=project_root,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line]


def check_tools_root(project_root: Path, tracked: list[str]) -> list[str]:
    """Ensure scripts/tools/ root has no stray files."""
    violations = []
    prefix = "scripts/tools/"
    for f in tracked:
        if not f.startswith(prefix):
            continue
        rest = f[len(prefix):]
        # Files in subdirs are fine
        if "/" in rest:
            continue
        basename = os.path.basename(rest)
        if basename not in ALLOWED_TOOLS_ROOT:
            violations.append(
                f"  STRAY   {f}  (move to ops/, dx/, or lint/)"
            )
    return violations


def check_jsx_placement(project_root: Path, tracked: list[str]) -> list[str]:
    """Ensure .jsx files are in allowed directories."""
    violations = []
    for f in tracked:
        if not f.endswith(".jsx"):
            continue
        parent = str(Path(f).parent)
        if parent not in ALLOWED_JSX_DIRS:
            # Allow JSX files outside docs/ (e.g. in src/) — only flag docs/ root
            if f.startswith("docs/") and parent == "docs":
                violations.append(
                    f"  MISPLACED  {f}  (move to docs/interactive/tools/)"
                )
    return violations


def check_test_placement(project_root: Path, tracked: list[str]) -> list[str]:
    """Ensure test_*.py files live under tests/."""
    violations = []
    for f in tracked:
        basename = os.path.basename(f)
        if not basename.startswith("test_") or not basename.endswith(".py"):
            continue
        if f.startswith("tests/"):
            continue
        violations.append(
            f"  MISPLACED  {f}  (move to tests/)"
        )
    return violations


def check_banned_dirs(project_root: Path, tracked: list[str]) -> list[str]:
    """Ensure test output directories are not tracked."""
    violations = []
    for d in BANNED_TRACKED_DIRS:
        prefix = d + "/"
        for f in tracked:
            if f.startswith(prefix) or f == d:
                violations.append(
                    f"  TRACKED  {f}  (add {d}/ to .gitignore)"
                )
    return violations


# ── Main ────────────────────────────────────────────────────────────

def main() -> int:
    """CLI entry point: Project structure enforcement."""
    parser = argparse.ArgumentParser(
        description="Validate project directory structure",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="Exit with code 1 on violations (for CI/pre-commit)",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    tracked = _git_tracked(project_root)

    all_violations: list[str] = []
    checks = [
        ("scripts/tools/ root cleanliness", check_tools_root),
        ("JSX file placement", check_jsx_placement),
        ("Test file placement", check_test_placement),
        ("Banned tracked directories", check_banned_dirs),
    ]

    for label, check_fn in checks:
        violations = check_fn(project_root, tracked)
        if violations:
            all_violations.append(f"\n▸ {label}:")
            all_violations.extend(violations)

    if not all_violations:
        print("✓ Project structure OK")
        return 0

    print("✗ Structure violations found:")
    for line in all_violations:
        print(line)

    if args.ci:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
