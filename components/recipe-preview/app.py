#!/usr/bin/env python3
"""recipe-preview service — would-fire preview HTTP facade (#657 PR 2).

A small stdlib HTTP service that wires the portal's recipe form to the
would-fire eval core (`_recipe_preview`): `POST /preview` answers "firing /
inactive / error" for ONE recipe + a scenario value, by going through the SAME
compiler + `promtool` the platform uses — never re-implementing eval.

Security model — this service is a PEP (policy enforcement point); it does NOT
decide tenant access itself. It forwards the caller's identity to tenant-api's
read-probe (`GET /api/v1/tenants/{id}/access`, #876) and treats 403 / any
non-200 / unreachable as DENY (fail-closed). The RBAC decision stays in
tenant-api — one authority, no cross-language drift.

Trust boundary: like tenant-api, this service trusts `X-Forwarded-*` identity
headers, so it MUST sit behind the auth proxy (oauth2-proxy) that strips
client-supplied headers and injects authenticated ones, with a NetworkPolicy
restricting ingress to that proxy. In try-local, dev-bypass (ADR-022) injects a
demo identity instead. Forwarding arbitrary client headers would be a confused-
deputy hole — hence only the two identity headers are ever forwarded.
"""
import http.client
import json
import os
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
TENANT_API_URL = os.environ.get("PREVIEW_TENANT_API_URL", "http://tenant-api.tenant-api.svc.cluster.local:8080").rstrip("/")
AUTHZ_TIMEOUT = float(os.environ.get("PREVIEW_AUTHZ_TIMEOUT", "5"))
MAX_CONCURRENCY = int(os.environ.get("PREVIEW_MAX_CONCURRENCY", "4"))
QUEUE_TIMEOUT = float(os.environ.get("PREVIEW_QUEUE_TIMEOUT", "10"))
RATE_LIMIT_PER_MIN = int(os.environ.get("PREVIEW_RATE_LIMIT_PER_MIN", "30"))
LISTEN_HOST = os.environ.get("PREVIEW_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PREVIEW_LISTEN_PORT", "8082"))
DEV_BYPASS = os.environ.get("PREVIEW_DEV_BYPASS_AUTH", "").lower() in ("1", "true", "yes")
DEV_BYPASS_EMAIL = os.environ.get("PREVIEW_DEV_BYPASS_EMAIL", "dev@local")
DEV_BYPASS_GROUPS = os.environ.get("PREVIEW_DEV_BYPASS_GROUPS", "demo-admins")
MAX_BODY_BYTES = int(os.environ.get("PREVIEW_MAX_BODY_BYTES", str(64 * 1024)))
REQUEST_TIMEOUT = float(os.environ.get("PREVIEW_REQUEST_TIMEOUT", "60"))
# Audience-bound projected SA token (aud=tenant-api) mounted by the Helm chart at
# /var/run/secrets/tokens/tenant-api-token (#962 b2). Sent as a Bearer to
# tenant-api's machine-identity AUDIT only. Unset (e.g. try-local, no K8s) → no
# Bearer, which the fail-open read below handles.
AUTH_TOKEN_FILE = os.environ.get("PREVIEW_AUTH_TOKEN_FILE", "")

# ADR-022-style containment: dev-bypass is a LOCAL-DEV escape hatch. Refuse to
# run it inside Kubernetes — where a real oauth2-proxy must front the service —
# so a direct-to-pod caller can never be auto-injected the demo admin identity.
if DEV_BYPASS and os.environ.get("KUBERNETES_SERVICE_HOST"):
    raise SystemExit("PREVIEW_DEV_BYPASS_AUTH must not be enabled inside Kubernetes")

_IDENTITY_HEADERS = ("X-Forwarded-Email", "X-Forwarded-Groups")

# ── §6 guardrails ────────────────────────────────────────────────────────
_eval_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


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


# ── PEP: delegate the tenant-access decision to tenant-api (#876) ────────
def _safe_stderr(msg):
    """Emit a diagnostic line to stderr, swallowing ANY failure. A logging call must
    never alter control flow: a non-UTF-8 stderr (e.g. cp950) with a non-ASCII tenant,
    or a closed/broken stderr pipe, must not turn a fail-closed deny into a raised
    exception, nor crash the fail-safe 500 handler. Diagnostics are best-effort."""
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass


def _apply_dev_bypass(headers):
    """try-local only: inject a demo identity when none is present (mirrors
    tenant-api ADR-022). No-op in prod, where oauth2-proxy injects the real
    identity. Never OVERRIDES a present header."""
    if not DEV_BYPASS or headers.get("X-Forwarded-Email"):
        return headers
    out = dict(headers)
    out["X-Forwarded-Email"] = DEV_BYPASS_EMAIL
    out["X-Forwarded-Groups"] = DEV_BYPASS_GROUPS
    return out


def _read_auth_token():
    """Bearer for tenant-api's machine-identity audit (ADR-027 / #962 b2). The
    kubelet-rotated projected SA token file is read at call time. FAIL-OPEN: the
    token only feeds tenant-api's AUDIT (never gates authz — that rides the
    /access probe below), so a read failure degrades to no Bearer with a warning,
    never blocks a preview. Returns "" when unset/unreadable."""
    if not AUTH_TOKEN_FILE:
        return ""
    try:
        with open(AUTH_TOKEN_FILE, encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, UnicodeDecodeError) as exc:
        # OSError = I/O failure; UnicodeDecodeError = a non-UTF-8/corrupt file
        # (NOT an OSError subclass). Both degrade to no Bearer — the fail-open
        # contract is "a token problem never blocks a preview", so a malformed
        # token file must not crash this request-path PEP. Log the exception CLASS
        # only — NOT str(exc): a UnicodeDecodeError's message echoes a raw byte of
        # the token file's content, and the token is a secret.
        _safe_stderr(f"warning: could not read PREVIEW_AUTH_TOKEN_FILE {AUTH_TOKEN_FILE!r} "
                     f"({type(exc).__name__}); continuing without a Bearer "
                     f"(tenant-api audit records no_token)")
        return ""


def authorize_tenant(headers, tenant):
    """Forward the caller's identity to tenant-api `GET /tenants/{id}/access`.

    200 → allow; 401/403/any-other/unreachable/timeout → DENY (fail-closed).
    Only the two gateway-injected identity headers are forwarded — never
    arbitrary client headers (confused-deputy guard). The machine-identity Bearer
    (audience-bound SA token) is attached ALONGSIDE the identity headers when
    mounted; it feeds tenant-api's AUDIT only and is orthogonal to this authz
    decision — a missing/unreadable token never flips the result.
    """
    fwd = {h: headers[h] for h in _IDENTITY_HEADERS if headers.get(h)}
    token = _read_auth_token()
    if token:
        fwd["Authorization"] = "Bearer " + token
    url = f"{TENANT_API_URL}/api/v1/tenants/{urllib.parse.quote(tenant, safe='')}/access"
    req = urllib.request.Request(url, headers=fwd, method="GET")
    # Fail-closed on every error. We log only the ANOMALOUS cases — an operator
    # debugging "why is every preview a 403?" needs to tell an authz-backend problem
    # apart from a genuine deny. A genuine 401/403 deny is EXPECTED and already shows
    # as a 403 in the access log, so we do NOT re-log it (that would be redundant and
    # would let a deny-spamming caller flood the log). All log lines carry tenant id
    # + host + status/class ONLY — NEVER the forwarded identity headers or the Bearer
    # token; tenant is `!r`-quoted so a crafted value can't inject a log line; and every
    # emission goes through _safe_stderr so a logging failure (non-UTF-8 stderr, broken
    # pipe) can never turn a fail-closed deny into a raised exception.
    try:
        with urllib.request.urlopen(req, timeout=AUTHZ_TIMEOUT) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        # tenant-api answered. Its /access contract is 200 (allow) or 401/403 (deny).
        # A 401/403 is a genuine deny — stay silent (the access log already has the
        # 403). ANY OTHER status is anomalous and worth surfacing: 5xx = a tenant-api
        # fault, and 4xx like 404/405/400 = a MISCONFIGURED tenant-api URL/route — a
        # real "why is every preview a 403?" cause that a bare `>= 500` would miss.
        if exc.code not in (401, 403):
            _safe_stderr(f"preview authz: tenant-api returned unexpected HTTP {exc.code} "
                         f"at {TENANT_API_URL}; failing closed for tenant {tenant!r}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        # the probe did not complete: unreachable / DNS / timeout / TLS handshake
        # (SSLError ⊂ OSError) / a malformed or truncated HTTP response (BadStatusLine,
        # IncompleteRead — http.client.HTTPException is NOT an OSError, so it must be
        # listed explicitly or it would fall through to the "bug" branch below and be
        # mislabeled a recipe-preview bug). All are tenant-api-side / network faults,
        # not our bug. Log the exception CLASS + host (never str(exc), which can echo a
        # URL); the class name distinguishes DNS vs TLS vs malformed-response.
        _safe_stderr(f"preview authz: probe to tenant-api at {TENANT_API_URL} failed "
                     f"({type(exc).__name__}); failing closed for tenant {tenant!r}")
        return False
    except Exception as exc:
        # An UNEXPECTED error — most likely a bug in THIS code path, not the network
        # (network / HTTP faults are caught above). Still fail closed; log a neutral
        # message + traceback so it is never misdiagnosed as a tenant-api outage.
        _safe_stderr(f"preview authz: unexpected error for tenant {tenant!r} "
                     f"({type(exc).__name__}); failing closed")
        try:
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        return False


# ── request logic (testable seam: inject authorizer / evaluator) ─────────
def handle_preview(body, headers, *, authorizer=authorize_tenant,
                   evaluator=core.preview_recipe, now=None):
    """Pure request logic → (http_status:int, response:dict). Order matters:
    validate → identity → authorize → rate-limit → bounded eval. Deps are
    injectable so the logic is unit-testable without HTTP / network / promtool.
    """
    now = time.monotonic() if now is None else now
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

    # Identity + tenant-isolation FIRST, fail closed. Authorizing BEFORE the
    # rate-limit/eval is deliberate: an unauthenticated/unauthorized caller must
    # not consume a victim tenant's rate-limit budget or grow the limiter's key
    # map (both keyed by the requested tenant). So only authorized requests ever
    # reach — or key — the limiter.
    hdrs = _apply_dev_bypass(headers)
    if not hdrs.get("X-Forwarded-Email"):
        return 401, {"error": "missing identity: X-Forwarded-Email required"}
    if not authorizer(hdrs, tenant):
        return 403, {"error": f"not authorized to preview tenant {tenant!r}"}

    # per-tenant rate limit (§6) — reached only AFTER authz, so an unauthorized
    # caller can't key (or exhaust) it; the limiter itself GCs expired keys.
    if not _rate.allow(tenant, now):
        return 429, {"error": "rate limit exceeded for this tenant"}

    # bounded concurrency around the (promtool subprocess) eval (§6)
    if not _eval_slots.acquire(timeout=QUEUE_TIMEOUT):
        return 503, {"error": "preview is busy; retry shortly"}
    try:
        return 200, evaluator(recipe, tenant, scenario)
    finally:
        _eval_slots.release()


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


class _Handler(BaseHTTPRequestHandler):
    server_version = "recipe-preview/1.0"
    # Bound per-connection socket reads so an idle / abandoned / slow-drip client
    # can't pin a ThreadingHTTPServer thread indefinitely (thread-leak guard).
    # NOT a full Slowloris defense — that's the auth proxy + a future production
    # WSGI server (design §9); this just caps dead-connection thread leakage.
    timeout = REQUEST_TIMEOUT

    def _send(self, status, obj):
        payload = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/healthz":
            self._send(200, {"status": "ok", "promtool": _PROMTOOL_VERSION,
                             "git_sha": _GIT_SHA})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/preview":
            self._send(404, {"error": "not found"})
            return
        # recipes are tiny; validate Content-Length (reject non-numeric, negative,
        # and oversized) BEFORE reading the body into memory — a pre-auth guard.
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
            status, resp = handle_preview(body, self.headers)
        except Exception:  # never leak a traceback to the client; fail safe
            # Prefix the server-side trace with request context so a 500 is actionable.
            # tenant + path ONLY — never the body (carries the recipe) or the identity
            # headers; both are client-controlled → `!r`-quote to neutralise control/
            # ANSI chars (log-injection defense; tenant has no charset guard). The whole
            # emission is wrapped so a stderr encoding error / broken pipe can NEVER
            # prevent the clean 500 response below (the fail-safe must not self-crash).
            try:
                t = body.get("tenant") if isinstance(body, dict) else None
                sys.stderr.write(f"preview 500 on {self.path!r} for tenant {t!r}\n")
                traceback.print_exc(file=sys.stderr)
            except Exception:
                pass
            status, resp = 500, {"error": "internal error"}
        self._send(status, resp)

    def log_message(self, fmt, *args):  # quieter, structured-ish access log
        sys.stderr.write("preview %s - %s\n" % (self.address_string(), fmt % args))


def _detect_promtool_version():
    import shutil
    import subprocess
    p = shutil.which("promtool")
    if not p:
        return "absent"
    try:
        out = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=10)
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
    # §6 #5: record the promtool version at startup — the firing/inactive
    # verdict contract is version-bound (baseline 3.12.0).
    sys.stderr.write(
        f"recipe-preview listening on {LISTEN_HOST}:{LISTEN_PORT} "
        f"(promtool: {_PROMTOOL_VERSION}, git-sha: {_GIT_SHA}, "
        f"tenant-api: {TENANT_API_URL}, "
        f"dev-bypass: {DEV_BYPASS}, max-concurrency: {MAX_CONCURRENCY}, "
        f"rate/min: {RATE_LIMIT_PER_MIN})\n"
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()
