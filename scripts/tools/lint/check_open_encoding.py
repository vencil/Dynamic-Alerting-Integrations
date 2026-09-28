#!/usr/bin/env python3
"""check_open_encoding.py — flag open() and subprocess text-mode calls without encoding=.

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

Second rule: subprocess text mode (#1374)
-----------------------------------------
``subprocess.run/Popen/check_output/check_call/call`` in text mode
(``text=True``, ``universal_newlines=True`` or ``errors=``) without
``encoding=`` decode the child's output with the locale codec — cp950 on a
zh-TW Windows host. There, a UTF-8 byte the codec rejects is raised in the
reader thread and swallowed: the caller gets ``returncode 0`` and
``stdout None``, not an exception. ``subprocess.getoutput`` /
``getstatusoutput`` take no ``encoding=`` at all and are always flagged.

Resolved through ``import subprocess [as x]`` and
``from subprocess import run [as x]``; a bare ``run(...)`` not imported from
subprocess is not looked at. Any ``encoding=`` value passes, ``"locale"``
included: the rule demands that the encoding be stated, not which one.
Text mode is flagged whether or not a stream is piped (an unpiped text-mode
call decodes nothing, but naming the encoding costs one keyword). Fail-closed
where the call cannot be read: ``**kwargs``, a non-literal ``text=`` /
``universal_newlines=``, or more than one positional argument is reported as
unmeasurable, not passed. The ignore marker may sit on any line of the call.

Which encoding to state — it is set by the process that WRITES the bytes:

- git and Go binaries (docker, gh, kubectl, helm, ...) write UTF-8:
  ``encoding="utf-8", errors="replace"``. ``errors="strict"`` buys nothing
  on Windows, where the reader thread swallows the error. Exception: FILE
  NAMES from a ``-z`` listing (``git ls-files -z``, ``diff --name-only -z``)
  are raw path bytes — use ``errors="surrogateescape"`` so a name that is not
  valid UTF-8 keeps its identity; ``replace`` silently renames it. Without
  ``-z``, git C-quotes such names (``core.quotePath``) and the output is ASCII.
- a Python child writes the parent's locale codec unless ITS environment
  sets ``PYTHONIOENCODING=utf-8`` (or ``PYTHONUTF8=1``; ``-X utf8`` on the
  parent is not inherited) — pair ``encoding="utf-8"`` with that env.
- output that is data rather than text (an archive, bytes to hash): drop
  text mode and decode explicitly.
- a Windows-native program (cmd, where, ...) writes the console codepage:
  ``encoding="locale"``.

Python 3.15 turns UTF-8 mode on by default (PEP 686), which fixes the first
case for whoever runs it and breaks the last; an explicit encoding is right
on every version.

Existing sites are frozen in ``docs/internal/subprocess-encoding-baseline.json``
(file → count). Under ``--strict-open-encoding`` each scanned file's count must
EQUAL its ledger row: more is new debt, fewer is a stale row (lower it with
``--write-subprocess-baseline``, which never raises a count or adds a file
once the ledger exists). Rows outside the scanned roots are not compared.
Known blind spot of a per-file count (the same one ESLint's bulk suppressions
have, eslint#21226): fixing one site and adding another in the same file
keeps the count and passes.

Severity model
--------------
- **default mode / --ci**: report violations, exit 0 (warn-only).
- **--strict-open-encoding**: open() violations exit 1; subprocess sites
  exit 1 when a file's count differs from its ledger row, or the ledger is
  missing or malformed.

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

    # Lower the subprocess ledger after fixing sites (never raises a count)
    python3 scripts/tools/lint/check_open_encoding.py --write-subprocess-baseline scripts components/da-tools tests

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
import json
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

SUBPROCESS_BASELINE = REPO_ROOT / "docs" / "internal" / "subprocess-encoding-baseline.json"
SUBPROCESS_TICKET = "#1374"
_SP_FUNCS = frozenset({"run", "Popen", "check_output", "check_call", "call"})
_SP_LOCALE_ONLY = frozenset({"getoutput", "getstatusoutput"})
_TEXT_FLAGS = ("text", "universal_newlines")


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


def _subprocess_bindings(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Names that reach the subprocess module / its calls in this file."""
    modules = {"subprocess"}
    funcs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    modules.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess" and not node.level:
            for alias in node.names:
                if alias.name in _SP_FUNCS | _SP_LOCALE_ONLY:
                    funcs[alias.asname or alias.name] = alias.name
    return modules, funcs


def _subprocess_func(call: ast.Call, modules: set[str], funcs: dict[str, str]) -> str | None:
    """The subprocess function this call resolves to, or None."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in modules:
        name = f.attr
    elif isinstance(f, ast.Name) and f.id in funcs:
        name = funcs[f.id]
    else:
        return None
    return name if name in _SP_FUNCS | _SP_LOCALE_ONLY else None


def _subprocess_verdict(call: ast.Call, func: str) -> str | None:
    """Why this subprocess call is flagged, or None when it states its encoding."""
    if func in _SP_LOCALE_ONLY:
        return f"subprocess.{func}() always decodes with the locale codec (it takes no encoding=)"
    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}
    if "encoding" in keywords:
        return None
    if any(kw.arg is None for kw in call.keywords):
        return "unmeasurable: **kwargs hide whether text mode is on and encoding= given"
    if len(call.args) > 1:
        return "unmeasurable: positional arguments after args= can switch text mode on"
    text = "errors" in keywords
    for flag in _TEXT_FLAGS:
        value = keywords.get(flag)
        if value is None:
            continue
        if not isinstance(value, ast.Constant):
            return f"unmeasurable: {flag}= is not a literal"
        text = text or bool(value.value)
    if not text:
        return None
    return "text mode without encoding= decodes with the locale codec"


def _span_has_ignore(source_lines: list[str], call: ast.Call) -> bool:
    """True if any line of the call carries the ignore marker."""
    end = getattr(call, "end_lineno", None) or call.lineno
    return any(_line_has_ignore(source_lines, n) for n in range(call.lineno, end + 1))


def scan_subprocess(path: Path) -> list[tuple[int, str, str]]:
    """Return (lineno, reason, snippet) for each subprocess call flagged."""
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    source_lines = source.splitlines()
    modules, funcs = _subprocess_bindings(tree)
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = _subprocess_func(node, modules, funcs)
        if func is None:
            continue
        reason = _subprocess_verdict(node, func)
        if reason is None or _span_has_ignore(source_lines, node):
            continue
        snippet = source_lines[node.lineno - 1].strip()[:120] \
            if 1 <= node.lineno <= len(source_lines) else ""
        found.append((node.lineno, reason, snippet))
    return sorted(found)


def ledger_key(path: Path) -> str:
    """How the ledger names a file: repo-relative POSIX, else absolute POSIX."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _key_in_roots(key: str, roots: list[Path]) -> bool:
    """True if ledger row *key* names a file inside one of the scanned roots."""
    p = Path(key)
    p = (p if p.is_absolute() else REPO_ROOT / p).resolve()
    for root in roots:
        r = root.resolve()
        if p == r or r in p.parents:
            return True
    return False


def load_subprocess_baseline(path: Path) -> tuple[dict[str, int] | None, list[str]]:
    """(rows, errors). rows is None when the ledger is missing or unreadable."""
    if not path.is_file():
        return None, [f"subprocess ledger {path} is missing"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"subprocess ledger {path} is not valid JSON: {exc}"]
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return None, [f"subprocess ledger {path} must be an object with a `files` object"]
    rows: dict[str, int] = {}
    errors: list[str] = []
    for key, count in files.items():
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            errors.append(f"subprocess ledger row {key}: count must be a positive "
                          f"integer, got {count!r}")
            continue
        rows[key] = count
    return rows, errors


def compare_subprocess_baseline(counts: dict[str, int], rows: dict[str, int],
                                roots: list[Path]) -> list[str]:
    """Every scanned file's count must equal its ledger row (absent = 0)."""
    errors: list[str] = []
    for key, n in sorted(counts.items()):
        allowed = rows.get(key, 0)
        if n > allowed:
            errors.append(f"{key}: {n} subprocess call(s) flagged, ledger allows "
                          f"{allowed} — {n - allowed} are new; state the encoding "
                          f"(the ledger only shrinks)")
    for key, allowed in sorted(rows.items()):
        if not _key_in_roots(key, roots):
            continue
        n = counts.get(key, 0)
        if n < allowed:
            errors.append(f"stale ledger row {key}: ledger says {allowed}, {n} remain — "
                          f"lower it with --write-subprocess-baseline")
    return errors


def write_subprocess_baseline(counts: dict[str, int], rows: dict[str, int] | None,
                              roots: list[Path], path: Path) -> list[str]:
    """Rewrite the ledger from *counts*; return the increases it refused.

    With no ledger yet every count is written (bootstrap). Once it exists a
    count is only ever lowered or dropped: an increase or a new file keeps
    its old row (or stays out) and is returned, so the caller exits 1.
    """
    new = {k: v for k, v in (rows or {}).items() if not _key_in_roots(k, roots)}
    refused: list[str] = []
    for key, n in sorted(counts.items()):
        if rows is None:
            new[key] = n
            continue
        allowed = rows.get(key, 0)
        if n > allowed:
            refused.append(f"{key}: {n} flagged, ledger allows {allowed} — not raised")
            if allowed:
                new[key] = allowed
        else:
            new[key] = n
    data = {
        "_comment": [
            "subprocess text mode without encoding= (check_open_encoding.py, "
            f"{SUBPROCESS_TICKET}). file -> count of flagged calls.",
            "Shrink-only: fix a site, then lower the count with "
            "--write-subprocess-baseline (a row at 0 is dropped).",
        ],
        "ticket": SUBPROCESS_TICKET,
        "files": dict(sorted(new.items())),
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8", newline="\n")
    return refused


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
    parser.add_argument(
        "--subprocess-baseline", type=Path, default=SUBPROCESS_BASELINE,
        help="Ledger of existing subprocess text-mode sites "
             "(default: docs/internal/subprocess-encoding-baseline.json).",
    )
    parser.add_argument(
        "--write-subprocess-baseline", action="store_true",
        help="Rewrite the subprocess ledger for the scanned roots: counts are "
             "only lowered (bootstraps the file if missing); exit 1 if any "
             "increase was refused.",
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
    sp_by_file: dict[str, list[tuple[int, str, str]]] = {}
    for f in files:
        v = scan_file(f)
        if v:
            by_file[f] = v
            total_violations += len(v)
        sp = scan_subprocess(f)
        if sp:
            sp_by_file[ledger_key(f)] = sp
    sp_counts = {k: len(v) for k, v in sp_by_file.items()}

    if args.write_subprocess_baseline:
        rows, errors = load_subprocess_baseline(args.subprocess_baseline)
        if rows is None and args.subprocess_baseline.exists():
            for e in errors:
                print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_VIOLATION
        refused = write_subprocess_baseline(sp_counts, rows, paths, args.subprocess_baseline)
        for r in refused:
            print(f"REFUSED: {r}", file=sys.stderr)
        print(f"Wrote {args.subprocess_baseline}: "
              f"{sum(sp_counts.values())} flagged subprocess call(s) in {len(sp_counts)} files.")
        return EXIT_VIOLATION if refused else EXIT_OK

    # Sort for deterministic output
    for f in sorted(by_file):
        rel = os.path.relpath(f, REPO_ROOT)
        for lineno, snippet in by_file[f]:
            print(f"{rel}:{lineno}: open() missing encoding= — {snippet}")

    if total_violations:
        print(
            f"\nTotal: {total_violations} violations in {len(by_file)} files.",
            file=sys.stderr,
        )
        print(
            "Fix: add `encoding='utf-8'` keyword arg, or append "
            f"`# {IGNORE_MARKER}` if intentional.",
            file=sys.stderr,
        )

    sp_errors: list[str] = []
    if args.strict_open_encoding:
        rows, ledger_errors = load_subprocess_baseline(args.subprocess_baseline)
        sp_errors = ledger_errors + (
            compare_subprocess_baseline(sp_counts, rows, paths) if rows is not None else [])
        # Only the files the ledger does not cover are printed site by site;
        # the rest are known debt and would bury the new ones.
        show = {k for k, n in sp_counts.items() if rows is None or n > rows.get(k, 0)}
    else:
        show = set(sp_counts)
    for key in sorted(show):
        for lineno, reason, snippet in sp_by_file[key]:
            print(f"{key}:{lineno}: subprocess {reason} — {snippet}")
    for e in sp_errors:
        print(f"ERROR: {e}", file=sys.stderr)
    if sp_errors:
        print(
            "Fix: state the encoding the child WRITES (see this tool's docstring), "
            f"or append `# {IGNORE_MARKER}` if intentional.",
            file=sys.stderr,
        )

    if args.strict_open_encoding and (total_violations or sp_errors):
        return EXIT_VIOLATION
    if not total_violations and not sp_counts and not args.ci:
        print("OK: no open() or subprocess calls missing encoding= found.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
