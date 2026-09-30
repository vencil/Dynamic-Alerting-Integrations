#!/usr/bin/env python3
"""check_open_encoding.py — text-I/O hygiene: encoding= on open()/subprocess, newline= on text writes.

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

Third rule: line endings on text writes (#1366)
-----------------------------------------------
Every call that WRITES text must state its line-ending policy with a
string-literal ``newline=``. Without it Python's text layer turns each
``\\n`` into the platform separator, so the same generator emits LF on
Linux/CI and CRLF on a Windows host. The conversion is compiled into
CPython on Windows (``#ifdef MS_WINDOWS``), not looked up in ``os.linesep``
at run time, so no behavioural test on a Linux runner can see the bug —
this static rule is the only cross-platform gate.

The rule is derived from the operation, not from a list of names: any call
that hands back a writable TEXT handle, or writes text directly, must state
``newline=``:

- ``write_text(...)`` and ``TextIOWrapper(...)`` — always text;
- ``open`` / ``Path.open`` / ``io.open`` / ``gzip.open`` / ``os.fdopen`` /
  ``NamedTemporaryFile`` / ``TemporaryFile`` / ``SpooledTemporaryFile`` when
  the mode is text and writes (``w`` / ``a`` / ``x`` / ``+``); binary and
  read-only modes are skipped. A mode that is not a literal fails closed,
  but only when ``encoding=`` proves the call is a text stream;
- ``atomic_write_text`` pins LF itself and is flagged only for an explicit
  ``newline=None``.

The value is the author's choice (``"\\n"``, ``""`` for the csv module,
``"\\r\\n"``), but it must be a string literal: ``newline=None`` or a
variable / ``os.linesep`` is flagged, since those can still be CRLF.
``<module>.open()`` of os / tarfile / zipfile / shelve / dbm / sqlite3 /
webbrowser / socket is not a text-file handle and is skipped.

Per-line ignore: ``# line-ending: ignore`` on the call's FIRST line — only
for a call that has no literal to give (a helper passing its own
``newline`` parameter through). It silences this rule only; the encoding
marker silences the encoding rules only.

Scope: this rule scans the ``--line-ending-root`` trees (repeatable). The
hook names shipped and production Python (``scripts``, ``components``,
``helm``) and leaves ``tests/`` out on purpose: tests write tmp fixtures that
nothing downstream consumes. Without ``--line-ending-root`` the rule scans
the positional paths, or ``scripts`` / ``components`` / ``helm`` when none
are given.

One report per call: when a builtin ``open()`` misses both ``encoding=``
and ``newline=``, it is printed once, naming both keywords. It still counts
towards each rule's total, since each rule has its own severity switch.

Severity model
--------------
- **default mode / --ci**: report violations, exit 0 (warn-only).
- **--strict-open-encoding**: open() violations exit 1; subprocess sites
  exit 1 when a file's count differs from its ledger row, or the ledger is
  missing or malformed.
- **--strict-line-ending**: line-ending violations exit 1.

The pre-commit hook passes both strict flags plus explicit scan roots, so it
blocks only under the roots it names; which roots those are lives in
``.pre-commit-config.yaml`` and nowhere else (#1984). A scan root that does
not exist exits 2: a typo or a renamed directory would otherwise scan zero
files and exit 0, indistinguishable from a clean tree.

Usage
-----
::

    # Local audit
    python3 scripts/tools/lint/check_open_encoding.py

    # Specific paths (both rule families)
    python3 scripts/tools/lint/check_open_encoding.py path/to/file.py ...

    # Hard gate over explicit roots (what the pre-commit hook does)
    python3 scripts/tools/lint/check_open_encoding.py --ci --strict-open-encoding \\
        --strict-line-ending --line-ending-root scripts --line-ending-root helm scripts

    # Lower the subprocess ledger after fixing sites (never raises a count)
    python3 scripts/tools/lint/check_open_encoding.py --write-subprocess-baseline scripts components/da-tools tests

Exit codes
----------
::

    0 — no violations, OR violations whose rule is not in strict mode
    1 — violations found under --strict-open-encoding / --strict-line-ending
    2 — a given scan path does not exist
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass, field
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

# ── line-ending rule (#1366) ─────────────────────────────────────────────
# Default roots of the line-ending rule when neither --line-ending-root nor
# positional paths are given. The hook names its own roots.
LINE_ENDING_DEFAULT_PATHS = [
    REPO_ROOT / "scripts",
    REPO_ROOT / "components",
    REPO_ROOT / "helm",
]

LINE_ENDING_IGNORE_MARKER = "line-ending: ignore"

# ⛔ This table lists WRAPPERS only, not the syntactic shapes of opening a
# file. The first version of the rule was "the names open / write_text" — an
# enumeration, not a derivation: Path.open("w") / os.fdopen(fd, "w") /
# NamedTemporaryFile("w") / io.open / gzip.open(..., "wt") all walked past it
# (20 of 22 shapes missed, 6 real unpinned sites in the tree at the time).
# Now the rule first asks "does this call hand back a writable TEXT handle?"
# and names only decide what the default mode is.
#
# atomic_write_text pins LF itself (default newline="\n"), so callers need not
# pass it; but an explicit newline=None asks for the platform default back
# (generate_tool_map.py was bitten exactly that way), so that shape is flagged.
LF_PINNING_WRAPPERS = frozenset({"atomic_write_text"})

# Calls that hand back a file handle -> their mode when none is given.
# "b" in the mode is binary (no newline translation).
HANDLE_FACTORIES = {
    "open": "r",          # builtin open / Path.open / io.open / gzip.open / codecs.open
    "fdopen": "r",        # os.fdopen
    "NamedTemporaryFile": "w+b",
    "TemporaryFile": "w+b",
    "SpooledTemporaryFile": "w+b",
}

# Constructors that produce a text handle and take NO mode: always in scope.
# io.TextIOWrapper(buf, encoding=...) defaults to newline=None — the very bug
# this rule exists for.
TEXT_WRAPPER_FACTORIES = frozenset({"TextIOWrapper"})

# `.open()` is borrowed by many unrelated APIs. On these modules it is NOT a
# text-file handle (int fd / binary stream / DB handle / not a file at all);
# they do not even accept newline=, so flagging them would give advice that
# raises TypeError if followed.
NON_TEXT_OPEN_MODULES = frozenset({
    "os",          # os.open -> int fd
    "tarfile", "zipfile", "shelve", "dbm", "sqlite3",
    "webbrowser",  # not a file at all
    "socket",
})

# Modules whose open() takes (file, mode) like the builtin. Where the mode can
# sit decides the first positional index scanned for it:
#   open(file, mode) / io.open(file, mode) / os.fdopen(fd, mode) -> 1
#   Path.open(mode) / NamedTemporaryFile(mode)                   -> 0
# ⛔ The offset is load-bearing: scanning from 0 would read open("r", "w")
# (a file literally named "r") as mode "r", i.e. read-only, and pass it — a
# fail-open hole in a rule that is fail-closed everywhere else.
_MODULE_STYLE_OPEN = frozenset({"io", "gzip", "codecs", "bz2", "lzma", "fileinput"})

_MODE_RE = re.compile(r"^[rwxa]\+?[btU]*\+?$")
_UNRESOLVED_MODE = object()

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


def _open_encoding_violation(call: ast.Call) -> bool:
    """True if this is a builtin open() in text mode without encoding=."""
    return (_is_open_call(call) and not _has_binary_mode(call)
            and not _has_encoding_kwarg(call))


# ── line-ending rule helpers (#1366) ─────────────────────────────────────

def _get_kwarg(call: ast.Call, name: str) -> ast.keyword | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw
    return None


def _mode_arg_start(call: ast.Call, name: str) -> int:
    """First positional index that could hold a mode string."""
    if name == "fdopen":
        return 1
    if name == "open":
        fn = call.func
        if isinstance(fn, ast.Name):
            return 1  # builtin open(file, mode)
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) \
                and fn.value.id in _MODULE_STYLE_OPEN:
            return 1  # io.open(file, mode) etc.
        return 0      # Path.open(mode)
    return 0          # tempfile factories take mode first


def _mode_of(call: ast.Call, default: str, name: str):
    """Resolve the file mode of a handle-producing call.

    Returns the mode string, or ``_UNRESOLVED_MODE`` when a mode is supplied
    but is not a static literal (e.g. ``open(p, mode_var)``).

    ⛔ "a mode is given but unreadable" and "no mode is given" must be
    DIFFERENT return values. The earliest version returned None for both and
    passed them, so ``open(p, mode_var)`` went through silently. Now the first
    fails closed and only the second takes the default.
    """
    kw = _get_kwarg(call, "mode")
    if kw is not None:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
        return _UNRESOLVED_MODE

    saw_non_literal = False
    for arg in call.args[_mode_arg_start(call, name):]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if _MODE_RE.match(arg.value):
                return arg.value
        elif not isinstance(arg, ast.Constant):
            saw_non_literal = True
    if saw_non_literal:
        return _UNRESOLVED_MODE
    return default


def _newline_verdict(kw: ast.keyword | None) -> str | None:
    """Classify a ``newline=`` keyword. None if acceptable, else a reason.

    ⛔ Passing newline= is not the same as pinning the line ending.
    ``newline=os.linesep`` writes the original bug back while looking
    compliant (measured: it produces CRLF) — and it is exactly what someone
    fixing this bug is most likely to write. So only a string LITERAL passes;
    any expression (a variable, os.linesep, an attribute) must become a
    literal or carry the ignore marker.
    """
    if kw is None:
        return "no newline= (platform default -> CRLF on Windows)"
    if isinstance(kw.value, ast.Constant):
        if kw.value.value is None:
            return "newline=None (explicit platform default)"
        if isinstance(kw.value.value, str):
            return None  # "\n" / "" / anything the author literally chose
    return (
        f"newline={ast.unparse(kw.value)} is not a string literal — "
        f"os.linesep / a variable can still be CRLF"
    )


def _line_ending_verdict(call: ast.Call) -> str | None:
    """Why this call breaks the line-ending rule, or None."""
    fn = call.func
    if isinstance(fn, ast.Attribute):
        name = fn.attr
    elif isinstance(fn, ast.Name):
        name = fn.id
    else:
        return None

    # <module>.open() whose open() is not a text-file handle. ⚠️ Scoped to the
    # name `open` on purpose: os.open is excluded but os.fdopen is a genuine
    # text-handle factory and must stay in scope (an earlier version excluded
    # the whole `os` module and silently lost os.fdopen).
    if name == "open" and isinstance(fn, ast.Attribute) \
            and isinstance(fn.value, ast.Name) \
            and fn.value.id in NON_TEXT_OPEN_MODULES:
        return None

    newline_kw = _get_kwarg(call, "newline")

    if name == "write_text" or name in TEXT_WRAPPER_FACTORIES:
        # Path.write_text: always text, always writing. TextIOWrapper: a text
        # handle by construction, no mode argument at all.
        verdict = _newline_verdict(newline_kw)
        return f"{name}(): {verdict}" if verdict else None

    if name in HANDLE_FACTORIES:
        mode = _mode_of(call, HANDLE_FACTORIES[name], name)
        if mode is _UNRESOLVED_MODE:
            # ⛔ Fail closed ONLY with positive evidence of a text stream — an
            # encoding= kwarg. Without it a dynamic-mode text open cannot be
            # told from os.open(p, flags, 0o600) or a binary API, and guessing
            # flags calls that have no newline= parameter to pin.
            if _get_kwarg(call, "encoding") is None:
                return None
            return (f"{name}(): mode is not a literal, so it cannot be proven "
                    f"read-only or binary — pin newline= or mark ignore")
        if "b" in mode or not any(c in mode for c in "wax+"):
            return None  # binary, or read-only text: nothing is translated
        verdict = _newline_verdict(newline_kw)
        return f"{name}(..., {mode!r}): {verdict}" if verdict else None

    if name in LF_PINNING_WRAPPERS:
        # The helper pins LF internally; only an explicit opt-out is a problem.
        if newline_kw is not None and isinstance(newline_kw.value, ast.Constant) \
                and newline_kw.value.value is None:
            return (f"{name}(newline=None) — opts back out of the helper's "
                    f'newline="\\n" default')
    return None


# ── one parse, one walk, every rule ──────────────────────────────────────

@dataclass
class FileScan:
    """Everything one file contributes. Rows carry (lineno, col) so a call
    flagged by two rules can be reported once."""
    open_encoding: list[tuple[int, int, str]] = field(default_factory=list)
    line_ending: list[tuple[int, int, str, str]] = field(default_factory=list)
    subprocess: list[tuple[int, str, str]] = field(default_factory=list)


def _snippet(source_lines: list[str], lineno: int) -> str:
    if 1 <= lineno <= len(source_lines):
        return source_lines[lineno - 1].strip()[:120]
    return ""


def scan_source(path: Path, *, encoding: bool = True,
                line_ending: bool = True) -> FileScan:
    """Parse *path* once and apply the selected rule families in one AST walk.

    A file that is not UTF-8 or does not parse contributes nothing: the
    offending file should not turn the gate into a traceback about the gate.
    """
    result = FileScan()
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return result
    source_lines = source.splitlines()

    sp_modules = {"subprocess"}
    sp_funcs: dict[str, str] = {}
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    sp_modules.add(alias.asname or "subprocess")
            continue
        if isinstance(node, ast.ImportFrom):
            if node.module == "subprocess" and not node.level:
                for alias in node.names:
                    if alias.name in _SP_FUNCS | _SP_LOCALE_ONLY:
                        sp_funcs[alias.asname or alias.name] = alias.name
            continue
        if not isinstance(node, ast.Call):
            continue
        if encoding:
            calls.append(node)  # subprocess bindings are only complete after the walk
            if _open_encoding_violation(node) \
                    and not _line_has_ignore(source_lines, node.lineno):
                result.open_encoding.append(
                    (node.lineno, node.col_offset, _snippet(source_lines, node.lineno)))
        if line_ending:
            reason = _line_ending_verdict(node)
            if reason is not None and not (
                    1 <= node.lineno <= len(source_lines)
                    and LINE_ENDING_IGNORE_MARKER in source_lines[node.lineno - 1]):
                result.line_ending.append(
                    (node.lineno, node.col_offset, reason,
                     _snippet(source_lines, node.lineno)))

    for node in calls:
        func = _subprocess_func(node, sp_modules, sp_funcs)
        if func is None:
            continue
        reason = _subprocess_verdict(node, func)
        if reason is None or _span_has_ignore(source_lines, node):
            continue
        result.subprocess.append((node.lineno, reason, _snippet(source_lines, node.lineno)))

    result.open_encoding.sort()
    result.line_ending.sort()
    result.subprocess.sort()
    return result


def scan_file(path: Path) -> list[tuple[int, str]]:
    """(lineno, snippet) for each builtin open() missing encoding=."""
    return [(ln, snip) for ln, _col, snip
            in scan_source(path, line_ending=False).open_encoding]


def scan_line_endings(path: Path) -> list[tuple[int, str]]:
    """(lineno, reason) for each text write that does not pin newline=."""
    return [(ln, reason) for ln, _col, reason, _snip
            in scan_source(path, encoding=False).line_ending]


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
    return scan_source(path, line_ending=False).subprocess


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


_LINE_ENDING_FIX = (
    'Fix: pass newline="\\n" (repo standard — .gitattributes pins '
    "`* text=auto eol=lf`). Any explicit string literal is accepted — the "
    "rule requires a stated policy, not LF specifically:\n"
    '  - csv via the csv module      -> newline=""\n'
    '  - must be CRLF on every host  -> newline="\\r\\n" (.bat/.cmd/.ps1 are '
    "the only paths .gitattributes marks eol=crlf; do NOT reach for the "
    "ignore marker, which restores the platform default and so is LF on "
    "Linux)\n"
    "Only if the call genuinely has no literal to give (a helper passing its "
    "own newline parameter through), append "
    f"`# {LINE_ENDING_IGNORE_MARKER}` on the call's first line."
)


def main() -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Text-I/O hygiene: flag open() / subprocess text mode without "
                    "encoding=, and text writes without a literal newline=.",
    )
    parser.add_argument(
        "paths", nargs="*", type=Path,
        help="Files or directories to scan (default: scripts/tools, tests, "
             "components/da-tools/app).",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: print violations, exit 0 (warn-only) unless a "
             "--strict-* flag is also given.",
    )
    parser.add_argument(
        "--strict-open-encoding", action="store_true",
        help="Treat encoding violations as errors (exit 1). Default: warn-only.",
    )
    parser.add_argument(
        "--strict-line-ending", action="store_true",
        help="Treat line-ending (newline=) violations as errors (exit 1). "
             "Default: warn-only.",
    )
    parser.add_argument(
        "--line-ending-root", action="append", type=Path, default=None,
        metavar="PATH",
        help="Scan root of the line-ending rule (repeatable). Default: the "
             "positional paths, or scripts, components and helm when none are given.",
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
    le_paths = [Path(p) for p in (
        args.line_ending_root or args.paths or LINE_ENDING_DEFAULT_PATHS)]
    missing = [p for p in dict.fromkeys(paths + le_paths) if not p.exists()]
    if missing:
        for p in missing:
            print(f"ERROR: scan path does not exist: {p}", file=sys.stderr)
        return EXIT_CALLER_ERROR

    # Each rule family keeps its own roots; a file under both is parsed once.
    enc_files = {f.resolve(): f for f in collect_files(paths)}
    le_files = ({} if args.write_subprocess_baseline
                else {f.resolve(): f for f in collect_files(le_paths)})
    all_files = sorted({**le_files, **enc_files}.items())

    enc_total = 0
    le_total = 0
    # rel -> (lineno, col) -> [encoding flagged?, line-ending reason, snippet]
    report: dict[str, dict[tuple[int, int], list]] = {}
    sp_by_file: dict[str, list[tuple[int, str, str]]] = {}
    for key, f in all_files:
        scan = scan_source(f, encoding=key in enc_files, line_ending=key in le_files)
        rel = os.path.relpath(f, REPO_ROOT)
        for lineno, col, snippet in scan.open_encoding:
            report.setdefault(rel, {}).setdefault((lineno, col), [False, None, snippet])[0] = True
            enc_total += 1
        for lineno, col, reason, snippet in scan.line_ending:
            report.setdefault(rel, {}).setdefault((lineno, col), [False, None, snippet])[1] = reason
            le_total += 1
        if scan.subprocess:
            sp_by_file[ledger_key(f)] = scan.subprocess
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

    # One line per call: a builtin open() missing both keywords names both.
    both = 0
    for rel in sorted(report):
        for (lineno, _col), (enc, le_reason, snippet) in sorted(report[rel].items()):
            if enc and le_reason:
                both += 1
                print(f"{rel}:{lineno}: open() missing encoding= and {le_reason} — {snippet}")
            elif enc:
                print(f"{rel}:{lineno}: open() missing encoding= — {snippet}")
            else:
                print(f"{rel}:{lineno}: line-ending {le_reason} — {snippet}")

    if enc_total:
        print(
            f"\nTotal: {enc_total} encoding violations in "
            f"{sum(1 for r in report.values() if any(v[0] for v in r.values()))} files.",
            file=sys.stderr,
        )
        print(
            "Fix: add `encoding='utf-8'` keyword arg, or append "
            f"`# {IGNORE_MARKER}` if intentional.",
            file=sys.stderr,
        )
    if le_total:
        print(
            f"\nTotal: {le_total} line-ending violations in "
            f"{sum(1 for r in report.values() if any(v[1] for v in r.values()))} files"
            + (f" ({both} also missing encoding=, reported once)." if both else "."),
            file=sys.stderr,
        )
        print(_LINE_ENDING_FIX, file=sys.stderr)

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

    if args.strict_open_encoding and (enc_total or sp_errors):
        return EXIT_VIOLATION
    if args.strict_line_ending and le_total:
        return EXIT_VIOLATION
    if not enc_total and not le_total and not sp_counts and not args.ci:
        print("OK: no open() / subprocess call missing encoding= and no text "
              "write missing newline= found.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
