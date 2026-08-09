#!/usr/bin/env python3
"""test_makefile_utf8_mode — every Python the Makefile starts must run in
UTF-8 mode.

Why
---
Through CPython 3.14, ``subprocess(..., text=True)`` without ``encoding=``
decodes the child's output with the **locale codec**. On a zh-TW Windows host
(cp950) that raises ``UnicodeDecodeError`` on this repo's own Chinese commit
subjects — and it raises inside subprocess's reader thread, so the caller sees
``stdout=None`` and then an unrelated-looking ``AttributeError`` one line later.

pre-commit passes ``-X utf8`` on all 107 of its entries, so the hook path has
always been immune; the Makefile passed it on none of its 83 invocations, which
is where ``make <target>`` on a Windows host actually got hurt. #1372 fixed the
mirror-image defect on the writing side (mkdocs' output encoding).

What is asserted, and why it is phrased this way
------------------------------------------------
The invariant is **"no Python starts here outside UTF-8 mode"**, not "line 6
says export PYTHONUTF8=1". Two implementations satisfy it — a top-level
``export`` or a per-call ``-X utf8`` — and pinning the literal line would fail a
legitimate switch between them while still passing a Makefile that grew a 84th
recipe with neither. So the test derives the interpreter call sites and checks
that each is covered by one or the other.

⛔ This is an entry-point mitigation, not the fix. Pinning ``encoding=`` at each
subprocess call site is (#1374). UTF-8 mode *hides* that class locally, so do
not read a green run here as evidence #1374 is done.
"""

from __future__ import annotations

import re
from pathlib import Path

_MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"

# An interpreter token used as a command word: at line start, or after any of
# the separators a recipe can put in front of one.
_INTERP = re.compile(r"(?:(?<=^)|(?<=[\s@\-;|&(`]))(python3?|py)(?=\s)")
_UTF8_FLAG = re.compile(r"-X\s*utf8")
# ⛔ `^[ ]*`, not `^\s*`: in a Makefile a leading TAB means the line is a RECIPE
# line, and an `export` there is a shell builtin scoped to that one recipe line's
# own shell — measured, it reaches neither the next line of the same recipe, nor
# a nested `bash -c`, nor any other target. Accepting it would let this guard
# return "covered" for a Makefile whose Python all still runs in the locale
# codec. Leading spaces are fine: make allows a top-level directive to be
# space-indented, and only TAB starts a recipe. (CodeRabbit, PR #1372.)
_EXPORT_UTF8 = re.compile(r"^[ ]*export\s+PYTHONUTF8\s*[:?]?=\s*1\s*$", re.MULTILINE)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backslash continuations so a flag on the next physical line still
    belongs to the invocation that opened the command."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = i
        buf += raw
        if raw.rstrip().endswith("\\"):
            buf = buf.rstrip()[:-1] + " "
            continue
        out.append((start, buf))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def _interpreter_calls(text: str) -> list[tuple[int, str]]:
    """(lineno, remainder-of-command) for each real interpreter invocation.

    Recipe comments (``@# …``) and interpreter names quoted inside ``echo``
    help text are prose, not invocations; counting them would demand a flag on
    a line where it would do nothing.
    """
    calls: list[tuple[int, str]] = []
    for lineno, line in _logical_lines(text):
        stripped = line.lstrip().lstrip("@-").lstrip()
        if stripped.startswith("#"):
            continue
        for m in _INTERP.finditer(line):
            before = line[: m.start()]
            if before.count('"') % 2 or before.count("'") % 2:
                continue  # inside a quoted string (echo'd usage hints)
            # Truncate at the next command separator so a later invocation's
            # flags cannot be credited to this one.
            rest = re.split(r"(?:&&|\|\||;|\|)", line[m.end():])[0]
            calls.append((lineno, rest))
    return calls


def _uncovered_lines(text: str) -> list[int]:
    """Line numbers that start Python outside UTF-8 mode. Empty == covered."""
    if _EXPORT_UTF8.search(text):
        return []  # one export covers every recipe, including nested `bash -c`
    return [ln for ln, rest in _interpreter_calls(text) if not _UTF8_FLAG.search(rest)]


def test_every_makefile_python_invocation_runs_in_utf8_mode() -> None:
    text = _MAKEFILE.read_text(encoding="utf-8")

    # Guard the guard: if the extractor ever stops finding anything, the
    # assertion below becomes vacuously true and this file stops protecting
    # anything at all.
    calls = _interpreter_calls(text)
    assert len(calls) >= 50, (
        f"only {len(calls)} interpreter invocations found in the Makefile — the "
        "extractor is probably broken, not the Makefile"
    )

    uncovered = _uncovered_lines(text)
    assert not uncovered, (
        "Makefile starts Python outside UTF-8 mode at line(s) "
        f"{uncovered}. Either restore `export PYTHONUTF8=1` near the top or "
        "give each invocation `-X utf8`. See #1374 / the comment above the "
        "export for why this matters on non-UTF-8 hosts."
    )


def test_a_recipe_local_export_does_not_count_as_coverage() -> None:
    """A TAB-indented `export` is a recipe-local shell builtin, so it must not
    satisfy the guard — otherwise one misplaced line silences the whole check.

    Measured before writing this: with the export on a recipe line, the very
    next line of the SAME recipe sees the variable unset, as do a nested
    `bash -c` and every other target.
    """
    body = "\t@python3 ./scripts/tools/dx/bump_docs.py --check\n"
    recipe_local = "SHELL := /bin/bash\n\nt:\n\texport PYTHONUTF8=1\n" + body
    top_level = "SHELL := /bin/bash\nexport PYTHONUTF8=1\n\nt:\n" + body

    assert _uncovered_lines(recipe_local) == [5], (
        "a recipe-local `export PYTHONUTF8=1` was accepted as coverage; it "
        "reaches nothing outside its own recipe line"
    )
    assert _uncovered_lines(top_level) == [], (
        "a top-level export must still count — the fix must not become a ban "
        "on the form the Makefile actually uses"
    )
