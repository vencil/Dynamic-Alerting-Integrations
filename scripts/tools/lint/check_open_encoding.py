#!/usr/bin/env python3
"""check_open_encoding.py — flag open() text-mode calls without encoding=.

Why this exists
---------------
PR-2.5 (v2.8.0) root-caused Tier 1 test failures to ``open(path)`` calls
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

Severity model
--------------
- **default mode / --ci**: report violations, exit 0 (warn-only).
- **--strict-open-encoding**: violations exit 1.

The pre-commit hook passes ``--strict-open-encoding`` plus explicit scan
roots, so it blocks only under the roots it names; which roots those are
lives in ``.pre-commit-config.yaml`` and nowhere else (#1984). A scan root
that does not exist exits 2: a typo or a renamed directory would otherwise
scan zero files and exit 0, indistinguishable from a clean tree.

Usage
-----
::

    # Local audit
    python3 scripts/tools/lint/check_open_encoding.py

    # Specific paths
    python3 scripts/tools/lint/check_open_encoding.py path/to/file.py ...

    # Hard gate over explicit roots (what the pre-commit hook does)
    python3 scripts/tools/lint/check_open_encoding.py --ci --strict-open-encoding scripts

Exit codes
----------
::

    0 — no violations, OR violations without --strict-open-encoding
    1 — violations found AND --strict-open-encoding
    2 — a given scan path does not exist
"""
from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

# Pull `try_utf8_stdout` from the shared compat lib at scripts/tools/.
# Migrated in #489 Phase B (was missing encoding setup → would crash on
# legacy Windows cp950/cp936 consoles when printing emoji to stdout).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK, EXIT_VIOLATION  # noqa: E402

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
    try_utf8_stdout()
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

    paths = [Path(p) for p in (args.paths or DEFAULT_PATHS)]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: scan path does not exist: {p}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    files = collect_files(paths)

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
        return EXIT_OK

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
        return EXIT_VIOLATION
    if args.ci:
        # Warn-only mode — surface count without blocking the commit.
        return EXIT_OK
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
