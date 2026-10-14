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

One judgement runs at a time; the others wait in a bounded queue. Each
request's time budget is ``min(--max-judgement-seconds, deadline_ms -
--deadline-reserve-seconds)`` from the moment its body has been read, queue
time included.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import signal
import stat
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
import _gsvc_child  # noqa: E402  (imports the whole generator — see _gsvc_child)
from _gsvc_child import ChildJob, gar  # noqa: E402
from _gsvc_proto import (  # noqa: E402
    CacheStore, FrameReader, RequestError, ValidateRequest,
    judgement_seconds, parse_validate_request,
)
from _lib_parse_cache import restricted_loads  # noqa: E402
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
DEFAULT_MAX_BODY_BYTES = 16 * MiB
DEFAULT_READ_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESULT_BYTES = 16 * MiB
DEFAULT_MAX_CACHE_TRANSFER_BYTES = 64 * MiB
DEFAULT_WARM_REF = "HEAD"
DEFAULT_WARM_SECONDS = 300.0
HEADER_LIMIT_BYTES = 16 * 1024
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


class Response:
    def __init__(self, status: int, doc: Optional[dict] = None, *,
                 text: str | None = None, headers: Optional[dict] = None) -> None:
        self.status = status
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
    peak_rss_bytes: int = 0


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
        self.loop: asyncio.AbstractEventLoop = None  # type: ignore[assignment]

    # ── HTTP ──────────────────────────────────────────────────────────────

    async def handle(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            resp = await self._respond(reader)
            if resp is not None:
                writer.write(resp.encode())
                await writer.drain()
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
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None
        try:
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
        except ValueError:
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
        if len(lengths) != 1 or not lengths[0].isdigit():
            return Response(400, {"error": "BAD_REQUEST",
                                  "message": "invalid Content-Length"})
        length = int(lengths[0])
        if length > self.s.max_body_bytes:
            return Response(413, {"error": "PAYLOAD_TOO_LARGE",
                                  "message": f"body is {length} bytes; the limit "
                                             f"is {self.s.max_body_bytes}"})
        try:
            body = await asyncio.wait_for(reader.readexactly(length), timeout)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None
        received = self.loop.time()
        try:
            request = parse_validate_request(body, self.object_format)
        except RequestError as exc:
            return Response(400, {"error": "BAD_REQUEST", "message": str(exc)})
        return await self.submit(request, received)

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
        if self.warming is not None and self.child_pid is not None \
                and not self._preempting:
            self._preempting = True
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
             policy: Optional[PolicySnapshot], warm: bool = False) -> ChildJob:
        return ChildJob(
            request=request, job_dir=job_dir, repo=self.s.repo,
            config_subdir=self.s.config_subdir, git=self.s.git,
            object_format=self.object_format,
            memory_limit=self.s.memory_limit_bytes,
            policy_copy=policy.copy if policy else None,
            policy_sha256=policy.sha256 if policy else None,
            store_entries=self.store.entries,
            max_transfer=self.s.max_cache_transfer_bytes, warm=warm)

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
            run = await self._spawn(self._job(ticket.request, job_dir, policy),
                                    ticket.deadline_at, request=True)
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
        sys.stdout.flush()
        sys.stderr.flush()
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:  # the judgement (or warming) process
            os.close(rfd)
            _gsvc_child.run_child(job, wfd)
        os.close(wfd)
        self.child_pid = pid
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
            return Response(200, {"result": "pass" if rc == 0 else "findings",
                                  "rc": rc, "findings": res.get("findings"),
                                  **common})
        if outcome == "bad_request":
            return Response(400, {"error": "BAD_REQUEST",
                                  "message": res.get("message", "")})
        if outcome == "memory":
            return _rejected("memory", "the judgement ran out of memory "
                             "(RLIMIT_DATA)", **common)
        if outcome == "git_read":
            return _rejected("git_read", "reading the base commit from git "
                             "failed twice", **common)
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
        out += ["# HELP da_generator_cache_base_coverage_ratio Share of the pinned base tree's blobs with a parse-cache entry.",
                "# TYPE da_generator_cache_base_coverage_ratio gauge",
                f"da_generator_cache_base_coverage_ratio {self.store.coverage()}"]
        out += ["# HELP da_generator_queue_depth Validate requests waiting for the running judgement.",
                "# TYPE da_generator_queue_depth gauge",
                f"da_generator_queue_depth {len(self.waiting)}",
                "# HELP da_generator_build_info Versions of the generator and da-guard in this container.",
                "# TYPE da_generator_build_info gauge",
                'da_generator_build_info{generator_version="%s",da_guard_version="%s"} 1'
                % (_escape_label(self.generator_version or ""),
                   _escape_label(self.da_guard_version or ""))]
        return Response(200, text="\n".join(out) + "\n")

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
                   help="conf.d's path inside the repository, e.g. conf.d")
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
    # Absolute: the judgement process runs in its own job directory.
    for name in ("socket", "repo", "work_dir", "policy"):
        value = getattr(settings, name)
        if value is not None:
            setattr(settings, name, os.path.abspath(value))
    if not os.path.isdir(settings.repo):
        print(f"ERROR: --repo {settings.repo!r} is not a directory", file=sys.stderr)
        return EXIT_CALLER_ERROR
    try:
        fmt = _object_format(settings.git, settings.repo)
        _prepare_work_dir(settings.work_dir)
        _prepare_socket_path(settings.socket)
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
    sys.exit(main())
