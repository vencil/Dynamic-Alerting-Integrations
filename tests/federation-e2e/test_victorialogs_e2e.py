"""Victorialogs-mode gateway E2E — tenant log-query isolation (ADR-021 #609 PR-5).

The epic finale's isolation proof. Each scenario walks a real request through the
federation-gateway running in `victorialogs` mode (rendered from the shipped Helm
chart) to a mock log store that echoes the AccountID/ProjectID headers the gateway
injected. See mock_logstore.py + README §victorialogs fidelity boundary.

Scope is GATEWAY-FOCUSED, by deliberate choice (flagged to PM): the existing harness
is metrics-plane only (prom-label-proxy + Prometheus), and the cross-tenant isolation
PRIMITIVE is VictoriaLogs-native — an upstream OSS guarantee, not our code. What OUR
code (the gateway) owns is the AUTHORIZATION plane: inject the JWT-verified AccountID,
overwrite any client-spoofed value, fail-closed on a missing/malformed claim, enforce
the logs audience, default-deny the endpoint surface. These scenarios assert exactly
that — the mock upstream makes "what AccountID reached the store" observable, so a
gateway regression that forwards a spoofed or zero AccountID goes red.

Core assertion (VL2/VL3): a tenant's request reaches the store carrying ONLY its own
JWT-verified AccountID — never another tenant's, never a client-supplied spoof, never
the platform-default 0. That is the gateway half of the cross-tenant isolation joint
property; the VictoriaLogs partition half is its own (real-store, full-stack) follow-up.

Fixtures live in THIS module (not a sibling conftest.py): the shared harness dir
already has the metrics conftest.py, and a second conftest.py cannot coexist there.
The victorialogs stack runs on its own port (E2E_VL_GATEWAY_PORT, default 18081,
distinct from the metrics gateway's 18080) with its own readiness probe.
"""
import http.client
import json
import os
import time
from urllib.parse import urlencode

import pytest
import requests

from helpers import load_signing, sign_logs_token, sign_token

# 127.0.0.1, not localhost: the compose stack publishes on IPv4; localhost can
# resolve to ::1 first and miss the binding (same reason as conftest.py).
VL_GATEWAY_URL = "http://127.0.0.1:" + os.environ.get("E2E_VL_GATEWAY_PORT", "18081")

# Two synthetic tenants for the isolation scenarios. AccountIDs are in the
# registry's reserved-floor range (>=1000, account.FirstTenantAccountID). The
# tenant ids are synthetic (tenant-agnostic — never a real customer id).
TENANT_A = ("tenant-log-a", 1000)
TENANT_B = ("tenant-log-b", 1001)


@pytest.fixture(scope="session")
def vl_gateway_url():
    return VL_GATEWAY_URL


@pytest.fixture(scope="session")
def logs_signer():
    """A `logs_signer(tenant, account_id, **kw) -> (token_id, jwt)` callable
    bound to the runner-rendered federation keypair, minting a logs-capability
    token (aud=tenant-federation-logs + numeric account_id claim). `.pem`/`.kid`
    expose the key material for the VL4 metrics-audience scenario."""
    pem, kid = load_signing()

    def _sign(tenant, account_id, **kw):
        return sign_logs_token(tenant, private_key_pem=pem, kid=kid,
                               account_id=account_id, **kw)

    _sign.pem = pem
    _sign.kid = kid
    return _sign


@pytest.fixture(scope="session", autouse=True)
def _vl_stack_ready(logs_signer):
    """End-to-end readiness probe for the victorialogs stack: a verified logs
    token for TENANT_A must walk gateway -> mock-logstore and come back 200 with
    the verified AccountID echoed, before any scenario runs. Fails fast so each
    scenario doesn't burn a timeout."""
    tenant, acct = TENANT_A
    _, token = logs_signer(tenant, acct)
    deadline = time.monotonic() + 120.0
    last = None
    while time.monotonic() < deadline:
        try:
            resp = requests.get(
                VL_GATEWAY_URL + "/select/logsql/query",
                params={"query": "log_type:federation_audit", "limit": "10"},
                headers={"Authorization": "Bearer " + token}, timeout=10)
            if resp.status_code == 200 and resp.json().get("received_account_id") == str(acct):
                return
            last = f"status={resp.status_code} body={resp.text[:200]}"
        except (requests.RequestException, ValueError) as exc:
            last = repr(exc)
        time.sleep(1.0)
    pytest.fail(f"victorialogs gateway stack not ready within 120s (last: {last})")


def _query(url, token, *, path="/select/logsql/query", extra_headers=None,
           method="GET"):
    """One LogsQL query through the victorialogs gateway."""
    hdr = {"Authorization": "Bearer " + token}
    if extra_headers:
        hdr.update(extra_headers)
    return requests.request(
        method, url + path,
        params={"query": "log_type:federation_audit", "limit": "50"},
        headers=hdr, timeout=35)


def _query_wire_duplicate_accountid(url, token, dup_values):
    """Send a query with TRUE WIRE-DUPLICATE AccountID headers — multiple same-key
    headers on the HTTP/1.1 wire — which the requests/urllib3 client cannot express
    (its header store is a case-insensitive single-value dict). Uses raw http.client.
    Probes header-smuggling (#905 隱患一 / Gemini): Envoy merges duplicate request
    headers into one comma-joined value; the gateway's Lua replace() must overwrite the
    WHOLE header with the JWT-verified value, so the store never receives a smuggled id
    nor a merged `a,b`. Returns (status_code, parsed_json_body)."""
    host = url.rsplit(":", 1)[0].replace("http://", "")
    port = int(url.rsplit(":", 1)[1])
    qs = urlencode({"query": "log_type:federation_audit", "limit": "50"})
    conn = http.client.HTTPConnection(host, port, timeout=35)
    try:
        conn.putrequest("GET", "/select/logsql/query?" + qs, skip_accept_encoding=True)
        conn.putheader("Authorization", "Bearer " + token)
        for v in dup_values:               # two same-key AccountID headers on the wire
            conn.putheader("AccountID", v)
        conn.endheaders()
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


# ── VL1 — happy path ──────────────────────────────────────────────────────────
def test_vl1_happy_path(vl_gateway_url, logs_signer):
    """A logs-capability token (aud=tenant-federation-logs + account_id=1000)
    walks the chain: gateway verifies the JWT, the Lua injects the verified
    AccountID, the store echoes it back and serves that partition's rows."""
    tenant, acct = TENANT_A
    _, token = logs_signer(tenant, acct)
    resp = _query(vl_gateway_url, token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received_account_id"] == str(acct), body
    assert body["received_project_id"] == "0", ("ProjectID pinned 0 for (b)", body)
    assert body["rows"], "expected tenant-A's own partition rows"
    assert all(r["account_id"] == str(acct) for r in body["rows"]), body


# ── VL2 — cross-tenant isolation (the headline assertion) ─────────────────────
def test_vl2_cross_tenant_isolation(vl_gateway_url, logs_signer):
    """Tenant A's verified token reaches the store as AccountID=1000 and sees
    ONLY 1000's rows; Tenant B's as 1001 and ONLY 1001's. Neither can be coaxed
    into the other's partition — the AccountID the store receives is always the
    one baked into the verified JWT, so A never sees a tenant-1001 row and B
    never sees a tenant-1000 row. This is the gateway half of the isolation
    joint property (VictoriaLogs-native partitioning is the other half)."""
    ten_a, acct_a = TENANT_A
    ten_b, acct_b = TENANT_B
    _, tok_a = logs_signer(ten_a, acct_a)
    _, tok_b = logs_signer(ten_b, acct_b)

    ra = _query(vl_gateway_url, tok_a).json()
    rb = _query(vl_gateway_url, tok_b).json()

    assert ra["received_account_id"] == str(acct_a)
    assert rb["received_account_id"] == str(acct_b)
    # A sees only A's rows; the other tenant's account_id never appears.
    assert {r["account_id"] for r in ra["rows"]} == {str(acct_a)}, ra
    assert {r["account_id"] for r in rb["rows"]} == {str(acct_b)}, rb
    assert str(acct_b) not in {r["account_id"] for r in ra["rows"]}
    assert str(acct_a) not in {r["account_id"] for r in rb["rows"]}


# ── VL3 — header spoofing / case-variant / smuggling (Gemini fold-in) ─────────
def test_vl3_client_accountid_spoof_overwritten(vl_gateway_url, logs_signer):
    """⛔ The regression guard for the breach this mode is built to prevent.

    Tenant A (verified account_id=1000) sends a CLIENT AccountID/ProjectID header
    claiming to be tenant B (1001), in EVERY case variant an HTTP layer could
    canonicalise differently (AccountID / accountid / ACCOUNTID / aCcOuNtId), one
    per request. The gateway's revoked_check.lua replace() OVERWRITES any client
    copy with the verified value at injection (case-insensitive — Envoy headers
    are case-insensitive), so the store must ALWAYS receive 1000, never the spoofed
    1001. A regression (route-layer header-strip re-introduced, or replace()
    weakened to add()) would surface here as received_account_id=1001 or =0.

    NB on coverage: this case tests the case-variant spoof (one header per request,
    requests-expressible); the platform-0 spoof is VL3b; a true wire-DUPLICATE of the
    same header key (two `AccountID:` headers on the wire, requests cannot express it)
    is exercised by VL3c below via raw http.client (#905 隱患一)."""
    tenant, acct = TENANT_A  # 1000
    spoof = str(TENANT_B[1])  # 1001
    _, token = logs_signer(tenant, acct)

    for hdr_name in ("AccountID", "accountid", "ACCOUNTID", "aCcOuNtId"):
        resp = _query(vl_gateway_url, token,
                      extra_headers={hdr_name: spoof, "ProjectID": "9"})
        assert resp.status_code == 200, (hdr_name, resp.text)
        body = resp.json()
        # The verified value won — the spoof never reached the store.
        assert body["received_account_id"] == str(acct), (
            f"client {hdr_name}={spoof} spoof was NOT overwritten — "
            f"store received {body['received_account_id']!r} (cross-tenant breach)")
        assert body["received_account_id"] != spoof, (hdr_name, body)
        # ProjectID is likewise pinned by the Lua to 0 (capability b), not the
        # client's 9 — a spoofed ProjectID must not select another project either.
        assert body["received_project_id"] == "0", (hdr_name, body)
        # And A still sees only its own rows despite the spoof attempt.
        assert {r["account_id"] for r in body["rows"]} == {str(acct)}, body


def test_vl3b_spoof_does_not_select_platform_partition(vl_gateway_url, logs_signer):
    """A subtler spoof: a tenant tries to reach the PLATFORM-default partition (0,
    where the cross-tenant operational logs live) by sending `AccountID: 0`. The
    verified value (1000) must still win — the store receives 1000, never 0, so the
    tenant cannot read the platform's own `0:0` logs by spoofing the default."""
    tenant, acct = TENANT_A
    _, token = logs_signer(tenant, acct)
    resp = _query(vl_gateway_url, token, extra_headers={"AccountID": "0"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["received_account_id"] == str(acct), (
        f"AccountID:0 spoof reached the store as {body['received_account_id']!r} "
        f"— a tenant could read the platform 0:0 partition (breach)")
    assert body["received_account_id"] != "0", body
    assert "PLATFORM-DEFAULT-PARTITION" not in str(body["rows"]), body


# ── VL3c — TRUE wire-duplicate header smuggling (raw http.client) ──────────────
def test_vl3c_wire_duplicate_accountid_smuggling(vl_gateway_url, logs_signer):
    """Stronger than VL3 (#905 隱患一 / Gemini adversarial): a TRUE wire-duplicate of
    the AccountID header — two `AccountID:` headers on the HTTP/1.1 wire, which the
    requests client cannot express. Tenant A (verified 1000) smuggles two spoofed
    AccountIDs (1001, 1002). Envoy merges duplicate request headers into one comma-
    joined value (`1001,1002`); the gateway's Lua `replace("AccountID", verified)` must
    overwrite the WHOLE header, so the store receives ONLY the verified 1000 — never a
    smuggled id, never a merged `1001,1002`. A regression to `add()` (instead of
    replace) or a header-strip ordering bug would surface here as a non-1000 / comma id."""
    tenant, acct = TENANT_A          # verified 1000
    b1, b2 = str(TENANT_B[1]), "1002"  # two spoofs, neither is the verified value
    _, token = logs_signer(tenant, acct)
    status, body = _query_wire_duplicate_accountid(vl_gateway_url, token, [b1, b2])
    assert status == 200, body
    got = str(body.get("received_account_id"))
    assert got == str(acct), (
        f"wire-duplicate AccountID smuggling was NOT overwritten — store received "
        f"{got!r} (expected verified {acct}); a duplicate/merged header leaked through")
    assert "," not in got, f"merged comma-value {got!r} reached the store (replace() did not fully overwrite)"
    assert {r["account_id"] for r in body["rows"]} == {str(acct)}, body


# ── VL4 — audience enforcement (capability model B) ───────────────────────────
def test_vl4_metrics_token_rejected_at_logs_endpoint(vl_gateway_url, logs_signer):
    """A metrics-pull token (aud=tenant-federation, NO account_id claim) presented
    to the log store endpoint is rejected by jwt_authn for the audience mismatch
    BEFORE the Lua runs — the audience-bound capability model (B). It must never
    reach the store as any AccountID. (A correctly-signed token, just the wrong
    audience — so this isolates the audience check, not a signature failure.)

    Envoy jwt_authn answers an audience mismatch with 403 "Audiences in Jwt are
    not allowed" (distinct from the 401 it returns for a missing/forged token —
    cf the metrics-plane S3). Asserting the body, not just the code, pins that
    this is the AUDIENCE rejection at jwt_authn and not the Lua's later
    account_id fail-closed 403 (which carries a different body) — so a regression
    that accidentally accepts the wrong audience and 403s only at the Lua would
    still be caught here."""
    _, metrics_token = sign_token(
        TENANT_A[0], private_key_pem=logs_signer.pem, kid=logs_signer.kid,
        aud="tenant-federation")  # the metrics plane audience
    resp = _query(vl_gateway_url, metrics_token)
    assert resp.status_code == 403, (resp.status_code, resp.text[:200])
    assert "Audiences in Jwt are not allowed" in resp.text, (
        f"expected jwt_authn audience rejection, got body: {resp.text[:200]}")


# ── VL5 — fail-closed on missing / malformed account_id claim ─────────────────
@pytest.mark.parametrize("bad_account_id, label", [
    (None, "null claim"),
    ("", "empty string"),
    (999, "reserved band (<1000)"),
    (0, "platform-default partition 0"),
    ("12.5", "non-integer"),
    ("not-a-number", "non-numeric"),
    (4294967296, "uint32 overflow (>2^32-1)"),
])
def test_vl5_failclosed_invalid_account_claim(vl_gateway_url, logs_signer,
                                              bad_account_id, label):
    """⛔ The Null-Claim Trap fail-closed defence. A logs-AUDIENCE token (so it
    passes jwt_authn) whose account_id claim is missing / empty / reserved / 0 /
    non-integer / overflow must be 403'd by the Lua BEFORE injection — it must
    NEVER reach the store (where a missing AccountID would default to partition 0
    = the platform's logs = cross-tenant breach). Asserting 403, not just
    non-200, pins the deliberate verdict."""
    # aud is the logs audience (passes jwt_authn); only the account_id claim is bad.
    _, token = logs_signer(TENANT_A[0], bad_account_id)
    resp = _query(vl_gateway_url, token)
    assert resp.status_code == 403, (
        f"{label}: expected 403 (fail-closed before injection), got "
        f"{resp.status_code}: {resp.text[:200]}")


def test_vl5b_valid_claim_still_reaches_store(vl_gateway_url, logs_signer):
    """Control for VL5: the lowest VALID account_id (1000, the reserved floor) is
    accepted and injected — so VL5's 403s are the malformed-claim rejection, not
    a blanket deny that would also break legitimate tenants."""
    _, token = logs_signer(TENANT_A[0], 1000)
    resp = _query(vl_gateway_url, token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["received_account_id"] == "1000"


# ── VL6 — endpoint allowlist (default-deny) ───────────────────────────────────
def test_vl6_default_deny_endpoint_allowlist(vl_gateway_url, logs_signer):
    """Default-deny: the live `/tail` long-connection, the `/insert/*` write path,
    a cross-tenant `/select/logsql/tenant_ids` enumeration, and any unknown path
    are 403'd by the catch-all — only the LogsQL query/metadata allowlist is
    forwarded. A valid token does not buy access to a non-allowlisted endpoint."""
    _, token = logs_signer(*TENANT_A)
    hdr = {"Authorization": "Bearer " + token}

    denied = [
        ("/select/logsql/tail", "GET"),           # live long-conn (bypasses duration cap)
        ("/insert/jsonline", "POST"),             # write path
        ("/select/logsql/tenant_ids", "GET"),     # cross-tenant enumeration
        ("/admin/storage", "GET"),                # unknown / admin
        ("/select/logsql/query/extra", "GET"),    # sub-path of an allowed endpoint
    ]
    for path, method in denied:
        resp = requests.request(method, vl_gateway_url + path, headers=hdr,
                                data=b"" if method == "POST" else None, timeout=35)
        assert resp.status_code == 403, (
            f"{path} must be default-denied (403), got {resp.status_code}")

    # An allowlisted metadata endpoint is reachable (200) — proving the allowlist
    # is positive, not a blanket deny.
    allowed = requests.get(vl_gateway_url + "/select/logsql/field_names",
                           headers=hdr, timeout=35)
    assert allowed.status_code == 200, (allowed.status_code, allowed.text[:200])
    assert allowed.json()["received_account_id"] == str(TENANT_A[1])
