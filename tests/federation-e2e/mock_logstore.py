#!/usr/bin/env python3
"""Mock VictoriaLogs upstream for the victorialogs-mode gateway E2E (ADR-021 #609 PR-5).

This stands in for VictoriaLogs in the gateway-focused isolation E2E. It does NOT
reimplement VictoriaLogs' native (AccountID, ProjectID) partitioning — that is an
upstream open-source guarantee, not our code. Instead it makes the gateway's OWN
guarantee observable end-to-end: it ECHOES the AccountID / ProjectID headers it
received back in the JSON body, and serves synthetic "log rows" keyed by the
received AccountID.

That lets the driver assert the thing the gateway is actually responsible for:

  * whatever AccountID reaches the store is ALWAYS the JWT-verified one the Lua
    injected — never a client-spoofed value (case variants / smuggled duplicates);
  * a tenant only ever sees rows for ITS OWN verified AccountID (because the gateway
    only ever forwards that tenant's AccountID — VictoriaLogs would then partition on
    it; here the mock returns rows keyed by the received header, so a leak would mean
    the gateway forwarded the wrong/zero AccountID);
  * AccountID 0 (the platform default partition VictoriaLogs falls back to when the
    header is absent) is observable, so a fail-closed-bypass that lets an
    unauthenticated/zero request through shows up as account_id=0 rows.

Why a mock and not a real VictoriaLogs container: identical philosophy to why
tenant-api is not in this harness (README §fidelity) — the gateway cannot tell the
difference, and the assertion target is the gateway's header handling, not the
store's partition engine. A full-stack VictoriaLogs + Vector projection E2E is a
heavier, separate follow-up (see README §victorialogs fidelity boundary).

Pure stdlib (no pip deps): the harness runs it as the upstream compose service from
a python:3.12-alpine image, no requirements install.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Synthetic per-partition log rows. Keyed by AccountID (as the gateway forwards it).
# Each tenant's rows are tagged with its own account_id so the driver can assert a
# token for A never sees B's row. 0 = the platform default partition (the breach
# bucket: a request with no AccountID header lands here in real VictoriaLogs).
_ROWS = {
    "1000": [{"_msg": "tenant-1000-audit-row", "account_id": "1000", "log_type": "federation_audit"}],
    "1001": [{"_msg": "tenant-1001-audit-row", "account_id": "1001", "log_type": "federation_audit"}],
    "0": [{"_msg": "PLATFORM-DEFAULT-PARTITION", "account_id": "0", "log_type": "gateway_operational"}],
}


class _Handler(BaseHTTPRequestHandler):
    # Quiet — the runner dumps container logs on failure anyway.
    def log_message(self, *_a):  # noqa: D401
        pass

    def _received_account(self) -> str:
        """The AccountID header as the gateway forwarded it. Header lookup is
        case-insensitive (http.client lowercases), mirroring how VictoriaLogs /
        any HTTP server treats it — so a client that sent `accountid`/`ACCOUNTID`
        is irrelevant once the gateway's Lua replace() has overwritten it."""
        return self.headers.get("AccountID", "")  # absent → "" → maps to no rows

    def _respond(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        # Echo what the gateway injected + the rows for that partition. The driver
        # reads `received_account_id` to assert the verified value won, and `rows`
        # to assert cross-tenant isolation.
        acct = self._received_account()
        proj = self.headers.get("ProjectID", "")
        self._respond(200, {
            "received_account_id": acct,
            "received_project_id": proj,
            # Echo whether the client's spoof header survived (it must NOT — the
            # gateway replace() overwrites it; this is belt-and-suspenders so the
            # assertion message is informative if it ever regresses).
            "rows": _ROWS.get(acct, []),
        })

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        # Drain the body (LogsQL form-POST) so the connection closes cleanly.
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        self._handle()


def main(port: int = 9428) -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[mock-logstore] listening on :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 9428)
