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

_IDENTITY_HEADERS = ("X-Forwarded-Email", "X-Forwarded-Groups")

# ── §6 guardrails ────────────────────────────────────────────────────────
_eval_slots = threading.BoundedSemaphore(MAX_CONCURRENCY)


class RateLimiter:
    """Per-key sliding-window limiter (in-memory; correct for a single replica).

    A horizontally-scaled deployment would need a shared store, but the preview
    service is try-local-first / low-QPS; this bounds the per-tenant abuse
    surface (§6) without a new dependency.
    """

    def __init__(self, per_min):
        self._per_min = per_min
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, now):
        if self._per_min <= 0:
            return True
        with self._lock:
            dq = self._hits[key]
            cutoff = now - 60.0
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
    validate → rate-limit → identity → authorize → bounded eval. Deps are
    injectable so the logic is unit-testable without HTTP / network / promtool.
    """
    now = time.monotonic() if now is None else now
    if not isinstance(body, dict):
        return 400, {"error": "request body must be a JSON object"}
    recipe = body.get("recipe")
    tenant = body.get("tenant")
    scenario = body.get("scenario") or {}
    if not isinstance(recipe, dict) or not recipe:
        return 400, {"error": "`recipe` (object) is required"}
    if not isinstance(tenant, str) or not tenant:
        return 400, {"error": "`tenant` (string) is required"}
    if not isinstance(scenario, dict):
        return 400, {"error": "`scenario` must be an object"}

    # per-tenant rate limit (§6) — keyed by the requested tenant
    if not _rate.allow(tenant, now):
        return 429, {"error": "rate limit exceeded for this tenant"}

    # identity + tenant-isolation: fail closed if we can't authorize
    hdrs = _apply_dev_bypass(headers)
    if not hdrs.get("X-Forwarded-Email"):
        return 401, {"error": "missing identity: X-Forwarded-Email required"}
    if not authorizer(hdrs, tenant):
        return 403, {"error": f"not authorized to preview tenant {tenant!r}"}

    # bounded concurrency around the (promtool subprocess) eval (§6)
    if not _eval_slots.acquire(timeout=QUEUE_TIMEOUT):
        return 503, {"error": "preview is busy; retry shortly"}
    try:
        return 200, evaluator(recipe, tenant, scenario)
    finally:
        _eval_slots.release()


# ── HTTP layer (thin wrapper over handle_preview) ────────────────────────
class _Handler(BaseHTTPRequestHandler):
    server_version = "recipe-preview/1.0"

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
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "request body must be valid JSON"})
            return
        status, resp = handle_preview(body, self.headers)
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
