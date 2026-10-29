"""`safe_label`: neutralise control characters in a value before it is printed.

Standard library only, so a tool that must not depend on PyYAML (or on the
crash hook `_lib_exitcodes` installs) can name an untrusted path too — the
caller-path judgement `_lib_paths` names every path it refuses with it
(#3276). `_lib_io` re-exports it: every existing `from _lib_io import
safe_label` keeps working, and there is one implementation.
"""
from __future__ import annotations

import re
from typing import Any

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_label(value: Any) -> str:
    """Neutralise control characters in an untrusted value before it is PRINTED.

    #1538. A tenant config *filename* is untrusted input — ``tenants/onboarding``
    and the tenant self-service flow both create files whose names the platform
    never chose. Every plain-text report in ``scripts/tools/`` interpolates those
    names (and the exception text derived from them) straight into ``print()``.
    Two things ride in on that:

    * **a newline** ends the current report line and starts a new one, so a file
      named ``evil\\n[PASS] all good\\nx.yaml`` prints ``[PASS] all good`` at
      column 0 — indistinguishable, to a human or to a ``grep '^\\[PASS\\]'``, from
      a verdict the tool actually emitted;
    * **an ESC (``\\x1b``)** starts an ANSI sequence, so the same channel can
      recolour the terminal, move the cursor, or clear the screen.

    Replacing each control char with ``?`` defuses both while keeping the payload
    visible (``\\x1b[2J`` prints as ``?[2J``) — the operator still sees that
    something odd is in the name, which deleting the characters would hide.

    ⛔ **Scope, stated so nobody over-reads it.** This covers C0
    ``\x00-\x1f``, DEL ``\x7f``, and the C1 range ``\x80-\x9f``.

    C1 was added in the #1538 review round, and not as widening for its own
    sake: ``U+0085`` (NEL) is *the same attack as the newline* — terminals that
    decode C1 treat it as a line break, so an unescaped NEL forges a report line
    exactly as ``\n`` does, which is the one thing this function exists to stop.
    An adversarial review measured it passing through ten already-fixed tools.

    ⚠️ Honest boundary on that evidence: what was measured is **byte
    passthrough**, not rendering. Nobody drove a real terminal to confirm NEL
    breaks the line there. C1 is covered because it belongs to the same attack
    class, not because the terminal behaviour was demonstrated.

    ⛔ This also means the class is NO LONGER byte-identical to the one
    ``compile_custom_alerts._safe_log`` carried from #1008 until this change: a
    C1 character that used to survive that tool's quarantine line now renders as
    ``?``. A deliberate behaviour change, not a refactoring accident.

    Still NOT covered, deliberately:

    * bidi overrides ``U+202A-U+202E`` / ``U+2066-U+2069``. They reorder a line
      **visually** without breaking it — a different attack class from forging a
      line, and folding it in here would blur what this function promises.
    * homoglyph or zero-width confusables.

    Those remain open. Do not read "went through ``safe_label``" as "is safe to
    render in an arbitrary terminal."

    This is an OUTPUT-layer helper, not a validator: it must not be used to
    sanitise a value on its way *into* a config, a filename, or a subprocess
    argument. It lives beside :func:`format_json_report` because they are the two
    halves of the same decision — ``--json`` carries its own escaping and this is
    the plain-text branch's equivalent. ⛔ Never apply it to data destined for the
    ``--json`` branch: that would corrupt machine-readable output.

    ⚠️ **``--json`` is NOT unconditionally safe, and an earlier wording here said
    it was.** ``json.dumps`` is only required to escape ``"``, ``\\`` and
    ``U+0000``–``U+001F``; under ``ensure_ascii=False`` (this repo's default, see
    :func:`format_json_report`) the **C1** range passes through verbatim —
    measured, ``json.dumps("a\\x85b", ensure_ascii=False)`` keeps the raw byte
    while ``"a\\x0ab"`` becomes ``\\n``. So a ``--json`` payload piped straight to
    a terminal that interprets C1 is still forgeable. That is EXISTING behaviour,
    deliberately not changed here: the byte-for-byte stability of ``--json`` is
    the mechanism guarantee this escaping rests on, and rewriting the serialized
    output would trade a measured guarantee for an unmeasured one. Tracked
    separately; do not read this paragraph as "handled".

    ⛔ Apply it to the FIELD, never to a whole rendered multi-line report — the
    report's own ``\\n`` separators are control characters too and would become
    ``?``, collapsing the layout.

    Args:
        value: Any value; coerced with ``str()``.

    Returns:
        *value* as text with every C0, DEL **and C1** character replaced by
        ``?`` — the class is ``[\\x00-\\x1f\\x7f-\\x9f]``. C1 is in because
        ``\\x85`` (NEL) forges a line exactly like ``\\n`` does.
    """
    return _CONTROL_CHARS_RE.sub("?", str(value))
