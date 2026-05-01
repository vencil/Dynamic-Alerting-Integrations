#!/usr/bin/env python3
"""check_subprocess_timeout.py — flag subprocess calls without explicit timeout.

Why this exists
---------------
PR #164 (S#74) root-caused a 14-min pre-commit hook hang to a classic
single-thread Popen pipe deadlock in ``_batch_cat_blobs``: the function
wrote stdin then read stdout in a loop without a thread, and once total
``git cat-file --batch`` output exceeded the OS pipe buffer git blocked
on stdout-write while the script was still reading body bytes from a
different path. The fix used ``proc.communicate(input=request,
timeout=60)`` which threads stdin write + stdout/stderr drain.

S#74 codified the lesson — "subprocess hang must have an SLA prior +
escalation policy" — and identified the most code-driven follow-up:
a lint rule that flags subprocess calls without an explicit ``timeout=``
kwarg. **This script is that rule** (Code-driven Layer A).

What it flags
-------------
Two heuristic classes, both backed by AST walking (no string matching,
no false positives from comments / strings):

1. ``subprocess.{run, call, check_call, check_output}(...)`` without
   ``timeout=`` keyword argument.

2. ``<expr>.communicate(...)`` without ``timeout=`` — the
   ``Popen.communicate()`` API is the canonical hang-prone call site
   (with or without ``Popen.wait()`` follow-up). Heuristic note: any
   ``.communicate(...)`` is flagged, not only those provably called on
   a ``subprocess.Popen`` instance — type tracking would require full
   inference. False positives are accepted (rare; ``.communicate()``
   outside subprocess is uncommon) and dismissable via per-line
   ignore comment.

Per-line ignore: append ``# subprocess-timeout: ignore`` to the line
containing the call. Useful for: short-lived commands where hang is
impossible (e.g. ``subprocess.run(["git", "rev-parse", "HEAD"])``),
intentional infinite-wait subprocess pairs, and constructor-pattern
calls that don't accept timeout (``subprocess.Popen(...)`` itself —
its constructor has no timeout; the timeout belongs on the later
``.communicate()`` / ``.wait()``).

Severity model (mirrors lint_jsx_babel.py from PR #162 / PR #154)
------------------------------------------------------------------

The codebase has **218 existing subprocess call sites** as of v2.8.0
PR #165 audit. Cleaning all of them in one PR is impractical, so this
linter ships with **granular --strict-subprocess-timeout flag** rather
than as a default-fatal rule. The split:

- **default mode** (no flags): report violations to stdout, exit 0.
  Useful for quick local audit (``python check_subprocess_timeout.py``).
- **--ci**: report violations + exit 0 (non-fatal). Pre-commit
  surfaces the count without blocking commits during the cleanup
  track.
- **--ci --strict-subprocess-timeout**: violations are fatal.
  Activated only after the 218-instance cleanup is complete (tracked
  in a follow-up issue, like PR #162 did with --strict-static for
  static-pattern violations).

Usage
-----
::

    # Local audit
    python3 scripts/tools/lint/check_subprocess_timeout.py

    # Specific paths
    python3 scripts/tools/lint/check_subprocess_timeout.py path/to/file.py ...

    # CI / pre-commit (warn-only)
    python3 scripts/tools/lint/check_subprocess_timeout.py --ci

    # Future: post-cleanup hard gate
    python3 scripts/tools/lint/check_subprocess_timeout.py --ci --strict-subprocess-timeout

Exit codes
----------
- ``0``  — no violations OR --ci without --strict-subprocess-timeout
- ``1``  — violations found AND (--ci AND --strict-subprocess-timeout)
- ``2``  — bad arguments / unreadable source

S#74 reference: ``docs/internal/testing-playbook.md`` v2.8.0
Lessons §4. PR #164 / PR #165 establish the underlying pattern.
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Functions in the subprocess module that accept ``timeout=`` and where
# omitting it makes hang-on-deadlock possible.
_SUBPROCESS_FNS_WITH_TIMEOUT = frozenset({
    "run",
    "call",
    "check_call",
    "check_output",
})

# Per-line ignore marker — appears at the end of the line containing
# the offending call.
_IGNORE_COMMENT = "subprocess-timeout: ignore"

# Default scan roots — every Python source under these dirs.
_DEFAULT_SCAN_ROOTS = (
    "scripts",
    "components/da-tools",
    "tests",
)


@dataclass
class TimeoutViolation:
    """A single subprocess call without an explicit ``timeout=`` kwarg."""

    path: Path
    line: int
    col: int
    rule: str  # "subprocess-fn" | "communicate"
    snippet: str

    def render(self) -> str:
        rel = self.path.relative_to(PROJECT_ROOT) if self.path.is_absolute() else self.path
        return f"{rel}:{self.line}:{self.col} [{self.rule}] {self.snippet}"


def _has_timeout_kwarg(call: ast.Call) -> bool:
    """True if the call has a ``timeout=...`` keyword arg."""
    return any(kw.arg == "timeout" for kw in call.keywords)


def _is_subprocess_fn_call(call: ast.Call) -> str | None:
    """Return the function name if this is ``subprocess.<fn>(...)``,
    else None.

    Matches ``subprocess.run`` / ``subprocess.call`` etc. Does NOT
    match ``run(...)`` from ``from subprocess import run`` because the
    bare-name case is hard to disambiguate without imports tracking;
    bare-import is a stylistic anti-pattern in the codebase anyway
    (per dev-rules: prefer ``import subprocess; subprocess.run(...)``
    for explicit attribution).
    """
    if not isinstance(call.func, ast.Attribute):
        return None
    if call.func.attr not in _SUBPROCESS_FNS_WITH_TIMEOUT:
        return None
    val = call.func.value
    if isinstance(val, ast.Name) and val.id == "subprocess":
        return call.func.attr
    return None


def _is_communicate_call(call: ast.Call) -> bool:
    """True if this is ``<expr>.communicate(...)``."""
    return isinstance(call.func, ast.Attribute) and call.func.attr == "communicate"


_IGNORE_LOOKBACK_LINES = 3


def _line_has_ignore(source_lines: list[str], line_no: int) -> bool:
    """True if the call's line OR up to 3 lines above contain the marker.

    Three-line lookback covers multi-line rationale comment blocks like::

        # Test fixture wrapper for local git commands that complete
        # in milliseconds. Per S#74 lint rule, explicit timeout would
        # just be noise — silenced via marker.
        # subprocess-timeout: ignore
        return subprocess.run(...)

    The marker line itself can be ABOVE the explanatory text (cleaner)
    or BELOW it adjacent to the call. Any of {N, N-1, N-2, N-3} is
    accepted.

    Lookback intentionally bounded at 3 to keep "ignore radius" tight —
    a marker far upstream of the actual call is too easy to forget /
    misattribute / leave dangling after refactor.
    """
    for offset in range(_IGNORE_LOOKBACK_LINES + 1):
        candidate = line_no - offset
        if 1 <= candidate <= len(source_lines):
            if _IGNORE_COMMENT in source_lines[candidate - 1]:
                return True
    return False


def scan_source(path: Path, source: str) -> list[TimeoutViolation]:
    """Walk a Python source string and return all violations.

    Robust to syntax errors (returns empty list rather than crashing —
    the lint should not block commits because some other file has a
    parse error; that's caught by other lints).
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    source_lines = source.splitlines()
    violations: list[TimeoutViolation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Class A: subprocess.run / subprocess.call / etc.
        fn_name = _is_subprocess_fn_call(node)
        if fn_name and not _has_timeout_kwarg(node):
            if not _line_has_ignore(source_lines, node.lineno):
                snippet = (
                    source_lines[node.lineno - 1].strip()
                    if 1 <= node.lineno <= len(source_lines)
                    else ""
                )
                violations.append(
                    TimeoutViolation(
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        rule=f"subprocess.{fn_name}-no-timeout",
                        snippet=snippet[:120],
                    )
                )
            continue  # don't double-flag a single call

        # Class B: x.communicate(...)
        if _is_communicate_call(node) and not _has_timeout_kwarg(node):
            if not _line_has_ignore(source_lines, node.lineno):
                snippet = (
                    source_lines[node.lineno - 1].strip()
                    if 1 <= node.lineno <= len(source_lines)
                    else ""
                )
                violations.append(
                    TimeoutViolation(
                        path=path,
                        line=node.lineno,
                        col=node.col_offset + 1,
                        rule="communicate-no-timeout",
                        snippet=snippet[:120],
                    )
                )

    return violations


def _iter_python_files(roots: Iterable[Path]) -> Iterator[Path]:
    """Yield every ``*.py`` file under the given roots, deterministic order."""
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            yield root
        elif root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _resolve_scan_paths(args: argparse.Namespace) -> list[Path]:
    """Resolve CLI-arg paths or fall back to default roots."""
    if args.paths:
        return [Path(p) if Path(p).is_absolute() else PROJECT_ROOT / p for p in args.paths]
    return [PROJECT_ROOT / r for r in _DEFAULT_SCAN_ROOTS if (PROJECT_ROOT / r).exists()]


def _compute_exit_code(*, ci: bool, strict_subprocess_timeout: bool, n_violations: int) -> int:
    """Pure helper for unit-testable severity routing.

    Severity matrix:

    | --ci  | --strict-subprocess-timeout | violations | exit |
    |-------|-----------------------------|------------|------|
    | False | *                           | *          | 0    |
    | True  | False                       | *          | 0    |
    | True  | True                        | 0          | 0    |
    | True  | True                        | >0         | 1    |
    """
    if not ci:
        return 0
    if not strict_subprocess_timeout:
        return 0
    return 1 if n_violations > 0 else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Flag subprocess calls (subprocess.run/call/check_*/communicate) "
            "without explicit timeout= kwarg. See script docstring for the "
            "S#74 PR #164 deadlock motivation."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files or directories to scan. Defaults to scripts/ + components/da-tools/ + tests/.",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit non-zero on violations (only with --strict-subprocess-timeout).",
    )
    parser.add_argument(
        "--strict-subprocess-timeout",
        action="store_true",
        help=(
            "Make violations fatal under --ci. Activate only after the 218-instance "
            "v2.8.0 audit cleanup track is complete (mirrors PR #162's --strict-static)."
        ),
    )
    args = parser.parse_args()

    scan_paths = _resolve_scan_paths(args)
    if not scan_paths:
        print("No paths to scan.", file=sys.stderr)
        return 2

    all_violations: list[TimeoutViolation] = []
    for py_file in _iter_python_files(scan_paths):
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"⚠ cannot read {py_file}: {exc}", file=sys.stderr)
            continue
        all_violations.extend(scan_source(py_file, source))

    if not all_violations:
        if args.ci:
            print("✓ no subprocess calls without timeout= found")
        return 0

    by_file: dict[Path, list[TimeoutViolation]] = {}
    for v in all_violations:
        by_file.setdefault(v.path, []).append(v)

    severity = "ERROR" if args.strict_subprocess_timeout else "WARN"
    print(
        f"{severity}: {len(all_violations)} subprocess call(s) without timeout= "
        f"in {len(by_file)} file(s):",
    )
    for path in sorted(by_file):
        for v in by_file[path]:
            print(f"  {v.render()}")

    print()
    print(
        "Add `timeout=N` to each call, OR add `# subprocess-timeout: ignore` "
        "on the call line if hang is impossible."
    )
    print("See S#74 (testing-playbook.md v2.8.0 Lessons §4) for rationale.")

    return _compute_exit_code(
        ci=args.ci,
        strict_subprocess_timeout=args.strict_subprocess_timeout,
        n_violations=len(all_violations),
    )


if __name__ == "__main__":
    raise SystemExit(main())
