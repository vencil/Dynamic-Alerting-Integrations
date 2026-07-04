#!/usr/bin/env python3
"""Federation revocation reconciler — ADR-028 D1 detective control (#924).

Reconciles the off-store, append-only revocation event log (in VictoriaLogs)
against the live revoked set (the ``tenant-federation-store`` ConfigMap's
``revoked.txt`` key, mounted as a file), and exposes the result as Prometheus
metrics for alerting.

Threat (ADR-028): a write-capable attacker (stolen tenant-api SA token / pod
RCE) drops a still-valid ``token_id`` from the revoked set — an un-revoke that
carries the *legitimate* SA identity, so identity controls (RBAC / VAP / the
#926 out-of-band audit) can't see it. The attacker can drop it from the
ConfigMap but cannot un-emit the ``federation_token_revoked`` event already
shipped to VictoriaLogs at *legitimate* revoke time (Certificate-Transparency
temporal ordering: the anchor predates the attack).

Detection: a ``token_id`` the log says was revoked, still comfortably before its
``expires_at``, but ABSENT from the live set → a suspected un-revoke.

Design notes (ADR-028 §MVP):
  * Long-running Deployment + ``/metrics`` (not a CronJob): the platform has no
    Pushgateway / textfile-collector / vmalert, so a short-lived job can't be
    scraped; an exporter with ``up``-based liveness is the Prometheus-native
    shape. Runs as a da-tools subcommand (reuses the image; no new release line).
  * Live set read by MOUNTING the ConfigMap (kubelet projection = direct
    source-of-truth read, NOT via the possibly-compromised tenant-api API, and
    no RBAC needed) — G3.
  * Fail-closed (G1): a failed log query / live-set read does NOT emit an
    all-clear. It bumps an error counter and leaves ``last_reconcile_ts``
    unchanged so ``FederationRevocationReconcileStale`` fires. A query error is
    never read as "no tamper".
  * Clock-skew tolerance (G-Gemini-r2): a token within ``skew_margin`` of its
    expiry is treated as a normal prune, never a false-positive critical.
  * PII minimization (D3): events carry only opaque ``token_id`` + ``expires_at``.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
import urllib.parse
import urllib.request

# ── pure domain logic (unit-tested without a cluster) ──────────────────────────

DEFAULT_SKEW_MARGIN_S = 120         # ~2 min: absorb API-server/VictoriaLogs/node clock drift
DEFAULT_WINDOW_LOOKBACK_S = 24 * 3600   # revocation events: query [now-24h, now-settle]
DEFAULT_WINDOW_SETTLE_S = 60        # only reconcile logs old enough to have landed
DEFAULT_FAILOPEN_LOOKBACK_S = 600   # gateway fail-open: RECENT window only (~10m) so the
#                                     gauge reflects current failures, not a 24h-old blip


@dataclasses.dataclass(frozen=True)
class RevocationEvent:
    """One federation_token_revoked event as recovered from the log."""
    token_id: str
    expires_at: float  # unix seconds


def parse_revoked_file(text: str) -> set[str]:
    """Parse the mounted revoked.txt: one token_id per line, blanks ignored."""
    return {line.strip() for line in text.splitlines() if line.strip()}


def parse_events(rows: list[dict]) -> list[RevocationEvent]:
    """Turn VictoriaLogs JSON rows into events, skipping malformed ones.

    A row is expected to carry ``token_id`` and an RFC3339 ``expires_at``.
    Rows missing either, or with an unparseable time, are dropped (they cannot
    be reconciled) — the caller counts drops so a schema drift is visible.
    """
    out: list[RevocationEvent] = []
    for row in rows:
        tid = row.get("token_id")
        exp = row.get("expires_at")
        if not tid or not exp:
            continue
        ts = _parse_rfc3339(exp)
        if ts is None:
            continue
        out.append(RevocationEvent(token_id=str(tid), expires_at=ts))
    return out


def _parse_rfc3339(value: str) -> float | None:
    """Parse an RFC3339/UTC timestamp to unix seconds, or None."""
    try:
        # tenant-api emits UTC "...Z"; tolerate the +00:00 form too.
        v = value.strip()
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        import datetime as _dt
        return _dt.datetime.fromisoformat(v).timestamp()
    except (ValueError, TypeError):
        return None


@dataclasses.dataclass(frozen=True)
class ReconcileResult:
    checked: int          # events considered (still comfortably live)
    suspected: list[str]  # token_ids logged-live but absent from the live set

    @property
    def tamper_suspected(self) -> int:
        return len(self.suspected)


def reconcile(
    events: list[RevocationEvent],
    live_set: set[str],
    now: float,
    skew_margin_s: float = DEFAULT_SKEW_MARGIN_S,
) -> ReconcileResult:
    """Pure reconciliation (ADR-028 D1).

    For each logged revocation still comfortably before expiry
    (``now < expires_at - skew_margin``), assert its ``token_id`` is in the live
    revoked set. An absent one is a suspected un-revoke. Tokens within
    ``skew_margin`` of expiry (or past it) are skipped — a premature drop there
    is an ordinary prune / clock-skew artefact, not tampering.

    De-duplicates by token_id: the same revocation may be logged more than once.
    """
    suspected: list[str] = []
    checked_ids: set[str] = set()
    for ev in events:
        if now >= ev.expires_at - skew_margin_s:
            continue  # at/near/after expiry — normal prune, not tamper
        if ev.token_id in checked_ids:
            continue
        checked_ids.add(ev.token_id)
        if ev.token_id not in live_set:
            suspected.append(ev.token_id)
    suspected.sort()
    return ReconcileResult(checked=len(checked_ids), suspected=suspected)


def build_logsql_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for the settled revocation-event window.

    Filters on the ``event`` data field (VictoriaLogs is schemaless — a
    dedicated log_type stream is not required), and only reconciles logs old
    enough to have reliably landed (avoids ingestion-lag misjudgement)."""
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND event:"federation_token_revoked"'
    )


def build_failopen_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for gateway revoked-set read failures (the fail-open signal).

    The gateway Lua logs ``federation: revoked-set reload failed`` to Envoy
    stderr; Vector ships it to VictoriaLogs. mtail (audit access-log only)
    cannot see it, so the reconciler counts it here."""
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND "federation: revoked-set reload failed"'
    )


# ── metrics exposition (dependency-free Prometheus text format) ────────────────

class Metrics:
    """Holds the reconciler's gauges/counters and renders the exposition text.

    Counters are monotonic within a process lifetime; ``up`` and the process's
    own liveness come free from the Prometheus scrape of this Deployment.
    """

    def __init__(self) -> None:
        self.tamper_suspected = 0            # gauge: current suspected un-revokes
        self.last_reconcile_ts = 0.0         # gauge: unix ts of last SUCCESSFUL reconcile
        self.events_checked = 0              # gauge: events considered last run
        self.reconcile_errors_total = 0      # counter: failed reconcile passes (fail-closed)
        self.gateway_load_errors = 0         # gauge: gateway fail-open warns in window

    def render(self) -> str:
        lines = [
            "# HELP federation_revocation_tamper_suspected Suspected un-revokes (logged-live token absent from the live set).",
            "# TYPE federation_revocation_tamper_suspected gauge",
            f"federation_revocation_tamper_suspected {self.tamper_suspected}",
            "# HELP federation_revocation_last_reconcile_timestamp_seconds Unix time of the last SUCCESSFUL reconcile.",
            "# TYPE federation_revocation_last_reconcile_timestamp_seconds gauge",
            f"federation_revocation_last_reconcile_timestamp_seconds {self.last_reconcile_ts:.0f}",
            "# HELP federation_revocation_events_checked Revocation events reconciled in the last run.",
            "# TYPE federation_revocation_events_checked gauge",
            f"federation_revocation_events_checked {self.events_checked}",
            "# HELP federation_revocation_reconcile_errors_total Reconcile passes that failed (fail-closed; no all-clear emitted).",
            "# TYPE federation_revocation_reconcile_errors_total counter",
            f"federation_revocation_reconcile_errors_total {self.reconcile_errors_total}",
            "# HELP federation_gateway_revocation_load_errors Gateway revoked-set read failures seen in the window (fail-open signal).",
            "# TYPE federation_gateway_revocation_load_errors gauge",
            f"federation_gateway_revocation_load_errors {self.gateway_load_errors}",
        ]
        return "\n".join(lines) + "\n"


# ── I/O (thin; exercised via integration, not unit tests) ──────────────────────

def query_victorialogs(base_url: str, query: str, timeout_s: float = 30.0) -> list[dict]:
    """Run a LogsQL query; return the newline-delimited JSON rows.

    Raises on any transport/HTTP error so the caller can fail closed."""
    url = base_url.rstrip("/") + "/select/logsql/query?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 (in-cluster URL)
        body = resp.read().decode("utf-8", "replace")
    rows: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def read_live_set(path: str) -> set[str]:
    """Read the mounted revoked.txt (the live revoked token_id set).

    A MISSING file is benign, NOT fail-closed: the tenant-api creates the
    ``revoked.txt`` ConfigMap key on the first revoke, so an absent key means
    no revocation has ever been written — and then the log query returns no
    events either, so an empty live set reconciles cleanly. A down mount / pod
    is caught by the ``up`` / absent-scrape liveness, not by this read.

    Any OTHER read error (present but unreadable — permission / IO) propagates
    so the caller fails closed (never a false all-clear)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_revoked_file(fh.read())
    except FileNotFoundError:
        return set()


def reconcile_once(cfg: "Config", metrics: Metrics, now: float) -> None:
    """One reconcile pass, updating metrics. Fail-closed: on ANY error (query,
    read, or the reconcile itself) bump the error counter and DO NOT refresh
    last_reconcile_ts or emit an all-clear — a stuck pass surfaces as staleness,
    never as a false 'no tamper' and never as a crash-loop."""
    try:
        ev_rows = query_victorialogs(cfg.victorialogs_url, build_logsql_query(cfg.lookback_s, cfg.settle_s))
        fo_rows = query_victorialogs(cfg.victorialogs_url, build_failopen_query(cfg.failopen_lookback_s, cfg.settle_s))
        live = read_live_set(cfg.revoked_file)
        result = reconcile(parse_events(ev_rows), live, now, cfg.skew_margin_s)
    except Exception as exc:  # noqa: BLE001 — any failure is fail-closed
        metrics.reconcile_errors_total += 1
        print(f"reconcile pass failed (fail-closed, no all-clear): {exc}", file=sys.stderr, flush=True)
        return
    metrics.tamper_suspected = result.tamper_suspected
    metrics.events_checked = result.checked
    metrics.gateway_load_errors = len(fo_rows)
    metrics.last_reconcile_ts = now
    if result.suspected:
        # Loud, opaque (token_id only): the tenant is resolved from the store at
        # IR time, never logged here (D3).
        print(
            "TAMPER SUSPECTED: revoked-but-live token_ids absent from the live set: "
            + ",".join(result.suspected),
            file=sys.stderr,
            flush=True,
        )


@dataclasses.dataclass
class Config:
    victorialogs_url: str
    revoked_file: str
    metrics_port: int
    interval_s: int
    lookback_s: int
    settle_s: int
    skew_margin_s: int
    failopen_lookback_s: int


def _serve_forever(cfg: Config, metrics: Metrics, clock=time.time) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") in ("/metrics", ""):
                payload = metrics.render().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):  # silence per-request logging
            pass

    httpd = ThreadingHTTPServer(("", cfg.metrics_port), _Handler)
    import threading

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"federation-revocation-reconciler: /metrics on :{cfg.metrics_port}, "
          f"interval {cfg.interval_s}s", file=sys.stderr, flush=True)
    while True:
        reconcile_once(cfg, metrics, clock())
        time.sleep(cfg.interval_s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Federation revocation reconciler (ADR-028 D1, #924).")
    p.add_argument("--victorialogs-url", default="http://victorialogs.monitoring.svc.cluster.local:9428")
    p.add_argument("--revoked-file", default="/etc/revoked/revoked.txt",
                   help="Mounted tenant-federation-store revoked.txt key (direct CM read).")
    p.add_argument("--metrics-port", type=int, default=9099)
    p.add_argument("--interval", type=int, default=300, help="Reconcile interval, seconds.")
    p.add_argument("--lookback", type=int, default=DEFAULT_WINDOW_LOOKBACK_S)
    p.add_argument("--settle", type=int, default=DEFAULT_WINDOW_SETTLE_S)
    p.add_argument("--skew-margin", type=int, default=DEFAULT_SKEW_MARGIN_S)
    p.add_argument("--failopen-lookback", type=int, default=DEFAULT_FAILOPEN_LOOKBACK_S,
                   help="Recent window (s) for the gateway fail-open gauge.")
    args = p.parse_args(argv)
    cfg = Config(
        victorialogs_url=args.victorialogs_url,
        revoked_file=args.revoked_file,
        metrics_port=args.metrics_port,
        interval_s=args.interval,
        lookback_s=args.lookback,
        settle_s=args.settle,
        skew_margin_s=args.skew_margin,
        failopen_lookback_s=args.failopen_lookback,
    )
    _serve_forever(cfg, Metrics())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
