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
Importing this module installs a ``sys.excepthook`` (only over the
interpreter default; an interactive session keeps the default behaviour).
An ``Exception`` escaping to the top level prints the traceback, then one
line ``ERROR: unexpected internal error (<Type>): <message>``, and exits
with the crash code: EXIT_CALLER_ERROR (2), or ``set_crash_exit_code``'s,
or — test suites only — ``DA_TEST_CRASH_EXIT_CODE`` (3..255, #3092).
``exit_on_crash`` is the same exit for a caller that caught the exception
(``run``, ``da-tools``' entrypoint). A ``CallerError`` is not a crash: one
``ERROR:`` line, the caller-error code.

``--json`` (#3115, #3216): rc 0/1 and a document, or nothing
--------------------------------------------------------------
A ``--json`` run's document is written once, by ``run()``, from the
:class:`Report` the tool returns, through the one encoder
(``encode_json``). Nothing else is ever written to stdout for the run:
a caller error or a crash writes its ``ERROR:`` line to stderr and exits
non-zero, with no envelope. The consumer's contract is the exit code
first: a document is the run's answer only when the run exited 0 (or 1,
its findings) and the document parses (as Terraform's ``-json``).

stdout failures (#3124, #3558)
------------------------------
Any OSError writing stdout — EPIPE (``tool | head``), ENOSPC, EIO, EBADF
alike — ends the run with one line ``ERROR: cannot write to stdout:
<reason>`` and the caller-error code, never 0: under ``set -o pipefail``
an early-closing reader fails the pipeline (as git's SIGPIPE, jq, Click,
and the Python ``signal`` docs' ``sys.exit(1)``). Checked where it is
seen, not on every write (gnulib ``close_stdout``): an OSError reaching
``run()`` / the excepthook when stdout itself still fails to flush, and
the last flush at exit.

A closed standard fd (``>&-``, ``2>&-``, ``<&-``) is not a case of its
own: importing this module first gives every closed fd 0..2 the null
device, opened read-only (``sanitize_std_fds``, as git's
``sanitize_stdfds`` and Rust's std), so a later ``open()`` can never be
handed fd 1 or 2, and a write to a closed stdout fails like any other —
the exit above. A ``sys.stdout`` left None by the interpreter is pointed at
fd 1 for that. A ``sys.stderr`` left None is pointed at a writable null
device instead: a tool's own stderr line must neither land on stdout nor
end a run that ``2>&-`` asked to be quiet (fd 2 itself stays read-only,
so a child process writing it is told).

``Report(..., delivery="best_effort")`` (patch-config only; gate:
tests/shared/test_json_stdout_contract.py): stdout failing still prints the
line, but the run keeps the tool's status — patch-config's exit code says
what the ConfigMap holds, which a lost stdout does not change.

Known limits (dev-rules §13): argparse's own ``--help`` / usage output
swallows a write error; the unbuffered partial write (CPython gh-85393,
#3554); a failure a tool swallows itself whose bytes left the buffer; a
child process writing the stdout it inherited; the stderr line may name
the wrong stream when both fail (the exit code does not depend on it); exit
codes passed through from Go binaries are the binary's.

Allowed extensions
------------------
A tool MAY use a code outside 0/1/2 only where :data:`EXTENDED_EXIT_CODES`
registers it for that entry — the one list, read at runtime by ``run()``
(an unregistered status ends at 2 with an ERROR line) and statically by
tests/shared/test_single_exit_path.py (every exit-status literal outside
0..2 under scripts/{tools,ops,dx,session-guards} must be in its entry's row),
so a code can no longer be added by writing it. Adding a row is an owner
decision (#3611). The rows, and why each exists:

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
  the remedy is to run
  `da-tools generate-routes --config-dir <dir> --validate --strict`.
  Precedence 3 > 1 > 4 > 0. Same contract places as exit 3.
* :data:`PASSTHROUGH` — ops/dx_run.py (``--run``: the command run in the
  dev container) and ops/commit_helper.py (``git commit``): wrappers that
  end with the wrapped command's own status, whatever it is (#3577). Their
  own failures — docker, the container, the wrapper's inputs — are 2 like
  any tool's, and a literal they choose is held to 0/1/2 by the gate.

No CLI tool repurposes 1 or 2: dx/tenant_verify.py, which inverted them
(exit 2 = verification finding, exit 1 = caller error and crash), follows
the base meanings since #3393 — a breaking change to its customer rollback
runbook, migrated with it. Do NOT repurpose 1 or 2 in a CLI tool — new
tools MUST follow the base violation / caller-error meanings above.

The one sanctioned exception is a protocol that is not ours
(:func:`use_hook_protocol`, #3579): scripts/session-guards/ are Claude Code
hooks, where exit 2 BLOCKS the tool call or the stop and any other non-zero
is a non-blocking error. There a crash, a failed stdout write or a caller
error ended by this module must never block, so they end at 1; a guard
blocks on purpose by returning 2. argparse's own usage error still exits 2
(it calls sys.exit itself; owner, #3579). A failed stdout write overrides
the entry's code as it does everywhere, a returned 2 included. Gate:
tests/shared/test_single_exit_path.py.

New subcommands MUST follow this contract — see docs/internal/dev-rules.md.
"""
from __future__ import annotations

import os
import sys
import traceback
from types import MappingProxyType
from typing import Any, Callable, Final, NoReturn, Optional


def sanitize_std_fds():
    """Give each closed fd 0, 1, 2 the null device, read-only (#3558).

    As git's ``sanitize_stdfds`` and Rust's std: checked in order, so the
    null device opens at the very fd it fills (``dup2`` if not). Read-only
    for all three: reading fd 0 gets EOF, writing fd 1 or 2 fails (EBADF)
    instead of vanishing or landing in a file a later ``open()`` was handed.
    A ``sys.stdout`` the interpreter left None (fd 1 was closed at start) is
    pointed at fd 1, so a write fails as any stdout failure does; a None
    ``sys.stderr`` at a writable null device, so a tool's stderr line is
    neither printed on stdout (``print(file=None)``) nor a failure of its own.
    Best effort: an fd that cannot be filled is left as it was.

    ⚠️ components/da-tools/app/entrypoint.py holds this same function, byte
    for byte (it may not import _lib_*): tests/shared/test_entrypoint.py.
    """
    import os
    import sys
    filled = set()
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
            continue
        except OSError:
            pass
        try:
            null = os.open(os.devnull, os.O_RDONLY)
            if null == fd:
                os.set_inheritable(fd, True)
            else:
                os.dup2(null, fd)
                os.close(null)
            filled.add(fd)
        except OSError:
            pass
    if sys.stdout is None and 1 in filled:
        sys.stdout = sys.__stdout__ = open(  # noqa: SIM115
            1, "w", encoding="utf-8", errors="backslashreplace", newline="\n",
            closefd=False)
    if sys.stderr is None:
        sys.stderr = sys.__stderr__ = open(  # noqa: SIM115
            os.devnull, "w", encoding="utf-8", errors="backslashreplace", newline="\n")


sanitize_std_fds()

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
    another meaning should change it (none does since #3393). Kept apart from the
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
    caller-error code with one ``ERROR:`` line on stderr — no traceback.

    Raise it from anywhere below ``run()`` (or let the module excepthook
    see it); it is never reported as a crash. A subclass names its
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
    """Print *message* to stderr and exit with the caller-error code (or
    *code*). Nothing goes to stdout (#3558): a caller error carries no
    document unless the tool returns one as a :class:`Report`. *reason*
    names the error for the reader of the call; nothing prints it."""
    _to_stderr(message)
    exit_caller_error(message, reason, code=code)


def exit_caller_error(error: str, reason: Optional[str] = None, *,
                      code: Optional[int] = None) -> NoReturn:
    """``die_caller_error`` for a caller that printed its own stderr lines."""
    sys.exit(_caller_error_code if code is None else code)

# ── Unexpected crash handling (#3090) ─────────────────────────────────

_CRASH_MESSAGE_MAX = 500
_crash_exit_code: int = EXIT_CALLER_ERROR

# #3092: set by test suites only (tests/conftest.py), read at every crash.
TEST_CRASH_EXIT_CODE_ENV: Final[str] = "DA_TEST_CRASH_EXIT_CODE"


def crash_exit_code_from_env() -> Optional[int]:
    """The crash code ``DA_TEST_CRASH_EXIT_CODE`` asks for, or None.

    Only ASCII digits naming 3..255 count: 0 would read as success, 1 and 2
    as a finding and a named caller error — the very confusion the variable
    exists to remove — and anything else (a sign, spaces, an empty value)
    is ignored rather than guessed at. ⚠️ components/da-tools/app/entrypoint.py
    mirrors this; tests/shared/test_entrypoint.py pins the two together.
    """
    import os
    raw = os.environ.get(TEST_CRASH_EXIT_CODE_ENV)
    if raw is None or not (raw.isascii() and raw.isdigit()):
        return None
    code = int(raw)
    return code if 3 <= code <= 255 else None


def _crash_code(code: Optional[int] = None) -> int:
    """The code a crash ends with: the test override, else *code*, else the
    process crash code."""
    override = crash_exit_code_from_env()
    if override is not None:
        return override
    return _crash_exit_code if code is None else code


def set_crash_exit_code(code: int) -> None:
    """Set the exit code this process uses for an unexpected crash.

    Default EXIT_CALLER_ERROR (2). Only a tool whose own contract gives 2
    another meaning should change it (none does since #3393). 0 is refused — a
    crash must never read as success — as are non-int and out-of-range
    values.
    """
    global _crash_exit_code
    _crash_exit_code = _check_code("crash", code)


def use_hook_protocol() -> None:
    """Declare that this process is a Claude Code hook (#3579): a crash, a
    failed stdout write and a caller error ended by this module end at 1 —
    "non-blocking error" in the hook protocol — not at 2, which there
    BLOCKS the tool call or the stop. argparse's own usage error is not
    ours and stays 2. A hook blocks on purpose by returning 2 from its
    entry, which ``run()`` passes through unless stdout then fails. Called
    as the statement right after ``import _lib_exitcodes`` at module level,
    so no crash between the two ends at the default 2. Only
    scripts/session-guards/ call it (the gate's list).
    """
    set_crash_exit_code(1)
    set_caller_error_code(1)


def get_crash_exit_code() -> int:
    """The crash code this process chose (``set_crash_exit_code``) — what a
    save / restore around a tool run must put back. The code a crash ACTUALLY
    ends with is ``effective_crash_exit_code()``."""
    return _crash_exit_code


def effective_crash_exit_code() -> int:
    """The exit code an unexpected crash ends with in this process: the one
    ``set_crash_exit_code`` chose, unless ``DA_TEST_CRASH_EXIT_CODE`` (tests
    only, #3092) overrides it. For code that ends a crash itself instead of
    going through ``exit_on_crash``."""
    return _crash_code()


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
    """Print the traceback of *exc*, then its ``crash_message`` line, to
    stderr — never to stdout (``print(file=None)`` would fall back to it)."""
    err = sys.stderr
    if err is None:
        return
    try:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=err)
        print(crash_message(exc), file=err, flush=True)
    except (OSError, ValueError):  # stderr's reader left: the code still says it
        pass


# #3107 v2: who writes to stdout this run (the operator-output ``claim_stdout``).
_stdout_owner: Optional[str] = None


def claim_stdout_owner(owner: str) -> Optional[str]:
    """Record *owner* as this run's stdout writer; return the other owner
    already recorded (a conflict), or None."""
    global _stdout_owner
    if _stdout_owner is not None and _stdout_owner != owner:
        return _stdout_owner
    _stdout_owner = owner
    return None


def reset_exit_state() -> None:
    """This module's process-global exit state back to its import-time
    value (tests/conftest.py, after every test: an idempotent reset)."""
    global _crash_exit_code, _caller_error_code, _stdout_owner
    global _stdout_failure_line, _failure_reported, _best_effort
    _failure_reported = False
    _best_effort = False
    _stdout_owner = None
    _stdout_failure_line = None
    _crash_exit_code = _IMPORT_CRASH_EXIT_CODE
    _caller_error_code = _IMPORT_CALLER_ERROR_CODE


def _to_stderr(line: str) -> None:
    """Print *line* to stderr — never to stdout; best effort."""
    err = sys.stderr
    if err is None or not line:
        return
    try:
        print(line, file=err, flush=True)
    except Exception:  # noqa: BLE001 — the exit code still says it
        pass


def exit_on_crash(exc: BaseException, *, code: Optional[int] = None) -> NoReturn:
    """Report *exc* as an unexpected crash and exit with *code*, else the
    process crash code (``DA_TEST_CRASH_EXIT_CODE`` overrides both). Not a
    crash: a ``CallerError`` (its caller-error exit) and a failing stdout
    (the stdout-failure exit)."""
    if isinstance(exc, CallerError):
        die_caller_error(exc.error_line, exc.reason)
    if _from_stdout(exc):
        _stdout_failed(exc)
    report_crash(exc)
    sys.exit(_crash_code(code))


_previous_excepthook = sys.excepthook


def _interactive() -> bool:
    """An interactive session (REPL, `python -i tool.py` post-mortem): an
    error is reported by the default hook and the session carries on —
    ending it at rc 2 would take the prompt away, and catching the error
    would leave `pdb.pm()` / `sys.last_value` a SystemExit (#3237).

    The interpreter's own test for "a prompt follows the script": ``-i``
    (``flags.interactive``), or ``PYTHONINSPECT`` (``flags.inspect`` alone)
    with a terminal on fd 0 — without one CPython exits instead, so the crash
    keeps its exit code; or already at a prompt (``sys.ps1``)."""
    if sys.flags.interactive or hasattr(sys, "ps1"):
        return True
    if not sys.flags.inspect:
        return False
    try:
        return os.isatty(0)
    except OSError:
        return False


def _crash_excepthook(exc_type, exc, tb) -> None:
    if not issubclass(exc_type, Exception) or _interactive():
        _previous_excepthook(exc_type, exc, tb)
        return
    if exc is None:  # defensive: hook called with a bare type
        exc = exc_type()
    if exc.__traceback__ is None:
        exc = exc.with_traceback(tb)
    try:
        exit_on_crash(exc)
    except SystemExit as done:
        _finalize_and_exit(done.code)


if sys.excepthook is sys.__excepthook__:
    sys.excepthook = _crash_excepthook


class Report:
    """What a ``--json`` run hands back to :func:`run`: its *document* and its
    exit *status*. ``run`` encodes the document (:func:`encode_json`), writes
    it to stdout and exits with *status* — the tool never serializes or
    prints its own document, so a crash can never leave a complete-looking
    one on stdout next to rc 2, and the encoder's rules hold for every tool.
    *status* is an exit code (an int in 0..255); anything else is the
    tool's defect, refused here rather than read the way ``sys.exit`` would.

    *on_stdout_failure* (#3450): the tool's own stderr line for stdout
    failing while ``run`` writes the document. It is given the shared
    detail (``cannot write to stdout: …``) and returns the whole line — so
    a tool whose error lines carry a code (waveform-score's ``ERROR
    [ERR_OUTPUT]: …``, which ``--redact`` prints without the detail) keeps
    it on this path too. Only the line: the exit code is *delivery*'s
    either way. A builder that raises, or
    returns anything but a non-empty str, gets the shared line instead.

    *delivery* (#3558): ``"all"`` (the default) — stdout failing is the
    run's failure, the caller-error exit. ``"best_effort"`` — the same
    stderr line, but the run keeps *status*: for a tool whose exit code
    states something a lost stdout does not change (patch-config's: what
    the ConfigMap holds after a write). Only the tools the gate lists
    (tests/shared/test_json_stdout_contract.py) may ask for it."""

    __slots__ = ("document", "status", "on_stdout_failure", "delivery")

    def __init__(self, document: Any, status: int = 0, *,
                 on_stdout_failure: Optional[Callable[[str], str]] = None,
                 delivery: str = "all") -> None:
        if status != 0 or isinstance(status, bool):
            _check_code("Report status", status)
        if on_stdout_failure is not None and not callable(on_stdout_failure):
            raise TypeError("Report on_stdout_failure must be callable")
        if delivery not in DELIVERIES:
            raise ValueError(f"Report delivery must be one of {DELIVERIES}, got {delivery!r}")
        self.document = document
        self.status = status
        self.on_stdout_failure = on_stdout_failure
        self.delivery = delivery


#: Report's *delivery* values: stdout's failure ends the run at the
#: caller-error code, or (patch-config) is said and the status kept.
DELIVERIES: Final[tuple[str, ...]] = ("all", "best_effort")


class OutputNotSerializable(Exception):
    """A document holds a value JSON cannot carry as what it means: a
    non-finite float, an int too long to print, a reference cycle, two keys
    that print as the same JSON key, or a type other than the JSON ones,
    ``date`` / ``datetime`` and a type with ``__json__``. Named by where in
    the document it is. Usually a defect of the tool that built it; a conf.d
    value written ``.inf`` / ``.nan`` (read as a float by `_lib_parse_tree`)
    lands here too — JSON has no spelling for it, so the run refuses rather
    than print one."""

    reason = "output_not_serializable"

    def __init__(self, where: str, why: str) -> None:
        super().__init__(f"{where or '(the document)'}: {why}")
        self.where = where


def _json_key(key: Any) -> Optional[str]:
    """*key* as the JSON object key ``json.dumps`` prints it, or None when
    it has none."""
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if isinstance(key, bool):
        return "true" if key else "false"
    if isinstance(key, int):
        return int.__repr__(key)
    return None


def _json_path(base: str, step: Any) -> str:
    """*base* extended by *step*: an int is a list index (``[3]``), a str an
    object key (``.name``, or ``["x y"]`` when it is not an identifier) —
    every key as the JSON key it prints as, so ``{1: …}`` reads ``["1"]``."""
    import json
    if isinstance(step, int):
        return f"{base}[{step}]"
    if step.isidentifier():
        return f"{base}.{step}" if base else step
    return f"{base}[{json.dumps(step, ensure_ascii=False)}]"


def _to_json_tree(value: Any) -> Any:
    """*value* as plain JSON data — dicts with str keys, lists, str, finite
    float, int, bool, None — or :class:`OutputNotSerializable` at the first
    value that is not JSON as what it means. ``__json__`` is called once
    and its answer obeys the same rules; ``date`` / ``datetime`` become ISO
    8601. Iterative, so a deep document is not a RecursionError."""
    import datetime
    import math
    root: list[Any] = [None]
    # (value, where, container to put it in, slot) — or an exit marker
    # (None, None, id, None) that ends a container's place on the path.
    stack: list[tuple[Any, Any, Any, Any]] = [(value, "", root, 0)]
    open_ids: set[int] = set()
    while stack:
        cur, at, into, slot = stack.pop()
        if at is None:
            open_ids.discard(into)
            continue
        while callable(getattr(type(cur), "__json__", None)):
            # The type says what it is in JSON; what it says obeys the rules.
            if id(cur) in open_ids:
                raise OutputNotSerializable(at, "a reference cycle has no JSON form")
            nxt = cur.__json__()
            if nxt is cur:
                raise OutputNotSerializable(at, f"{type(cur).__name__}.__json__ returns itself")
            cur = nxt
        if cur is None or isinstance(cur, (str, bool)):
            into[slot] = cur if cur is None or type(cur) in (str, bool) else str.__str__(cur)
        elif isinstance(cur, int):
            try:
                into[slot] = int(int.__repr__(cur))
            except ValueError:
                raise OutputNotSerializable(at, "an int too long to print") from None
        elif isinstance(cur, float):
            if not math.isfinite(cur):
                raise OutputNotSerializable(at, f"{cur!r} is not a JSON number")
            into[slot] = float(cur)
        elif isinstance(cur, (datetime.date, datetime.datetime)):
            into[slot] = cur.isoformat()
        elif isinstance(cur, (dict, list, tuple)):
            if id(cur) in open_ids:
                raise OutputNotSerializable(at, "a reference cycle has no JSON form")
            open_ids.add(id(cur))
            stack.append((None, None, id(cur), None))
            if isinstance(cur, dict):
                out: Any = {}
                children = []
                for k, v in cur.items():
                    key = _json_key(k)
                    if key is None:
                        raise OutputNotSerializable(
                            f"{at}[{k!r}]", f"a {type(k).__name__} key is not a JSON key")
                    if key in out:
                        raise OutputNotSerializable(
                            _json_path(at, key), f"two keys print as the JSON key {key!r}")
                    out[key] = None
                    children.append((v, _json_path(at, key), out, key))
            else:
                out = [None] * len(cur)
                children = [(v, _json_path(at, i), out, i) for i, v in enumerate(cur)]
            into[slot] = out
            stack.extend(reversed(children))
        else:
            raise OutputNotSerializable(at, f"a {type(cur).__name__} is not a JSON value")
    return root[0]


def encode_json(document: Any) -> str:
    """The one JSON encoder for what a tool hands its caller (#3216).

    Two-space indent, UTF-8 text (``ensure_ascii=False``), no ``NaN`` /
    ``Infinity`` (they are not JSON), ``date`` / ``datetime`` as ISO 8601, a
    value whose type defines ``__json__()`` as what that returns (checked by
    the same rules — `_lib_parse_tree.Timestamp` gives its source text); any
    other value that is not JSON as what it means is
    :class:`OutputNotSerializable`, named by its path in the document. No
    formatting options: one format for every tool. ``json.dumps`` only
    prints the plain tree :func:`_to_json_tree` built, so what was checked
    is what is printed.
    """
    import json
    return json.dumps(_to_json_tree(document), allow_nan=False, ensure_ascii=False, indent=2)


def _write_report(report: Report) -> int:
    """Encode and print *report*'s document; the exit status. A document
    that cannot be encoded is the tool's defect: one named line on stderr,
    no part of the document, and the crash exit code."""
    try:
        text = encode_json(report.document)
    except OutputNotSerializable as exc:
        _to_stderr(f"ERROR: the --json document cannot be written as JSON: {exc}")
        return _crash_code()
    global _stdout_failure_line, _best_effort
    _stdout_failure_line = report.on_stdout_failure  # #3450: through the last flush
    _best_effort = report.delivery == "best_effort"  # through the last flush too
    try:
        print(text)
    except OSError as exc:  # this write is stdout's
        if not _best_effort:
            _stdout_failed(exc)
        _report_stdout_failure(exc)
    except Exception as exc:  # noqa: BLE001 — best effort: no write changes the status
        if not _best_effort:
            raise
        _report_stdout_failure(exc)
    return report.status


def emit_result(result: Any) -> Any:
    """What *entry* returned, as the exit status: a :class:`Report` is
    written here (its document encoded and printed) and its status
    returned; anything else is returned as it is. :func:`run` takes this
    step; a caller that runs a tool's ``main`` in-process (a test, a tool
    driving another) takes it too, so the document comes out the one way."""
    return _write_report(result) if isinstance(result, Report) else result


def run(entry: Callable[[], Any]) -> NoReturn:
    """Run a tool's *entry* (its ``main``) and exit — the one way out.

    *entry*'s return value is the exit status (as ``sys.exit`` reads it), or
    a :class:`Report`, whose document is encoded and printed here. An
    ``Exception`` goes to ``exit_on_crash``. The last flush of stdout is
    made here (``_close_stdout``).

    Gate: tests/shared/test_single_exit_path.py.
    """
    global _stdout_owner, _stdout_failure_line, _best_effort
    _stdout_owner = None
    _stdout_failure_line = None
    _best_effort = False
    try:
        status = _contract_status(emit_result(entry()))
    except SystemExit as exc:
        status = _contract_status(exc.code)
    except Exception as exc:  # noqa: BLE001 — that is the point
        if _interactive():
            raise
        try:
            exit_on_crash(exc)
        except SystemExit as done:
            status = done.code  # the crash code: the contract's own, not checked
    _finalize_and_exit(status)


# ── The exit-code registry (#3611) ────────────────────────────────────

#: An entry may also end at any code its wrapped command ends at.
PASSTHROUGH: Final[str] = "passthrough"

#: ⛔ The one list of exit codes beyond 0/1/2, by entry module name (the
#: ``__main__`` file's stem — the da-tools image is flat, so a path would
#: not match there). ``run()`` refuses any other code at runtime (#3611):
#: a code a tool picked on its own, a multiple of 256 (which the OS would
#: truncate to 0 — a false success) or a non-int status. Each row is an
#: owner-sanctioned exception; dev-rules §13 names them.
EXTENDED_EXIT_CODES: Final = MappingProxyType({
    # Network-blocked host: "switch host", not 2's "gh missing" (diag_pr_ci).
    "diag_pr_ci": frozenset({3}),
    # What happened after or around the write (#1950, docs/cli-reference.md).
    "patch_config": frozenset({3, 4, 5, 6, 7}),
    # da-guard's 3 (cannot decode) and 4 (not judged), passed through (#2123, #2904).
    "guard_dispatch": frozenset({3, 4}),
    # Wrappers of an arbitrary command (#3577 R3): `--run` ends at the wrapped
    # command's own code; the wrapper's own failures are 2, named on stderr.
    "dx_run": PASSTHROUGH,
    # `git commit`'s own code (a hook's refusal among them), passed through.
    "commit_helper": PASSTHROUGH,
})


def _entry_name() -> str:
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None) or ""
    return os.path.splitext(os.path.basename(path))[0]


def _contract_status(status: Any) -> Any:
    """*status* as ``run()`` may end with it (#3611): None, 0/1/2, a code
    the registry gives this entry — or, for anything else, the caller-error
    code after one ``ERROR:`` line (under tests: the test crash code, so a
    test pinning an unregistered code goes red). A string is printed first,
    as Python would."""
    if status is None or isinstance(status, bool):
        return status
    name = _entry_name()
    allowed = EXTENDED_EXIT_CODES.get(name, frozenset())
    if isinstance(status, int):
        if status in (0, 1, 2) or (
                1 <= status <= 255 and (allowed == PASSTHROUGH or status in allowed)):
            return status
        why = f"exit status {status} is not in {name or 'this tool'}'s exit-code contract"
    else:
        _to_stderr(str(status))
        why = f"a non-integer exit status ({type(status).__name__}) ends as a caller error"
        if crash_exit_code_from_env() is None:
            return _caller_error_code
    _to_stderr(f"ERROR: {why} (scripts/tools/_lib_exitcodes.py EXTENDED_EXIT_CODES, #3611)")
    override = crash_exit_code_from_env()
    return override if override is not None else _caller_error_code


# ── stdout failures (#3124) ───────────────────────────────────────────

# #3450: the Report being written names its own stdout-failure line.
_stdout_failure_line: Optional[Callable[[str], str]] = None
_failure_reported = False
# #3558: the Report written asked for delivery="best_effort".
_best_effort = False


def _from_stdout(exc: BaseException) -> bool:
    """*exc* is stdout's failure: an OSError naming no file, and stdout,
    asked once more, fails too (what it still buffers will not go). The
    attribution only picks the stderr line: both answers are non-zero."""
    if not isinstance(exc, OSError) or exc.filename is not None:
        return False
    out = sys.stdout
    if out is None:
        return False
    try:
        out.flush()
    except OSError:
        return True
    except ValueError:  # closed
        return False
    # CPython drops the bytes a failed write could not send, so the flush
    # above often succeeds: for EPIPE, ask whether stdout's reader is gone.
    # Only the message depends on it (both exits are non-zero).
    if getattr(exc, "errno", None) != 32:  # EPIPE
        return False
    try:
        import select
        p = select.poll()
        p.register(sys.__stdout__.fileno(), select.POLLOUT)
        return any(ev & select.POLLERR for _, ev in p.poll(0))
    except Exception:  # noqa: BLE001 — no poll / no fd: not attributed
        return False


def _silence_stdout() -> None:
    """Point stdout's fd at the null device, so what it still buffers is
    dropped there and the interpreter's last flush cannot fail (no 120)."""
    err = sys.stderr
    for out in {id(s): s for s in (sys.stdout, sys.__stdout__) if s is not None}.values():
        if out is not err:  # a stdout rebound to stderr keeps stderr
            _to_null(out)


def _to_null(stream: Any) -> None:
    """Point *stream*'s fd at the null device (best effort). A closed fd is
    the lowest free one, which ``os.open`` may hand back: keep it then."""
    try:
        fd = stream.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        if devnull != fd:
            os.dup2(devnull, fd)
            os.close(devnull)
    except Exception:  # noqa: BLE001 — best effort
        pass


def _failure_line(exc: BaseException) -> str:
    global _failure_reported
    if _failure_reported:
        return ""
    _failure_reported = True
    line = f"ERROR: cannot write to stdout: {getattr(exc, 'strerror', None) or exc}"
    build = _stdout_failure_line
    if build is None:
        return line
    try:
        own = build(line[len("ERROR: "):])
    except Exception:  # noqa: BLE001 — the exit must still say why
        return line
    return own if isinstance(own, str) and own.strip() else line


def _stdout_failed(exc: OSError) -> NoReturn:
    """The stdout-failure exit: one line, the caller-error code."""
    _report_stdout_failure(exc)
    sys.exit(_caller_error_code)


def _report_stdout_failure(exc: BaseException) -> None:
    """Say that stdout failed (*exc*), once per run, and point stdout at the
    null device, so nothing it still buffers fails again."""
    _silence_stdout()
    _to_stderr(_failure_line(exc))


def note_stdout_failure(exc: BaseException) -> None:
    """A stdout write the tool caught itself failed (*exc*): say so, and
    deliver the rest of this run best effort — as
    ``Report(..., delivery="best_effort")`` does — so the run keeps the
    tool's status. For patch-config only (the gate's list): its output is
    written mid-apply, where a raise would change what apply does; the
    failure is said, not swallowed."""
    global _best_effort
    _best_effort = True
    _report_stdout_failure(exc)


def _close_stdout(status: int) -> int:
    """The last flush of stdout (gnulib ``close_stdout``): the exit code.
    stderr is flushed too; its failure only loses text (no 120)."""
    err = sys.stderr
    if err is not None:
        try:
            err.flush()
        except Exception:  # noqa: BLE001 — stderr's reader left: drop the rest
            _to_null(err)
    # The original stdout too: a tool may have rebound sys.stdout, and the
    # interpreter's own last flush of a stream no longer bound is silent.
    for out in {id(o): o for o in (sys.stdout, sys.__stdout__) if o is not None}.values():
        try:
            out.flush()
        except ValueError:  # closed
            pass
        except Exception as exc:  # noqa: BLE001 — sorted below
            if _best_effort:  # the status stands; the failure is said
                _report_stdout_failure(exc)
                continue
            if not isinstance(exc, OSError):
                raise
            try:
                _stdout_failed(exc)
            except SystemExit as done:
                return done.code
    return status


def _exit_status(status: Any) -> int:
    if status is None:
        return 0
    if isinstance(status, int):
        return int(status) & 0xFF
    return 1


def _finalize_and_exit(status: Any) -> NoReturn:
    code = _close_stdout(_exit_status(status))
    if code != _exit_status(status):
        if status is not None and not isinstance(status, int):
            _to_stderr(str(status))  # sys.exit("msg"): printed, as Python would
        raise SystemExit(code)
    raise SystemExit(status)


def _close_stdout_at_exit() -> None:
    """Fallback for a process that reaches neither ``run()`` nor the
    excepthook: the same last flush; a failure changes the status by
    ``os._exit``."""
    code = _close_stdout(0)
    if code:
        os._exit(code)


import atexit as _atexit  # noqa: E402

_atexit.register(_close_stdout_at_exit)

_IMPORT_CRASH_EXIT_CODE = _crash_exit_code
_IMPORT_CALLER_ERROR_CODE = _caller_error_code
