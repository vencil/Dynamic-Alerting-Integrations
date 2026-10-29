#!/usr/bin/env python3
"""`da-tools generator-service`: judges conf.d writes for tenant-api (ADR-036 §3).

The generator container's service (ADR-036 §3, §5, §6; #2901).

tenant-api asks it, before every write, whether the conf.d tree with that
write applied passes ``generate-routes --validate --strict``. It speaks
HTTP/1.1 on a Unix socket (no network port) and serves:

* ``POST /v1/validate`` — judge a base commit plus writes; answers 200 with
  ``result`` ``pass`` / ``findings`` / ``rejected`` whenever this process
  reaches a verdict for that request, 400 for a malformed request, 503 when
  the container cannot judge at all (``VALIDATOR_UNAVAILABLE``: no amtool, a
  generator older than the policy's ``min_generator_version``;
  ``POLICY_UNAVAILABLE``: the deployment ``--policy`` file is unusable);
* ``GET /v1/status`` — policy availability, generator and da-guard versions,
  amtool, readiness;
* ``GET /metrics`` — Prometheus text format.

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
import _gsvc_child  # noqa: E402  (imports the whole generator — see _gsvc_child)
from _gsvc_child import ChildJob, gar  # noqa: E402
from _gsvc_proto import (  # noqa: E402
    CacheStore, FrameReader, RequestError, ValidateRequest,
    judgement_seconds, parse_validate_request,
)
from _lib_parse_cache import restricted_loads  # noqa: E402
import _lib_parse_tree  # noqa: E402
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
        return (json.dumps(self.doc, ensure_ascii=False) + "\n").encode("utf-8")

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


def _rejected(reason: str, message: str, **extra) -> Response:
    doc = {"result": "rejected", "reason": reason, "message": message,
           "rc": None, "findings": None, "stderr_tail": "", "validated": None}
    doc.update(extra)
    return Response(200, doc)


def _unavailable(error: str, reason: str, message: str) -> Response:
    return Response(503, {"error": error, "reason": reason, "message": message},
                    headers={"Retry-After": "5"})


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
    orphans_reaped: int = 0
    accept_errors: Counter = field(default_factory=Counter)
    peer_rejected: int = 0
    peak_rss_bytes: int = 0
    #: ADR-038 decision 5: key verdicts by outcome — complete, or why not
    #: (no_time, deadline, preempted, memory, error, platform_file) — and
    #: the latest one's duration.
    key_verdicts: Counter = field(default_factory=Counter)
    key_verdict_seconds: float = 0.0


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def da_guard_version(binary: Optional[str]) -> Optional[str]:
    """``da-guard --version``'s version, or None when there is no binary."""
    import guard_dispatch
    exe = guard_dispatch.DISPATCHER.resolve_binary(binary)
    if exe is None:
        return None
    try:
        proc = subprocess.run([exe, "--version"], capture_output=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = proc.stdout.decode("utf-8", "replace").split()
    return words[-1] if proc.returncode == 0 and words else None


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
        self.da_guard_version = da_guard_version(settings.da_guard_binary)
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
            await self._send(reader, writer, self._count(_rejected(
                "busy", "too many open connections; retry")))
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
            return Response(431, {"error": "BAD_REQUEST",
                                  "message": "request header too large"})
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
            return None
        # #2901 O6: the request's clock starts here — reading the body is
        # part of its budget.
        received = self.loop.time()
        try:
            method, target, headers = _parse_head(head)
        except Exception:  # noqa: BLE001 — #2901 O1: any bad header is a 400
            return Response(400, {"error": "BAD_REQUEST",
                                  "message": "malformed request line or header"})

        routes = {"/v1/validate": "POST", "/v1/status": "GET", "/metrics": "GET"}
        if target not in routes:
            return Response(404, {"error": "NOT_FOUND", "message": target})
        if method != routes[target]:
            return Response(405, {"error": "METHOD_NOT_ALLOWED",
                                  "message": f"{target} takes {routes[target]}"},
                            headers={"Allow": routes[target]})
        if "transfer-encoding" in headers:
            return Response(411, {"error": "LENGTH_REQUIRED",
                                  "message": "Transfer-Encoding is not supported; "
                                             "send Content-Length"})
        if method == "GET":
            return self._status() if target == "/v1/status" else self._metrics()

        lengths = headers.get("content-length")
        if not lengths:
            return Response(411, {"error": "LENGTH_REQUIRED",
                                  "message": "Content-Length is required"})
        if len(lengths) != 1 or not _CONTENT_LENGTH.fullmatch(lengths[0]):
            return Response(400, {"error": "BAD_REQUEST",
                                  "message": "invalid Content-Length"})
        length = int(lengths[0])
        if length > self.s.max_body_bytes:
            resp = Response(413, {"error": "PAYLOAD_TOO_LARGE",
                                  "message": f"body is {length} bytes; the limit "
                                             f"is {self.s.max_body_bytes}"})
            resp.unread_body = True
            return resp
        # #2901 R1, admission (b): the body's bytes are reserved against the
        # in-flight budget BEFORE they are read, and given back when this
        # request is answered. Over budget: busy, body never read.
        if self.inflight_body_bytes + length > self.s.max_inflight_body_bytes:
            resp = self._count(_rejected(
                "busy", "too many request bytes in flight; retry"))
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
                return Response(400, {"error": "BAD_REQUEST", "message": str(exc)})
            del body
            return await self.submit(request, received)
        finally:
            self.inflight_body_bytes -= length

    # ── queue ─────────────────────────────────────────────────────────────

    async def submit(self, request: ValidateRequest, received: float) -> Response:
        budget = judgement_seconds(request.deadline_ms, self.s.max_judgement_seconds,
                                   self.s.deadline_reserve_seconds)
        if budget <= 0:
            return self._count(_rejected(
                "deadline", "deadline_ms leaves no time to judge"))
        if len(self.waiting) >= self.s.max_queue:
            return self._count(_rejected(
                "busy", "too many judgements are waiting; retry"))
        ticket = Ticket(request, received + budget, self.loop.create_future(),
                        received=received)
        self.waiting.append(ticket)
        ticket.timer = self.loop.call_at(ticket.deadline_at, self._expire, ticket)
        # ADR-038 decision 5: a running judgement's key verdict yields to a
        # waiting write (the judgement's own verdict is already given).
        if self.running is not None and self.child_pid is not None:
            try:
                os.kill(self.child_pid, signal.SIGUSR1)
            except ProcessLookupError:
                pass
        # #2901 E2: a request never waits for a warming run — end it now; the
        # entries it already handed back stay in the cache (E1).
        self._preempt_warm()
        self._start_next()
        return self._count(await ticket.future)

    def _expire(self, ticket: Ticket) -> None:
        if ticket in self.waiting:
            self.waiting.remove(ticket)
            if not ticket.future.done():
                ticket.future.set_result(_rejected(
                    "deadline", "the deadline passed while queued"))

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
            return _rejected("deadline", "the deadline passed while queued")
        if shutil.which("amtool") is None:
            return _unavailable(
                "VALIDATOR_UNAVAILABLE", "amtool_missing",
                "amtool is not on PATH: without it the generator's check of the "
                "rendered Alertmanager config is skipped, so no verdict is given")
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
                return _rejected("busy", f"cannot start a judgement "
                                         f"({errno.errorcode.get(exc.errno, exc.errno)}); retry")
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
        writes, no verdict, no amtool — only to fill the cache (E1). Ended
        at once when a request arrives; bounded by --warm-seconds; never on
        a request's clock."""
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
            complete = (os.WIFEXITED(run.status) and os.WEXITSTATUS(run.status) == 0
                        and run.reader.ended and run.reader.result is not None
                        and run.reader.result.get("outcome") == "warmed")
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
            _, status, rusage = os.wait4(pid, 0)
            _kill_group(pid)  # anything the process left running
            self.child_pid = None
            # the one direct child has been collected by wait4 above; only
            # orphans can be left for waitpid(-1)
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
            return _rejected("deadline", "the deadline passed while judging",
                             cache=received)
        if os.WIFSIGNALED(run.status) or os.WEXITSTATUS(run.status) != 0:
            return _rejected("incomplete", "the judgement process ended without "
                             "a complete result", cache=received)
        res = run.reader.result
        if res is None or not run.reader.ended:
            return _rejected("incomplete", "the judgement process ended without "
                             "a complete result", cache=received)
        self.store.touch(tuple(k) for k in res.get("hit_keys", [])
                         if isinstance(k, list) and len(k) == 2)
        outcome = res.get("outcome")
        cache = res.get("cache")
        cache = {**cache, **received} if isinstance(cache, dict) else received
        common = {"stderr_tail": res.get("stderr_tail", ""),
                  "validated": res.get("validated"), "cache": cache}
        if outcome == "ran":
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
            return Response(400, {"error": "BAD_REQUEST",
                                  "message": res.get("message", "")})
        if outcome == "memory":
            return _rejected("memory", "the judgement ran out of memory "
                             "(RLIMIT_DATA)", **common)
        if outcome == "git_read":
            return _rejected("git_read", "reading the base commit from git "
                             "failed twice", **common)
        if outcome == "da_guard":
            # No reason of its own on the wire (tenant-api's closed list):
            # no verdict was reached, like any other incomplete judgement;
            # the message says which step failed.
            return _rejected("incomplete", "da-guard parse-tree could not classify the "
                             "base tree's path names: " + str(res.get("message", "")),
                             **common)
        return _rejected("incomplete", "the judgement process sent no "
                         "complete result", **common)

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
        amtool = shutil.which("amtool") is not None
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
            "amtool_available": amtool,
            "ready": policy.available and amtool and too_old is None,
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
        for reason in ("amtool_missing", "generator_too_old", "policy_unusable",
                       "internal_error"):
            out.append(f'da_generator_unavailable_total{{reason="{reason}"}} '
                       f"{self.metrics.unavailable[reason]}")
        out += ["# HELP da_generator_warm_runs_total Pre-warming runs (cache filling, no verdict), by outcome.",
                "# TYPE da_generator_warm_runs_total counter"]
        for outcome in ("completed", "preempted", "deadline", "failed"):
            out.append(f'da_generator_warm_runs_total{{outcome="{outcome}"}} '
                       f"{self.metrics.warm_runs[outcome]}")
        out += ["# HELP da_generator_key_verdicts_total Key verdicts (ADR-038) on writes the generator passed, by outcome: complete, or why not completed.",
                "# TYPE da_generator_key_verdicts_total counter"]
        for outcome in ("complete", "no_time", "deadline", "preempted", "memory", "error",
                        "platform_file"):
            out.append(f'da_generator_key_verdicts_total{{outcome="{outcome}"}} '
                       f"{self.metrics.key_verdicts[outcome]}")
        out += ["# HELP da_generator_key_verdict_seconds Duration of the latest key verdict.",
                "# TYPE da_generator_key_verdict_seconds gauge",
                f"da_generator_key_verdict_seconds {self.metrics.key_verdict_seconds}"]
        out += ["# HELP da_generator_cache_base_coverage_ratio Share of the pinned base tree's blobs with a parse-cache entry.",
                "# TYPE da_generator_cache_base_coverage_ratio gauge",
                f"da_generator_cache_base_coverage_ratio {self.store.coverage()}"]
        out += ["# HELP da_generator_orphans_reaped_total Exited processes left by judgements (amtool and its children) collected by this subreaper.",
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
        (amtool, its children). This process is a child subreaper, so they are
        re-parented here when the judgement ends — whether or not it is PID 1.

        ⛔ Only between runs: ``waitpid(-1)`` would also collect the running
        judgement and take its exit status from ``wait4``. At most one direct
        child exists at a time, and this is called only after ``wait4`` has
        collected it (or before the next fork), so there is none to steal.
        """
        if self.child_pid is not None:
            return 0  # a run is in progress: wait for it to end first
        reaped = 0
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
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
                capture_output=True, check=False, timeout=60)
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
                   help="da-guard used for the reported version (default: "
                        "$DA_GUARD_BINARY, then PATH)")
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
    return p


def _object_format(git: str, repo: str) -> str:
    proc = subprocess.run([git, "-C", repo, "rev-parse", "--show-object-format"],
                          capture_output=True, check=False, timeout=60)
    fmt = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or fmt not in ("sha1", "sha256"):
        raise RuntimeError(proc.stderr.decode("utf-8", "replace").strip()
                           or f"unknown object format {fmt!r}")
    return fmt


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
    # Inherited by every judgement process from its fork on: a key verdict
    # yields to a waiting write (submit); anywhere else SIGUSR1 does nothing.
    signal.signal(signal.SIGUSR1, _gsvc_child.on_preempt)
    service.loop.set_exception_handler(service.loop_exception)
    service.start_warming()
    try:
        await stop
    finally:
        server.close()
        service.shutdown()
        try:
            os.unlink(settings.socket)
        except OSError:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings(**{k: getattr(args, k) for k in Settings.__dataclass_fields__})
    # Absolute: the judgement process runs in its own job directory — the
    # da-guard it runs too (a relative $DA_GUARD_BINARY, #2906 review).
    _lib_parse_tree.absolute_da_guard_binary()
    for name in ("socket", "repo", "work_dir", "policy"):
        value = getattr(settings, name)
        if value is not None:
            setattr(settings, name, os.path.abspath(value))
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
        fmt = _object_format(settings.git, settings.repo)
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
