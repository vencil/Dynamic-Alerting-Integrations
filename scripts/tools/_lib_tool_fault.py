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

One reason is not a fault: :data:`MEMORY_END`, a program the Go runtime
ended because it could not get memory, while this process runs under a
memory limit (RLIMIT_DATA, which every program inherits): ended by one of
the signals the runtime ends itself with (:data:`MEMORY_SIGNALS`), or —
when it fails before it has read ``GOTRACEBACK``, at its start — exit 2
with one of its own out-of-memory lines on stderr
(:data:`GO_OUT_OF_MEMORY`). That is the size of the write, not a broken
program: the service rejects that write as ``memory`` and leaves its
readiness as it was, where a fault makes it answer 503 and not ready. It
is recorded all the same, because a verdict reached without that program
is no verdict either: the judgement process reports ``memory`` for it,
never ``ran``. :func:`end_reason` is the one place that tells the two
apart, for all three run functions.

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

#: The reason of a program the Go runtime ended for want of memory under
#: the memory limit (:func:`end_reason`): the judgement's memory, not a
#: fault (see the module docstring).
MEMORY_END = "memory_end"

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
    MEMORY_END,         # the Go runtime's own end for want of memory: not a fault
)

#: The reasons that are a fault of the program: every one but MEMORY_END.
FAULT_REASONS = tuple(r for r in REASONS if r != MEMORY_END)

#: The signals the Go runtime ends itself with under ``GOTRACEBACK=crash``
#: (a fatal error, an out-of-memory, an allocation failure that faults
#: inside the runtime): under the judgement's memory limit, its memory.
MEMORY_SIGNALS = frozenset({signal.SIGABRT, signal.SIGSEGV, signal.SIGBUS})

#: The messages of the Go runtime's own ``throw`` when it cannot get memory
#: (every one in src/runtime of go1.26: ``grep -rhoE 'throw\("[^"]*(out of
#: memory|cannot allocate)[^"]*"\)'``). ``throw`` writes each as one stderr
#: line ``fatal error: <message>`` and exits 2 — unless ``GOTRACEBACK=crash``
#: has been read, which a runtime that fails while it starts (``heap arena
#: map`` at a small limit) has not.
GO_OUT_OF_MEMORY = frozenset({
    "out of memory",
    "out of memory (stackalloc)",
    "out of memory allocating allArenas",
    "out of memory allocating checkmarks bitmap",
    "out of memory allocating heap arena map",
    "out of memory allocating heap arena metadata",
    "pageAlloc: out of memory",
    "runtime: cannot allocate memory",
    "runtime: out of memory",
    "trace: out of memory",
    "traceRegion: out of memory",
    "heapdump: out of memory",
})

#: The exit code of a Go runtime's ``throw``.
_GO_THROW_EXIT = 2

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
    """The records that are a program's fault (not :data:`MEMORY_END`)."""
    return [r for r in records() if r.reason != MEMORY_END]


def memory_ends() -> "list[ToolFault]":
    """The records of a program that ended as :data:`MEMORY_END`."""
    return [r for r in records() if r.reason == MEMORY_END]


def go_out_of_memory(stderr: "bytes | str | None") -> bool:
    """Whether *stderr* holds the Go runtime's own out-of-memory line: a
    line that is exactly ``fatal error: `` and one of
    :data:`GO_OUT_OF_MEMORY`."""
    if not stderr:
        return False
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    prefix = "fatal error: "
    return any(line.startswith(prefix) and line[len(prefix):] in GO_OUT_OF_MEMORY
               for line in (ln.rstrip("\r") for ln in stderr.split("\n")))


def end_reason(returncode: int, stderr: "bytes | str | None",
               memory_limited: bool) -> "str | None":
    """The reason to record for a program that ended *returncode* (< 0:
    ended by signal ``-returncode``) with *stderr*, when that end is not an
    exit its caller reads; None when it is.

    * a signal: :data:`MEMORY_END` for one of :data:`MEMORY_SIGNALS`, else
      ``signalled`` (a SIGKILL from outside, …);
    * exit 2 with the Go runtime's own out-of-memory line
      (:func:`go_out_of_memory`), only while *memory_limited* (this process
      runs under its RLIMIT_DATA): :data:`MEMORY_END`. Without a limit that
      exit is read as before — a command-line tool does not change;
    * anything else: None."""
    if returncode < 0:
        return MEMORY_END if -returncode in MEMORY_SIGNALS else "signalled"
    if memory_limited and returncode == _GO_THROW_EXIT and go_out_of_memory(stderr):
        return MEMORY_END
    return None


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
