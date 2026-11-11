#!/usr/bin/env python3
"""recipe-preview service — would-fire preview on a pod-internal Unix socket.

A small stdlib HTTP service over the would-fire eval core (`_recipe_preview`):
`POST /v1/preview` answers "firing / inactive / error" for ONE recipe + a
scenario value, by going through the SAME compiler + `promtool` the platform
uses — never re-implementing eval.

Placement and trust (ADR-027 §5): it runs as a container in the tenant-api pod
and listens only on a Unix socket in a volume the two containers share, like
the generator container (ADR-036 §3). The socket is 0600 and every connection's
peer uid (SO_PEERCRED) must be this process's own — tenant-api runs as the
same uid — so nothing else in the pod or on the network can ask. tenant-api
authorizes the caller before it calls; this service reads no identity, asks
nobody about access and has no network port.

What it still owns is the computation: input validation, a per-tenant rate
limit, a bounded number of concurrent evaluations, and the caller's deadline.
Every request carries `deadline_ms` (time left on tenant-api's side). The wait
for an evaluation slot is min(QUEUE_TIMEOUT, time left); the evaluation gets
the absolute deadline and bounds each da-guard / promtool run by it; and a
verdict that arrives at or after the deadline is answered 503 `deadline`, not
sent as a verdict — tenant-api has stopped waiting, and a run cut short reads
as `error`.
"""
import json
import os
import socket
import socketserver
import stat
import struct
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler

# ── import the eval core (+ its compiler) ────────────────────────────────
# In the image the core is copied to ./core/dx (with its ../lint sibling, which
# compile_custom_alerts resolves relatively); in the repo it lives at
# scripts/tools/dx. Resolve either (PREVIEW_CORE_DIR wins).
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (
    os.environ.get("PREVIEW_CORE_DIR"),
    os.path.join(_HERE, "core", "dx"),                             # image layout
    os.path.join(_HERE, "..", "..", "scripts", "tools", "dx"),     # repo layout
):
    if _cand and os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
import _recipe_preview as core  # noqa: E402

# ── config (env) ─────────────────────────────────────────────────────────
SOCKET_PATH = os.environ.get("PREVIEW_SOCKET", "")
MAX_CONCURRENCY = int(os.environ.get("PREVIEW_MAX_CONCURRENCY", "4"))
QUEUE_TIMEOUT = float(os.environ.get("PREVIEW_QUEUE_TIMEOUT", "10"))
RATE_LIMIT_PER_MIN = int(os.environ.get("PREVIEW_RATE_LIMIT_PER_MIN", "30"))
MAX_BODY_BYTES = int(os.environ.get("PREVIEW_MAX_BODY_BYTES", str(64 * 1024)))
REQUEST_TIMEOUT = float(os.environ.get("PREVIEW_REQUEST_TIMEOUT", "60"))

#: The largest deadline a caller may give (ms). tenant-api's own per-request
#: deadline is far below it; the cap only stops a malformed value from
#: holding an evaluation slot for hours.
MAX_DEADLINE_MS = 10 * 60 * 1000

_eval_slots = threading.BoundedSemaphore(max(1, MAX_CONCURRENCY))


class RateLimiter:
    """Per-key sliding-window limiter (in-memory; correct for a single replica).

    A horizontally-scaled deployment would need a shared store: with N replicas
    behind a load balancer the effective per-tenant ceiling becomes
    RATE_LIMIT_PER_MIN * N (and is non-deterministic per request). That's an
    accepted MVP trade-off — this is a blast-radius guard, not billing — but a
    Platform Engineer scaling it out must know. try-local-first / low-QPS; bounds
    the per-tenant abuse surface (§6) without a new dependency.
    """

    def __init__(self, per_min, max_keys=10000):
        self._per_min = per_min
        self._max_keys = max_keys
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, now):
        if self._per_min <= 0:
            return True
        with self._lock:
            cutoff = now - 60.0
            # Bound the key map: a NEW key past the cap triggers a sweep of keys
            # whose window has fully expired (amortized GC — only when large). So
            # the map is bounded by tenants ACTIVE in the last 60s, not by the
            # number of distinct tenant strings an authorized caller has sprayed.
            if key not in self._hits and len(self._hits) >= self._max_keys:
                for k in [k for k, d in self._hits.items() if not d or d[-1] < cutoff]:
                    del self._hits[k]
            dq = self._hits[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= self._per_min:
                return False
            dq.append(now)
            return True


_rate = RateLimiter(RATE_LIMIT_PER_MIN)


def _safe_stderr(msg):
    """Emit a diagnostic line to stderr, swallowing ANY failure: a non-UTF-8
    stderr with a non-ASCII tenant, or a broken stderr pipe, must never crash
    the fail-safe 500 handler. Diagnostics are best-effort."""
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


# ── request logic (testable seam: inject evaluator / clock) ─────────────
def _deadline_ms(body):
    raw = body.get("deadline_ms")
    # bool is an int subclass: reject it explicitly.
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw <= 0 or raw > MAX_DEADLINE_MS:
        return None
    return raw


def handle_preview(body, *, evaluator=core.preview_recipe, now=None, clock=time.monotonic):
    """Pure request logic → (http_status:int, response:dict). Order:
    validate → rate-limit → bounded wait for a slot → evaluation under the
    caller's deadline. `now` keys the rate limiter (tests pin it); `clock`
    measures the deadline."""
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}
    recipe = body.get("recipe")
    tenant = body.get("tenant")
    # NB: get-with-default, NOT `or {}` — `or {}` would coerce a falsy non-dict
    # (0 / "" / False / []) to {} and slip it past the type check below.
    scenario = body.get("scenario", {})
    if not isinstance(recipe, dict) or not recipe:
        return 400, {"error": "`recipe` (object) is required"}
    if not isinstance(tenant, str) or not tenant:
        return 400, {"error": "`tenant` (string) is required"}
    if len(tenant) > 253:
        return 400, {"error": "`tenant` is too long"}
    if not isinstance(scenario, dict):
        return 400, {"error": "`scenario` must be an object"}
    budget_ms = _deadline_ms(body)
    if budget_ms is None:
        return 400, {"error": f"`deadline_ms` (integer, 1..{MAX_DEADLINE_MS}) is required"}

    start = clock()
    deadline = start + budget_ms / 1000.0

    # per-tenant rate limit — a blast-radius guard on the evaluation.
    if not _rate.allow(tenant, time.monotonic() if now is None else now):
        return 429, {"error": "rate limit exceeded for this tenant"}

    # bounded concurrency around the (promtool subprocess) eval; the wait
    # never outlives the caller.
    left = deadline - clock()
    wait = min(QUEUE_TIMEOUT, max(0.0, left))
    if not _eval_slots.acquire(timeout=wait):
        if QUEUE_TIMEOUT < left:
            return 503, {"error": "preview is busy; retry shortly", "reason": "busy"}
        return 503, {"error": "the caller's deadline passed while waiting for an evaluation slot",
                     "reason": "deadline"}
    try:
        result = evaluator(recipe, tenant, scenario, deadline=deadline)
    finally:
        _eval_slots.release()
    if clock() >= deadline:
        return 503, {"error": "the evaluation did not finish before the caller's deadline",
                     "reason": "deadline"}
    return 200, result


# ── HTTP layer (thin wrapper over handle_preview) ────────────────────────
def parse_content_length(raw_header):
    """Validate a Content-Length header BEFORE reading the body.

    Returns ((status, body) | None, length). A NEGATIVE length is rejected as
    400 — it must never reach rfile.read(), where a negative size reads to EOF
    and would bypass the MAX_BODY_BYTES cap (a memory-exhaustion vector).
    """
    try:
        length = int(raw_header or 0)
    except (TypeError, ValueError):
        return (400, {"error": "invalid Content-Length"}), 0
    if length < 0:
        return (400, {"error": "invalid Content-Length"}), 0
    if length > MAX_BODY_BYTES:
        return (413, {"error": "request body too large"}), 0
    return None, length


# struct ucred: pid_t (signed), uid_t and gid_t (unsigned).
_UCRED = struct.Struct("iII")


def peer_uid(sock):
    """The connecting process's uid (SO_PEERCRED), None when unreadable.
    A seam: tests replace it rather than switch uid."""
    try:
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _UCRED.size)
    except (OSError, AttributeError):
        return None
    return _UCRED.unpack(raw)[1]


class _Handler(BaseHTTPRequestHandler):
    server_version = "recipe-preview/2"
    # Bound per-connection socket reads so an idle or slow-drip peer can't pin
    # a thread indefinitely.
    timeout = REQUEST_TIMEOUT

    def address_string(self):
        return "unix"  # a Unix socket peer has no address

    def _send(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/v1/status":
            self._send(200, {"status": "ok", "promtool": _PROMTOOL_VERSION,
                             "git_sha": _GIT_SHA})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/v1/preview":
            self._send(404, {"error": "not found"})
            return
        # validate Content-Length (reject non-numeric, negative, and oversized)
        # BEFORE reading the body into memory.
        err, length = parse_content_length(self.headers.get("Content-Length"))
        if err:
            self._send(*err)
            return
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "request body must be valid JSON"})
            return
        try:
            status, resp = handle_preview(body)
        except Exception:  # never leak a traceback to the client; fail safe
            # tenant + path ONLY — never the body (carries the recipe). Both are
            # client-controlled → `!r`-quote to neutralise control/ANSI chars.
            try:
                t = body.get("tenant") if isinstance(body, dict) else None
                sys.stderr.write(f"preview 500 on {self.path!r} for tenant {t!r}\n")
                traceback.print_exc(file=sys.stderr)
            except Exception:
                pass
            status, resp = 500, {"error": "internal error"}
        self._send(status, resp)

    def log_message(self, fmt, *args):
        _safe_stderr("preview - %s" % (fmt % args))


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path):
        self.peer_rejected = 0
        self.own_uid = os.getuid()
        old = os.umask(0o177)  # the socket is created 0600
        try:
            super().__init__(path, _Handler)
        finally:
            os.umask(old)

    def verify_request(self, request, client_address):
        # The trust boundary: only a process running as this uid (tenant-api)
        # may talk to the service. Anything else is closed unanswered.
        if peer_uid(request) != self.own_uid:
            self.peer_rejected += 1
            return False
        return True

    def server_close(self):
        super().server_close()
        try:
            os.unlink(self.server_address)
        except OSError:
            pass


def make_server(path):
    """A server bound to the Unix socket *path*. A stale socket there is
    replaced; anything else at the path is refused, never removed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None:
        if not stat.S_ISSOCK(st.st_mode):
            raise RuntimeError(f"PREVIEW_SOCKET {path!r} exists and is not a socket; refusing to replace it")
        os.unlink(path)
    return _Server(path)


def _detect_promtool_version():
    import shutil
    import subprocess
    p = shutil.which("promtool")
    if not p:
        return "absent"
    try:
        out = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace")
        return (out.stderr or out.stdout).strip().splitlines()[0] if (out.stderr or out.stdout) else "unknown"
    except Exception:
        return "unknown"


_PROMTOOL_VERSION = _detect_promtool_version()

# Build provenance — the release/CI build stamps the image's GIT_SHA (Dockerfile
# ARG→ENV); /healthz echoes it so an operator can confirm WHICH commit's bundled
# compiler snapshot is running (drift observability, design §6 #5). "unknown" for
# local/un-stamped builds.
_GIT_SHA = os.environ.get("GIT_SHA", "unknown")


def main():
    if not SOCKET_PATH:
        sys.exit("recipe-preview: PREVIEW_SOCKET is required (the Unix socket shared with tenant-api)")
    # The promtool version is recorded at startup — the firing/inactive verdict
    # contract is version-bound.
    sys.stderr.write(
        f"recipe-preview listening on {SOCKET_PATH} "
        f"(promtool: {_PROMTOOL_VERSION}, git-sha: {_GIT_SHA}, "
        f"max-concurrency: {MAX_CONCURRENCY}, queue-timeout: {QUEUE_TIMEOUT}s, "
        f"rate/min: {RATE_LIMIT_PER_MIN})\n"
    )
    make_server(SOCKET_PATH).serve_forever()


if __name__ == "__main__":
    main()
