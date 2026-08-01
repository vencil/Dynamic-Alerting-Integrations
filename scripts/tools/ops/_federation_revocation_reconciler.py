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
  * Evidence-channel canary (#1234): "no revocations happened" and "the
    tenant-api → Vector → VictoriaLogs evidence path is severed" both look like
    zero rows, and the severed case still publishes a clean all-clear with a
    fresh ``last_reconcile_ts`` — a silent false-green. tenant-api therefore
    emits a content-free ``federation_revocation_channel_heartbeat`` every ~5m
    down the SAME transform and sink path, and ``channel_up`` asserts it arrived
    (alert ``FederationRevocationEvidenceChannelDown``). ⛔ The heartbeat query
    lives INSIDE the fail-closed try block: a VictoriaLogs outage must leave
    ``channel_up`` at its previous value, never report a dead channel.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path as _P

# cp950-console --help hardening (da-tools ROI r4 W7): importing a root lib
# reconfigures stdout errors at import time — before argparse can print
# --help — so unencodable help chars degrade instead of crashing on a
# legacy Windows console. scripts/tools is one level up from ops/.
sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
import _lib_compat  # noqa: E402,F401  (import-time side effect; see _lib_compat)

# ── pure domain logic (unit-tested without a cluster) ──────────────────────────

DEFAULT_SKEW_MARGIN_S = 120         # ~2 min: absorb API-server/VictoriaLogs/node clock drift
DEFAULT_WINDOW_LOOKBACK_S = 24 * 3600   # revocation events: query [now-24h, now-settle]
DEFAULT_WINDOW_SETTLE_S = 60        # only reconcile logs old enough to have landed
DEFAULT_FAILOPEN_LOOKBACK_S = 600   # gateway fail-open: RECENT window only (~10m) so the
#                                     gauge reflects current failures, not a 24h-old blip
DEFAULT_HEARTBEAT_LOOKBACK_S = 1800  # evidence-channel canary window (30m): tenant-api emits
#                                      every 5m, so ~5-6 land here and one missed emission
#                                      cannot read as a dead channel (#1234)
# ⛔ THREE COUPLED NUMBERS — do not tune one alone (external review fold-in, #1252).
# This window (30m), tenant-api's heartbeat interval (5m, --federation-heartbeat-interval)
# and FederationRevocationEvidenceChannelDown's `for: 15m` are load-bearing on EACH OTHER:
#   * window >> interval is what makes a single missed emission a non-event. Shrink the
#     window toward the interval and the canary becomes a flap generator; at window ==
#     interval a producer restart alone can zero `channel_up`.
#   * The producer emits ONCE IMMEDIATELY before entering its ticker loop
#     (heartbeat.go Run) — that covers COLD START / fresh deploy, where the window is
#     genuinely empty. It is NOT what protects the `for: 15m` budget across a producer
#     restart: the previous pod's ~5 heartbeats are still inside this window, so
#     `channel_up` never dips. (tenant-api is `strategy: Recreate` per ADR-023, so there
#     is no rolling surge either.) If someone ever shrinks this window to ≈ the interval,
#     that immediate emit becomes load-bearing for restart survival too — which is the
#     real reason the three numbers must move together.
#   * for: 15m ≈ 3 intervals also doubles as the fresh-deploy grace period, because
#     `channel_up` starts at 0 before the first successful reconcile.

# The evidence-channel canary event, and the Vector stream class it arrives on.
# Both strings are hand-copied contracts:
#   * EVENT_HEARTBEAT      — tenant-api's internal/federation/token/heartbeat.go
#                            and helm/vector/values.yaml `evidenceChannel.events`
#   * LOG_TYPE_EVIDENCE    — helm/vector/values.yaml `evidenceChannel.logType`
# A rename on any single side severs the channel while every component still
# reports healthy; that is precisely what channel_up exists to catch, and the
# canary catches a producer-side rename. A VECTOR-side rename shows up as a dead
# channel too, so the tuple is covered end to end.
EVENT_HEARTBEAT = "federation_revocation_channel_heartbeat"
LOG_TYPE_EVIDENCE = "federation_evidence"

# The Vector stream class for gateway operational output (non-JSON Envoy stderr
# / Lua logWarn), plus the container whose output legitimately lands there.
# Both are hand-copied contracts, pinned to source by the tests below:
#   * LOG_TYPE_GATEWAY_OP — helm/vector/templates/configmap.yaml, the demux
#                           `else` branch that classes non-JSON gateway output.
#   * GATEWAY_APP          — helm/federation-gateway/templates/deployment.yaml,
#                           the Envoy container `name:`. `.app` in Vector is the
#                           k8s container_name.
# `app` narrows gateway_operational — which ALSO carries the mtail / logrotate
# sidecars' non-JSON stderr (same pod, same label) — to the Envoy container
# whose Lua actually emits these warnings.
LOG_TYPE_GATEWAY_OP = "gateway_operational"
GATEWAY_APP = "envoy"

# The revoked.txt line contract (#1235 / TRK-349). revoked.txt has two
# independently-implemented readers — this one and the gateway Lua filter —
# and they used to apply different line/whitespace semantics, so the same
# file could parse to different token sets on the two ends. ADR-028's whole
# detection argument assumes both ends see the SAME set, so the contract is
# now explicit and identical on both sides.
#
# Peers that must stay equivalent:
#   * helm/federation-gateway/values.yaml  `revokedSet.tokenIdPattern`
#     (templated into revoked_check.lua — the gateway does not hard-code it)
#   * components/tenant-api/internal/federation/token/manager.go
#     `tokenIDPattern` (the writer)
# tests/ops/test_revoked_set_contract.py asserts the three literals agree.
# ⛔ Literal agreement is necessary, not sufficient — and it is one of four
# guards, none of which substitutes for another:
#   * the three-literal check       — a hand-copied pattern drifting;
#   * federation-e2e S11 (positive) — PATTERN-DIALECT drift, one literal
#     meaning different things to three engines;
#   * federation-e2e S10 (negative) — READER-SEMANTICS drift, the two readers
#     disagreeing about what a line IS. ⚠️ S11 has NO resistance to that
#     class: both the old and the current reader pass S11;
#   * the conformance matrix in tests/ops/test_revoked_set_contract.py — the
#     byte space, mechanically enumerated through the production entry point.
TOKEN_ID_PATTERN = r"^ftk_[0-9a-f]+$"
_TOKEN_ID_RE = re.compile(TOKEN_ID_PATTERN)


@dataclasses.dataclass(frozen=True)
class RevocationEvent:
    """One federation_token_revoked event as recovered from the log."""
    token_id: str
    expires_at: float  # unix seconds


def parse_revoked_file(text: str) -> tuple[set[str], int]:
    """Parse the mounted revoked.txt under the strict line contract.

    Returns ``(token_ids, rejected_count)``.

    A line is what lies between LF separators — nothing else starts a new
    line — and is compared to :data:`TOKEN_ID_PATTERN` byte-for-byte, with a
    single trailing CR tolerated so a CRLF-written file yields the same ids.
    Both rules exist to be IDENTICAL to the gateway's reader
    (helm/federation-gateway/files/revoked_check.lua); the previous version
    normalised each line instead, and that normalisation is what let the two
    readers derive different sets from the same file (see the private
    advisory).

    ⛔ ASYMMETRY WITH THE GATEWAY — INTENTIONAL, DO NOT "UNIFY".
    When the gateway meets a contract-violating line it discards the whole
    reload and KEEPS ITS PREVIOUS SET: it is the enforcement plane, and
    holding the older, pre-tamper set is what makes the attack ineffective.
    This function must NOT do that. It is the DETECTION plane, and the id
    that a rejected line would have carried has to come back ABSENT from the
    live set — that absence is precisely what makes ``reconcile`` raise
    TamperSuspected. Making the two ends behave the same way here would
    remove the detection while leaving every component looking healthy,
    i.e. it would re-create, in a new place, the failure ADR-028 is about.

    Rejected lines are counted only; their content is never returned or
    logged (ADR-028 D3).

    Dialect note: ``fullmatch``, not ``match`` — Python's bare ``$`` also
    matches just before a trailing newline, while Lua's and Go's anchor at
    true end-of-string. ``fullmatch`` is what makes the three agree.
    """
    tokens: set[str] = set()
    rejected = 0
    for raw in text.split("\n"):
        line = raw[:-1] if raw.endswith("\r") else raw
        if not line:
            continue
        if not _TOKEN_ID_RE.fullmatch(line):
            rejected += 1
            continue
        tokens.add(line)
    return tokens, rejected


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

    Source-qualified on ``log_type`` (#1237), matching build_heartbeat_query so
    the revocation-event query and its liveness canary read the SAME stream
    class. That symmetry is load-bearing: a qualifier here the canary did NOT
    also carry would be a new silent-failure surface — it could exclude real
    revocation events (→ tamper_suspected 0) while the canary still passed
    (→ channel_up 1), i.e. a clean all-clear over a blind query.

    ⚠️ This narrows the search (and stops a future ``additionalSources``
    producer, or anything else that logs the ``event`` field name, from being
    counted); it does NOT bind producer IDENTITY. ``log_type`` is stamped by
    the platform in the evidence transform whose only entry ticket is a pod
    label, so a principal able to create a pod with that label still reaches
    this stream. Closing that forgery gap needs producer binding in the
    evidence branch, not a query-side field (tracked separately). Only
    reconciles logs old enough to have reliably landed (ingestion-lag guard)."""
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND log_type:"{LOG_TYPE_EVIDENCE}" '
        f'AND event:"federation_token_revoked"'
    )


def build_heartbeat_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for the evidence-channel liveness canary (ADR-028 D1 / #1234).

    tenant-api emits ``federation_revocation_channel_heartbeat`` every ~5m down
    the SAME Vector transform and sink path as real revocation events, so its
    presence in this window proves the whole producer→Vector→VictoriaLogs chain
    is intact. Absence means the chain is severed — which is otherwise
    indistinguishable from "no revocations happened".

    ⛔ SOURCE-QUALIFIED on purpose, and build_logsql_query above now carries the
    SAME ``log_type`` qualifier (#1237 closed) — the two MUST stay matched, or a
    real revocation event and its canary could be split across stream classes.
    ``log_type`` is written by the platform inside the ``federation_evidence``
    transform (from pre-merge locals, so a log payload cannot forge the CLASS),
    which pins the rows to the one stream class the evidence channel owns. This
    binds the stream class, not the producer identity — see build_logsql_query."""
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND log_type:"{LOG_TYPE_EVIDENCE}" '
        f'AND event:"{EVENT_HEARTBEAT}"'
    )


def build_failopen_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for gateway revoked-set read failures (the fail-open signal).

    The gateway Lua logs ``federation: revoked-set reload failed`` to Envoy
    stderr; Vector classes non-JSON gateway output as ``gateway_operational``.
    Source-qualified on ``log_type`` + ``app`` (#1237, was a bare ``_msg``
    phrase): the class alone still spans the mtail / logrotate sidecars' stderr
    (same pod, same label), so ``app`` narrows it to the Envoy container. A
    tenant cannot forge this VIA THE REQUEST PATH — request-derived content is
    JSON and parses into another class (federation_audit), never
    gateway_operational; verified against the real Envoy access-log encoder,
    which escapes any injected byte into a single valid JSON line.
    ⚠️ ``app`` excludes the sidecars' NOISE, it is NOT producer authentication:
    a principal that can create a pod with the gateway label and an ``envoy``
    container still lands in this class (gateway_operational has no pod_owner
    check — the demux spoof guard only covers federation_audit). That is the
    same producer-identity gap as the evidence branch, closed only by producer
    binding at ingest, not a query-side field (tracked separately). mtail (audit
    access-log only) cannot see this warning, so the reconciler counts it here.

    ⚠️ Until #1294 that sentence UNDERSTATED the exposure: ``app`` is written
    from ``.kubernetes.container_name`` AFTER demux deep-merges the producer's
    own JSON, so a payload carrying ``{"kubernetes":{"container_name":"envoy"}}``
    set this field without the attacker naming any container ``envoy`` at all.
    #1294 moved that read to a pre-merge snapshot, so the capability described
    above ("can create a pod with the gateway label AND an ``envoy`` container")
    is now the real bar rather than an overestimate of it. The producer-identity
    gap itself is unchanged — this only restores the accuracy of the boundary
    this docstring documents."""
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND log_type:"{LOG_TYPE_GATEWAY_OP}" '
        f'AND app:"{GATEWAY_APP}" '
        f'AND "{GATEWAY_FAILOPEN_PHRASE}"'
    )


# ⛔ The gateway's THREE revoked-set warnings mean different things about what is
# being let through right now, and each feeds its own gauge:
#
#   reload failed → could not read it;  the PREVIOUS set is still enforced
#   rejected      → read and refused it; the PREVIOUS set is still enforced
#   missing       → the file is gone;    the set is EMPTY, nothing is enforced
#
# No literal here may be a substring of another, in either direction, or one
# query would count another's rows and an incident responder would read the
# posture backwards — "we are letting revoked tokens through" about a gateway
# that is still enforcing, or the reverse, which is worse.
# tests/ops/test_federation_revocation_reconciler.py pins all three against the
# shipped Lua and asserts pairwise non-containment.
GATEWAY_FAILOPEN_PHRASE = "federation: revoked-set reload failed"
GATEWAY_REJECTED_PHRASE = "federation: revoked-set rejected"
GATEWAY_MISSING_PHRASE = "federation: revoked-set missing"


def build_missing_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for the gateway reporting the revoked-set file is ABSENT (#1236).

    The one path that CLEARS the enforcement plane. The other two warnings leave
    the previously loaded set in force, so they are staleness signals; this one
    means the gateway is checking every token against an EMPTY set and honouring
    all of them until their TTL — ADR-028 §相鄰破口 names deleting/unmounting the
    projected key as the attack, and until #1236 the gateway said nothing at all
    when it happened (the missing branch returns normally from inside the pcall,
    so neither existing warning fires).

    Same window as the other two gateway queries — all three ask "what did the
    gateway say in the recent past". Source-qualified on ``log_type`` + ``app``
    exactly like them; see :func:`build_failopen_query` for the honest boundary
    on what that qualification does and does not prove about the producer.
    """
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND log_type:"{LOG_TYPE_GATEWAY_OP}" '
        f'AND app:"{GATEWAY_APP}" '
        f'AND "{GATEWAY_MISSING_PHRASE}"'
    )


def build_rejected_query(lookback_s: int, settle_s: int) -> str:
    """LogsQL for the gateway REFUSING to load the revoked set (#1235).

    The enforcement plane's own account of what it did, rather than this
    process's second reading of the file. That distinction is the point:
    the two ends read different kubelet projections of the same ConfigMap
    at different moments, so "the file I can see is clean" does not mean
    "the gateway loaded it". A refusal means the gateway is still on the
    set it had BEFORE the file changed — every revocation issued since is
    unenforced, and a worker that started into a refusal enforces nothing.

    Source-qualified on ``log_type`` + ``app`` (build_failopen_query now carries
    the same pair, #1237). ``gateway_operational`` is the class Vector assigns to
    gateway output that is NOT parseable audit JSON — which is where a Lua
    ``logWarn`` lands, and which request-derived content cannot reach because it
    parses as JSON into another class. ``app`` narrows the class to the Envoy
    container, excluding the mtail / logrotate sidecars that share the pod and
    its label — but see build_failopen_query: ``app`` is noise-exclusion, NOT
    producer authentication, and this class shares the evidence branch's
    pod-label-only forging boundary.
    """
    return (
        f'_time:[now-{lookback_s}s, now-{settle_s}s] '
        f'AND log_type:"{LOG_TYPE_GATEWAY_OP}" '
        f'AND app:"{GATEWAY_APP}" '
        f'AND "{GATEWAY_REJECTED_PHRASE}"'
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
        self.events_dropped = 0              # gauge: event-filtered rows that failed to parse (schema-drift signal)
        self.reconcile_errors_total = 0      # counter: failed reconcile passes (fail-closed)
        self.gateway_load_errors = 0         # gauge: gateway fail-open warns in window
        # ADR-028 D1 / #1234 evidence-channel canary. channel_up starts at 0:
        # before the first SUCCESSFUL pass the reconciler has no evidence the
        # channel works, and claiming 1 would be exactly the optimistic default
        # this control exists to remove. The alert's `for: 15m` (≈3 heartbeat
        # periods) is the deploy-time grace window for that.
        self.channel_up = 0                  # gauge: 1 = heartbeat seen in window, 0 = channel dead
        self.heartbeats_seen = 0             # gauge: heartbeat rows in window (debug / chaos verification)
        # ── Enforcement-plane staleness (#1235 / TRK-349) ──────────────
        # Two gauges, deliberately, because they observe DIFFERENT planes
        # and each is blind where the other sees.
        #
        #   live_set_rejected_lines — what THIS process reads out of the
        #     mounted revoked.txt. Present whenever the file carries a
        #     contract-violating line at all, which is the precondition for
        #     every way the gateway can end up stale.
        #   gateway_reload_rejected — what the GATEWAY says it did, read
        #     back off its own log stream. Immune to the two ends reading
        #     different kubelet projections at different times, which the
        #     first gauge cannot see.
        #
        # Why either is needed at all: when the gateway refuses a reload it
        # keeps the set it already had, so every revocation issued AFTER the
        # bad line appeared is silently unenforced — and a freshly started
        # worker, having no previous set, enforces NOTHING. Neither shows up
        # in tamper_suspected, because that compares the audit log against
        # the FILE (which does contain those newer entries), not against
        # what the gateway actually loaded. A MISSING file is loud for
        # exactly this reason and a REFUSED one was not.
        self.live_set_rejected_lines = 0     # gauge: contract-violating lines in the mounted file
        self.gateway_reload_rejected = 0     # gauge: gateway reload-refusal warns in window
        # ⛔ NOT one of the two above. Those two mean the gateway is FROZEN on a
        # set it already had — stale, but still rejecting. This one means the
        # file is GONE and the enforcement plane is EMPTY, which is the only
        # gateway-side state where revoked tokens are actually being honoured.
        # It is also the state the sentence above ("a MISSING file is loud")
        # was describing from the DETECTION end only: tamper_suspected needs
        # revocation events still inside the window to say anything, so until
        # #1236 nothing at the ENFORCEMENT end said it at all.
        self.gateway_revoked_set_missing = 0  # gauge: gateway "file is absent" warns in window

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
            "# HELP federation_revocation_events_dropped Event-filtered log rows that failed to parse last run (schema-drift signal; coverage is degraded while non-zero).",
            "# TYPE federation_revocation_events_dropped gauge",
            f"federation_revocation_events_dropped {self.events_dropped}",
            "# HELP federation_revocation_reconcile_errors_total Reconcile passes that failed (fail-closed; no all-clear emitted).",
            "# TYPE federation_revocation_reconcile_errors_total counter",
            f"federation_revocation_reconcile_errors_total {self.reconcile_errors_total}",
            "# HELP federation_gateway_revocation_load_errors Gateway revoked-set read failures seen in the window (fail-open signal).",
            "# TYPE federation_gateway_revocation_load_errors gauge",
            f"federation_gateway_revocation_load_errors {self.gateway_load_errors}",
            "# HELP federation_revocation_channel_up Evidence channel liveness: 1 = the tenant-api heartbeat canary was seen in the window, 0 = the evidence path is severed (an empty channel would otherwise be indistinguishable from 'no revocations happened').",
            "# TYPE federation_revocation_channel_up gauge",
            f"federation_revocation_channel_up {self.channel_up}",
            "# HELP federation_revocation_heartbeats_seen Canary heartbeat rows in the window (debug / chaos verification; channel_up is the alerting signal).",
            "# TYPE federation_revocation_heartbeats_seen gauge",
            f"federation_revocation_heartbeats_seen {self.heartbeats_seen}",
            "# HELP federation_revocation_live_set_rejected_lines Contract-violating lines in the mounted revoked.txt. Non-zero means the gateway is refusing to load the current revoked set, so revocations made since are NOT being enforced (#1235).",
            "# TYPE federation_revocation_live_set_rejected_lines gauge",
            f"federation_revocation_live_set_rejected_lines {self.live_set_rejected_lines}",
            "# HELP federation_gateway_revoked_set_reload_rejected Gateway reload-refusal warnings in the window, read back off the gateway's own log stream (enforcement-plane self-report; #1235).",
            "# TYPE federation_gateway_revoked_set_reload_rejected gauge",
            f"federation_gateway_revoked_set_reload_rejected {self.gateway_reload_rejected}",
            "# HELP federation_gateway_revoked_set_missing Gateway reports the revoked-set file is ABSENT, so it is enforcing an EMPTY set and honouring every revoked token until its TTL. Unlike the two staleness gauges, this is the enforcement plane being open, not frozen (#1236).",
            "# TYPE federation_gateway_revoked_set_missing gauge",
            f"federation_gateway_revoked_set_missing {self.gateway_revoked_set_missing}",
        ]
        return "\n".join(lines) + "\n"


# ── I/O (thin; exercised via integration, not unit tests) ──────────────────────

def query_victorialogs(base_url: str, query: str, timeout_s: float = 30.0) -> list[dict]:
    """Run a LogsQL query; return the newline-delimited JSON rows.

    Raises on any transport/HTTP error so the caller can fail closed."""
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"victorialogs url must be http(s): {base_url!r}")
    url = base_url.rstrip("/") + "/select/logsql/query?" + urllib.parse.urlencode({"query": query})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # The scheme is guarded to http(s) above; the URL is the operator-set
    # in-cluster VictoriaLogs endpoint, not user input.
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310  # nosec B310
        body = resp.read().decode("utf-8", "replace")
    rows: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def read_live_set(path: str) -> tuple[set[str], int]:
    """Read the mounted revoked.txt (the live revoked token_id set).

    Returns ``(token_ids, rejected_count)`` — see :func:`parse_revoked_file`
    for the line contract and for why a rejected line is simply absent from
    the returned set rather than suppressing the whole read.

    A MISSING file is benign, NOT fail-closed: the tenant-api creates the
    ``revoked.txt`` ConfigMap key on the first revoke, so an absent key means
    no revocation has ever been written — and then the log query returns no
    events either, so an empty live set reconciles cleanly. A down mount / pod
    is caught by the ``up`` / absent-scrape liveness, not by this read.

    Any OTHER read error (present but unreadable — permission / IO) propagates
    so the caller fails closed (never a false all-clear)."""
    try:
        # ⛔ newline="" IS LOAD-BEARING — it disables the decode layer's
        # newline translation. With the default, that layer rewrites a
        # legacy line terminator into a separator ANYWHERE in the file
        # BEFORE parse_revoked_file sees a single byte, while the gateway's
        # reader does no such thing. The two ends would then derive
        # different token sets from the same file — the exact divergence
        # #1235 exists to close, reintroduced one layer above the parser.
        # It is also what makes the parser's trailing-terminator tolerance
        # real: on translated text that branch can never run.
        # tests/ops/test_federation_revocation_reconciler.py pins this at
        # the FILE level; a string-level test cannot see this layer at all.
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return parse_revoked_file(fh.read())
    except FileNotFoundError:
        return set(), 0


def reconcile_once(cfg: "Config", metrics: Metrics, now: float) -> None:
    """One reconcile pass, updating metrics. Fail-closed: on ANY error (query,
    read, or the reconcile itself) bump the error counter and DO NOT refresh
    last_reconcile_ts or emit an all-clear — a stuck pass surfaces as staleness,
    never as a false 'no tamper' and never as a crash-loop."""
    try:
        ev_rows = query_victorialogs(cfg.victorialogs_url, build_logsql_query(cfg.lookback_s, cfg.settle_s))
        fo_rows = query_victorialogs(cfg.victorialogs_url, build_failopen_query(cfg.failopen_lookback_s, cfg.settle_s))
        # ⛔ INSIDE the fail-closed try, deliberately. If the heartbeat query
        # were run outside it (or its result written to the gauge before the
        # early-return), a VictoriaLogs blip would set channel_up = 0 and page
        # FederationRevocationEvidenceChannelDown — a FALSE "the evidence
        # channel is severed" caused by the very outage that already surfaces
        # correctly as ReconcileStale. On any error the pass returns below
        # WITHOUT touching channel_up, so it holds its previous value: an
        # unreachable log store means "unknown", not "dead channel".
        hb_rows = query_victorialogs(
            cfg.victorialogs_url, build_heartbeat_query(cfg.heartbeat_lookback_s, cfg.settle_s))
        # Enforcement-plane self-report (#1235). Same window as the fail-open
        # query — both ask "what did the gateway say in the recent past".
        rj_rows = query_victorialogs(
            cfg.victorialogs_url, build_rejected_query(cfg.failopen_lookback_s, cfg.settle_s))
        # The enforcement plane reporting it has NO list at all (#1236). Same
        # window and the same fail-closed block as its two siblings — an
        # unreachable store must leave every gateway-side gauge at its previous
        # value rather than publish a reassuring zero.
        ms_rows = query_victorialogs(
            cfg.victorialogs_url, build_missing_query(cfg.failopen_lookback_s, cfg.settle_s))
        live, live_rejected = read_live_set(cfg.revoked_file)
        parsed = parse_events(ev_rows)
        result = reconcile(parsed, live, now, cfg.skew_margin_s)
    except Exception as exc:  # noqa: BLE001 — any failure is fail-closed
        metrics.reconcile_errors_total += 1
        print(f"reconcile pass failed (fail-closed, no all-clear): {exc}", file=sys.stderr, flush=True)
        return
    metrics.tamper_suspected = result.tamper_suspected
    metrics.events_checked = result.checked
    # Rows carried the event marker but couldn't be parsed (missing field / bad
    # time) → tenant-api event-schema drift silently narrowing coverage. Exposed
    # as a gauge so the drift is visible, even though any single drop is benign:
    # a fully-drifted feed would otherwise reconcile to a clean, healthy zero
    # while a real un-revoke went unseen (ADR-028 D3 honest-boundary).
    metrics.events_dropped = len(ev_rows) - len(parsed)
    metrics.gateway_load_errors = len(fo_rows)
    # Evidence-channel liveness (#1234). Only reached on a SUCCESSFUL pass, so a
    # 0 here means "the store was reachable and held no canary in the window" —
    # a real severed channel — never "we could not ask".
    metrics.heartbeats_seen = len(hb_rows)
    metrics.channel_up = 1 if hb_rows else 0
    metrics.live_set_rejected_lines = live_rejected
    metrics.gateway_reload_rejected = len(rj_rows)
    metrics.gateway_revoked_set_missing = len(ms_rows)
    metrics.last_reconcile_ts = now
    if live_rejected:
        # Count only — never the line itself (ADR-028 D3; the content is the
        # sensitive part).
        #
        # ⚠️ An earlier revision of this fix deliberately shipped NO metric
        # here, reasoning that "a rejected line means the id it would have
        # carried is absent from the live set, so TamperSuspected already
        # fires". That reasoning only covers the id ON the bad line. It says
        # nothing about the OTHER ids, and the gateway's disposition is to
        # discard the WHOLE reload — so every revocation issued after the
        # bad line appeared is unenforced while the audit log and the file
        # agree about it perfectly, and tamper_suspected stays 0. That is
        # why the gauges above exist. Do not remove them on the strength of
        # the argument they were added to refute.
        print(f"revoked.txt: {live_rejected} line(s) rejected by the token-id contract "
              f"(they are NOT in the live set; see the federation reconciler runbook)",
              file=sys.stderr, flush=True)
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
    heartbeat_lookback_s: int = DEFAULT_HEARTBEAT_LOOKBACK_S


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
    p.add_argument("--heartbeat-lookback", type=int, default=DEFAULT_HEARTBEAT_LOOKBACK_S,
                   help="Window (s) for the evidence-channel canary (default 1800 = ~5-6 of "
                        "tenant-api's 5m heartbeats, so one missed emission never reads as a "
                        "dead channel).")
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
        heartbeat_lookback_s=args.heartbeat_lookback,
    )
    _serve_forever(cfg, Metrics())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
