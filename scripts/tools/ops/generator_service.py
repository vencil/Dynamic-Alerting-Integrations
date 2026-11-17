#!/usr/bin/env python3
"""`da-tools generator-service`: judges conf.d writes for tenant-api (ADR-036 §3).

The generator container's service (ADR-036 §3, §5, §6; #2901).

tenant-api asks it, before every write, whether the conf.d tree with that
write applied passes ``generate-routes --validate --strict``. It speaks
HTTP/1.1 on a Unix socket (no network port) and serves:

* ``POST /v1/validate`` — judge a base commit plus writes; answers 200 with
  ``result`` ``pass`` / ``findings`` / ``rejected`` whenever this process
  reaches a verdict for that request, 400 for a malformed request, 503 when
  the container cannot judge (``VALIDATOR_UNAVAILABLE``: an external program
  the judgement runs — da-guard, da-read, da-crdecode — did not run
  (``tool_fault``, #3862), a generator older than the policy's
  ``min_generator_version``; ``POLICY_UNAVAILABLE``: the deployment
  ``--policy`` file is unusable);
* ``GET /v1/status`` — policy availability, generator and da-guard versions,
  the programs' self-check, the latest tool fault, readiness;
* ``GET /metrics`` — Prometheus text format.

READINESS (#3943). The service is ready only once the self-check of each
program a judgement runs — ``da-guard self-check``, ``da-read self-check``,
``da-crdecode --self-check``, resolved as the judgement resolves them — has
passed: each runs every subcommand of its own dispatch table on fixtures
built into it. Each round first probes --repo (#4035) with the judgement of
--repo start-up makes (``_repo_judgement``: its object format, and a HEAD
that is a commit git reads or a branch with no commit yet), each git run as
a program's self-check is run: a --repo git no longer reads, an object
format other than the one at start, a git that cannot be run or that does
not answer within --self-check-timeout-seconds fails the round. The first
round starts once the socket is bound (``pending`` until it ends: not
ready); a tool fault of a judgement or a warming run makes the service not
ready at once and starts a round at once too; a judgement's ``git_read``
asks for one — no sooner than --self-check-seconds after the latest round
ended, the asks meanwhile one — and leaves ready to it. After a failed round
the next comes every ``--self-check-seconds``, or at once when a new tool
fault comes. A step past its time limit fails the round that moment. Only a
passing round clears the fault — a judgement that ran may never have
reached the program that failed. The self-check decides readiness only,
never a verdict.

Defaults of every limit are in ``--help``; this docstring does not repeat
them.

⛔ ONE THREAD, NO EXCEPTIONS. Every judgement is a ``fork`` of this process,
so the judgement inherits the parse cache without copying it. A fork in a
process with more than one thread can leave the child waiting forever on a
lock another thread held at the moment of the fork (Python 3.12 warns about
exactly that). So the server is ``asyncio.start_unix_server`` on one event
loop; there is no ``run_in_executor``, no ``ThreadingHTTPServer`` and nothing
else that starts a thread, and :meth:`Service._fork_judgement` refuses to fork
when ``threading.active_count()`` is not 1.

⛔ This process never parses what a request carries. It reads the request's
JSON envelope (paths, commit id, deadline) and checks that each
``content_b64`` is base64; it never parses the YAML. Fetching the base tree,
applying the writes and running the generator happen in the judgement process
(``_gsvc_child``), under ``RLIMIT_DATA`` and the judgement deadline, in a
process group of its own that the deadline ends with ``killpg``. What the
judgement process sends back — the result and new parse-cache entries — is
read within byte limits, and the cache entries are kept as opaque bytes.

THREAT MODEL. (1) The trust boundary is the socket: it is created 0600, the
peer must run as this process's uid (SO_PEERCRED, checked on every
connection; anyone else is closed unanswered and counted in
``da_generator_peer_rejected_total``), and only the tenant-api container
mounts the directory holding it (the chart will pin that with a test, #2902).
(2) The adversary is an API user writing through tenant-api: what a request
carries and how fast requests come. The admission limits (connection slots,
in-flight body bytes, queue, writes per request) bound that relayed load;
they are not a defence against a hostile local peer. (3) Known limits, both
needing code execution next to the socket: a local peer's idle connections
can hold reserved budget until the read timeout, and a connection flood can
use up descriptors. Whoever can run code in the tenant-api container can
already write to the repository directly, and whoever can run code in this
container can already replace the judgement itself.

One judgement runs at a time; the others wait in a bounded queue. Each
request's time budget is ``min(--max-judgement-seconds, deadline_ms -
--deadline-reserve-seconds)`` from the moment its header has been read —
reading the body and queueing included.
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import hashlib
import json
import os
import posixpath
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _THIS_DIR)  # Docker flat layout
sys.path.insert(0, os.path.join(_THIS_DIR, ".."))  # Repo subdir layout

from _lib_exitcodes import EXIT_CALLER_ERROR, EXIT_OK  # noqa: E402
from _lib_paths import classify_path  # noqa: E402  (#3276)
from _lib_label import safe_label  # noqa: E402  (#3415)
import _gsvc_child  # noqa: E402  (imports the whole generator — see _gsvc_child)
from _gsvc_child import ChildJob, gar  # noqa: E402
from _gsvc_proto import (  # noqa: E402
    SELF_CHECK_GIT_BINARY, UNAVAILABLE_REASONS, CacheStore, FrameReader, RequestError,
    ValidateRequest,
    judgement_seconds, parse_validate_request, tool_fault_message, tool_faults_of,
)
from _lib_parse_cache import restricted_loads  # noqa: E402
import _lib_consumer_read  # noqa: E402  (absolute_reader_binaries, #3424)
import _lib_tool_fault  # noqa: E402  (os_error_reason: one reading of a failed exec)
from _grar_validate import (  # noqa: E402
    PolicyInputError, generator_version_problem, load_policy_document,
)

MiB = 1024 * 1024

# ⛔ The one place these numbers are written (ADR-036 §3). Docs say "see
# --help"; nothing else repeats them.
DEFAULT_MAX_JUDGEMENT_SECONDS = 20.0
DEFAULT_DEADLINE_RESERVE_SECONDS = 5.0
DEFAULT_MEMORY_LIMIT_BYTES = 192 * MiB
DEFAULT_CACHE_MAX_BYTES = 32 * MiB
DEFAULT_MAX_QUEUE = 32
# #2901 R1, admission: sized so that, all full, this process stays within the
# about 80 Mi ADR-036 §3 budgets for it — the reasoning and the measurements
# are in the pull request's description. The in-flight budget must hold at
# least one largest body (checked at start, _check_limits).
DEFAULT_MAX_BODY_BYTES = 4 * MiB
DEFAULT_MAX_INFLIGHT_BODY_BYTES = 4 * MiB
DEFAULT_MAX_CONNECTIONS = 40
DEFAULT_MAX_WRITES = 10_000
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULT_BYTES = 16 * MiB
DEFAULT_MAX_CACHE_TRANSFER_BYTES = 64 * MiB
DEFAULT_WARM_REF = "HEAD"
DEFAULT_WARM_SECONDS = 300.0
DEFAULT_SELF_CHECK_SECONDS = 30.0
DEFAULT_SELF_CHECK_TIMEOUT_SECONDS = 60.0
#: A self-check's stdout read back: its whole document is far shorter, and
#: anything longer is not it. Its stderr: the start goes to the log.
_SELF_CHECK_STDOUT_LIMIT = 4096
_SELF_CHECK_STDERR_LOGGED = 1024
#: RLIMIT_FSIZE of a self-check process: its stdout and stderr files (and
#: da-guard's fixture tree copy) live in --work-dir, and a program that
#: writes without end must not fill it. Far above what a passing one writes.
_SELF_CHECK_FILE_LIMIT_BYTES = 1 * MiB


def _self_check_limits(memory_limit: int):
    """The self-check process's limits, set before exec: RLIMIT_DATA
    *memory_limit* — --memory-limit-bytes, the one a judgement's Go programs
    run under (inherited from the judgement process), so a self-check fits
    the container's budget as one of them does — and RLIMIT_FSIZE, so a
    program that writes without end is stopped (EFBIG, or SIGXFSZ ends it)."""
    def limit() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_DATA, (memory_limit, memory_limit))
        resource.setrlimit(resource.RLIMIT_FSIZE,
                           (_SELF_CHECK_FILE_LIMIT_BYTES, _SELF_CHECK_FILE_LIMIT_BYTES))
    return limit


HEADER_LIMIT_BYTES = 16 * 1024
#: Descriptors kept free beyond the connection slots: stdio, the listening
#: socket, the event loop's epoll and self-pipe, a judgement's result pipe
#: and pidfd, the policy copy and status reads, with margin.
FD_RESERVE = 32
_PR_SET_CHILD_SUBREAPER = 36
#: Errors starting a judgement that mean "loaded", answered busy: out of
#: descriptors (pipe, pidfd) or of processes / memory (fork).
_LOAD_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE, errno.EAGAIN, errno.ENOMEM})
# A connection answered without its input read (busy, 413) is half-closed and
# drained for at most this long, at most this many at once (fds: within
# FD_RESERVE).
_LINGER_SECONDS = 1.0
_MAX_LINGERING = 16
_SLOT_FULL_READ_SECONDS = 0.05
#: An accept() that fails for lack of resources is counted every time and
#: logged at most once per errno in this many seconds (#2901 N2).
ACCEPT_ERROR_LOG_INTERVAL_SECONDS = 60.0
_KILL_GRACE_SECONDS = 5.0

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found",
            405: "Method Not Allowed", 411: "Length Required",
            413: "Content Too Large", 431: "Request Header Fields Too Large",
            500: "Internal Server Error", 501: "Not Implemented",
            503: "Service Unavailable"}


@dataclass
class Settings:
    socket: str
    repo: str
    config_subdir: str
    work_dir: str
    policy: Optional[str]
    git: str
    da_guard_binary: Optional[str]
    max_judgement_seconds: float
    deadline_reserve_seconds: float
    memory_limit_bytes: int
    cache_max_bytes: int
    max_queue: int
    max_body_bytes: int
    read_timeout_seconds: float
    max_result_bytes: int
    max_cache_transfer_bytes: int
    warm_ref: str
    warm_seconds: float
    max_connections: int
    max_inflight_body_bytes: int
    max_writes: int
    self_check_seconds: float = DEFAULT_SELF_CHECK_SECONDS
    self_check_timeout_seconds: float = DEFAULT_SELF_CHECK_TIMEOUT_SECONDS


class Response:
    def __init__(self, status: int, doc: Optional[dict] = None, *,
                 text: str | None = None, headers: Optional[dict] = None) -> None:
        self.status = status
        self.unread_body = False  # answered before the body was read
        self.doc = doc if text is None else None
        self.text = text
        self.headers = headers or {}

    @property
    def body(self) -> bytes:
        if self.text is not None:
            return self.text.encode("utf-8")
        # ASCII (#3961): any value — a lone surrogate included — encodes, so
        # an answer is never lost to its own encoding.
        return (json.dumps(self.doc) + "\n").encode("ascii")

    @property
    def ctype(self) -> str:
        return ("text/plain; version=0.0.4; charset=utf-8" if self.text is not None
                else "application/json")

    def encode(self) -> bytes:
        body = self.body
        lines = [f"HTTP/1.1 {self.status} {_REASONS.get(self.status, 'Unknown')}",
                 f"Content-Type: {self.ctype}",
                 f"Content-Length: {len(body)}",
                 "Connection: close"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


# struct ucred: pid_t (signed), uid_t and gid_t (unsigned — a uid mapped by
# a user namespace can be 2^31 or more, and reading it signed would reject
# every peer).
_UCRED = struct.Struct("iII")


def ucred_uid(raw: bytes) -> int:
    """The uid in an SO_PEERCRED ``struct ucred``."""
    return _UCRED.unpack(raw)[1]


_CONTENT_LENGTH = re.compile(r"[0-9]{1,12}")  # ASCII digits only (#2901 O1)


def _parse_head(head: bytes) -> tuple[str, str, dict[str, list[str]]]:
    """(method, target, headers by lower-case name); raises on anything
    malformed."""
    lines = head.decode("latin-1").split("\r\n")
    method, target, version = lines[0].split(" ")
    if not version.startswith("HTTP/1."):
        raise ValueError(version)
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise ValueError(line)
        headers.setdefault(name.lower(), []).append(value.strip())
    return method, target, headers


# #3961: an answer to /v1/validate carries no free text — only a closed
# `error` / `reason` and the structured fields. What the service knows
# beyond the reason goes to its own stderr (`_log`), not to tenant-api:
# tenant-api renders every message from the reason.
def _rejected(reason: str, **extra) -> Response:
    doc = {"result": "rejected", "reason": reason, "rc": None, "findings": None,
           "validated": None}
    doc.update(extra)
    return Response(200, doc)


def _unavailable(error: str, reason: str, detail: str, **extra) -> Response:
    _log(f"{error} ({reason}): {detail}")
    return Response(503, {"error": error, "reason": reason, **extra}, headers={"Retry-After": "5"})


def _error(status: int, error: str, **headers: str) -> Response:
    """A non-200 answer to a request the service does not take: its closed
    `error` only."""
    return Response(status, {"error": error}, headers=headers or None)


def _log(line: str) -> None:
    # One line whatever it holds (#3961): a request's own text (an unknown
    # field's name) must not forge a log line — `safe_label`, the one exit.
    print(f"generator-service: {safe_label(line)}", file=sys.stderr, flush=True)


@dataclass
class Ticket:
    request: ValidateRequest
    deadline_at: float  # loop time
    future: asyncio.Future
    timer: Optional[asyncio.TimerHandle] = None
    received: float = 0.0  # loop time the body was read
    started: Optional[float] = None  # loop time it left the queue


@dataclass
class ChildRun:
    """How one forked process ended, and what it streamed back."""

    timed_out: bool
    status: int
    reader: FrameReader
    received: int  # cache entries admitted into the store


@dataclass
class HeldRun:
    """How one process of a self-check round ended (``Service._run_held``)."""

    rc: int
    stdout: bytes  # at most _SELF_CHECK_STDOUT_LIMIT + 1 bytes
    stderr: bytes  # at most _SELF_CHECK_STDERR_LOGGED bytes, for the log
    timed_out: bool


#: #4035: an exec of --git failing with one of these is the git program's
#: own (gone, not executable, not a program): git cannot read --repo. Any
#: other (EMFILE, ENOMEM, …) is this service's own failure.
_GIT_EXEC_ERRNOS = frozenset({errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOEXEC})

#: How often a process the loop cannot watch (no pidfd, no reader) is asked
#: whether it has ended: from the first to the last, doubling.
_POLL_FIRST_SECONDS = 0.005
_POLL_MAX_SECONDS = 0.25


class _ExecFailed(Exception):
    """A self-check round's process could not be started: its OSError."""

    def __init__(self, error: OSError) -> None:
        super().__init__(error)
        self.error = error


@dataclass
class PolicySnapshot:
    configured: bool
    available: bool
    sha256: Optional[str] = None
    error: Optional[str] = None
    min_generator_version: Optional[str] = None
    copy: Optional[str] = None


@dataclass
class Metrics:
    judgements: Counter = field(default_factory=Counter)
    rejections: Counter = field(default_factory=Counter)
    unavailable: Counter = field(default_factory=Counter)
    warm_runs: Counter = field(default_factory=Counter)
    #: #3943: self-check rounds of the three programs, by state.
    self_checks: Counter = field(default_factory=Counter)
    orphans_reaped: int = 0
    accept_errors: Counter = field(default_factory=Counter)
    peer_rejected: int = 0
    peak_rss_bytes: int = 0
    #: ADR-038 decision 5: key verdicts by outcome — complete, or why not
    #: (KEY_VERDICT_OUTCOMES) — and the latest one's duration.
    key_verdicts: Counter = field(default_factory=Counter)
    key_verdict_seconds: float = 0.0
    #: ADR-038 decision 5: the time the latest judgement took from its
    #: request's arrival (queue included) over its time budget (deadline_ms,
    #: capped) — the supported size's own measure.
    judgement_deadline_ratio: float = 0.0


#: A key verdict's outcomes (``complete`` or ``_gsvc_child``'s reasons).
KEY_VERDICT_OUTCOMES = ("complete", "no_time", "deadline", "memory", "error",
                        "undecidable", "platform_file")


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def da_guard_version(binary: Optional[str], timeout: float = 10) -> Optional[str]:
    """``da-guard --version``'s version, or None when there is no binary."""
    import guard_dispatch
    exe = guard_dispatch.DISPATCHER.resolve_binary(binary)
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = proc.stdout.decode("utf-8", "replace").split()
    return words[-1] if proc.returncode == 0 and words else None


def _given(flag: Optional[str]) -> bool:
    """Whether --da-guard-binary names a da-guard. Blank is not given, as a
    blank $DA_GUARD_BINARY is not set (the dispatcher strips it)."""
    return bool(flag and flag.strip())


def _pin_readers() -> None:
    """Each reader binary found now, written absolute to its variable
    (``_lib_consumer_read.absolute_reader_binaries``, a ``$PATH`` hit
    included). ⛔ Before every judgement, not only at start: the judgement
    process inherits the variables and resolves the readers from its own job
    directory, where a relative ``$PATH`` entry names another place — so the
    reader found here is the one the judgement runs. A reader that appears
    on ``$PATH`` after start is found, and pinned, before the next
    judgement; once pinned, that path is the reader until the service
    restarts."""
    _lib_consumer_read.absolute_reader_binaries(pin_path_hits=True)


def snapshot_policy(path: Optional[str], into: str) -> PolicySnapshot:
    """The deployment policy file, copied into *into* and read from the copy
    — so its hash is the hash of what the generator reads."""
    if path is None:
        return PolicySnapshot(configured=False, available=True)
    copy = os.path.join(into, "policy.yaml")
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        return PolicySnapshot(True, False, error=f"--policy: cannot read {path!r}: {exc}")
    with open(copy, "wb") as fh:
        fh.write(data)
    os.chmod(copy, 0o600)
    digest = hashlib.sha256(data).hexdigest()
    try:
        doc = load_policy_document(copy)
    except PolicyInputError as exc:
        return PolicySnapshot(True, False, sha256=digest,
                              error=str(exc).replace(copy, path))
    return PolicySnapshot(True, True, sha256=digest,
                          min_generator_version=doc.min_generator_version,
                          copy=copy)


class Service:
    def __init__(self, settings: Settings, object_format: str) -> None:
        self.s = settings
        self.object_format = object_format
        self.store = CacheStore(settings.cache_max_bytes)
        self.metrics = Metrics()
        self.waiting: deque[Ticket] = deque()
        self.running: Optional[Ticket] = None
        self.warming: Optional[str] = None  # the commit a warming run is on
        self.child_pid: Optional[int] = None
        self.pending_warm: Optional[str] = None
        self.pinned_commit: Optional[str] = None
        self.warm_commit: Optional[str] = None
        self.warm_error: Optional[str] = None
        self._request_pinned = False
        self._preempting = False
        self.generator_version = gar.da_tools_version()
        # #3415: the one resolution ($DA_GUARD_BINARY, which main() set from
        # --da-guard-binary): the version reported is the da-guard that judges.
        self.da_guard_version = da_guard_version(None)
        #: #3862: the latest tool faults of a judgement or a warming run
        #: (``_lib_tool_fault`` records, as dicts), or None. Not ready while
        #: set; only a self-check that passes clears it (#3943) — one that
        #: started before the latest fault does not (``_fault_seq``).
        self.tool_fault: Optional[list] = None
        self._fault_seq = 0
        #: #3943: the latest self-check round of the three programs
        #: (``_gsvc_proto.SELF_CHECK_STATES``), its failures and whether one
        #: is running. Not ready until one has passed.
        self.self_check_state = "pending"
        self.self_check_failures: list = []
        self.self_check_running = False
        self._self_check_task: Optional[asyncio.Task] = None
        #: Set by a tool fault while the loop waits between rounds: run now.
        self._self_check_wake = asyncio.Event()
        #: #4035: a judgement's git_read asks for a round (`_note_git_read`):
        #: one asked for and not yet begun, the timer that starts it, and the
        #: loop time the latest round ended — a round asked for so starts
        #: --self-check-seconds after that at the earliest.
        self._git_read_pending = False
        self._git_read_timer: Optional[asyncio.TimerHandle] = None
        self._last_round_end: Optional[float] = None
        #: A self-check round holds (or waits for) the judgement slot; set
        #: by ``_start_next`` when the slot is free for it.
        self._self_check_slot = False
        self._slot_free = asyncio.Event()
        self._self_check_seq = 0
        #: Self-check processes not yet collected: ``_reap_orphans`` leaves
        #: them to their own ``wait``.
        self._held_pids: set[int] = set()
        self._job_seq = 0
        self.connections = 0
        self.inflight_body_bytes = 0
        self._lingering = 0
        self._accept_logged: dict[str, float] = {}
        self.own_uid = os.getuid()
        self.loop: asyncio.AbstractEventLoop = None  # type: ignore[assignment]

    # ── HTTP ──────────────────────────────────────────────────────────────

    @staticmethod
    def _fork() -> int:
        """``os.fork()`` — a seam for tests (a fork that fails)."""
        return os.fork()

    def _peer_uid(self, sock: socket.socket) -> Optional[int]:
        """The connecting process's uid (SO_PEERCRED), None when unreadable.
        A seam: tests replace it rather than switch uid."""
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                    _UCRED.size)
        except (OSError, AttributeError):
            return None
        return ucred_uid(creds)

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        # #2901: the trust boundary — only a process running as this uid
        # (tenant-api, see the module docstring) may talk to the service.
        # Anything else is closed at once, unanswered, and counted.
        if self._peer_uid(writer.get_extra_info("socket")) != self.own_uid:
            self.metrics.peer_rejected += 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            return
        # #2901 R1, admission (a): a connection slot before anything is read.
        # Past the limit the connection is answered busy and closed at once,
        # so no number of idle connections can take this process's fds.
        if self.connections >= self.s.max_connections:
            # Take what the client already sent (a request's header, briefly)
            # so closing does not reset it before it reads the answer; an
            # idle connection costs at most _SLOT_FULL_READ_SECONDS.
            try:
                await asyncio.wait_for(reader.read(HEADER_LIMIT_BYTES),
                                       _SLOT_FULL_READ_SECONDS)
            except (asyncio.TimeoutError, ConnectionError, OSError):
                pass
            await self._send(reader, writer, self._count(_rejected("busy")))
            return
        self.connections += 1
        try:
            resp = await self._respond(reader)
            # a request answered without reading its body: linger so the
            # client reads the answer instead of a connection reset
            await self._send(reader, writer, resp,
                             linger=resp is not None and resp.unread_body)
        finally:
            self.connections -= 1

    async def _send(self, reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter, resp: Optional[Response], *,
                    linger: bool = False) -> None:
        try:
            if resp is not None:
                writer.write(resp.encode())
                await writer.drain()
            # Closing with unread input makes the kernel reset the peer, who
            # may then never read the answer. So, for a bounded time and a
            # bounded number of connections, half-close and drop whatever
            # the client still sends (dropped, never kept).
            if linger and self._lingering < _MAX_LINGERING:
                self._lingering += 1
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                    end = self.loop.time() + _LINGER_SECONDS
                    while (left := end - self.loop.time()) > 0:
                        if not await asyncio.wait_for(reader.read(1 << 16), left):
                            break
                except (asyncio.TimeoutError, ConnectionError, OSError):
                    pass
                finally:
                    self._lingering -= 1
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _respond(self, reader: asyncio.StreamReader) -> Optional[Response]:
        timeout = self.s.read_timeout_seconds
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        except asyncio.LimitOverrunError:
            return _error(431, "BAD_REQUEST")
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return None
        # #2901 O6: the request's clock starts here — reading the body is
        # part of its budget.
        received = self.loop.time()
        try:
            method, target, headers = _parse_head(head)
        except Exception:  # noqa: BLE001 — #2901 O1: any bad header is a 400
            return _error(400, "BAD_REQUEST")

        routes = {"/v1/validate": "POST", "/v1/status": "GET", "/metrics": "GET"}
        if target not in routes:
            return _error(404, "NOT_FOUND")
        if method != routes[target]:
            return _error(405, "METHOD_NOT_ALLOWED", Allow=routes[target])
        if "transfer-encoding" in headers:
            return _error(411, "LENGTH_REQUIRED")
        if method == "GET":
            return self._status() if target == "/v1/status" else self._metrics()

        lengths = headers.get("content-length")
        if not lengths:
            return _error(411, "LENGTH_REQUIRED")
        if len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0]):
            return _error(400, "BAD_REQUEST")
        length = int(lengths[0])
        if length > self.s.max_body_bytes:
            resp = _error(413, "PAYLOAD_TOO_LARGE")
            resp.unread_body = True
            return resp
        # #2901 R1, admission (b): the body's bytes are reserved against the
        # in-flight budget BEFORE they are read, and given back when this
        # request is answered. Over budget: busy, body never read.
        if self.inflight_body_bytes + length > self.s.max_inflight_body_bytes:
            resp = self._count(_rejected("busy"))
            resp.unread_body = True
            return resp
        self.inflight_body_bytes += length
        try:
            try:
                body = await asyncio.wait_for(reader.readexactly(length), timeout)
            except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                    ConnectionError):
                return None
            try:
                request = parse_validate_request(body, self.object_format,
                                                 self.s.max_writes)
            except RequestError as exc:
                _log(f"bad request: {exc}")  # a place and a constant, never a path (#3961)
                return _error(400, "BAD_REQUEST")
            del body
            return await self.submit(request, received)
        finally:
            self.inflight_body_bytes -= length

    # ── queue ─────────────────────────────────────────────────────────────

    async def submit(self, request: ValidateRequest, received: float) -> Response:
        budget = judgement_seconds(request.deadline_ms, self.s.max_judgement_seconds,
                                   self.s.deadline_reserve_seconds)
        if budget <= 0:
            return self._count(_rejected("deadline"))
        if len(self.waiting) >= self.s.max_queue:
            return self._count(_rejected("busy"))
        ticket = Ticket(request, received + budget, self.loop.create_future(),
                        received=received)
        self.waiting.append(ticket)
        ticket.timer = self.loop.call_at(ticket.deadline_at, self._expire, ticket)
        # #2901 E2: a request never waits for a warming run — end it now; the
        # entries it already handed back stay in the cache (E1).
        self._preempt_warm()
        self._start_next()
        return self._count(await ticket.future)

    def _expire(self, ticket: Ticket) -> None:
        if ticket in self.waiting:
            self.waiting.remove(ticket)
            if not ticket.future.done():
                ticket.future.set_result(_rejected("deadline"))

    def _preempt_warm(self) -> None:
        """End the warming run for a request. #2901 R2: the flag is set even
        when the warming run has not forked yet (its task is scheduled but has
        not run): ``_warm`` then does not start, and ``_spawn`` kills a child
        it forks while the flag is up."""
        if self.warming is None or self._preempting:
            return
        self._preempting = True
        if self.child_pid is not None:
            _kill_group(self.child_pid)

    def _start_next(self) -> None:
        if self.running is not None or self.warming is not None:
            return
        if self._self_check_slot:
            # #3943: a self-check round holds the slot — it runs alone, as a
            # judgement does (the memory budget, ADR-036 §3); the queue waits.
            self._slot_free.set()
            return
        if self.waiting:
            ticket = self.waiting.popleft()
            if ticket.timer is not None:
                ticket.timer.cancel()
            ticket.started = self.loop.time()
            self.running = ticket
            self.loop.create_task(self._run(ticket))
        elif self.pending_warm is not None:
            commit, self.pending_warm = self.pending_warm, None
            self.warming = commit
            self.loop.create_task(self._warm(commit))

    async def _run(self, ticket: Ticket) -> None:
        try:
            resp = await self._judge(ticket)
        except Exception as exc:  # noqa: BLE001 — this process erred: 503
            resp = _unavailable("VALIDATOR_UNAVAILABLE", "internal_error",
                                f"generator service error: {exc!r}")
        finally:
            self.running = None
            self.child_pid = None
        if resp.doc is not None and resp.status == 200 and ticket.started is not None:
            resp.doc["queue_wait_ms"] = round((ticket.started - ticket.received) * 1000)
        if not ticket.future.done():
            ticket.future.set_result(resp)
        self._start_next()

    def _count(self, resp: Response) -> Response:
        if resp.status == 200:
            self.metrics.judgements[resp.doc["result"]] += 1
            if resp.doc["result"] == "rejected":
                self.metrics.rejections[resp.doc["reason"]] += 1
        elif resp.status == 503:
            self.metrics.unavailable[resp.doc.get("reason") or resp.doc["error"]] += 1
        return resp

    # ── one judgement ─────────────────────────────────────────────────────

    def _new_job_dir(self) -> str:
        self._job_seq += 1
        path = os.path.join(_jobs_dir(self.s.work_dir), f"job-{self._job_seq}")
        os.mkdir(path, 0o700)
        return path

    def _job(self, request: ValidateRequest, job_dir: str,
             policy: Optional[PolicySnapshot], warm: bool = False,
             deadline_at: float = 0.0) -> ChildJob:
        return ChildJob(
            request=request, job_dir=job_dir, repo=self.s.repo,
            config_subdir=self.s.config_subdir, git=self.s.git,
            object_format=self.object_format,
            memory_limit=self.s.memory_limit_bytes,
            policy_copy=policy.copy if policy else None,
            policy_sha256=policy.sha256 if policy else None,
            store_entries=self.store.entries,
            max_transfer=self.s.max_cache_transfer_bytes, warm=warm,
            deadline_at=deadline_at)

    async def _judge(self, ticket: Ticket) -> Response:
        remaining = ticket.deadline_at - self.loop.time()
        if remaining <= 0:
            return _rejected("deadline")
        # Whether its programs run is the judgement's own answer (#3862):
        # no probe here. Only the readers' paths are pinned before it.
        _pin_readers()
        job_dir = self._new_job_dir()
        try:
            policy = snapshot_policy(self.s.policy, job_dir)
            if not policy.available:
                return _unavailable("POLICY_UNAVAILABLE", "policy_unusable",
                                    policy.error or "policy file is unusable")
            too_old = generator_version_problem(self.generator_version,
                                                policy.min_generator_version)
            if too_old is not None:
                return _unavailable("VALIDATOR_UNAVAILABLE", "generator_too_old",
                                    too_old)
            pinned_before = self.pinned_commit
            try:
                run = await self._spawn(self._job(ticket.request, job_dir, policy,
                                                  deadline_at=ticket.deadline_at),
                                        ticket.deadline_at, request=True)
            except OSError as exc:
                if exc.errno not in _LOAD_ERRNOS:
                    raise
                # #2901 R1: out of descriptors, processes or memory to fork
                # with is load, not a broken container — reject this write.
                _log(f"cannot start a judgement ({errno.errorcode.get(exc.errno, exc.errno)})")
                return _rejected("busy")
            # ADR-038 §5: how much of its time budget the judgement used,
            # queue included — only when the judgement process ran.
            if ticket.deadline_at > ticket.received:
                self.metrics.judgement_deadline_ratio = round(
                    (self.loop.time() - ticket.received) / (ticket.deadline_at - ticket.received), 4)
            # #2901 E2: a new base: warm it after this request, so whatever
            # this request did not get to parse is parsed off the clock.
            if ticket.request.base_commit != pinned_before:
                self.pending_warm = ticket.request.base_commit
            return self._verdict(run)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def _warm(self, commit: str) -> None:
        """#2901 E2: run the generator's conf.d read (``load_tenant_tree`` —
        every parse a judgement makes happens there) on *commit*'s tree, no
        writes, no verdict, no Alertmanager reading — only to fill the cache
        (E1). Ended at once when a request arrives; bounded by --warm-seconds;
        never on a request's clock."""
        outcome = "failed"
        job_dir = None
        try:
            if self._preempting or self.waiting:
                outcome = "preempted"  # a request came before the fork
                return
            job_dir = self._new_job_dir()
            run = await self._spawn(
                self._job(ValidateRequest(commit, (), 0), job_dir, None, warm=True),
                self.loop.time() + self.s.warm_seconds, request=False)
            ended = (os.WIFEXITED(run.status) and os.WEXITSTATUS(run.status) == 0
                     and run.reader.ended and run.reader.result is not None)
            if ended and run.reader.result.get("outcome") == "tool_fault":
                # #3862: a program the warming run reads with did not run —
                # not ready until a self-check passes (#3943).
                self._note_tool_fault(tool_faults_of(run.reader.result))
            complete = ended and run.reader.result.get("outcome") == "warmed"
            if complete:
                outcome = "completed"
            elif self._preempting:
                outcome = "preempted"
            elif run.timed_out:
                outcome = "deadline"
        except Exception as exc:  # noqa: BLE001 — warming never affects requests
            print(f"generator-service: warming {commit} failed: {exc!r}",
                  file=sys.stderr, flush=True)
        finally:
            if job_dir is not None:
                shutil.rmtree(job_dir, ignore_errors=True)
            self.metrics.warm_runs[outcome] += 1
            self.warming = None
            self.child_pid = None
            self._preempting = False
            self._start_next()

    def _pin(self, blobs: list[str], commit: str, request: bool) -> None:
        """The pinned base follows the latest REQUEST's base commit; a
        warming run's commit counts only before any request (or when it is
        that same commit)."""
        if request or not self._request_pinned or commit == self.pinned_commit:
            self.store.set_pinned(blobs)
            self.pinned_commit = commit
            self._request_pinned = self._request_pinned or request

    def _admit(self, key: tuple[str, str], value: bytes) -> bool:
        """One streamed cache entry, kept only if it reads back under the
        restricted unpickler (``_lib_parse_cache.ALLOWED_GLOBALS``)."""
        try:
            restricted_loads(value)
        except Exception:  # noqa: BLE001 — anything unreadable is dropped
            return False
        self.store.add(key, value)
        return True

    async def _spawn(self, job: ChildJob, deadline_at: float, *,
                     request: bool) -> ChildRun:
        threads = threading.active_count()
        if threads != 1:
            raise RuntimeError(f"refusing to fork with {threads} threads")
        self._reap_orphans()  # whatever the previous run's killpg left behind
        sys.stdout.flush()
        sys.stderr.flush()
        rfd, wfd = os.pipe()
        try:
            pid = self._fork()
        except OSError:
            os.close(rfd)
            os.close(wfd)
            raise
        if pid == 0:  # the judgement (or warming) process
            os.close(rfd)
            _gsvc_child.run_child(job, wfd)
        os.close(wfd)
        self.child_pid = pid
        if not request and self._preempting:
            _kill_group(pid)  # #2901 R2: a request came while forking
        reader = FrameReader(max_result=self.s.max_result_bytes,
                             max_entries=self.s.max_cache_transfer_bytes)
        received = 0
        commit = job.request.base_commit
        exited = self.loop.create_future()

        def on_data() -> None:
            nonlocal received
            while True:
                try:
                    chunk = os.read(rfd, 1 << 16)
                except BlockingIOError:
                    return
                except OSError:
                    chunk = b""
                if not chunk:
                    self.loop.remove_reader(rfd)
                    return
                # #2901 E1: entries join the cache as they arrive, so a
                # process killed later still leaves them behind.
                for kind, value in reader.feed(chunk):
                    if kind == "E":
                        received += self._admit(*value)
                    elif kind == "B":
                        self._pin(value, commit, request)

        def on_exit() -> None:
            if not exited.done():
                exited.set_result(None)

        timed_out = False
        pidfd = -1
        try:
            os.set_blocking(rfd, False)
            self.loop.add_reader(rfd, on_data)
            # pidfd: the exit is an event on this loop — no SIGCHLD handler,
            # no watcher thread.
            pidfd = os.pidfd_open(pid)
            self.loop.add_reader(pidfd, on_exit)
            await asyncio.wait_for(asyncio.shield(exited),
                                   max(0.0, deadline_at - self.loop.time()))
        except asyncio.TimeoutError:
            timed_out = True
            _kill_group(pid)
            await asyncio.wait_for(exited, _KILL_GRACE_SECONDS)
        except BaseException:
            _kill_group(pid)
            raise
        finally:
            if pidfd >= 0:
                self.loop.remove_reader(pidfd)
                os.close(pidfd)
            # anything the process left running. #3923: before wait4, while
            # the run's process is a zombie (or still running, on the
            # exception path) and so holds its pid and group id; after wait4
            # either number can be an unrelated process's.
            _kill_group(pid)
            _, status, rusage = os.wait4(pid, 0)
            self.child_pid = None
            # the run's own process has been collected by wait4 above; what
            # has exited now is an orphan, and nothing is held back for it
            assert self.child_pid is None
            self._reap_orphans()
            on_data()
            self.loop.remove_reader(rfd)
            os.close(rfd)
            self.store.evict()
        if request:
            self.metrics.peak_rss_bytes = rusage.ru_maxrss * 1024
        return ChildRun(timed_out, status, reader, received)

    def _verdict(self, run: ChildRun) -> Response:
        received = {"received": run.received}
        if run.timed_out:
            return _rejected("deadline", cache=received)
        if os.WIFSIGNALED(run.status) or os.WEXITSTATUS(run.status) != 0:
            return _rejected("incomplete", cache=received)
        res = run.reader.result
        if res is None or not run.reader.ended:
            return _rejected("incomplete", cache=received)
        self.store.touch(tuple(k) for k in res.get("hit_keys", [])
                         if isinstance(k, list) and len(k) == 2)
        outcome = res.get("outcome")
        cache = res.get("cache")
        cache = {**cache, **received} if isinstance(cache, dict) else received
        common = {"validated": res.get("validated"), "cache": cache}
        if outcome == "tool_fault":
            # #3862: an external program of this judgement did not run, so no
            # verdict was reached — whatever the generator concluded without it.
            faults = tool_faults_of(res)
            self._note_tool_fault(faults)
            return _unavailable("VALIDATOR_UNAVAILABLE", "tool_fault",
                                tool_fault_message(faults), tool_fault=faults)
        if outcome == "ran":
            # #3943: a judgement that ran clears no tool fault — it may have
            # ended (content refused early) before the program that failed.
            # Only a passing self-check does.
            rc = res.get("rc")
            doc = {"result": "pass" if rc == 0 else "findings",
                   "rc": rc, "findings": res.get("findings"), **common}
            kv = res.get("key_verdict")
            if isinstance(kv, dict):
                # ADR-038 decision 5: reported, never the result.
                doc["key_verdict"] = kv
                self.metrics.key_verdicts[kv.get("reason") or kv.get("status") or "error"] += 1
                self.metrics.key_verdict_seconds = float(kv.get("seconds") or 0.0)
            return Response(200, doc)
        if outcome == "bad_request":
            return _error(400, "BAD_REQUEST")
        if outcome == "memory":
            return _rejected("memory", **common)
        if outcome == "git_read":
            # #4035: git could not read --repo, or the request's base commit
            # is not in it — this answer cannot tell which. A self-check
            # round can (its --repo probe): ready follows that, not this.
            self._note_git_read()
            return _rejected("git_read", **common)
        # `da_guard` (names mode could not classify the base tree's path
        # names) has no reason of its own on the wire (tenant-api's closed
        # list): no verdict was reached, like any other incomplete judgement.
        if outcome == "da_guard":
            _log("incomplete: da-guard names mode could not classify the base tree's path names")
        return _rejected("incomplete", **common)

    # ── self-check (#3943) ────────────────────────────────────────────────

    def _note_tool_fault(self, faults: list) -> None:
        """A judgement or warming run whose program did not run: not ready
        at once, and the self-check runs again until one passes."""
        self.tool_fault = faults
        self._fault_seq += 1
        self._ensure_self_check_loop()

    def _note_git_read(self) -> None:
        """#4035: a judgement's ``git_read`` asks for a round — never sooner
        than --self-check-seconds after the latest one ended, every ask
        until it begins one ask, and never a wake of the loop's wait after a
        failed round (that wait is --self-check-seconds already). Unlike a
        tool fault it marks nothing and leaves ready alone: a request's own
        base commit missing from --repo is ``git_read`` too."""
        self._git_read_pending = True
        if self._self_check_task is None or self._self_check_task.done():
            self._schedule_git_read()
        # else: the running loop's next round answers the ask, or the loop
        # schedules it when it ends (`_self_check_loop`)

    def _schedule_git_read(self) -> None:
        if self._git_read_timer is not None:
            return  # one ask, one timer
        delay = 0.0
        if self._last_round_end is not None:
            delay = max(0.0, self._last_round_end + self.s.self_check_seconds - self.loop.time())
        self._git_read_timer = self.loop.call_later(delay, self._git_read_due)

    def _git_read_due(self) -> None:
        self._git_read_timer = None
        if not self._git_read_pending or not (self._self_check_task is None
                                              or self._self_check_task.done()):
            return
        # a round that ended after the timer was set (a tool fault's) moved
        # the earliest start: wait the rest, measured from that round
        if (self._last_round_end is not None
                and self.loop.time() < self._last_round_end + self.s.self_check_seconds):
            self._schedule_git_read()
            return
        self._ensure_self_check_loop()

    def _ensure_self_check_loop(self) -> None:
        """Run a self-check round now: start the loop, or — when it is
        running — wake it from its wait between rounds."""
        if self._self_check_task is None or self._self_check_task.done():
            self._self_check_wake.clear()
            # the slot is the round's from now, not from when its task first
            # runs: a judgement or warming run started in between would only
            # be waited for (or a warming run preempted)
            self._self_check_slot = True
            self._self_check_task = self.loop.create_task(self._self_check_loop())
        else:
            self._self_check_wake.set()

    async def _self_check_loop(self) -> None:
        """A self-check round now, then again after each failed one — every
        --self-check-seconds, or at once when a new tool fault wakes it —
        until one passes with no tool fault left."""
        while True:
            self._self_check_wake.clear()
            if await self.self_check_round():
                if self._git_read_pending:  # asked for after the round took its slot
                    self._schedule_git_read()
                return
            try:
                await asyncio.wait_for(self._self_check_wake.wait(),
                                       self.s.self_check_seconds)
            except asyncio.TimeoutError:
                pass

    async def stop_self_check(self) -> None:
        """End the self-check loop and wait for it: a running self-check's
        process group is killed and collected (``_run_held``)."""
        task = self._self_check_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _self_check_programs() -> list[tuple[str, Optional[str], tuple[str, ...]]]:
        """(name, path or None, arguments) of each program's self-check, the
        path by the resolution its judgement uses: da-guard's dispatcher,
        ``_lib_consumer_read.resolve_binary`` for the readers — after
        ``_pin_readers``, as before a judgement."""
        import guard_dispatch
        _pin_readers()
        out = []
        for env, (_module, _pkg, name) in _lib_consumer_read.READER_BUILD_SOURCES.items():
            if env == _lib_consumer_read.DA_GUARD_ENV:
                exe = guard_dispatch.DISPATCHER.resolve_binary(None)
            else:
                exe = _lib_consumer_read.resolve_binary(None, env, name)
            out.append((name, exe, _lib_consumer_read.READER_SELF_CHECK_ARGS[env]))
        return sorted(out)

    async def _take_slot(self) -> None:
        """Hold the one slot judgements and warming runs take: no new one
        starts (``_start_next``), a warming run is ended, and a running
        judgement is waited for — so a self-check never overlaps either and
        the container's memory budget holds (ADR-036 §3). A request that
        comes meanwhile waits in the queue, its deadline running."""
        self._self_check_slot = True
        if self.warming is not None and self.pending_warm is None:
            self.pending_warm = self.warming  # warmed again after the round
        self._preempt_warm()
        while self.running is not None or self.warming is not None:
            self._slot_free.clear()
            await self._slot_free.wait()

    def _free_slot(self) -> None:
        self._self_check_slot = False
        if self.loop is not None:
            self._start_next()

    async def self_check_round(self) -> bool:
        """Run the --repo probe and the three self-checks one after another,
        alone in the slot (``_take_slot``), and record the round; whether it
        passed and no tool fault is left. A pass clears the tool fault
        unless a newer one came while it ran."""
        seq = self._fault_seq
        self.self_check_running = True
        try:
            await self._take_slot()
            # no judgement ends while the round holds the slot: every
            # git_read asked for so far is answered by this probe
            self._git_read_pending = False
            repo_reason = await self._repo_probe()
            try:
                programs = self._self_check_programs()
            except Exception as exc:  # noqa: BLE001 — the round fails, the service stays up
                _log(f"self-check: cannot resolve the programs: {exc!r}")
                programs = [(name, None, ()) for name in _lib_tool_fault.BINARIES]
                reasons = ["internal_error"] * len(programs)
            else:
                # one at a time: each is one Go program under the judgement's
                # memory limit, as a judgement runs one at a time
                reasons = [await self._self_check_one(name, exe, args)
                           for name, exe, args in programs]
        finally:
            self.self_check_running = False
            if self.loop is not None:
                self._last_round_end = self.loop.time()
            self._free_slot()
        repo = ([] if repo_reason is None
                else [{"binary": SELF_CHECK_GIT_BINARY, "reason": repo_reason}])
        self.self_check_failures = repo + [
            {"binary": name, "reason": reason}
            for (name, _exe, _args), reason in zip(programs, reasons) if reason is not None]
        self.self_check_state = "failed" if self.self_check_failures else "passed"
        self.metrics.self_checks[self.self_check_state] += 1
        if self.self_check_state == "passed" and self._fault_seq == seq:
            self.tool_fault = None
        return self.self_check_state == "passed" and self.tool_fault is None

    def _new_self_check_dir(self) -> str:
        """A directory of its own (0700) for one process of a round."""
        self._self_check_seq += 1
        work = os.path.join(_jobs_dir(self.s.work_dir), f"selfcheck-{self._self_check_seq}")
        os.mkdir(work, 0o700)
        return work

    async def _repo_probe(self) -> Optional[str]:
        """#4035: whether git still reads --repo as the service judged it at
        start; None when it does, else a reason of
        ``_gsvc_proto.SELF_CHECK_GIT_REASONS``, git's own words logged only.
        A judgement's ``git_read`` cannot tell a --repo git no longer reads
        from a request whose base commit is not in it; this probe can.

        The one judgement of --repo, :func:`_repo_judgement` — each git run
        it yields run in turn as a program's self-check is
        (:meth:`_run_held`: the loop serves meanwhile, each under the time
        limit and the limits). A HEAD with no commit yet reads as it did at
        start. Refused, timed out, a git program that cannot be run (gone,
        not executable, not a program), or an object format other than the
        one at start: ``repo_unreadable``. A failure of this service's own
        (its work directory, descriptors, memory, a pidfd): ``internal_error``."""
        failure = {"binary": SELF_CHECK_GIT_BINARY, "reason": "repo_unreadable"}
        try:
            env = _gsvc_child.git_env(self.s.git)  # answered at start: kept
            steps = _repo_judgement(self.s.git, self.s.repo)
            argv = next(steps)
            while True:
                work = self._new_self_check_dir()
                try:
                    run = await self._run_held(argv, work, env, timeout_failure=failure)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                if run.timed_out:
                    _log(f"self-check: --repo: git did not answer within "
                         f"{self.s.self_check_timeout_seconds}s")
                    return "repo_unreadable"
                argv = steps.send((run.rc, run.stdout, run.stderr))
        except StopIteration as done:
            state = done.value
        except RepoRefused as exc:
            _log(f"self-check: {exc}")
            return "repo_unreadable"
        except _ExecFailed as exc:
            _log(f"self-check: --git: {exc.error.strerror}")
            # the git program itself (gone, not executable) — not this service
            return "repo_unreadable" if exc.error.errno in _GIT_EXEC_ERRNOS else "internal_error"
        except Exception as exc:  # noqa: BLE001 — the probe fails, the service stays up
            _log(f"self-check: --repo: {exc!r}")
            return "internal_error"
        if state.object_format != self.object_format:
            _log(f"self-check: --repo: object format {state.object_format}, not the "
                 f"{self.object_format} it had at start")
            return "repo_unreadable"
        return None

    async def _self_check_one(self, name: str, exe: Optional[str],
                              args: tuple[str, ...]) -> Optional[str]:
        """Run one program's self-check; None when it passed, else its
        reason (``_gsvc_proto.SELF_CHECK_PROGRAM_REASONS``), the detail
        logged.

        Passed is exit 0 with exactly ``SELF_CHECK_PASSED`` on stdout. The
        process runs as :meth:`_run_held` runs one, its directory also its
        $TMPDIR (da-guard writes its fixture tree there)."""
        if not exe:
            _log(f"self-check: {name}: not found")
            return "not_found"
        try:
            work = self._new_self_check_dir()
        except OSError as exc:
            _log(f"self-check: {name}: cannot make its directory: {exc!r}")
            return "internal_error"
        try:
            return await self._self_check_in(name, exe, args, work)
        except Exception as exc:  # noqa: BLE001 — the self-check fails, the service stays up
            _log(f"self-check: {name}: {exc!r}")
            return "internal_error"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _self_check_in(self, name: str, exe: str, args: tuple[str, ...],
                             work: str) -> Optional[str]:
        try:
            run = await self._run_held([exe, *args], work, {**os.environ, "TMPDIR": work},
                                       timeout_failure={"binary": name, "reason": "timed_out"})
        except _ExecFailed as exc:
            reason = _lib_tool_fault.os_error_reason(exc.error, exe)
            _log(f"self-check: {name}: {reason}: {exc.error.strerror}")
            return reason
        rc, stdout = run.rc, run.stdout
        if run.timed_out:
            reason = "timed_out"
        elif rc < 0:
            reason = "signalled"
        elif rc == 0:
            reason = None if stdout == _lib_consumer_read.SELF_CHECK_PASSED else "bad_output"
        elif rc == 1:
            reason = "failed" if stdout == _lib_consumer_read.SELF_CHECK_FAILED else "bad_output"
        else:
            reason = "unexpected_exit"
        if reason is not None:
            _log(f"self-check: {name}: {reason} (exit {rc}): "
                 + run.stderr.decode("utf-8", "replace"))
        return reason

    def _fail_round_now(self, failure: dict) -> None:
        """#4035: a step of the running round is past its time limit — the
        round has failed, said at once: ``failed`` with *failure* (the
        reason the step records when the round ends, so the round's own
        record agrees) and so not ready, while the killed process is still
        being collected and holds the slot. Neither the tool fault nor
        ``_fault_seq`` is touched, nor the metric: the round is counted when
        it ends, and its failures are worked out whole then."""
        self.self_check_state = "failed"
        self.self_check_failures = [dict(failure)]

    async def _run_held(self, argv: list, work: str, env: dict,
                        timeout_failure: Optional[dict] = None) -> HeldRun:
        """Run *argv* for a self-check round — a program's self-check or a
        git of the --repo probe — and return how it ended; `_ExecFailed`
        when it could not be started. Past --self-check-timeout-seconds the
        round fails that moment with *timeout_failure*
        (``_fail_round_now``), before the process is collected.

        In *work* (its cwd, its stdout and stderr files 0600), in a process
        group of its own that the time limit ends, under the self-check's
        limits (``_self_check_limits``); collected here, never by
        ``_reap_orphans`` (``_held_pids``). No thread and no blocking wait:
        its exit is a pidfd on this loop, which serves meanwhile — after the
        kill too: a process SIGKILL cannot end yet (git in an
        uninterruptible sleep on a hung NFS mount) holds the slot until it
        does, never the loop. When its exit cannot be watched (``pidfd_open``
        or ``add_reader`` failing: descriptors, epoll's ENOSPC) it is killed
        and polled for without blocking; the failure then propagates, once
        it is collected (``internal_error`` for the caller)."""
        out_path, err_path = os.path.join(work, "stdout"), os.path.join(work, "stderr")
        # 0600 from creation (the work directory is 0700 already): what a
        # self-check prints is the service's to read, nobody else's
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        out = os.open(out_path, flags, 0o600)
        try:
            err = os.open(err_path, flags, 0o600)
        except OSError:
            os.close(out)
            raise
        try:
            proc = subprocess.Popen(  # noqa: S603 — the resolved program, no shell
                argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                cwd=work, env=env, start_new_session=True,
                preexec_fn=_self_check_limits(self.s.memory_limit_bytes))
        except OSError as exc:
            raise _ExecFailed(exc) from exc
        finally:
            os.close(out)
            os.close(err)
        # No await between the fork and this: _reap_orphans (a SIGCHLD
        # callback on this loop) cannot have collected it.
        self._held_pids.add(proc.pid)
        pidfd = -1
        exited: Optional[asyncio.Future] = None
        watched = False  # its exit is a reader on this loop
        timed_out = False
        try:
            # inside the try: a pidfd_open, create_future or add_reader that
            # fails still kills, collects and lets go of the process below
            pidfd = os.pidfd_open(proc.pid)
            exited = self.loop.create_future()
            self.loop.add_reader(pidfd, lambda: exited.done() or exited.set_result(None))
            watched = True
            try:
                # shielded: the time limit ends the wait, not the future the
                # kill's end is awaited on below
                await asyncio.wait_for(asyncio.shield(exited),
                                       max(0.0, self.s.self_check_timeout_seconds))
            except asyncio.TimeoutError:
                timed_out = True
                if timeout_failure is not None:
                    self._fail_round_now(timeout_failure)
        finally:
            _kill_group(proc.pid)  # #3923: before it is collected
            try:
                if watched:
                    await exited  # the same pidfd: the loop serves until it has ended
            finally:
                if watched:
                    self.loop.remove_reader(pidfd)
                if pidfd >= 0:
                    os.close(pidfd)
                try:
                    # ended (watched), or polled for until it has: never a
                    # blocking wait on this loop
                    await self._collected(proc)
                finally:
                    self._held_pids.discard(proc.pid)
                    self._reap_orphans()  # what was held back behind it
        with open(out_path, "rb") as fh:
            stdout = fh.read(_SELF_CHECK_STDOUT_LIMIT + 1)
        with open(err_path, "rb") as fh:
            stderr = fh.read(_SELF_CHECK_STDERR_LOGGED)
        return HeldRun(proc.returncode, stdout, stderr, timed_out)

    @staticmethod
    async def _collected(proc: subprocess.Popen) -> None:
        """Until *proc* has been collected: ``poll`` (``waitpid`` WNOHANG)
        with a growing sleep between — the loop serves meanwhile."""
        delay = _POLL_FIRST_SECONDS
        while proc.poll() is None:
            await asyncio.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SECONDS)

    def _self_check_doc(self) -> dict:
        return {"state": self.self_check_state,
                "failures": [dict(f) for f in self.self_check_failures],
                "running": self.self_check_running}

    # ── status and metrics ────────────────────────────────────────────────

    def _policy_now(self) -> PolicySnapshot:
        tmp = os.path.join(_jobs_dir(self.s.work_dir), "status")
        os.makedirs(tmp, mode=0o700, exist_ok=True)
        try:
            return snapshot_policy(self.s.policy, tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _status(self) -> Response:
        policy = self._policy_now()
        too_old = (generator_version_problem(self.generator_version,
                                             policy.min_generator_version)
                   if policy.available else None)
        return Response(200, {
            "policy": {"configured": policy.configured,
                       "available": policy.available,
                       "sha256": policy.sha256, "error": policy.error,
                       "min_generator_version": policy.min_generator_version},
            "generator_version": self.generator_version,
            "generator_too_old": too_old,
            "da_guard_version": self.da_guard_version,
            "self_check": self._self_check_doc(),
            "tool_fault": self.tool_fault,
            "ready": (policy.available and too_old is None
                      and self.self_check_state == "passed" and self.tool_fault is None),
            "cache_base_coverage_ratio": self.store.coverage(),
            "warm": {"ref": self.s.warm_ref or None, "commit": self.warm_commit,
                     "error": self.warm_error, "running": self.warming,
                     "pinned_commit": self.pinned_commit},
        })

    def _metrics(self) -> Response:
        pinned, unpinned = self.store.sizes()
        out = [
            "# HELP da_generator_cache_bytes Serialized parse-cache bytes; pinned = in the latest base tree.",
            "# TYPE da_generator_cache_bytes gauge",
            f'da_generator_cache_bytes{{pinned="true"}} {pinned}',
            f'da_generator_cache_bytes{{pinned="false"}} {unpinned}',
            "# HELP da_generator_cache_entries Parse-cache entries.",
            "# TYPE da_generator_cache_entries gauge",
            f"da_generator_cache_entries {len(self.store.entries)}",
            "# HELP da_generator_judgement_peak_rss_bytes Peak RSS of the latest judgement process (wait4 ru_maxrss).",
            "# TYPE da_generator_judgement_peak_rss_bytes gauge",
            f"da_generator_judgement_peak_rss_bytes {self.metrics.peak_rss_bytes}",
            "# HELP da_generator_judgement_memory_limit_bytes RLIMIT_DATA of every judgement process.",
            "# TYPE da_generator_judgement_memory_limit_bytes gauge",
            f"da_generator_judgement_memory_limit_bytes {self.s.memory_limit_bytes}",
            "# HELP da_generator_judgements_total Validate requests answered with a verdict, by result.",
            "# TYPE da_generator_judgements_total counter",
        ]
        for result in ("pass", "findings", "rejected"):
            out.append(f'da_generator_judgements_total{{result="{result}"}} '
                       f"{self.metrics.judgements[result]}")
        out += ["# HELP da_generator_rejections_total Requests rejected without a verdict on their content, by reason.",
                "# TYPE da_generator_rejections_total counter"]
        for reason in ("deadline", "memory", "incomplete", "git_read", "busy"):
            out.append(f'da_generator_rejections_total{{reason="{reason}"}} '
                       f"{self.metrics.rejections[reason]}")
        out += ["# HELP da_generator_unavailable_total Validate requests answered 503, by reason.",
                "# TYPE da_generator_unavailable_total counter"]
        for reason in (*UNAVAILABLE_REASONS, "policy_unusable"):
            out.append(f'da_generator_unavailable_total{{reason="{reason}"}} '
                       f"{self.metrics.unavailable[reason]}")
        out += ["# HELP da_generator_warm_runs_total Pre-warming runs (cache filling, no verdict), by outcome.",
                "# TYPE da_generator_warm_runs_total counter"]
        for outcome in ("completed", "preempted", "deadline", "failed"):
            out.append(f'da_generator_warm_runs_total{{outcome="{outcome}"}} '
                       f"{self.metrics.warm_runs[outcome]}")
        out += ["# HELP da_generator_self_checks_total Self-check rounds of da-guard, da-read and da-crdecode (readiness), by result.",
                "# TYPE da_generator_self_checks_total counter"]
        for result in ("passed", "failed"):
            out.append(f'da_generator_self_checks_total{{result="{result}"}} '
                       f"{self.metrics.self_checks[result]}")
        out += ["# HELP da_generator_key_verdicts_total Key verdicts (ADR-038) on writes the generator passed, by outcome: complete, or why not completed.",
                "# TYPE da_generator_key_verdicts_total counter"]
        for outcome in KEY_VERDICT_OUTCOMES:
            out.append(f'da_generator_key_verdicts_total{{outcome="{outcome}"}} '
                       f"{self.metrics.key_verdicts[outcome]}")
        out += ["# HELP da_generator_key_verdict_seconds Duration of the latest key verdict.",
                "# TYPE da_generator_key_verdict_seconds gauge",
                f"da_generator_key_verdict_seconds {self.metrics.key_verdict_seconds}",
                "# HELP da_generator_judgement_deadline_ratio The time the latest judgement took, queue included, over its time budget; alert above 0.5, near 1 key verdicts run out of time (ADR-038 decision 5).",
                "# TYPE da_generator_judgement_deadline_ratio gauge",
                f"da_generator_judgement_deadline_ratio {self.metrics.judgement_deadline_ratio}"]
        out += ["# HELP da_generator_cache_base_coverage_ratio Share of the pinned base tree's blobs with a parse-cache entry.",
                "# TYPE da_generator_cache_base_coverage_ratio gauge",
                f"da_generator_cache_base_coverage_ratio {self.store.coverage()}"]
        out += ["# HELP da_generator_orphans_reaped_total Exited processes left by judgements (da-guard, da-read and their children) collected by this subreaper.",
                "# TYPE da_generator_orphans_reaped_total counter",
                f"da_generator_orphans_reaped_total {self.metrics.orphans_reaped}",
                "# HELP da_generator_connections Open client connections (admission slots in use).",
                "# TYPE da_generator_connections gauge",
                f"da_generator_connections {self.connections}",
                "# HELP da_generator_inflight_body_bytes Request body bytes reserved by requests being read or queued.",
                "# TYPE da_generator_inflight_body_bytes gauge",
                f"da_generator_inflight_body_bytes {self.inflight_body_bytes}"]
        out += ["# HELP da_generator_peer_rejected_total Connections closed unanswered: the peer's uid (SO_PEERCRED) is not this process's.",
                "# TYPE da_generator_peer_rejected_total counter",
                f"da_generator_peer_rejected_total {self.metrics.peer_rejected}"]
        out += ["# HELP da_generator_accept_errors_total Failed accept() of a client connection, by errno.",
                "# TYPE da_generator_accept_errors_total counter"]
        for name in sorted(self.metrics.accept_errors):
            out.append(f'da_generator_accept_errors_total{{errno="{name}"}} '
                       f"{self.metrics.accept_errors[name]}")
        out += ["# HELP da_generator_queue_depth Validate requests waiting for the running judgement.",
                "# TYPE da_generator_queue_depth gauge",
                f"da_generator_queue_depth {len(self.waiting)}",
                "# HELP da_generator_build_info Versions of the generator and da-guard in this container.",
                "# TYPE da_generator_build_info gauge",
                'da_generator_build_info{generator_version="%s",da_guard_version="%s"} 1'
                % (_escape_label(self.generator_version or ""),
                   _escape_label(self.da_guard_version or ""))]
        return Response(200, text="\n".join(out) + "\n")

    def _reap_orphans(self) -> int:
        """#2901 R3: collect the zombies of processes a judgement started
        (da-guard, da-read, their children). This process is a child
        subreaper, so they are re-parented here when the judgement ends —
        whether or not it is PID 1.

        Called on SIGCHLD, before each fork and after each run's ``wait4``.

        ⛔ Never the running judgement (or warming) process: ``wait4`` in
        :meth:`_spawn` takes its exit status — nor a self-check process
        (``_held_pids``), whose exit :meth:`_self_check_in` reads. So each
        exited child is looked at first without collecting it (``WNOWAIT``)
        and collected only when it is neither. #3557: an orphan that ends while another run
        is in progress is collected on its own SIGCHLD, not left a zombie
        until that run ends. When the exited child the kernel reports is the
        running one, the rest wait for that run's end, which its exit has
        already set off.
        """
        reaped = 0
        while True:
            try:
                info = os.waitid(os.P_ALL, 0,
                                 os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                break
            # #3943: nor a self-check process, which its own wait collects
            if info is None or info.si_pid == self.child_pid or info.si_pid in self._held_pids:
                break
            # it has exited (WNOWAIT left it waitable): this does not block
            os.waitpid(info.si_pid, 0)
            reaped += 1
        self.metrics.orphans_reaped += reaped
        return reaped

    def loop_exception(self, loop: asyncio.AbstractEventLoop, context: dict) -> None:
        """The event loop's exception handler. #2901 N2: an accept() that
        fails for lack of resources (EMFILE, ENFILE, …; asyncio retries it
        itself) is counted in da_generator_accept_errors_total{errno} and
        logged at most once per errno per ACCEPT_ERROR_LOG_INTERVAL_SECONDS,
        without a traceback. Everything else goes to the default handler."""
        exc = context.get("exception")
        if (isinstance(exc, OSError)
                and context.get("message", "").startswith("socket.accept()")):
            name = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            self.metrics.accept_errors[name] += 1
            now = loop.time()
            last = self._accept_logged.get(name)
            if last is None or now - last >= ACCEPT_ERROR_LOG_INTERVAL_SECONDS:
                self._accept_logged[name] = now
                print(f"generator-service: accept() failed with {name} "
                      f"({self.metrics.accept_errors[name]} so far); new "
                      f"connections wait until descriptors free up",
                      file=sys.stderr, flush=True)
            return
        loop.default_exception_handler(context)

    def shutdown(self) -> None:
        if self.child_pid is not None:
            _kill_group(self.child_pid)
        for pid in self._held_pids:  # a self-check still running
            _kill_group(pid)
        if self._self_check_task is not None:
            self._self_check_task.cancel()
        if self._git_read_timer is not None:
            self._git_read_timer.cancel()
            self._git_read_timer = None

    def start_warming(self) -> None:
        """#2901 E2: resolve --warm-ref and queue a warming run for it. A ref
        that does not resolve is logged and counted, nothing else."""
        ref = self.s.warm_ref
        if not ref:
            return
        try:
            proc = subprocess.run(
                [self.s.git, "-C", self.s.repo, "rev-parse", "--verify",
                 "--quiet", f"{ref}^{{commit}}"],
                capture_output=True, check=False, timeout=60,
                env=_gsvc_child.git_env(self.s.git))
            commit = proc.stdout.decode("ascii", "replace").strip()
            ok = proc.returncode == 0 and bool(commit)
        except (OSError, subprocess.SubprocessError) as exc:
            ok, commit = False, repr(exc)
        if not ok:
            self.warm_error = f"--warm-ref {ref!r} does not resolve to a commit"
            self.metrics.warm_runs["failed"] += 1
            print(f"generator-service: {self.warm_error}; not warming",
                  file=sys.stderr, flush=True)
            return
        self.warm_commit = commit
        self.pending_warm = commit
        self._start_next()


def _kill_group(pid: int) -> None:
    """SIGKILL the process group *pid* leads, and *pid* itself.

    ⛔ #3923: only while *pid* is not yet collected (running, or a zombie):
    once ``wait4`` has taken it, the pid and the group id are free for reuse
    and both signals could reach an unrelated process."""
    for kill in (lambda: os.killpg(pid, signal.SIGKILL),
                 lambda: os.kill(pid, signal.SIGKILL)):
        try:
            kill()
        except (ProcessLookupError, PermissionError):
            pass


# ── CLI ───────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="da-tools generator-service",
        description="The generator container's service (ADR-036 §3): judge a "
                    "conf.d tree plus writes with generate-routes --validate "
                    "--strict, over HTTP/1.1 on a Unix socket.")
    p.add_argument("--socket", required=True,
                   help="Unix socket path to listen on (created with mode 0600; "
                        "an old socket there is replaced, anything else refuses)")
    p.add_argument("--repo", required=True,
                   help="the git repository (read-only is enough); files are "
                        "read from git objects, never from its working tree")
    p.add_argument("--config-subdir", required=True,
                   help="conf.d's path inside the repository, e.g. conf.d (. is the "
                        "root; absolute or with a '..' segment refuses to start)")
    p.add_argument("--work-dir", required=True,
                   help="scratch directory for the trees being judged")
    p.add_argument("--policy", default=None,
                   help="deployment policy file (allowed_domains, "
                        "min_generator_version); read for every judgement")
    p.add_argument("--git", default="git", help="git executable (default: git)")
    p.add_argument("--da-guard-binary", default=None,
                   help="da-guard every judgement runs and whose version is "
                        "reported (default: $DA_GUARD_BINARY, then PATH)")
    p.add_argument("--max-judgement-seconds", type=float,
                   default=DEFAULT_MAX_JUDGEMENT_SECONDS,
                   help="upper bound of one request's time, queue included "
                        "(default: %(default)s)")
    p.add_argument("--deadline-reserve-seconds", type=float,
                   default=DEFAULT_DEADLINE_RESERVE_SECONDS,
                   help="time left to the caller out of its deadline_ms "
                        "(default: %(default)s)")
    p.add_argument("--memory-limit-bytes", type=int,
                   default=DEFAULT_MEMORY_LIMIT_BYTES,
                   help="RLIMIT_DATA of each judgement process (default: %(default)s)")
    p.add_argument("--cache-max-bytes", type=int, default=DEFAULT_CACHE_MAX_BYTES,
                   help="parse-cache bytes kept for content not in the latest "
                        "base tree, least recently used dropped first "
                        "(default: %(default)s)")
    p.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE,
                   help="requests that may wait; more are rejected as busy "
                        "(default: %(default)s)")
    p.add_argument("--max-body-bytes", type=int, default=DEFAULT_MAX_BODY_BYTES,
                   help="largest request body; larger is 413 (default: %(default)s)")
    p.add_argument("--read-timeout-seconds", type=float,
                   default=DEFAULT_READ_TIMEOUT_SECONDS,
                   help="time to receive a request's header, and again its "
                        "body (default: %(default)s)")
    p.add_argument("--max-result-bytes", type=int, default=DEFAULT_MAX_RESULT_BYTES,
                   help="largest result a judgement process may send "
                        "(default: %(default)s)")
    p.add_argument("--max-cache-transfer-bytes", type=int,
                   default=DEFAULT_MAX_CACHE_TRANSFER_BYTES,
                   help="new parse-cache bytes one judgement may hand back; "
                        "the rest are dropped (default: %(default)s)")
    p.add_argument("--max-connections", type=int, default=DEFAULT_MAX_CONNECTIONS,
                   help="open client connections served at once; past it a new "
                        "connection is answered busy and closed (default: %(default)s)")
    p.add_argument("--max-inflight-body-bytes", type=int,
                   default=DEFAULT_MAX_INFLIGHT_BODY_BYTES,
                   help="request body bytes being read or queued at once, "
                        "reserved from Content-Length before the body is read; "
                        "past it a request is answered busy (default: %(default)s)")
    p.add_argument("--max-writes", type=int, default=DEFAULT_MAX_WRITES,
                   help="writes in one request; more is a 400 (default: %(default)s)")
    p.add_argument("--warm-ref", default=DEFAULT_WARM_REF,
                   help="commit-ish whose tree is parsed into the cache at start, "
                        "off any request's clock; an empty value turns start-up "
                        "warming off (default: %(default)s)")
    p.add_argument("--warm-seconds", type=float, default=DEFAULT_WARM_SECONDS,
                   help="time limit of one warming run; a request arriving ends "
                        "it at once (default: %(default)s)")
    p.add_argument("--self-check-seconds", type=float, default=DEFAULT_SELF_CHECK_SECONDS,
                   help="after a failed self-check of da-guard, da-read and "
                        "da-crdecode, the wait before the next (a new tool fault "
                        "runs one at once); only a passing one makes the service "
                        "ready (default: %(default)s)")
    p.add_argument("--self-check-timeout-seconds", type=float,
                   default=DEFAULT_SELF_CHECK_TIMEOUT_SECONDS,
                   help="time limit of one program's self-check, or of one git "
                        "run of the --repo probe; the moment it is past, the round "
                        "fails (timed_out; repo_unreadable for the probe) and the "
                        "service is not ready (default: %(default)s)")
    return p


class RepoRefused(RuntimeError):
    """The judgement of --repo (:func:`_repo_judgement`) refuses it; the
    message names --repo and says why."""


@dataclass(frozen=True)
class RepoState:
    """What the judgement of --repo found: its object format, and the commit
    ``HEAD`` names — None when ``HEAD`` is a branch with no commit yet
    (`_head_is_unborn`)."""

    object_format: str
    head: Optional[str]


def _repo_judgement(git: str, repo: str):
    """The one judgement of --repo (#3739, #3903, #4035), as a generator of
    its steps: it yields each git argv to run, in order, is sent ``(rc,
    stdout, stderr)`` of each, and returns a :class:`RepoState` — or raises
    :class:`RepoRefused`. It runs nothing itself: start-up
    (:func:`_repo_state`) and the self-check's probe
    (``Service._repo_probe``) each run what it yields their own way, and
    neither repeats an argv or a reading. Git is the reader of --repo, so
    git judges it, and the refusal names the flag.

    * not a repository: git's own reason, after ``--repo <path>:`` — it was
      git's bare ``fatal: not a git repository … .git``, naming neither;
    * a directory INSIDE a repository (``--show-prefix`` not empty): refused —
      ``git -C`` walks up to the enclosing repository, so a subdirectory was
      served as that repository with --config-subdir read from its root (an
      empty conf.d, every write judged against nothing);
    * a bare repository or a linked worktree (``.git`` a ``gitdir:`` file):
      the root, served. A directory inside a bare repository (``objects/``)
      has an empty prefix too and is served: it resolves to that same
      repository, whose ``HEAD:<config-subdir>`` is the same conf.d;
    * ``HEAD``: the commit it resolves to (``rev-parse --verify``), or a
      branch with no commit yet (`_head_is_unborn`).

    ⛔ Only that (#3903): every other way ``HEAD`` resolves to no commit —
    the repository gone or unreadable since the first git run, a ``HEAD``
    naming a commit whose object is missing — is a repository git cannot
    read, refused, naming --repo. It was all read as "no HEAD commit": the
    service started, answered ``ready``, and every request was
    ``rejected/git_read``."""
    shown = safe_label(repo) if repo else "''"
    rc, out, err = yield [git, "-C", repo, "rev-parse", "--show-object-format",
                          "--show-prefix"]
    if rc != 0:
        why = " ".join(err.decode("utf-8", "replace").split()) or f"git exited {rc}"
        raise RepoRefused(f"--repo {shown}: {safe_label(why)}")
    fmt, _, prefix = out.decode("utf-8", "replace").partition("\n")
    fmt, prefix = fmt.strip(), prefix.strip()
    if prefix:
        raise RepoRefused(f"--repo {shown}: a directory inside a repository "
                          f"({safe_label(prefix)} below its root), not the root of one: "
                          f"give the repository's root")
    if fmt not in ("sha1", "sha256"):
        raise RepoRefused(f"--repo {shown}: unknown object format {safe_label(fmt)!r}")

    resolve = [git, "-C", repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"]

    def commit_of(rc: int, out: bytes) -> str:
        return out.decode("ascii", "replace").strip() if rc == 0 else ""

    rc, out, _err = yield resolve
    head = commit_of(rc, out)
    if not head:
        unborn, why = yield from _head_is_unborn(git, repo)
        if not unborn:
            # Once more: the first commit may have landed between the two
            # looks (HEAD was unborn for `rev-parse`, a commit for `show-ref`).
            rc, out, _err = yield resolve
            head = commit_of(rc, out)
            if not head:
                raise RepoRefused(f"--repo {shown}: HEAD names no commit git can "
                                  f"read ({safe_label(why)})")
    return RepoState(fmt, head or None)


def _repo_state(git: str, repo: str) -> RepoState:
    """:func:`_repo_judgement` at start: each git it yields run here in
    turn, synchronously (no loop runs yet). An exec of --git that fails on
    its first run names --git (#3415), not a bare errno."""
    shown_git = safe_label(git) if git else "''"  # '' is SUPPLIED, as _lib_paths shows it
    try:
        env = _gsvc_child.git_env(git)  # the first run of --git (#3739: its own answer)
    except OSError as exc:
        raise RuntimeError(f"--git {shown_git}: {exc.strerror}") from exc
    except _gsvc_child.GitReadError as exc:
        raise RuntimeError(f"--git {shown_git}: {exc}") from exc
    steps = _repo_judgement(git, repo)
    first = True
    try:
        argv = next(steps)
        while True:
            try:
                proc = subprocess.run(argv, capture_output=True, check=False, timeout=60,
                                      env=env)
            except OSError as exc:
                if not first:
                    raise
                raise RuntimeError(f"--git {shown_git}: {exc.strerror}") from exc
            first = False
            argv = steps.send((proc.returncode, proc.stdout, proc.stderr))
    except StopIteration as done:
        return done.value


def _normalize_config_subdir(value: str) -> str:
    """--config-subdir as the paths ``git ls-tree`` prints: no ``./``, no
    trailing ``/``, and ``""`` for the repository root (``.``). Normalised
    once here and used as is from then on, because a spelling ls-tree does not
    print (``./conf.d``) matches no entry: the judged tree would be empty and
    every write would come back as findings, which reads as a config error
    when it is a deployment one. An absolute path or a ``..`` segment names
    nothing inside the repository and refuses to start."""
    if value.startswith("/"):
        raise RuntimeError(f"--config-subdir {value!r} is absolute; give conf.d's "
                           f"path inside the repository")
    if ".." in value.split("/"):
        raise RuntimeError(f"--config-subdir {value!r} has a '..' segment; give "
                           f"conf.d's path inside the repository")
    norm = posixpath.normpath(value) if value else "."
    return "" if norm == "." else norm


#: `git ls-tree`'s mode of a directory.
_TREE_MODE = "040000"


def _head_is_unborn(git: str, repo: str):
    """A step of :func:`_repo_judgement` (``yield from``): ``(whether HEAD
    is a branch nothing was committed to yet, what git said when it is
    not)`` — `git init`, `git checkout --orphan`: a symbolic ref (``git
    symbolic-ref -q HEAD`` exits 0, naming it) whose ref is not there (``git
    show-ref --verify --quiet`` exits 1).

    ⛔ Not ``rev-parse --verify --quiet HEAD^{commit}`` failing: that exits
    1 with nothing on stderr for an unborn branch AND for a branch naming a
    commit whose object is gone (measured, git 2.43: #3903). What tells them
    apart: a ref that is there but broken — empty, garbage, unreadable — is
    ``symbolic-ref``'s 128 (``No such ref: HEAD``: it resolves the chain); a
    ref naming a missing object is ``show-ref``'s 128 (``bad ref``); a ref
    naming something that is no commit is ``show-ref``'s 0; a detached
    ``HEAD`` is no symbolic ref (1); a repository git cannot open is 128."""
    def said(rc, stderr, what):
        return (" ".join(stderr.decode("utf-8", "replace").split())
                or f"git {what} exited {rc}")
    rc, out, err = yield [git, "-C", repo, "symbolic-ref", "-q", "HEAD"]
    # ⛔ The ref's own bytes, less git's one newline: `str.strip()` also takes
    # a trailing U+00A0, which a branch name may end in — `topic `
    # naming a missing commit was asked about as `topic`, found absent, and
    # so taken for unborn.
    name = out[:-1] if out.endswith(b"\n") else out
    if rc != 0 or not name:
        return False, said(rc, err, "symbolic-ref")
    rc, _out, err = yield [os.fsencode(git), b"-C", os.fsencode(repo), b"show-ref",
                           b"--verify", b"--quiet", name]
    if rc == 0:
        return False, f"{name.decode('utf-8', 'replace')} is there and names no commit"
    return rc == 1, said(rc, err, "show-ref")


def _check_config_subdir(git: str, repo: str, sub: str, shown: str,
                         head: Optional[str]) -> None:
    """#3392: --config-subdir is looked up in the tree of the revision the
    service starts on (*head*, the commit ``HEAD`` names as
    :func:`_repo_judgement` found it) — by its own entry,
    `_gsvc_child.path_mode`, as a literal path like every judgement's
    reading. A directory starts; what it holds is each judgement's to read.
    A path that is in the tree but is no directory — a file, a symlink, a
    submodule — is an empty conf.d for every judgement (a write that
    duplicates a tenant came back ``pass``): the service refuses to start.

    A path with no entry at all is said on stderr and the service starts:
    git keeps no empty directory, so a conf.d whose last tenant was
    offboarded, or one the first write has yet to create, reads the same as
    a misspelt one — and a service that refused it could never take that
    first write. So does a ``HEAD`` that is no commit YET (*head* None: a
    branch with nothing committed). Every other ``HEAD`` git cannot read
    was refused by :func:`_repo_judgement` before this runs."""
    if head is None:
        print(f"generator-service: --repo {safe_label(repo)} has no HEAD commit; "
              f"--config-subdir {shown} is not checked", file=sys.stderr, flush=True)
        return
    if not sub:
        return  # the root is a tree whatever it holds
    try:
        mode = _gsvc_child.path_mode(git, repo, head, sub)
    except _gsvc_child.GitReadError as exc:
        raise RuntimeError(f"--config-subdir {shown}: {exc}") from exc
    if mode == _TREE_MODE:
        return  # what is in it is each judgement's to read
    if mode is not None:
        raise RuntimeError(f"--config-subdir {shown}: not a directory in --repo "
                           f"{safe_label(repo)} at HEAD ({head[:12]}; mode {mode})")
    print(f"generator-service: --config-subdir {shown}: no entries in --repo "
          f"{safe_label(repo)} at HEAD ({head[:12]}); judging it as an empty conf.d",
          file=sys.stderr, flush=True)


def _prepare_socket_path(path: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise RuntimeError(f"--socket {path!r} exists and is not a socket; "
                           f"refusing to replace it")
    os.unlink(path)


def _jobs_dir(work_dir: str) -> str:
    """Where the judgements' directories live: one subdirectory of
    --work-dir that this service owns whole."""
    return os.path.join(work_dir, "jobs")


def _check_limits(settings: Settings) -> None:
    """Limits that contradict each other are a caller error at start: an
    in-flight budget below the largest body would answer busy, forever, to
    every request whose body lies between the two."""
    if settings.max_inflight_body_bytes < settings.max_body_bytes:
        raise RuntimeError(
            f"--max-inflight-body-bytes {settings.max_inflight_body_bytes} is "
            f"below --max-body-bytes {settings.max_body_bytes}: a request in "
            f"between could never be admitted; raise the first or lower the "
            f"second")
    # A self-check every 0 seconds is a busy loop; one with no time never passes.
    for flag, value in (("--self-check-seconds", settings.self_check_seconds),
                        ("--self-check-timeout-seconds", settings.self_check_timeout_seconds)):
        if not value > 0:
            raise RuntimeError(f"{flag} {value} is not a positive number of seconds")


def _ensure_fd_limit(max_connections: int) -> None:
    """#2901 R1: RLIMIT_NOFILE must hold every connection slot plus
    FD_RESERVE, so running out of descriptors is decided here, at start, and
    not as an EMFILE under load. The soft limit is raised (up to the hard
    limit) when it is short; still short is a refusal to start."""
    import resource
    need = max_connections + FD_RESERVE
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= need:
        return
    if hard != resource.RLIM_INFINITY and hard < need:
        raise RuntimeError(
            f"RLIMIT_NOFILE hard limit {hard} is below the {need} descriptors "
            f"--max-connections {max_connections} needs; lower "
            f"--max-connections or raise the limit")
    resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))


def _become_subreaper() -> None:
    """#2901 R3: adopt orphaned descendants (prctl PR_SET_CHILD_SUBREAPER), so
    the processes a killed judgement leaves behind are collected here whether
    or not this process is PID 1."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise RuntimeError(f"prctl(PR_SET_CHILD_SUBREAPER): {os.strerror(err)}")


def _prepare_work_dir(path: str) -> None:
    """Create --work-dir, and empty what a previous run left in its jobs
    directory (a crash between fork and cleanup)."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    shutil.rmtree(_jobs_dir(path), ignore_errors=True)
    os.mkdir(_jobs_dir(path), 0o700)


async def serve(settings: Settings, object_format: str) -> None:
    service = Service(settings, object_format)
    service.loop = asyncio.get_running_loop()
    old = os.umask(0o177)  # the socket is created 0600
    try:
        server = await asyncio.start_unix_server(
            service.handle, path=settings.socket, limit=HEADER_LIMIT_BYTES)
    finally:
        os.umask(old)
    stop = service.loop.create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        service.loop.add_signal_handler(
            sig, lambda: stop.done() or stop.set_result(None))
    print(f"generator-service: listening on {settings.socket}", file=sys.stderr,
          flush=True)
    service.loop.add_signal_handler(signal.SIGCHLD, service._reap_orphans)
    service.loop.set_exception_handler(service.loop_exception)
    # #3943: the programs' self-check, on the path a tool fault's rerun takes
    # — after the signal handlers, so SIGTERM ends it like anything else.
    # Until its first round has passed the status says `pending`, not ready.
    service._ensure_self_check_loop()
    service.start_warming()
    try:
        await stop
    finally:
        server.close()
        service.shutdown()
        await service.stop_self_check()
        try:
            os.unlink(settings.socket)
        except OSError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings(**{k: getattr(args, k) for k in Settings.__dataclass_fields__})
    # #3415: --da-guard-binary is the first step of the one resolution, so
    # it goes where every reader of it looks — $DA_GUARD_BINARY, inherited by
    # each judgement process — not into a second lookup of its own: the
    # version reported and the da-guard that judges are then one binary.
    if _given(settings.da_guard_binary):
        os.environ[_lib_consumer_read.DA_GUARD_ENV] = settings.da_guard_binary
    # Absolute: the judgement process runs in its own job directory, and so
    # does every reader binary it runs — da-guard (#2906 review), and the
    # da-read / da-crdecode of the route generator (#3424).
    _pin_readers()
    for name in ("socket", "repo", "work_dir", "policy"):
        value = getattr(settings, name)
        if value is not None:
            setattr(settings, name, os.path.abspath(value))
    # #3739 review: a --git with a path (`tools/git`) is made absolute too —
    # each judgement runs it from its own job directory, where a relative
    # one is not there. A bare name (`git`) stays a PATH lookup.
    if settings.git and os.path.dirname(settings.git):
        settings.git = os.path.abspath(settings.git)
    try:
        settings.config_subdir = _normalize_config_subdir(settings.config_subdir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    repo_problem = classify_path(settings.repo, "dir", lang="en", flag="--repo")  # #3276
    if repo_problem is not None:
        print(f"ERROR: {repo_problem.message}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        state = _repo_state(settings.git, settings.repo)
        fmt = state.object_format
        _check_config_subdir(settings.git, settings.repo, settings.config_subdir,
                             safe_label(repr(args.config_subdir)), state.head)
        _prepare_work_dir(settings.work_dir)
        _check_limits(settings)
        _prepare_socket_path(settings.socket)
        _ensure_fd_limit(settings.max_connections)
        _become_subreaper()
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        asyncio.run(serve(settings, fmt))
    except OSError as exc:  # bind: path too long, no such directory, …
        print(f"ERROR: cannot listen on {settings.socket!r}: {exc}", file=sys.stderr)
        return EXIT_CALLER_ERROR
    return EXIT_OK


if __name__ == "__main__":
    import _lib_exitcodes  # noqa: E402
    _lib_exitcodes.run(main)
