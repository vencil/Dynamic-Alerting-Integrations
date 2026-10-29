"""Signal dispositions for a child a test signals (#3249, #3297).

A child inherits SIG_IGN across exec: a pytest started by a runner or wrapper
that ignores SIGTERM (or as a background job, SIGINT) would start every child
with it ignored, and a test that sends that signal would measure nothing.
"""
from __future__ import annotations

import signal


def signals_to_default() -> None:
    """preexec_fn: the child starts with SIGINT and SIGTERM at their
    defaults, whatever this process inherited or was left with."""
    for s in (signal.SIGINT, signal.SIGTERM):
        signal.signal(s, signal.SIG_DFL)
