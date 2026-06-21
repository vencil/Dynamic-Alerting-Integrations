"""Shared helpers for the federation E2E driver (ADR-020 IV-2j, #516)."""
import json
import secrets
import time
from pathlib import Path

import jwt as pyjwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

RENDERED = Path(__file__).resolve().parent / "rendered"

# Client-side timeout. The gateway route timeout is 30s; allow margin.
HTTP_TIMEOUT = 35


def load_signing():
    """Return (private_key_pem, kid) from the runner-rendered keypair."""
    pem = (RENDERED / "private-key.pem").read_text()
    jwks = json.loads((RENDERED / "jwks.json").read_text())
    return pem, jwks["keys"][0]["kid"]


def fresh_rsa_pem():
    """A brand-new RSA private key PEM, NOT in the gateway's JWKS — used
    to forge a wrong-signature token for the S3 enforcement scenario."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def new_token_id():
    """A token id of tenant-api's shape: ftk_ + 16 hex chars."""
    return "ftk_" + secrets.token_hex(8)


# Sentinel distinguishing "account_id claim omitted entirely" from an
# explicit None claim (None is a valid malformed-claim test input).
_UNSET = object()


def sign_token(tenant_id, *, private_key_pem, kid, token_id=None,
               iss="tenant-api", aud="tenant-federation", ttl_seconds=3600,
               account_id=_UNSET):
    """RS256-sign a federation JWT with tenant-api's claim shape.

    Returns (token_id, compact_jwt). The gateway's jwt_authn verifies
    signature + iss + aud and the Lua reads tenant_id / token_id from
    the payload, so a token of this shape signed with the federation key
    is indistinguishable from one tenant-api issued (README §fidelity).

    `account_id` (ADR-021 logs plane): when supplied, embed it as the
    numeric `account_id` claim a logs-capability token carries (the
    VictoriaLogs partition key the gateway injects). Pass it through
    VERBATIM (no coercion) so a test can mint a deliberately malformed
    claim — None, "", "12.5", 0, 999 (reserved band), an overflow — to
    exercise the gateway's fail-closed validation. Left absent by default
    so a metrics-plane token carries no account_id (byte-shape parity)."""
    tid = token_id or new_token_id()
    now = int(time.time())
    claims = {
        "tenant_id": tenant_id,
        "token_id": tid,
        "iss": iss,
        "aud": aud,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if account_id is not _UNSET:
        claims["account_id"] = account_id
    token = pyjwt.encode(
        claims, private_key_pem, algorithm="RS256", headers={"kid": kid})
    return tid, token


def sign_logs_token(tenant_id, *, private_key_pem, kid, account_id,
                    aud="tenant-federation-logs", **kw):
    """RS256-sign a LOGS-capability federation JWT (ADR-021): audience
    `tenant-federation-logs` + a numeric `account_id` claim. Thin wrapper
    over sign_token mirroring what tenant-api issues for `capability=logs`
    (audience-bound model B). `account_id` is passed through verbatim."""
    return sign_token(tenant_id, private_key_pem=private_key_pem, kid=kid,
                      aud=aud, account_id=account_id, **kw)


def gateway_request(gateway_url, path, *, token=None, method="GET",
                    params=None, data=None, headers=None):
    """One HTTP request to the federation gateway."""
    hdr = dict(headers or {})
    if token is not None:
        hdr["Authorization"] = "Bearer " + token
    return requests.request(method, gateway_url + path, params=params,
                            data=data, headers=hdr, timeout=HTTP_TIMEOUT)


def query(gateway_url, token, promql):
    """Instant query through the gateway. Returns the requests.Response."""
    return gateway_request(gateway_url, "/api/v1/query", token=token,
                           params={"query": promql})


def result_series(resp):
    """The result series list from a /api/v1/query response (asserts the
    Prometheus envelope is a success)."""
    body = resp.json()
    assert body.get("status") == "success", body
    return body["data"]["result"]


def tenants_in(resp):
    """The set of distinct `tenant` label values across result series."""
    return {s["metric"].get("tenant") for s in result_series(resp)}


def expect_status(resp, code):
    """Assert an HTTP status; return the response. For assert_eventually."""
    assert resp.status_code == code, \
        f"expected HTTP {code}, got {resp.status_code}: {resp.text[:200]}"
    return resp


def expect_positive(value, desc="value"):
    """Assert a number is > 0; return it. For assert_eventually."""
    assert value > 0, f"{desc}: expected > 0, got {value}"
    return value


def assert_eventually(fn, *, timeout=15.0, interval=0.4, desc="condition"):
    """Poll fn() until it returns without raising AssertionError, else
    fail at `timeout`. The async safety-net for pipeline lag — the mtail
    metric flow (S5/S7) and the Lua revoked-set reload gate (S4)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return fn()
        except AssertionError as exc:
            last = exc
            time.sleep(interval)
    raise AssertionError(f"{desc}: not satisfied within {timeout}s (last: {last})")


def mtail_counter(mtail_url, metric, **labels):
    """Sum of an mtail counter filtered to the given labels (extra labels
    on the series, e.g. `prog`, are ignored). 0.0 if no series match."""
    text = requests.get(mtail_url + "/metrics", timeout=10).text
    total = 0.0
    for line in text.splitlines():
        if not line.startswith(metric + "{"):
            continue
        label_blob = line[line.index("{") + 1:line.rindex("}")]
        value = float(line.rsplit("}", 1)[1])
        got = {}
        for pair in label_blob.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                got[k.strip()] = v.strip().strip('"')
        if all(got.get(k) == want for k, want in labels.items()):
            total += value
    return total
