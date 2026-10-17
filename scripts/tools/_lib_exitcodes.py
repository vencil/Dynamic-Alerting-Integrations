"""Canonical exit-code contract for da-tools CLI tools (#452 Track A).

Single source of truth for the 0/1/2 exit-code convention shared by the
Python tool suite and the Go binaries (da-guard / da-parser / da-batchpr).
Before #452 the convention lived in three divergent ad-hoc spots
(_lib_godispatch._EXIT_CALLER_ERROR, diag_pr_ci.EXIT_*, trufflehog_to_sarif
.EXIT_*); this module unifies the base contract so call sites share one
set of constants.

What is tested, and what is not: tests/shared/test_tool_exit_codes.py pins
these constants to 0/1/2 and checks that every tool exits 2 on an
unrecognised flag — the one trigger below that argparse provides for free.
The other EXIT_CALLER_ERROR triggers (missing path, invalid value,
unreachable Prometheus / API, malformed input, IO failure) are NOT checked
by that gate; each tool's own tests carry them, where they exist. See that
file's Scope block and #1642.

Go equivalent (stable contract, mirrored here):
  components/threshold-exporter/app/cmd/da-guard/main.go  (// Exit codes ...)
  components/threshold-exporter/app/cmd/da-parser/main.go

Semantics
---------
EXIT_OK (0)
    Clean run. Nothing to fix; the tool did its job and found no problem.

EXIT_VIOLATION (1)
    The tool ran correctly and found something the USER must act on:
    rule violations, drift detected, validation failures, findings,
    per-target failures, `--ci` / `--strict` fail-on-finding. CI gates
    treat this as "fail the check". User-actionable.

EXIT_CALLER_ERROR (2)
    The tool could NOT do its job because of how it was invoked or its
    environment: bad/missing args, file/path not found, cannot reach
    Prometheus / API, malformed input YAML/JSON, missing prerequisite
    binary, IO failure, or an unexpected crash. System-actionable
    (fix the invocation or environment, then retry). argparse already
    exits 2 on unrecognised flags, which is consistent with this.

Unexpected crash (#3090)
------------------------
An uncaught exception used to end at the interpreter's default rc 1 —
indistinguishable from EXIT_VIOLATION. Importing this module now installs
a ``sys.excepthook`` (only if the process still has the default one), so
any script that imports it — directly or through one of the shared
``_lib_*`` helpers that import it — and then lets an ``Exception``
escape to the top level:

1. prints the usual traceback to stderr,
2. prints one line ``ERROR: unexpected internal error (<Type>): <message>``
   (single line, bounded length — see ``crash_message``),
3. exits with the crash code: EXIT_CALLER_ERROR (2) unless the tool chose
   another one with ``set_crash_exit_code``.

The hook exits via ``sys.exit`` (not ``os._exit``), so atexit handlers run
and buffered stdout is flushed. An interactive session (``python -i``,
the REPL) keeps the default hook's behaviour. ``SystemExit`` never reaches it, and
``KeyboardInterrupt`` and any other non-``Exception`` base exception are
handed to the previous hook untouched (Ctrl-C keeps its own exit status).

Overrides:

* ``set_crash_exit_code(code)`` — per-process crash code. tenant_verify
  sets 1, because its 2 means "verification failed" (see below); it sets
  its caller-error code (``set_caller_error_code``) to 1 for the same reason.
* ``@main`` / ``@main(crash_exit_code=..., on_crash=...)`` — wrap a tool's
  ``main()``: same report + exit, but in-function, so a tool with a
  ``--json`` contract can emit its ``internal_error`` envelope from
  ``on_crash(exc)`` before the process ends ("rc 2 always has an
  envelope", #3059).
* ``arm_crash_envelope(build, **fields)`` (#3094) — the way a ``--json``
  tool keeps that promise without a decorator: called right after the
  arguments are parsed (only when ``--json`` was given), it makes the crash
  path print one JSON document to stdout — the tool's own zeroed report from
  ``build()`` (dev-rules §13) plus ``{"status": "caller_error", "reason":
  "internal_error", "error": <the ERROR line>}``. Every crash path uses it
  (this hook, ``@main`` without ``on_crash``, and ``da-tools``' last line of
  defence). It fires at most once, and not at all once the tool wrote to
  stdout itself (#3115); a tool that can still run code after printing its
  own document may also call ``disarm_crash_envelope()`` there. A crash
  before the arguments are parsed has no envelope — the tool does not know
  yet that ``--json`` was asked for.

Caller errors and the single exit path (#3115)
----------------------------------------------
The same armed envelope covers every caller-error exit, so "rc 2 in
``--json`` mode has one document" has one implementation:

* ``die_caller_error(message, reason=...)`` — stderr line, envelope with
  ``reason`` (default ``"caller_error"``) and ``error`` = *message*, exit.
  ``exit_caller_error(error, reason=...)`` is the same for a caller that
  printed its own stderr lines. The shared decorators go through them:
  ``exit_on_yaml_file_error`` (``yaml_file_unreadable``),
  ``exit_on_output_write_error`` and the ``*_or_die`` writers
  (``output_unwritable``), ``exit_on_served_values_error``
  (``served_values_unavailable``), ``exit_on_observed_map_error``
  (``observed_map_unavailable``).
* ``CallerError(message, reason=None)`` — the raisable form. Wherever it
  ends up (``run``, ``@main``, the excepthook, ``exit_on_crash``) it is not
  a crash: one ``ERROR:`` line, no traceback, the caller-error code, the
  envelope. A subclass names its reason on the class.
* ``run(main)`` — the one process entry: ``if __name__ == "__main__":
  _lib_exitcodes.run(main)``. *main*'s return value is the exit status; a
  hand-written ``sys.exit(2)`` that printed no envelope gets the generic one
  (``reason: "caller_error"``, ``error``: the last line the tool wrote to
  stderr). Every tool that calls ``arm_crash_envelope`` must enter through
  it (gate: tests/shared/test_single_exit_path.py).

No envelope is printed when nothing was armed (no ``--json``), or when the
tool already wrote to stdout since arming (its own document — or prose,
which breaks the contract anyway). Arming notices that by wrapping
``write`` on the stdout / stderr objects; the objects, their buffers,
filenos and encodings stay as they were.

The caller-error code is per process like the crash code, and separate from
it: ``set_caller_error_code(code)`` (default 2; tenant_verify sets 1). A
caller error and a closed stdout (below) use it; a crash never does.

stdout closed by the reader (#3124)
-----------------------------------
``tool | head -c1``, ``tool | true``, a closed fd 1: when the reader is gone
while the tool still has output buffered, the interpreter's last flush
failed and the process ended at rc 120 — whatever it was exiting with, 0, 1,
2 or a crash — and a write that failed mid-run was reported as
``unexpected internal error (BrokenPipeError)``. Neither is a crash. Now:
an atexit hook registered at import (so it runs after the tool's own) makes
that last flush; if it fails, and whenever a ``BrokenPipeError`` reaches
``exit_on_crash`` while stdout's reader is gone, stdout is pointed at the
null device, stderr gets one line ``ERROR: stdout was closed before all
output was written``, and the process exits with the caller-error code.
A ``BrokenPipeError`` from anything else (a socket) stays a crash. Output
that was fully handed to the pipe before the reader left keeps the tool's
own exit code. ops/patch_config.py seals its streams itself and keeps its
own codes (#2505).

``da-tools`` (components/da-tools/app/entrypoint.py) is the last line of
defence for tools that never import this module: it catches what
``exec_module`` raises and hands it to ``exit_on_crash`` when this module
is loaded (honouring the tool's crash code), else prints the same line
itself and exits 2. Tests: tests/shared/test_tool_exit_codes.py (crash
section) and tests/shared/test_entrypoint.py.

Allowed extensions
------------------
A tool MAY define higher codes (>= 3) for finer-grained caller-error
subtypes, as long as 0/1/2 keep the meanings above. The sanctioned >= 3
extensions today:

* diag_pr_ci.py's EXIT_NETWORK_BLOCKED = 3 (documented in
  docs/internal/windows-mcp-playbook.md trap #64 — "switch host" hint,
  distinct from exit 2 "gh missing/unauthenticated").
* ops/patch_config.py's apply (#1950): its module-level EXIT_* constants
  >= 3 name what happened after (or around) the write — rolled back, may
  still be written, or unknown — which exit 2's "nothing written" cannot
  say; its verification failure stays EXIT_VIOLATION (1). Table in
  docs/cli-reference.md §patch-config.
* da-guard's exitParseFailed = 3 (#2123; `da-tools guard` passes it
  through): config files the exporter cannot decode (docs/cli-reference.md
  §guard). Author-actionable
  like 1 (fix the file, re-run), but not a finding about the
  tenants the guard DID check — which is why it is not 1. Contract in the
  comment at the top of cmd/da-guard/main.go; docs/cli-reference.md §guard.
* da-guard's exitUndetermined = 4 (#2904, ADR-036 §9; `da-tools guard`
  passes it through): the tree holds routing or domain policy, which the
  default check no longer judges — "not judged", kept apart from 0
  ("judged, nothing wrong"). Neither a finding (1) nor a caller error (2):
  the remedy is to run `da-tools generate-routes --validate --strict`.
  Precedence 3 > 1 > 4 > 0. Same contract places as exit 3.

One tool — dx/tenant_verify.py — INVERTS 1/2 (exit 2 = verification
finding, exit 1 = caller error) as a sanctioned pre-SSOT exception: its
codes are load-bearing in a shipped customer rollback runbook that keys on
"exit 2 = mismatch" (see that file's header for the full rationale). It is
the sole exception; its crash code is set to 1 for the same reason (a
crash must not read as "hash mismatch"). Do NOT repurpose 1 or 2 anywhere else — new tools MUST
follow the base violation / caller-error meanings above.

New subcommands MUST follow this contract — see docs/internal/dev-rules.md.
"""
from __future__ import annotations

import functools
import sys
import traceback
from typing import Any, Callable, Final, NoReturn, Optional, TypeVar, overload

# Import-time stdout hardening (cp950-class consoles): every da-tools CLI
# imports at least one of the four root libs (_lib_compat / _lib_python /
# _lib_godispatch / this module) at module level — i.e. before
# argparse.parse_args() can print --help — so chain-importing _lib_compat
# here guarantees unencodable characters degrade instead of crashing.
# Gate: tests/shared/test_console_encoding_resilience.py
import _lib_compat  # noqa: F401  (import-time side effect; see _lib_compat)

EXIT_OK: Final[int] = 0
EXIT_VIOLATION: Final[int] = 1
EXIT_CALLER_ERROR: Final[int] = 2


_caller_error_code: int = EXIT_CALLER_ERROR


def _check_code(what: str, code: Any) -> int:
    if (isinstance(code, bool) or not isinstance(code, int)
            or not 1 <= code <= 255):
        raise ValueError(f"{what} exit code must be an int in 1..255, got {code!r}")
    return code


def set_caller_error_code(code: int) -> None:
    """Set the exit code this process uses for a caller error.

    Default EXIT_CALLER_ERROR (2). Only a tool whose own contract gives 2
    another meaning should change it (tenant_verify: 1). Kept apart from the
    crash code (``set_crash_exit_code``): "stdout was closed by the reader"
    and a named caller error are not crashes, whatever the crash code is.
    """
    global _caller_error_code
    _caller_error_code = _check_code("caller-error", code)


def get_caller_error_code() -> int:
    """The exit code a caller error ends with in this process."""
    return _caller_error_code


class CallerError(Exception):
    """A caller error raised instead of exiting: the process ends at the
    caller-error code with one ``ERROR:`` line on stderr — no traceback — and,
    in ``--json`` mode, one envelope naming ``reason``.

    Raise it from anywhere below ``run()`` (or let the module excepthook /
    ``@main`` see it); it is never reported as a crash. A subclass names its
    reason once, on the class (``reason = "prometheus_query_error"``); the
    constructor's *reason* overrides that for one instance.
    """

    reason: str = "caller_error"

    def __init__(self, message: str, reason: Optional[str] = None) -> None:
        super().__init__(message)
        if reason is not None:
            self.reason = reason

    @property
    def error_line(self) -> str:
        """The stderr line: the message, prefixed ``ERROR:`` unless it is."""
        text = str(self)
        return text if text.startswith("ERROR") else f"ERROR: {text}"


def die_caller_error(message: str, reason: Optional[str] = None, *,
                     code: Optional[int] = None) -> NoReturn:
    """Print *message* to stderr and exit with the caller-error code.

    Convenience for the common "bad invocation / unusable environment"
    bail-out so call sites don't hand-roll the print + sys.exit(2) pair.
    In ``--json`` mode (an armed envelope, see ``arm_crash_envelope``) it
    also prints one ``caller_error`` envelope whose ``reason`` is *reason*
    (default ``"caller_error"``) and whose ``error`` is *message* — unless
    the tool already wrote to stdout. *code* overrides the process
    caller-error code (``set_caller_error_code``) for this one exit.
    """
    print(message, file=sys.stderr)
    exit_caller_error(message, reason, code=code)


def exit_caller_error(error: str, reason: Optional[str] = None, *,
                      code: Optional[int] = None) -> NoReturn:
    """``die_caller_error`` for a caller that already printed its own
    stderr lines: only the armed envelope (``error`` = *error*) and the
    exit. Use it when stderr carries more than the one line the envelope
    should name (``exit_on_served_values_error``: da-guard's own lines)."""
    _print_armed_envelope(reason or CallerError.reason, error)
    sys.exit(_caller_error_code if code is None else code)


# ── Unexpected crash handling (#3090) ─────────────────────────────────

_F = TypeVar("_F", bound=Callable[..., Any])

_CRASH_MESSAGE_MAX = 500
_crash_exit_code: int = EXIT_CALLER_ERROR


def set_crash_exit_code(code: int) -> None:
    """Set the exit code this process uses for an unexpected crash.

    Default EXIT_CALLER_ERROR (2). Only a tool whose own contract gives 2
    another meaning should change it (tenant_verify: 1). 0 is refused — a
    crash must never read as success — as are non-int and out-of-range
    values.
    """
    global _crash_exit_code
    _crash_exit_code = _check_code("crash", code)


def get_crash_exit_code() -> int:
    """The exit code an unexpected crash ends with in this process."""
    return _crash_exit_code


def crash_message(exc: BaseException) -> str:
    """The one ``ERROR:`` line printed after the traceback of a crash.

    The exception text is collapsed to one line and bounded, so the line
    stays greppable; an exception whose ``str()`` itself fails still gets a
    line. ⚠️ components/da-tools/app/entrypoint.py mirrors this (it may not
    import _lib_*); tests/shared/test_entrypoint.py pins the two together.
    """
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 — a broken __str__ must not hide the crash
        text = "(unprintable message)"
    text = " ".join(text.split()) or "(no message)"
    if len(text) > _CRASH_MESSAGE_MAX:
        text = text[:_CRASH_MESSAGE_MAX] + "..."
    return f"ERROR: unexpected internal error ({type(exc).__name__}): {text}"


def report_crash(exc: BaseException) -> None:
    """Print the traceback of *exc*, then its ``crash_message`` line, to stderr."""
    try:
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 — a closed/broken stdout must not mask the crash
        pass
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    print(crash_message(exc), file=sys.stderr, flush=True)


_DocBuilder = Callable[[], dict[str, Any]]
_crash_envelope_armed: Optional[tuple[Optional[_DocBuilder], dict[str, Any]]] = None
# Since the envelope was armed: did the tool write to stdout itself (then it
# owes no envelope — it printed its own document, or broke the contract
# already), and what did it last write to stderr (the generic envelope's
# ``error``). Kept by the taps ``arm_crash_envelope`` puts on the streams.
_stdout_written = False
_stderr_tail = ""
_STDERR_TAIL_MAX = 4000


def crash_envelope(exc: BaseException, base: Optional[dict[str, Any]] = None,
                   **fields: Any) -> dict[str, Any]:
    """The ``--json`` document a crash prints (#3094).

    ``status``/``reason`` follow the ``caller_error`` envelopes the tools
    already print for their named exit-2 paths; ``error`` is the same
    ``crash_message`` line stderr carries. *base* is the tool's own zeroed
    report and *fields* its extra keys (dev-rules §13: an early exit keeps
    the tool's schema keys, zeroed); neither can replace the three above.
    """
    return caller_error_envelope("internal_error", crash_message(exc), base, **fields)


def caller_error_envelope(reason: str, error: str, base: Optional[dict[str, Any]] = None,
                          **fields: Any) -> dict[str, Any]:
    """The ``--json`` document a caller-error exit prints (#3115): the
    tool's zeroed report *base* and extra *fields*, then ``status:
    caller_error``, *reason* and *error* — which neither can replace."""
    return {**(base or {}), **fields, "status": "caller_error",
            "reason": reason, "error": error}


def _note_stdout(text: Any) -> None:
    global _stdout_written
    if text:
        _stdout_written = True


def _note_stderr(text: Any) -> None:
    global _stderr_tail
    if isinstance(text, str) and text:
        _stderr_tail = (_stderr_tail + text)[-_STDERR_TAIL_MAX:]


def _tap(stream: Any, note: Callable[[Any], None]) -> None:
    """Make *stream*'s ``write`` call *note* first. The stream object, its
    buffer, fileno and encoding stay what they were; ``write`` returns what
    it returned. Once per stream; a stream that refuses attributes (a
    custom object) is left alone."""
    if stream is None or getattr(stream, "_da_exit_tap", False):
        return
    try:
        inner = stream.write

        def write(text: Any) -> Any:
            note(text)
            return inner(text)

        stream.write = write
        stream._da_exit_tap = True
    except (AttributeError, TypeError):
        pass


def arm_crash_envelope(build: Optional[_DocBuilder] = None, **fields: Any) -> None:
    """Make every rc-2 exit of this process print one ``--json`` envelope.

    Call it right after parsing the arguments, and only when ``--json`` was
    given. A crash then prints ``crash_envelope``; a caller-error exit
    (``die_caller_error``, ``CallerError``, a hand-written exit with the
    caller-error code reaching ``run()``) prints ``caller_error_envelope``.
    Neither prints once the tool wrote to stdout itself. *build* returns the
    tool's zeroed report; it runs at exit time, so a timestamp in it is the
    exit's. Calling it again replaces both.
    """
    global _crash_envelope_armed, _stdout_written, _stderr_tail
    _crash_envelope_armed = (build, dict(fields))
    _stdout_written = False
    _stderr_tail = ""
    _tap(sys.stdout, _note_stdout)
    _tap(sys.stderr, _note_stderr)


def disarm_crash_envelope() -> None:
    """Undo ``arm_crash_envelope``: a tool whose own document is already on
    stdout calls this, so a later crash does not print a second one."""
    global _crash_envelope_armed
    _crash_envelope_armed = None


def _print_crash_envelope(exc: BaseException) -> None:
    _print_armed_envelope("internal_error", crash_message(exc))


def _last_stderr_line() -> str:
    for line in reversed(_stderr_tail.splitlines()):
        if line.strip():
            line = " ".join(line.split())
            return line[:_CRASH_MESSAGE_MAX] + ("..." if len(line) > _CRASH_MESSAGE_MAX else "")
    return "caller error (no message)"


def _print_armed_envelope(reason: str, error: str) -> None:
    """Print the armed envelope with *reason* / *error*, at most once, and
    not after the tool wrote to stdout itself."""
    global _crash_envelope_armed
    armed, _crash_envelope_armed = _crash_envelope_armed, None
    if armed is None or _stdout_written:
        return
    build, fields = armed
    base = None
    if build is not None:
        try:
            base = build()
        except Exception:  # noqa: BLE001 — the three keys still print
            base = None
    import json  # deferred: only an exit in --json mode needs it
    import os
    data = (json.dumps(caller_error_envelope(reason, error, base, **fields),
                       ensure_ascii=False, indent=2, default=str) + "\n").encode(
        getattr(sys.stdout, "encoding", None) or "utf-8", "replace")
    # Straight to the fd, never through the stdout buffer: on a closed pipe a
    # write left in the buffer fails again at interpreter exit and turns the
    # exit code into 120. A stdout that cannot even flush gets no envelope.
    try:
        sys.stdout.flush()
        fd = sys.stdout.fileno()
        while data:
            data = data[os.write(fd, data):]
    except (OSError, ValueError):  # closed pipe / closed or fd-less stdout
        pass


def exit_on_crash(exc: BaseException, *, code: Optional[int] = None,
                  on_crash: Optional[Callable[[BaseException], Any]] = None
                  ) -> NoReturn:
    """Report *exc* as an unexpected crash and exit.

    Exits with *code*, else the process crash code (``set_crash_exit_code``).
    *on_crash* runs after the report; without one, the envelope armed by
    ``arm_crash_envelope`` (if any) is printed instead. If that raises, it is
    reported too and the exit code is unchanged. Uses ``sys.exit`` so atexit
    handlers run.

    Two exceptions are not crashes and leave by their own exit instead: a
    ``CallerError`` (caller-error code, one line, its envelope) and a
    ``BrokenPipeError`` whose broken end is stdout (the reader left: #3124).
    """
    if isinstance(exc, CallerError):
        die_caller_error(exc.error_line, exc.reason)
    if isinstance(exc, BrokenPipeError) and _stdout_is_gone():
        _exit_stdout_closed(at_exit=False)
    report_crash(exc)
    try:
        (on_crash or _print_crash_envelope)(exc)
    except Exception as hook_exc:  # noqa: BLE001
        what = "on_crash handler" if on_crash else "crash envelope"
        print(f"ERROR: {what} failed:", file=sys.stderr)
        traceback.print_exception(type(hook_exc), hook_exc,
                                  hook_exc.__traceback__, file=sys.stderr)
    sys.exit(_crash_exit_code if code is None else code)


@overload
def main(func: _F) -> _F: ...
@overload
def main(*, crash_exit_code: Optional[int] = None,
         on_crash: Optional[Callable[[BaseException], Any]] = None
         ) -> Callable[[_F], _F]: ...


def main(func: Optional[_F] = None, *, crash_exit_code: Optional[int] = None,
         on_crash: Optional[Callable[[BaseException], Any]] = None) -> Any:
    """Decorator for a tool's ``main()``: an ``Exception`` escaping it becomes
    an unexpected crash (traceback + ERROR line + crash exit code).

    ``@main`` or ``@main(crash_exit_code=1, on_crash=emit_envelope)``.
    ``crash_exit_code`` overrides ``set_crash_exit_code`` for this function;
    ``on_crash(exc)`` lets a ``--json`` tool emit its envelope. ``SystemExit``
    and ``KeyboardInterrupt`` pass through untouched.
    """
    if crash_exit_code is not None:
        _check_code("crash", crash_exit_code)

    def decorate(f: _F) -> _F:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return f(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — that is the point
                exit_on_crash(exc, code=crash_exit_code, on_crash=on_crash)
        return wrapper  # type: ignore[return-value]

    if func is not None:
        return decorate(func)
    return decorate


_previous_excepthook = sys.excepthook


def _crash_excepthook(exc_type, exc, tb) -> None:
    # An interactive session (REPL, `python -i tool.py` post-mortem) reports
    # an error and carries on: ending it at rc 2 would take the prompt away.
    if not issubclass(exc_type, Exception) or sys.flags.interactive or hasattr(sys, "ps1"):
        _previous_excepthook(exc_type, exc, tb)
        return
    if exc is None:  # defensive: hook called with a bare type
        exc = exc_type()
    if exc.__traceback__ is None:
        exc = exc.with_traceback(tb)
    exit_on_crash(exc)


# Install only over the interpreter default: a process (or host such as an
# IDE / test runner) that set its own hook keeps it.
if sys.excepthook is sys.__excepthook__:
    sys.excepthook = _crash_excepthook


# ── The single process entry (#3115) ──────────────────────────────────


def run(entry: Callable[[], Any]) -> NoReturn:
    """Run a tool's *entry* (its ``main``) and exit — the one way out.

    ``if __name__ == "__main__": _lib_exitcodes.run(main)`` replaces both
    ``main()`` and ``sys.exit(main())``: *entry*'s return value is the exit
    status, as ``sys.exit`` reads it. On top of that:

    * an ``Exception`` is a crash (``exit_on_crash``) — except a
      ``CallerError`` or a stdout ``BrokenPipeError``, which leave by their
      own exits (see ``exit_on_crash``);
    * an exit with the caller-error code that has printed no envelope yet
      — a hand-written ``sys.exit(2)`` — prints the armed envelope with
      ``reason: "caller_error"`` and, as ``error``, the last line the tool
      wrote to stderr. Nothing is printed when not armed (no ``--json``) or
      when the tool already wrote to stdout itself.

    Gate: tests/shared/test_single_exit_path.py (every tool that calls
    ``arm_crash_envelope`` enters through here).
    """
    try:
        status = entry()
    except SystemExit as exc:
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — that is the point
        exit_on_crash(exc)
    if (isinstance(status, int) and not isinstance(status, bool)
            and status == _caller_error_code):
        _print_armed_envelope(CallerError.reason, _last_stderr_line())
    sys.exit(status)


# ── stdout closed by the reader (#3124) ────────────────────────────────
#
# `tool | head -c1`, `tool | true`, a closed fd 1: the reader is gone while
# the tool still has output. Left alone, the interpreter's last flush of
# stdout fails and the process ends at rc 120 — whatever the tool exited
# with, crash or not; and a write that fails mid-run surfaces as an
# "unexpected internal error (BrokenPipeError)". Neither is a crash, and 120
# is outside 0/1/2. Both now end here: stdout is pointed at the null device
# (so nothing fails again), one line goes to stderr, and the process exits
# with the CALLER-ERROR code — not the crash code.

STDOUT_CLOSED_MESSAGE = "ERROR: stdout was closed before all output was written"
_stdout_closed_reported = False


def _stdout_is_gone() -> bool:
    """Is stdout's reader gone (the far end of a pipe closed, fd 1 closed)?

    A ``BrokenPipeError`` from a socket or a child's pipe leaves stdout
    writable, so it answers False there and that error stays a crash.
    """
    out = sys.stdout
    if out is None:
        return False
    try:
        fd = out.fileno()
    except (AttributeError, OSError, ValueError):  # no fd (a capture object)
        fd = None
    if fd is not None:
        try:
            import select
            poller = select.poll()
            poller.register(fd, select.POLLOUT)
            gone = select.POLLERR | select.POLLHUP | select.POLLNVAL
            return any(ev & gone for _, ev in poller.poll(0))
        except (AttributeError, OSError, ValueError):  # no poll() (Windows)
            pass
    try:
        out.flush()
    except OSError:
        return True
    except ValueError:  # closed by the tool itself: not the reader
        return False
    return False


def _exit_stdout_closed(*, at_exit: bool) -> NoReturn:
    global _stdout_closed_reported, _crash_envelope_armed
    _crash_envelope_armed = None  # no stdout to put it on
    import os
    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        fd = 1
    try:
        null = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null, fd)
        finally:
            os.close(null)
    except OSError:
        pass
    if not _stdout_closed_reported:
        _stdout_closed_reported = True
        try:
            print(STDOUT_CLOSED_MESSAGE, file=sys.stderr, flush=True)
        except Exception:  # noqa: BLE001 — stderr gone too: the code still says it
            pass
    if at_exit:
        # atexit cannot change the exit status by raising; this runs last.
        os._exit(_caller_error_code)
    sys.exit(_caller_error_code)


def _flush_stdout_at_exit() -> None:
    out = sys.stdout
    if out is None or getattr(out, "closed", False):
        return
    try:
        out.flush()
    except OSError:
        _exit_stdout_closed(at_exit=True)
    except ValueError:
        pass


# Registered at import, before any tool code can register its own, so it
# runs LAST of them (atexit is LIFO): the tool's own exit handlers have
# written whatever they write by then.
import atexit as _atexit  # noqa: E402

_atexit.register(_flush_stdout_at_exit)
