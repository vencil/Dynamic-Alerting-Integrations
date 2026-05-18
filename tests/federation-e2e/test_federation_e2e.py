"""Federation E2E scenarios — ADR-020 IV-2j (#516).

Each test walks a real request through the gateway -> proxy -> storage
chain. See README for the design and the fidelity boundary.

Tenant assignment is deliberate: the data scenarios (S1/S2/S7/S8) share
the `db-a` bucket and stay far under the rendered 30/min per-tenant rate
limit; the rate-limit / payload / revocation scenarios use their own
throwaway tenants so they cannot deplete each other's bucket.
"""
from helpers import (RENDERED, assert_eventually, expect_positive,
                     expect_status, fresh_rsa_pem, gateway_request,
                     mtail_counter, query, result_series, sign_token,
                     tenants_in)


def test_s1_happy_path(gateway_url, signer):
    """S1 — a signed token walks the full chain: gateway verifies the
    RS256 JWT, proxy injects {tenant="db-a"}, storage answers."""
    _, token = signer("db-a")
    resp = query(gateway_url, token, "process_open_fds")
    assert resp.status_code == 200, resp.text
    assert result_series(resp), "expected db-a series in the result"
    assert tenants_in(resp) == {"db-a"}


def test_s2_cross_tenant_isolation(gateway_url, signer):
    """S2 — a db-a token cannot reach db-b data by any route: an explicit
    {tenant="db-b"} selector, a bare selector, or the metadata APIs.
    (Folds in #512's metadata-leak coverage.)"""
    _, token = signer("db-a")

    # An explicit cross-tenant selector is forced back to db-a by the
    # proxy's label injection — zero db-b leak.
    resp = query(gateway_url, token, 'process_open_fds{tenant="db-b"}')
    assert resp.status_code == 200, resp.text
    assert "db-b" not in tenants_in(resp)

    # A bare selector returns ONLY db-a series.
    resp2 = query(gateway_url, token, "http_requests_total")
    assert resp2.status_code == 200, resp2.text
    assert tenants_in(resp2) == {"db-a"}

    # Metadata API /series must not leak db-b topology.
    series = gateway_request(gateway_url, "/api/v1/series", token=token,
                             params={"match[]": "http_requests_total"})
    assert series.status_code == 200, series.text
    for metric in series.json()["data"]:
        assert metric.get("tenant") == "db-a", metric

    # Metadata API /label/tenant/values must show only db-a.
    values = gateway_request(gateway_url, "/api/v1/label/tenant/values",
                             token=token)
    assert values.status_code == 200, values.text
    assert set(values.json()["data"]) <= {"db-a"}, values.json()


def test_s3_jwt_enforcement(gateway_url, signer):
    """S3 — the gateway rejects a missing token, a forged signature, a
    wrong issuer, and an expired token, all with 401 (before any tenant
    wiring)."""
    params = {"query": "process_open_fds"}

    # No Authorization header.
    no_token = gateway_request(gateway_url, "/api/v1/query", params=params)
    assert no_token.status_code == 401, no_token.status_code

    # Forged signature — signed with a fresh key NOT in the gateway JWKS,
    # but carrying the real kid so jwt_authn selects the right key and
    # the signature check is what fails.
    _, forged = sign_token("db-a", private_key_pem=fresh_rsa_pem(),
                           kid=signer.kid)
    bad_sig = gateway_request(gateway_url, "/api/v1/query", token=forged,
                              params=params)
    assert bad_sig.status_code == 401, bad_sig.status_code

    # Wrong issuer — correctly signed, but iss != tenant-api.
    _, wrong_iss = signer("db-a", iss="evil-issuer")
    bad_iss = gateway_request(gateway_url, "/api/v1/query", token=wrong_iss,
                              params=params)
    assert bad_iss.status_code == 401, bad_iss.status_code

    # Expired token — exp is an hour in the past, far beyond the 60s
    # jwt_authn clock-skew leeway (jwt.clockSkewSeconds), so the verdict
    # is unambiguous. The short token TTL is the security backstop —
    # there is no server-side allowlist — so exp enforcement is asserted
    # explicitly; an over-wide clock skew would also surface here.
    _, expired = signer("db-a", ttl_seconds=-3600)
    expired_resp = gateway_request(gateway_url, "/api/v1/query",
                                   token=expired, params=params)
    assert expired_resp.status_code == 401, expired_resp.status_code


def test_s4_revocation_propagation(gateway_url, signer):
    """S4 — revoking a token (writing its id to the revoked set) makes
    the gateway reject it after the Lua's reload interval. Tests the
    gateway revocation LOGIC; the kubelet projected-volume swap is out of
    scope (README §fidelity boundary)."""
    token_id, token = signer("s4-revoke")
    revoked_file = RENDERED / "revoked.txt"

    # The token works before revocation.
    expect_status(query(gateway_url, token, "process_open_fds"), 200)

    try:
        # Revoke: rewrite the bind-mounted revoked.txt in place (same
        # inode, so the gateway container sees it). The Lua re-reads on
        # its reload gate (rendered to 2s for the E2E). newline="\n" is
        # pinned so a Windows-host driver writes a Unix revoked set (no
        # CR), matching what tenant-api writes in production.
        revoked_file.write_text(token_id + "\n", newline="\n")

        # After the reload interval the gateway rejects it. Poll slowly
        # (1s) so this probe does not itself deplete the rate limiter.
        assert_eventually(
            lambda: expect_status(
                query(gateway_url, token, "process_open_fds"), 403),
            timeout=12.0, interval=1.0,
            desc="revoked token rejected with 403")
    finally:
        # Reset so a stale id cannot affect a re-run.
        revoked_file.write_text("")


def test_s5_sybil_rate_limit(gateway_url, mtail_url, signer):
    """S5 — Sybil: one tenant round-robins multiple tokens to try to
    multiply its quota. The per-token limiter is generous; the per-tenant
    limiter (rendered 30/min) is the ceiling, so 40 requests yield 429s.
    The audit metric records them as `rate_limited`."""
    tokens = [signer("s5-sybil")[1] for _ in range(3)]
    statuses = [
        query(gateway_url, tokens[i % len(tokens)],
              "process_open_fds").status_code
        for i in range(40)
    ]
    assert 429 in statuses, f"per-tenant limiter never tripped: {statuses}"
    assert 200 in statuses, f"no request got through: {statuses}"

    # The audit pipeline records the throttled requests (mtail buckets
    # HTTP 429 -> rate_limited). Async — allow the log->mtail lag.
    assert_eventually(
        lambda: expect_positive(
            mtail_counter(mtail_url, "tenant_federation_requests_total",
                          tenant="s5-sybil", status="rate_limited"),
            "rate_limited count"),
        timeout=20.0, desc="audit metric records rate_limited")


def test_s6_oversized_payload(gateway_url, signer):
    """S6 — a 1.5 MiB request body exceeds the Envoy buffer filter's
    1 MiB cap and is rejected with 413 (a single request, so it is never
    rate-limited — the buffer filter sits after the rate limiters)."""
    _, token = signer("s6-payload")
    oversized = "x" * (1_500_000)  # 1.5 MiB, over the 1 MiB buffer cap
    resp = gateway_request(
        gateway_url, "/api/v1/query", token=token, method="POST",
        data="query=" + oversized,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 413, (resp.status_code, resp.text[:200])


def test_s7_storage_cap(gateway_url, mtail_url, signer):
    """S7 — a query touching every series (16 for db-a) exceeds the
    deliberately-low --query.max-samples (12) and the storage backend
    rejects it with 422; the audit metric records it as `bad_request`."""
    _, token = signer("db-a")
    resp = query(gateway_url, token, '{__name__=~".+"}')
    assert resp.status_code == 422, (resp.status_code, resp.text[:200])

    # mtail buckets HTTP 422 -> bad_request. Async — allow the pipeline lag.
    assert_eventually(
        lambda: expect_positive(
            mtail_counter(mtail_url, "tenant_federation_requests_total",
                          tenant="db-a", status="bad_request"),
            "bad_request count"),
        timeout=20.0, desc="audit metric records bad_request")


def test_s8_remote_read_blocked(gateway_url, signer):
    """S8 — Prometheus remote_read cannot be tenant-scoped by the Layer 3
    proxy, so the gateway 403s /api/v1/read and every path variant
    (a trailing slash must not slip past the guard)."""
    _, token = signer("db-a")
    for path in ("/api/v1/read", "/api/v1/read/", "/api/v1//read"):
        resp = gateway_request(gateway_url, path, token=token,
                               method="POST", data=b"")
        assert resp.status_code == 403, (path, resp.status_code)
