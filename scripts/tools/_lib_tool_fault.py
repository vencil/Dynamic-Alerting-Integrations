"""The one record of "an external program of this process did not run" (#3862).

The three functions that run da-guard, da-read and da-crdecode
(``_lib_tenant_values._run_da_guard``, ``_lib_parse_tree._run_stdout``,
``_lib_consumer_read._invoke``) and the readers of their output call
:func:`record` in the branch that has decided the PROGRAM failed, not the
input: it is not there or cannot be run, it did not finish, a signal ended
it, it exited with a code its subcommand never uses, its output is not the
document it writes, or it is older than this tool. An answer about the
input — a file da-guard cannot read, da-read refusing a config, a tree
da-guard declines to judge — is never recorded.

Recording changes nothing a command-line tool does: the callers raise and
return exactly as before. Only the generator service reads it: its
judgement process (``_gsvc_child``) reports a judgement with a fault as
``tool_fault``, and the service answers that write 503 (ADR-036 §3) — a
verdict reached without one of its programs is no verdict. Only a
judgement that ran through (outcome ``ran``) clears the service's mark.

A record is ``(binary, subcommand, reason)``: the program's name, never its
path, and a reason from :data:`REASONS`, never its stderr — what reaches a
tenant-api caller must not carry the container's paths or a program's
prose.

One reason is not a fault: :data:`MEMORY_SIGNAL`, a program ended by one of
the signals the Go runtime ends itself with when it runs out of memory
(:data:`MEMORY_SIGNALS`; every program inherits the judgement's
RLIMIT_DATA). That is the size of the write, not a broken program: the
service rejects that write as ``memory`` and stays ready, where a fault
makes it answer 503 and not ready. It is recorded all the same, because a
verdict reached without that program is no verdict either: the judgement
process reports ``memory`` for it, never ``ran``. :func:`signal_reason` is
the one place that tells the two apart, for all three run functions.

Standard library only.
"""
from __future__ import annotations

import errno
import os
import signal
import threading
from typing import NamedTuple

#: The programs whose failure is recorded.
BINARIES = ("da-guard", "da-read", "da-crdecode")

#: The reason of a program ended by one of :data:`MEMORY_SIGNALS`: the
#: judgement's memory, not a fault (see the module docstring).
MEMORY_SIGNAL = "memory_signal"

#: Why a program did not run, a closed set.
REASONS = (
    "not_found",        # no such file (the resolution named nothing, or nothing is there)
    "not_executable",   # there, but not executable by this process
    "exec_failed",      # exec failed otherwise: not a program this system runs, EAGAIN, …
    "timed_out",        # did not finish within its time limit
    "signalled",        # ended by a signal (not one of MEMORY_SIGNALS)
    "unexpected_exit",  # an exit code its subcommand never uses
    "bad_output",       # its output is not the document it writes
    "stale",            # older than this tool (a subcommand, flag or field it lacks)
    MEMORY_SIGNAL,      # ended by one of MEMORY_SIGNALS: not a fault
)

#: The reasons that are a fault of the program: every one but MEMORY_SIGNAL.
FAULT_REASONS = tuple(r for r in REASONS if r != MEMORY_SIGNAL)

#: The signals the Go runtime ends itself with under ``GOTRACEBACK=crash``
#: (a fatal error, an out-of-memory, an allocation failure that faults
#: inside the runtime): under the judgement's memory limit, its memory.
MEMORY_SIGNALS = frozenset({signal.SIGABRT, signal.SIGSEGV, signal.SIGBUS})

#: At most this many records are kept (the first ones): a judgement needs
#: only to know that one exists, and a tool that keeps running after a
#: failure must not grow a list without end.
MAX_RECORDS = 16


class ToolFault(NamedTuple):
    binary: str
    subcommand: str
    reason: str

    def as_dict(self) -> dict:
        return {"binary": self.binary, "subcommand": self.subcommand, "reason": self.reason}


_RECORDS: "list[ToolFault]" = []
#: da-crdecode runs in a thread pool (`_lib_consumer_read._files_of`).
_LOCK = threading.Lock()


def record(binary: str, subcommand: str, reason: str) -> None:
    """Record that *binary* (one of :data:`BINARIES`) did not run
    *subcommand*, for *reason* (one of :data:`REASONS`). A record already
    kept is not kept twice."""
    if binary not in BINARIES:
        raise ValueError(f"not a recorded binary: {binary!r}")
    if reason not in REASONS:
        raise ValueError(f"not a tool-fault reason: {reason!r}")
    fault = ToolFault(binary, subcommand, reason)
    with _LOCK:
        if fault not in _RECORDS and len(_RECORDS) < MAX_RECORDS:
            _RECORDS.append(fault)


def records() -> "list[ToolFault]":
    """Every record since the last :func:`clear`, in order."""
    with _LOCK:
        return list(_RECORDS)


def faults() -> "list[ToolFault]":
    """The records that are a program's fault (not :data:`MEMORY_SIGNAL`)."""
    return [r for r in records() if r.reason != MEMORY_SIGNAL]


def memory_ends() -> "list[ToolFault]":
    """The records of a program ended by one of :data:`MEMORY_SIGNALS`."""
    return [r for r in records() if r.reason == MEMORY_SIGNAL]


def signal_reason(returncode: int) -> str:
    """The reason of a program that exited *returncode* (< 0: ended by
    signal ``-returncode``): :data:`MEMORY_SIGNAL` for one of
    :data:`MEMORY_SIGNALS`, else ``signalled`` (a SIGKILL from outside, …)."""
    return MEMORY_SIGNAL if -returncode in MEMORY_SIGNALS else "signalled"


def clear() -> None:
    with _LOCK:
        _RECORDS.clear()


def os_error_reason(exc: OSError, path: "str | None" = None) -> str:
    """The reason of an exec of *path* that raised *exc*. ENOENT for a file
    that is there is its interpreter line naming nothing: an exec failure."""
    if isinstance(exc, FileNotFoundError) or exc.errno == errno.ENOENT:
        return "exec_failed" if path and os.path.lexists(path) else "not_found"
    if isinstance(exc, PermissionError) or exc.errno in (errno.EACCES, errno.EPERM):
        return "not_executable"
    return "exec_failed"
