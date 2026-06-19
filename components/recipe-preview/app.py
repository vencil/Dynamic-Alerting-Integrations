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
TENANT_API_URL = os.environ.get("PREVIEW_TENANT_API_URL", "http://tenant-api:8080").rstrip("/")
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


def authorize_tenant(headers, tenant):
    """Forward the caller's identity to tenant-api `GET /tenants/{id}/access`.

    200 → allow; 401/403/any-other/unreachable/timeout → DENY (fail-closed).
    Only the two gateway-injected identity headers are forwarded — never
    arbitrary client headers (confused-deputy guard).
    """
    fwd = {h: headers[h] for h in _IDENTITY_HEADERS if headers.get(h)}
    url = f"{TENANT_API_URL}/api/v1/tenants/{urllib.parse.quote(tenant, safe='')}/access"
    req = urllib.request.Request(url, headers=fwd, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=AUTHZ_TIMEOUT) as resp:
            return resp.status == 200
    except urllib.error.HTTPError:
        return False   # 401 / 403 / 4xx / 5xx → deny
    except Exception:
        return False   # unreachable / timeout → fail-closed deny


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
            self._send(200, {"status": "ok", "promtool": _PROMTOOL_VERSION})
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
            traceback.print_exc(file=sys.stderr)
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


def main():
    # §6 #5: record the promtool version at startup — the firing/inactive
    # verdict contract is version-bound (baseline 2.53.2).
    sys.stderr.write(
        f"recipe-preview listening on {LISTEN_HOST}:{LISTEN_PORT} "
        f"(promtool: {_PROMTOOL_VERSION}, tenant-api: {TENANT_API_URL}, "
        f"dev-bypass: {DEV_BYPASS}, max-concurrency: {MAX_CONCURRENCY}, "
        f"rate/min: {RATE_LIMIT_PER_MIN})\n"
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler).serve_forever()


if __name__ == "__main__":
    main()
