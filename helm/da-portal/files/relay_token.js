// relay_token.js — da-portal machine-identity relay token injector
// (ADR-027 D2-B O1, GHSA-3g2h-rf85-5rrv hardening series).
//
// njs handler for `js_set $relay_auth`: returns the Authorization header
// value for the /api/v1 reverse proxy to tenant-api, read from the
// kubelet-projected, audience-bound ServiceAccount token.
//
// Per-request read is the whole design: js_set evaluates lazily on each
// request, and the kubelet rotates the projected file (at ~80% of its 900s
// TTL) via an atomic symlink swap — so every request gets a fresh token with
// no reload hook and no staleness window. The read hits a tmpfs-backed
// projected volume; the cost is negligible for an admin UI.
//
// NOTE: a synchronous file read (fs.readFileSync) is generally an
// ANTI-PATTERN in nginx/njs — it blocks the worker's event loop, so any
// disk-I/O latency stalls every in-flight request on that worker. It is safe
// here EXCLUSIVELY because (a) js_set variable handlers must return
// synchronously (async I/O is not available in this hook), (b) the projected
// token volume is tmpfs-backed (an in-memory read, µs-scale), and (c) this
// is a low-QPS administrative portal, not a data plane. Do NOT copy this
// pattern for high-throughput or real-disk I/O.
//
// Fail-soft by design: outside Kubernetes (docker compose / try-local) the
// token file does not exist — return "" so nginx DROPS the Authorization
// header (proxy_set_header with an empty value omits the header) instead of
// failing the request. The token only feeds tenant-api's audit-only machine
// identity; it never gates the user's authorization (that stays on the
// oauth2-proxy X-Forwarded-* identity), so a missing token must never break
// the human plane.
//
// ⚠ Dual-copy contract: helm/da-portal/files/relay_token.js MUST stay
// byte-identical to this file (the helm configmap volume hides the image's
// /etc/nginx/conf.d). tests/helm/test_portal_relay_token_guard.py pins it.

import fs from 'fs';

// House path convention for audience-bound relay tokens (mirrors
// recipe-preview's PREVIEW_AUTH_TOKEN_FILE and threshold-govern's mount).
const TOKEN_PATH = '/var/run/secrets/tokens/tenant-api-token';

function relayAuth(r) {
    try {
        const tok = fs.readFileSync(TOKEN_PATH, 'utf8').trim();
        return tok ? 'Bearer ' + tok : '';
    } catch (e) {
        // ENOENT is the normal non-k8s path (no token volume) — stay silent.
        // Anything else (EACCES from a securityContext override, EISDIR, …)
        // is a misconfiguration: log it, or the drop would be
        // indistinguishable from the designed compose fallback (tenant-api's
        // audit only ever sees no_token either way).
        if (e.code !== 'ENOENT') {
            r.error('relay token unreadable: ' + e.message);
        }
        return '';
    }
}

export default { relayAuth };
