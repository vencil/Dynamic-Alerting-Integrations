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
    CacheStore, RequestError, ValidateRequest, decode_frames,
    judgement_seconds, parse_validate_request,
)
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


class Response:
    def __init__(self, status: int, doc: object = None, *, text: str | None = None,
                 headers: Optional[dict] = None) -> None:
        self.status = status
        if text is not None:
            self.body = text.encode("utf-8")
            self.ctype = "text/plain; version=0.0.4; charset=utf-8"
        else:
            self.body = (json.dumps(doc, ensure_ascii=False) + "\n").encode("utf-8")
            self.ctype = "application/json"
        self.headers = headers or {}

    def encode(self) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {_REASONS.get(self.status, 'Unknown')}",
                 f"Content-Type: {self.ctype}",
                 f"Content-Length: {len(self.body)}",
                 "Connection: close"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + self.body


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
        self.running_pid: Optional[int] = None
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
        ticket = Ticket(request, received + budget, self.loop.create_future())
        self.waiting.append(ticket)
        ticket.timer = self.loop.call_at(ticket.deadline_at, self._expire, ticket)
        self._start_next()
        return self._count(await ticket.future)

    def _expire(self, ticket: Ticket) -> None:
        if ticket in self.waiting:
            self.waiting.remove(ticket)
            if not ticket.future.done():
                ticket.future.set_result(_rejected(
                    "deadline", "the deadline passed while queued"))

    def _start_next(self) -> None:
        if self.running is not None or not self.waiting:
            return
        ticket = self.waiting.popleft()
        if ticket.timer is not None:
            ticket.timer.cancel()
        self.running = ticket
        self.loop.create_task(self._run(ticket))

    async def _run(self, ticket: Ticket) -> None:
        try:
            resp = await self._judge(ticket)
        except Exception as exc:  # noqa: BLE001 — this process erred: 503
            resp = _unavailable("VALIDATOR_UNAVAILABLE", "internal_error",
                                f"generator service error: {exc!r}")
        finally:
            self.running = None
            self.running_pid = None
        if not ticket.future.done():
            ticket.future.set_result(resp)
        self._start_next()

    def _count(self, resp: Response) -> Response:
        if resp.status == 200:
            doc = json.loads(resp.body)
            self.metrics.judgements[doc["result"]] += 1
            if doc["result"] == "rejected":
                self.metrics.rejections[doc["reason"]] += 1
        elif resp.status == 503:
            doc = json.loads(resp.body)
            self.metrics.unavailable[doc.get("reason") or doc["error"]] += 1
        return resp

    # ── one judgement ─────────────────────────────────────────────────────

    def _new_job_dir(self) -> str:
        self._job_seq += 1
        path = os.path.join(_jobs_dir(self.s.work_dir), f"job-{self._job_seq}")
        os.mkdir(path, 0o700)
        return path

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
            job = ChildJob(
                request=ticket.request, job_dir=job_dir, repo=self.s.repo,
                config_subdir=self.s.config_subdir, git=self.s.git,
                object_format=self.object_format,
                memory_limit=self.s.memory_limit_bytes,
                policy_copy=policy.copy, policy_sha256=policy.sha256,
                store_entries=self.store.entries,
                max_transfer=self.s.max_cache_transfer_bytes)
            return await self._fork_judgement(job, ticket.deadline_at)
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)

    async def _fork_judgement(self, job: ChildJob, deadline_at: float) -> Response:
        threads = threading.active_count()
        if threads != 1:
            raise RuntimeError(f"refusing to fork with {threads} threads")
        sys.stdout.flush()
        sys.stderr.flush()
        rfd, wfd = os.pipe()
        pid = os.fork()
        if pid == 0:  # the judgement process
            os.close(rfd)
            _gsvc_child.run_child(job, wfd)
        os.close(wfd)
        self.running_pid = pid
        buf = bytearray()
        cap = self.s.max_result_bytes + self.s.max_cache_transfer_bytes + 64 * 1024
        exited = self.loop.create_future()

        def on_data() -> None:
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
                if len(buf) < cap:
                    buf.extend(chunk[:cap - len(buf)])

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
            _kill_group(pid)  # anything the judgement left running
            on_data()
            self.loop.remove_reader(rfd)
            os.close(rfd)
        self.metrics.peak_rss_bytes = rusage.ru_maxrss * 1024
        if timed_out:
            return _rejected("deadline", "the deadline passed while judging")
        if os.WIFSIGNALED(status) or os.WEXITSTATUS(status) != 0:
            return _rejected("incomplete", "the judgement process ended without "
                             "a complete result")
        got = decode_frames(bytes(buf), max_result=self.s.max_result_bytes,
                            max_entries=self.s.max_cache_transfer_bytes)
        if got.result is None or not got.ended:
            return _rejected("incomplete", "the judgement process ended without "
                             "a complete result")
        return self._absorb(got)

    def _absorb(self, got) -> Response:
        res = got.result
        for key, value in got.entries:
            self.store.add(key, value)
        self.store.touch(tuple(k) for k in res.get("hit_keys", [])
                         if isinstance(k, list) and len(k) == 2)
        if isinstance(res.get("base_blobs"), list):
            self.store.set_pinned(b for b in res["base_blobs"] if isinstance(b, str))
        self.store.evict()
        outcome = res.get("outcome")
        common = {"stderr_tail": res.get("stderr_tail", ""),
                  "validated": res.get("validated"), "cache": res.get("cache")}
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
        if self.running_pid is not None:
            _kill_group(self.running_pid)


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
