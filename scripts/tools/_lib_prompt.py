"""`prompt_line`: the one read of an interactive answer from stdin (#3822).

A tool that asks a person a question reads the answer here, never with a
bare ``input()``. A stdin that cannot give a line — ``< /dev/null``, an
empty pipe, CI, cron, a closed fd 0 — ends the run as a caller error
(:class:`_lib_exitcodes.CallerError`, rc 2): one ``ERROR:`` line that says
the answer could not be read and names the tool's own way to run without
prompts. Before, ``input()``'s EOFError escaped as a crash (rc 2 plus
``unexpected internal error (EOFError)`` and a traceback), which told the
caller nothing about the flag that would have unblocked them.

"Cannot read a line" has more than one spelling, so all three are caught:
``EOFError`` (end of input), ``RuntimeError`` (``input(): lost sys.stdin``
— ``sys.stdin`` is None) and ``OSError`` (a bad descriptor). Ctrl-C
(``KeyboardInterrupt``) is not caught: it is the person's answer, not a
stdin that has none; a tool that owes it a message catches it itself.

⛔ Gate: ``tests/shared/test_prompt_line_boundary.py`` — an AST ratchet
that allows a call of the builtin ``input`` under ``scripts/`` only in this
module.
"""
from __future__ import annotations

from _lib_exitcodes import CallerError

#: The stderr line's lead-in, per language; the tool's *remedy* follows it.
_UNREADABLE = {
    "en": "an answer was needed on stdin, but none could be read ({detail}).",
    "zh": "需要從 stdin 讀取回答，但讀不到（{detail}）。",
}


class StdinUnreadable(CallerError):
    """An interactive answer was asked for and stdin could not give one."""

    reason = "stdin_unreadable"


def prompt_line(prompt: str = "", *, remedy: str, lang: str = "en") -> str:
    """``input(prompt)``, or :class:`StdinUnreadable` (rc 2) when stdin
    cannot give a line.

    *remedy* is the tool's own way to run without prompts (its
    non-interactive flags), printed after the lead-in; *lang* picks the
    lead-in's language (``en`` / ``zh``) to match the tool's other output.
    The answer is returned as ``input`` returns it — not stripped.
    """
    try:
        return input(prompt)
    except (EOFError, RuntimeError, OSError) as exc:
        detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        lead = _UNREADABLE.get(lang, _UNREADABLE["en"]).format(detail=detail)
        sep = "" if lang == "zh" else " "
        raise StdinUnreadable(f"{lead}{sep}{remedy}") from None
