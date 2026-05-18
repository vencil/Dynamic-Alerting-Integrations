"""pytest fixtures for the federation E2E driver (ADR-020 IV-2j, #516).

The driver runs on the host; the stack is the docker-compose harness
brought up by scripts/ops/federation_e2e_run.sh. Service URLs come from
the compose-published ports (overridable via E2E_GATEWAY_PORT /
E2E_MTAIL_PORT, matching docker-compose.yml)."""
import os
import time

import pytest
import requests

from helpers import load_signing, query, result_series, sign_token

# 127.0.0.1, not localhost: the compose stack publishes the ports on
# IPv4; `localhost` can resolve to IPv6 ::1 first and miss the binding.
GATEWAY_URL = "http://127.0.0.1:" + os.environ.get("E2E_GATEWAY_PORT", "18080")
MTAIL_URL = "http://127.0.0.1:" + os.environ.get("E2E_MTAIL_PORT", "13903")


@pytest.fixture(scope="session")
def gateway_url():
    return GATEWAY_URL


@pytest.fixture(scope="session")
def mtail_url():
    return MTAIL_URL


@pytest.fixture(scope="session")
def signer():
    """A `signer(tenant, **kw) -> (token_id, jwt)` callable bound to the
    runner-rendered federation keypair. `signer.pem` / `signer.kid`
    expose the raw key material for the S3 forged-token scenario."""
    pem, kid = load_signing()

    def _sign(tenant, **kw):
        return sign_token(tenant, private_key_pem=pem, kid=kid, **kw)

    _sign.pem = pem
    _sign.kid = kid
    return _sign


@pytest.fixture(scope="session", autouse=True)
def _stack_ready(signer):
    """End-to-end readiness probe. compose healthchecks only prove each
    container's own health; this proves the full chain serves — gateway
    -> proxy -> Prometheus, with the fixture scraped — before any
    scenario runs. Fails fast so scenarios don't each burn a timeout.

    Probes as db-b, not db-a: each poll that reaches the gateway spends
    a per-tenant rate-limit token, and this loop can run up to 120s. db-b
    has fixture data (so the full-chain check is real) but no scenario
    spends db-b's token budget, so a slow startup cannot deplete the
    db-a bucket S1/S2/S7/S8 share."""
    _, token = signer("db-b")
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        try:
            resp = query(GATEWAY_URL, token, "process_open_fds")
            if resp.status_code == 200 and result_series(resp):
                return
        except (requests.RequestException, AssertionError,
                KeyError, ValueError):
            pass
        time.sleep(1.0)
    pytest.fail("federation stack not ready within 120s — see container logs")
