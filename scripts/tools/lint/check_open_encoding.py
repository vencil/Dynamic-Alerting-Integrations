#!/usr/bin/env python3
"""check_open_encoding.py — flag open() text-mode calls without encoding=.

Why this exists
---------------
PR-2.5 (v2.8.0) root-caused ~30 Tier 1 test failures to ``open(path)`` calls
that never specified ``encoding='utf-8'``. On Windows / cp950 / shift_jis /
non-UTF-8 Linux locales, the OS-default codec chokes on chinese-content
YAML / Markdown / source files with ``UnicodeDecodeError``. Linux + Docker
CI happens to be UTF-8, so the bugs were silent there but real.

This is a **portability bug** not a stylistic preference: identical code
crashes on customer Windows jump-hosts and on legacy CentOS images with
LANG=POSIX. Always-explicit encoding closes the gap.

What it flags
-------------
AST walk for ``open(...)`` (built-in, NOT ``urllib.request.urlopen`` —
those are byte streams already and don't accept ``encoding=``).

Flagged when ALL of the following hold:
  1. The call's positional/keyword args don't include ``mode='rb'/'wb'/...``
     (binary modes — encoding is meaningless there).
  2. No ``encoding=`` keyword argument is present.

Per-line ignore: append ``# open-encoding: ignore`` for cases where the
file might legitimately be in OS-default encoding (rare — log reads from
foreign tools, encoding-detection workflows, etc.).

Severity model (mirrors check_subprocess_timeout.py)
----------------------------------------------------
PR-2.5 cleaned the test files containing actual Tier 1 failures (10 files,
~33 sites) plus the CSV CRLF bug in production. ~80 sites remain across
test files that don't currently exercise non-ASCII content — they're
latent portability bugs but not blockers. So this lint ships warn-only:

- **default mode**: report violations to stdout, exit 0.
- **--ci**: same — non-fatal, surfaces count for tracking.
- **--ci --strict-open-encoding**: violations are fatal. Activate once
  the remaining ~80 sites are cleaned (follow-up PR after v2.8.0).

Usage
-----
::

    # Local audit
    python3 scripts/tools/lint/check_open_encoding.py

    # Specific paths
    python3 scripts/tools/lint/check_open_encoding.py path/to/file.py ...

    # CI / pre-commit (warn-only)
    python3 scripts/tools/lint/check_open_encoding.py --ci

    # Future: post-cleanup hard gate
    python3 scripts/tools/lint/check_open_encoding.py --ci --strict-open-encoding

Exit codes
----------
::

    0 — no violations, OR --ci without --strict (warn-only)
    1 — violations found AND --ci --strict-open-encoding
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Default scan roots when no explicit paths given.
DEFAULT_PATHS = [
    REPO_ROOT / "scripts" / "tools",
    REPO_ROOT / "tests",
    REPO_ROOT / "components" / "da-tools" / "app",
]

IGNORE_MARKER = "open-encoding: ignore"


def _has_binary_mode(call: ast.Call) -> bool:
    """True if the call's mode arg is binary (contains 'b')."""
    # Positional: open(path, mode) — mode is args[1]
    if len(call.args) >= 2:
        mode_node = call.args[1]
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            if "b" in mode_node.value:
                return True
    # Keyword: open(path, mode='rb')
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            v = kw.value.value
            if isinstance(v, str) and "b" in v:
                return True
    return False


def _has_encoding_kwarg(call: ast.Call) -> bool:
    """True if encoding= is among the keyword args."""
    return any(kw.arg == "encoding" for kw in call.keywords)


def _is_open_call(call: ast.Call) -> bool:
    """True if this is the built-in ``open(...)`` (not ``foo.open()`` or
    ``urllib.request.urlopen()``).

    AST distinguishes:
      - ``open(...)``           → Call(func=Name('open'))
      - ``something.open(...)`` → Call(func=Attribute(...))
      - ``urlopen(...)``        → Call(func=Name('urlopen')) — different name
    """
    return isinstance(call.func, ast.Name) and call.func.id == "open"


def _line_has_ignore(source_lines: list[str], lineno: int) -> bool:
    """True if the source line at lineno carries the ignore marker."""
    if 1 <= lineno <= len(source_lines):
        return IGNORE_MARKER in source_lines[lineno - 1]
    return False


def scan_file(path: Path) -> list[tuple[int, str]]:
    """Return list of (lineno, snippet) for each violation."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_open_call(node):
            continue
        if _has_binary_mode(node):
            continue
        if _has_encoding_kwarg(node):
            continue
        if _line_has_ignore(source_lines, node.lineno):
            continue
        snippet = source_lines[node.lineno - 1].strip()[:120] \
            if 1 <= node.lineno <= len(source_lines) else ""
        violations.append((node.lineno, snippet))

    return violations


def collect_files(paths: list[Path]) -> list[Path]:
    """Expand directories to *.py files; pass-through individual files."""
    out: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            out.append(p)
        elif p.is_dir():
            out.extend(sorted(p.rglob("*.py")))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Flag open() text-mode calls without explicit encoding=.",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Files or directories to scan (default: scripts/tools, tests, "
             "components/da-tools/app).",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: print violations, exit 0 (warn-only) unless "
             "--strict-open-encoding is also given.",
    )
    parser.add_argument(
        "--strict-open-encoding", action="store_true",
        help="Treat violations as errors (exit 1). Default: warn-only.",
    )
    args = parser.parse_args()

    paths = args.paths or DEFAULT_PATHS
    files = collect_files([Path(p) for p in paths])

    total_violations = 0
    by_file: dict[Path, list[tuple[int, str]]] = {}
    for f in files:
        v = scan_file(f)
        if v:
            by_file[f] = v
            total_violations += len(v)

    if total_violations == 0:
        if not args.ci:
            print("OK: no open() calls missing encoding= found.")
        return 0

    # Sort for deterministic output
    for f in sorted(by_file):
        rel = os.path.relpath(f, REPO_ROOT)
        for lineno, snippet in by_file[f]:
            print(f"{rel}:{lineno}: open() missing encoding= — {snippet}")

    print(
        f"\nTotal: {total_violations} violations in {len(by_file)} files.",
        file=sys.stderr,
    )
    print(
        "Fix: add `encoding='utf-8'` keyword arg, or append "
        f"`# {IGNORE_MARKER}` if intentional.",
        file=sys.stderr,
    )

    if args.strict_open_encoding:
        return 1
    if args.ci:
        # Warn-only mode — surface count without blocking the commit.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
