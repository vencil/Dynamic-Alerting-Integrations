"""Tests for federation_revocation_reconciler.py (ADR-028 D1, #924).

The reconcile logic is pure (events + live set + now -> suspected list), so the
correctness core — un-revoke detection, clock-skew tolerance, expiry skip,
dedup — is unit-tested here. The fail-closed I/O contract (G1: a failed pass
never emits an all-clear) is tested by monkeypatching the I/O seams to raise.
"""
from __future__ import annotations

import dataclasses
import os
import re
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops")
sys.path.insert(0, _TOOLS_DIR)

import _federation_revocation_reconciler as rec  # noqa: E402


def _gateway_phrases() -> dict[str, str]:
    """Every ``GATEWAY_*_PHRASE`` the reconciler declares, discovered from the
    module rather than hand-listed.

    The guards over these are about a CLASS (no phrase may absorb another's
    rows), so enumerating by hand would leave the next phrase uncovered on the
    day it is added — which is exactly when the guard is needed.
    """
    return {
        name: value
        for name, value in vars(rec).items()
        if name.startswith("GATEWAY_") and name.endswith("_PHRASE")
    }


def _shipped_lua() -> str:
    """The Envoy filter as shipped by the chart — the actual producer of the
    log lines the reconciler's queries treat as an API."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "helm", "federation-gateway", "files", "revoked_check.lua")
    return open(os.path.abspath(path), encoding="utf-8").read()

HOUR = 3600.0


def _ev(token_id: str, expires_in_s: float, now: float) -> rec.RevocationEvent:
    return rec.RevocationEvent(token_id=token_id, expires_at=now + expires_in_s)


class TestReconcile:
    def test_no_events_no_suspicion(self):
        r = rec.reconcile([], {"ftk_1"}, now=1000.0)
        assert r.tamper_suspected == 0
        assert r.checked == 0

    def test_live_and_present_is_clean(self):
        now = 1000.0
        events = [_ev("ftk_1", HOUR, now)]
        r = rec.reconcile(events, {"ftk_1"}, now)
        assert r.suspected == []
        assert r.checked == 1

    def test_live_but_absent_is_suspected_unrevoke(self):
        now = 1000.0
        events = [_ev("ftk_gone", HOUR, now)]
        r = rec.reconcile(events, set(), now)  # dropped from the live set
        assert r.suspected == ["ftk_gone"]
        assert r.tamper_suspected == 1

    def test_within_skew_margin_of_expiry_is_not_flagged(self):
        now = 1000.0
        # expires in 60s, margin 120s -> inside the tolerance band -> normal prune
        events = [_ev("ftk_edge", 60, now)]
        r = rec.reconcile(events, set(), now, skew_margin_s=120)
        assert r.suspected == []

    def test_past_expiry_is_skipped(self):
        now = 1000.0
        events = [_ev("ftk_old", -10, now)]  # already expired
        r = rec.reconcile(events, set(), now)
        assert r.suspected == []
        assert r.checked == 0

    def test_dedup_same_token_counted_once(self):
        now = 1000.0
        events = [_ev("ftk_dup", HOUR, now), _ev("ftk_dup", HOUR, now)]
        r = rec.reconcile(events, set(), now)
        assert r.suspected == ["ftk_dup"]
        assert r.checked == 1

    def test_comfortably_live_boundary(self):
        now = 1000.0
        # expires just beyond the margin -> flagged if absent
        events = [_ev("ftk_x", 121, now)]
        r = rec.reconcile(events, set(), now, skew_margin_s=120)
        assert r.suspected == ["ftk_x"]


class TestParsing:
    def test_parse_revoked_file(self):
        assert rec.parse_revoked_file("ftk_1\nftk_2\n\n") == ({"ftk_1", "ftk_2"}, 0)

    def test_parse_revoked_file_tolerates_crlf(self):
        """A CRLF-written file must yield the same ids as an LF one — the one
        deviation the line contract allows, so the two readers agree on a file
        that a Windows-side tool touched."""
        assert rec.parse_revoked_file("ftk_1\r\nftk_2\r\n") == ({"ftk_1", "ftk_2"}, 0)

    def test_parse_revoked_file_rejects_and_counts_non_conforming_lines(self):
        """#1235 / TRK-349: a line that violates the contract is REJECTED and
        counted, never normalised into a clean id. Silent normalisation is what
        let the two readers of this file derive different token sets."""
        tokens, rejected = rec.parse_revoked_file(
            "ftk_1\n"
            " ftk_2 \n"          # contract-violating line
            "ftk_3 ftk_4\n"      # contract-violating line
            "FTK_5\n"            # contract-violating line
            "ftk_6\n"
        )
        assert tokens == {"ftk_1", "ftk_6"}
        assert rejected == 3

    def test_parse_revoked_file_never_recovers_an_id_from_a_rejected_line(self):
        """The load-bearing property: nothing inside a rejected line may
        reappear as a token. A reader that salvaged an id out of a malformed
        line would report a token as revoked that the gateway does not — that
        divergence is what #1235 closes."""
        for text in (
            "ftk_dead ftk_beef\n",      # contract-violating line
            " ftk_1\n",                 # contract-violating line
            "ftk_1\rextra\n",           # contract-violating line
        ):
            tokens, rejected = rec.parse_revoked_file(text)
            assert tokens == set(), text
            assert rejected == 1, text

    def test_a_trailing_separator_is_never_part_of_an_id(self):
        """Asserted through parse_revoked_file, NOT through the bare regex.

        Python's `$` also matches just before a trailing newline, where Lua's
        and Go's anchor at true end-of-string; `fullmatch` is what closes that
        gap. An earlier version of this test compared `re.match` with
        `re.fullmatch` directly — which is a property of the standard library,
        true whatever this module does, and stayed green when the function
        under test was reverted to the loose form. Assert the FUNCTION."""
        assert rec.parse_revoked_file("ftk_1\n") == ({"ftk_1"}, 0)
        # A separator inside the line is not a separator the gateway honours,
        # so this is one line, and one that violates the contract.
        assert rec.parse_revoked_file("ftk_1\\n\n") == (set(), 1)

    def test_read_live_set_missing_file_is_benign(self, tmp_path):
        """A missing revoked.txt is 'nothing revoked yet', not an error — and
        it reports zero rejections rather than crashing the tuple unpack."""
        assert rec.read_live_set(str(tmp_path / "nope.txt")) == (set(), 0)

    def test_read_live_set_reads_and_rejects(self, tmp_path):
        p = tmp_path / "revoked.txt"
        p.write_text("ftk_a1\nnot a token\n", encoding="utf-8", newline="\n")
        assert rec.read_live_set(str(p)) == ({"ftk_a1"}, 1)

    @pytest.mark.parametrize("raw", [
        b"ftk_a1\nftk_b2\n",        # plain
        b"ftk_a1\r\nftk_b2\r\n",    # the one tolerated deviation
        b"ftk_a1\rftk_b2\n",        # contract-violating line
        b"ftk_a1\rftk_b2\r\n",      # contract-violating line
        b"ftk_a1\nnot a token\n",   # contract-violating line
        b"ftk_a1",                  # no terminator on the last line
    ], ids=lambda r: repr(r))
    def test_read_live_set_adds_no_interpretation_of_its_own(self, tmp_path, raw):
        """⛔ The FILE layer must hand parse_revoked_file the bytes verbatim.

        This is the regression guard for a divergence that lived ABOVE the
        parser: the decode layer's default newline handling rewrites a legacy
        terminator into a separator anywhere in the file, whereas the gateway's
        reader does not — so the two ends could still derive different token
        sets from the same file while every string-level test stayed green.
        A test that calls parse_revoked_file with a str CANNOT see this layer,
        which is exactly why one is pinned here at the file level.
        """
        p = tmp_path / "revoked.txt"
        p.write_bytes(raw)
        assert rec.read_live_set(str(p)) == rec.parse_revoked_file(raw.decode("utf-8"))

    def test_read_live_set_rejects_a_split_that_only_the_decoder_would_make(
            self, tmp_path):
        """The concrete half of the invariant above, spelled out: a single line
        carrying a legacy terminator in the MIDDLE is one line, it violates the
        contract, and nothing inside it may come back as a token. The gateway
        refuses the same bytes; before this was pinned, this end recovered two
        clean ids from them and reported nothing wrong."""
        p = tmp_path / "revoked.txt"
        p.write_bytes(b"ftk_a1\rftk_b2\n")
        assert rec.read_live_set(str(p)) == (set(), 1)

    def test_parse_events_valid(self):
        rows = [{"token_id": "ftk_1", "expires_at": "2026-07-04T13:00:00Z"}]
        evs = rec.parse_events(rows)
        assert len(evs) == 1 and evs[0].token_id == "ftk_1"

    def test_parse_events_drops_incomplete_and_malformed(self):
        rows = [
            {"token_id": "ftk_ok", "expires_at": "2026-07-04T13:00:00Z"},
            {"token_id": "ftk_no_exp"},                       # missing expires_at
            {"expires_at": "2026-07-04T13:00:00Z"},            # missing token_id
            {"token_id": "ftk_bad", "expires_at": "not-a-time"},  # unparseable
        ]
        evs = rec.parse_events(rows)
        assert [e.token_id for e in evs] == ["ftk_ok"]

    def test_rfc3339_accepts_z_and_offset(self):
        assert rec._parse_rfc3339("2026-07-04T13:00:00Z") is not None
        assert rec._parse_rfc3339("2026-07-04T13:00:00+00:00") is not None
        assert rec._parse_rfc3339("garbage") is None

    def test_logsql_query_filters_event_field_and_settles(self):
        q = rec.build_logsql_query(lookback_s=86400, settle_s=60)
        assert 'event:"federation_token_revoked"' in q
        assert f'log_type:"{rec.LOG_TYPE_EVIDENCE}"' in q, (
            "#1237: revocation-event query is not source-qualified — it would "
            "match a same-named event from any producer in the store"
        )
        assert "now-86400s" in q and "now-60s" in q

    def test_event_settle_is_the_larger_lag_never_the_sum(self):
        """#1238. VictoriaLogs ingestion lag and this pod's own kubelet
        ConfigMap projection lag are two independent delays measured from the
        SAME instant (tenant-api commits the ConfigMap before emitting the
        event), so the guard is max() — summing them would delay detection for
        no additional safety, and taking either alone reopens the other hazard.
        """
        assert rec.event_settle_s(60, 180) == 180     # projection lag dominates
        assert rec.event_settle_s(300, 180) == 300    # a raised --settle still wins
        assert rec.event_settle_s(60, 60) == 60
        # grace disabled => exactly the pre-#1238 behaviour, so an operator can
        # revert the mitigation without a redeploy of a different image.
        assert rec.event_settle_s(60, 0) == 60

    def test_failopen_query_uses_recent_window(self):
        # The fail-open gauge must reflect RECENT failures, not a 24h-old blip.
        q = rec.build_failopen_query(lookback_s=600, settle_s=60)
        assert "now-600s" in q and "now-60s" in q
        assert rec.GATEWAY_FAILOPEN_PHRASE in q
        # #1237: was a bare _msg phrase; now class- AND app-qualified so a
        # tenant request or a sidecar cannot forge the fail-open signal.
        assert f'log_type:"{rec.LOG_TYPE_GATEWAY_OP}"' in q
        assert f'app:"{rec.GATEWAY_APP}"' in q

    def test_rejected_query_is_stream_field_qualified(self):
        """#1235 / #1237. Qualify on the class Vector assigns to non-JSON gateway
        output (which request-derived content cannot reach — it parses as JSON
        into another class) AND on the Envoy container `app`, excluding the
        mtail / logrotate sidecars that share the pod and its label."""
        q = rec.build_rejected_query(lookback_s=600, settle_s=60)
        assert f'log_type:"{rec.LOG_TYPE_GATEWAY_OP}"' in q
        assert f'app:"{rec.GATEWAY_APP}"' in q
        assert rec.GATEWAY_REJECTED_PHRASE in q
        assert "now-600s" in q and "now-60s" in q

    def test_gateway_phrases_can_never_be_confused(self):
        """⛔ The gateway's revoked-set warnings mean DIFFERENT things about what
        is being let through, and each feeds its own gauge:

            reload failed → could not read it;  PREVIOUS set still enforced
            rejected      → read and refused it; PREVIOUS set still enforced
            missing       → file is gone;        the set is EMPTY (#1236)

        If any phrase were a substring of another, one query would count the
        other's rows and an incident responder would read the posture backwards.

        Enumerated from the module rather than hand-listed, so a FOURTH phrase is
        covered by this guard the moment it is declared — hand-listed pairs are
        how the class gets half-fixed.
        """
        phrases = _gateway_phrases()
        assert len(phrases) >= 3, f"expected at least 3 gateway phrases, got {sorted(phrases)}"
        for na, a in phrases.items():
            for nb, b in phrases.items():
                if na == nb:
                    continue
                assert a not in b, (
                    f"{na} is a substring of {nb} — the {nb} query would also count "
                    f"{na} rows, reporting the wrong enforcement posture"
                )

    def test_every_phrase_is_actually_emitted_by_the_shipped_lua(self):
        """The queries treat log text as an API. Nothing else binds them to the
        producer, so a reworded `logWarn` would silently zero a gauge — the
        failure mode where monitoring looks healthy because it stopped asking."""
        src = _shipped_lua()
        for name, phrase in _gateway_phrases().items():
            assert phrase in src, (
                f"the gateway no longer emits {name} ({phrase!r}); the gauge that "
                "counts it now reads zero forever"
            )

    def test_no_lua_warning_is_unclaimed_or_double_claimed(self):
        """⛔ The two guards above compare the reconciler's CONSTANTS. They are
        structurally blind to a warning the Lua emits that no constant describes
        — or worse, one that shares a prefix with an existing phrase and is
        therefore counted into that gauge silently.

        This asserts the other direction: every `revoked-set` warning the shipped
        filter can emit is claimed by EXACTLY ONE phrase. A new warning must
        either get its own gauge or deliberately extend an existing phrase's
        meaning — never drift into one by prefix.
        """
        literals = set(re.findall(r'"(federation: revoked-set[^"]*)"', _shipped_lua()))
        assert literals, "no revoked-set warning literals found — did the filter move?"
        phrases = _gateway_phrases()
        for lit in sorted(literals):
            owners = [n for n, p in phrases.items() if lit.startswith(p)]
            assert len(owners) == 1, (
                f"Lua emits {lit!r} but it is claimed by {owners or 'NO phrase'} — "
                "an unclaimed warning is invisible to every gauge; a doubly-claimed "
                "one is counted into a gauge that means something else"
            )

    def test_missing_query_is_stream_field_qualified(self):
        """#1236. The one gateway state where the enforcement plane is EMPTY.
        Qualified identically to its two siblings — see build_failopen_query for
        the honest boundary on what `app` does and does not prove."""
        q = rec.build_missing_query(lookback_s=600, settle_s=60)
        assert f'log_type:"{rec.LOG_TYPE_GATEWAY_OP}"' in q
        assert f'app:"{rec.GATEWAY_APP}"' in q
        assert rec.GATEWAY_MISSING_PHRASE in q
        assert "now-600s" in q and "now-60s" in q

    def test_every_metrics_field_reaches_the_exposition(self):
        """A gauge that is computed but never rendered is the same as no gauge.

        Mechanical over the object's own fields rather than a hand-listed set:
        the failure this catches is "a field was added to Metrics and to
        reconcile_once, but forgotten in render()", and a hand-listed assertion
        is blind to exactly the field nobody remembered.
        """
        m = rec.Metrics()
        exposed = [ln for ln in m.render().splitlines() if ln.startswith("# TYPE ")]
        assert len(exposed) == len(vars(m)), (
            f"Metrics carries {len(vars(m))} fields but render() exposes "
            f"{len(exposed)} series — {sorted(vars(m))} vs {exposed}"
        )

    def test_new_gauges_are_exposed(self):
        """Both enforcement-staleness gauges must reach /metrics — a gauge that
        is computed but never rendered is the same as no gauge."""
        m = rec.Metrics()
        m.live_set_rejected_lines = 3
        m.gateway_reload_rejected = 2
        m.gateway_revoked_set_missing = 4
        out = m.render()
        assert "federation_gateway_revoked_set_missing 4" in out
        assert "federation_revocation_live_set_rejected_lines 3" in out
        assert "federation_gateway_revoked_set_reload_rejected 2" in out

    def test_heartbeat_query_is_source_qualified(self):
        """#1234: the canary query filters on the event field AND the stream
        class it arrives on. `log_type` is written by the platform inside the
        Vector transform (from pre-merge locals), so a log payload cannot forge
        the class. The revocation query now carries the SAME qualifier (#1237
        closed); test_event_and_heartbeat_queries_share_stream_class pins the two
        together so they can never diverge into a silent all-clear."""
        q = rec.build_heartbeat_query(lookback_s=1800, settle_s=60)
        assert 'event:"federation_revocation_channel_heartbeat"' in q
        assert 'log_type:"federation_evidence"' in q, (
            "heartbeat query is not source-qualified — it would match a "
            "same-named event from any producer in the store"
        )
        assert "now-1800s" in q and "now-60s" in q

    def test_heartbeat_and_revocation_queries_are_distinct_filters(self):
        """The two evidence queries must not be substring-confusable: a test (or
        a future dispatcher) that routes on 'federation_token_revoked' must never
        also match the heartbeat query, or the canary would be fed the revocation
        rows and read healthy for the wrong reason."""
        hb = rec.build_heartbeat_query(lookback_s=1800, settle_s=60)
        ev = rec.build_logsql_query(lookback_s=86400, settle_s=60)
        assert "federation_token_revoked" not in hb
        assert rec.EVENT_HEARTBEAT not in ev

    def test_event_and_heartbeat_queries_share_stream_class(self):
        """⛔ #1237 load-bearing invariant. The revocation-event query and its
        liveness canary MUST filter on the same stream class. If they diverge, a
        misqualified event query could return zero (→ tamper_suspected 0, a clean
        all-clear) while the canary still matched (→ channel_up 1), so the very
        signal meant to catch a severed channel would pass. Pin them equal."""
        ev = rec.build_logsql_query(lookback_s=86400, settle_s=60)
        hb = rec.build_heartbeat_query(lookback_s=1800, settle_s=60)
        token = f'log_type:"{rec.LOG_TYPE_EVIDENCE}"'
        assert token in ev and token in hb, (
            "event and canary queries no longer share the evidence stream class "
            "— a divergence here is a silent all-clear over a blind query"
        )

    def test_gateway_app_matches_shipped_container_name(self):
        """The gateway_operational queries narrow on `app` == the Envoy container
        name, a hand-copied contract with the chart. A rename there would
        silently zero the fail-open / reload-rejected gauges (monitoring looks
        healthy because it stopped matching). Pin GATEWAY_APP against the shipped
        Deployment, mirroring the phrase pin above. Exact per-line match, not a
        substring — `- name: envoy` is a prefix of `- name: envoy-config`."""
        dep = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "helm", "federation-gateway", "templates", "deployment.yaml")
        container_names = [
            ln.strip() for ln in open(os.path.abspath(dep), encoding="utf-8")
        ]
        assert f"- name: {rec.GATEWAY_APP}" in container_names, (
            f"GATEWAY_APP={rec.GATEWAY_APP!r} no longer matches a container name "
            "in the federation-gateway Deployment — the app qualifier would zero "
            "the gateway_operational gauges"
        )

    def test_log_type_constants_match_vector_configmap(self):
        """LOG_TYPE_EVIDENCE / LOG_TYPE_GATEWAY_OP are hand-copied from the
        Vector pipeline's demux / evidence classifier. A rename on the Vector
        side would silently zero the queries that filter on them (monitoring
        looks healthy because it stopped matching). Pin BOTH literals against the
        shipped configmap — mirroring the GATEWAY_APP / phrase pins, so every
        hand-copied stream contract has a cross-file guard, not just `app`."""
        cm = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "helm", "vector", "templates", "configmap.yaml")
        src = open(os.path.abspath(cm), encoding="utf-8").read()
        assert f'"{rec.LOG_TYPE_GATEWAY_OP}"' in src, (
            f"LOG_TYPE_GATEWAY_OP={rec.LOG_TYPE_GATEWAY_OP!r} no longer appears "
            "in the Vector configmap — the gateway_operational queries would zero"
        )
        assert f'"{rec.LOG_TYPE_EVIDENCE}"' in src, (
            f"LOG_TYPE_EVIDENCE={rec.LOG_TYPE_EVIDENCE!r} no longer appears in "
            "the Vector configmap — the evidence queries would zero out"
        )


class TestMetrics:
    def test_render_has_all_series(self):
        text = rec.Metrics().render()
        for name in (
            "federation_revocation_tamper_suspected",
            "federation_revocation_last_reconcile_timestamp_seconds",
            "federation_revocation_events_dropped",
            "federation_revocation_reconcile_errors_total",
            "federation_gateway_revocation_load_errors",
            "federation_revocation_channel_up",
            "federation_revocation_heartbeats_seen",
        ):
            assert name in text

    def test_channel_up_defaults_to_down(self):
        """Before the first SUCCESSFUL pass the reconciler has no evidence the
        channel works, so the initial exposition must say 0 — an optimistic 1 is
        exactly the false-green this control exists to remove. The alert's
        `for: 15m` (~3 heartbeat periods) is the deploy grace window."""
        assert rec.Metrics().channel_up == 0
        assert "federation_revocation_channel_up 0" in rec.Metrics().render()


def _cfg(tmp_path) -> rec.Config:
    return rec.Config(
        victorialogs_url="http://vl:9428",
        revoked_file=str(tmp_path / "revoked.txt"),
        metrics_port=9099,
        interval_s=300,
        lookback_s=86400,
        settle_s=60,
        skew_margin_s=120,
        failopen_lookback_s=600,
        heartbeat_lookback_s=1800,
    )


def _query_router(*, events=(), failopen=(), heartbeats=(), rejected=(), missing=()):
    """Route each of reconcile_once's LogsQL queries to its own rows.

    Dispatches on the EVENT NAME each query filters on (not a loose substring of
    the whole query) so a future query can't silently be fed another's fixture —
    the sibling failure mode of substring matching that hides drift. The three
    gateway phrases are asserted elsewhere to be pairwise non-overlapping, so
    none can absorb another's rows.

    ⛔ Deliberately raises on an unrouted query rather than returning []. A new
    query added to reconcile_once would otherwise be fed an empty result here and
    every test would stay green while its gauge read zero in production."""
    def _q(_url, query, **_k):
        if rec.EVENT_HEARTBEAT in query:
            return list(heartbeats)
        if "federation_token_revoked" in query:
            return list(events)
        if rec.GATEWAY_REJECTED_PHRASE in query:
            return list(rejected)
        if rec.GATEWAY_MISSING_PHRASE in query:
            return list(missing)
        if rec.GATEWAY_FAILOPEN_PHRASE in query:
            return list(failopen)
        raise AssertionError(f"unrouted query: {query!r}")
    return _q


class TestEvidenceChannelCanary:
    """ADR-028 D1 / #1234: `channel_up` distinguishes 'no revocations happened'
    from 'the evidence path is severed' — the two states the reconciler otherwise
    observes identically (zero rows), the severed one publishing a clean
    all-clear."""

    def test_heartbeat_in_window_marks_channel_up(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        m = rec.Metrics()
        monkeypatch.setattr(rec, "query_victorialogs", _query_router(
            heartbeats=[{"event": rec.EVENT_HEARTBEAT}] * 6))
        rec.reconcile_once(cfg, m, now=1000.0)
        assert m.channel_up == 1
        assert m.heartbeats_seen == 6
        assert m.reconcile_errors_total == 0

    def test_no_heartbeat_in_window_marks_channel_down(self, tmp_path, monkeypatch):
        """The load-bearing case: the store is REACHABLE and holds no canary, so
        the channel really is severed. Note last_reconcile_ts still refreshes —
        the pass succeeded; it is channel_up, not staleness, that carries this."""
        cfg = _cfg(tmp_path)
        m = rec.Metrics()
        m.channel_up = 1                      # was healthy
        monkeypatch.setattr(rec, "query_victorialogs", _query_router())
        rec.reconcile_once(cfg, m, now=1000.0)
        assert m.channel_up == 0
        assert m.heartbeats_seen == 0
        assert m.last_reconcile_ts == 1000.0  # the pass itself succeeded
        assert m.reconcile_errors_total == 0

    def test_query_failure_holds_channel_up_at_previous_value(self, tmp_path, monkeypatch):
        """⛔ FAIL-CLOSED CONSISTENCY (the reason the heartbeat query sits inside
        reconcile_once's existing try block). A VictoriaLogs outage must NOT be
        reported as a dead evidence channel: that would page
        FederationRevocationEvidenceChannelDown for an outage that already
        surfaces correctly as ReconcileStale, and it would teach on-call that the
        channel alert means 'VictoriaLogs is down'. channel_up must HOLD its
        previous value — unreachable means unknown, not dead."""
        cfg = _cfg(tmp_path)
        m = rec.Metrics()
        m.channel_up = 1                      # last known good
        m.heartbeats_seen = 6
        m.last_reconcile_ts = 500.0

        def _boom(*_a, **_k):
            raise urllib_error()

        monkeypatch.setattr(rec, "query_victorialogs", _boom)
        rec.reconcile_once(cfg, m, now=1000.0)

        assert m.reconcile_errors_total == 1
        assert m.last_reconcile_ts == 500.0   # unchanged -> staleness alert fires
        assert m.channel_up == 1, (
            "a failed pass reported the evidence channel DEAD — the heartbeat "
            "query must be inside the fail-closed try block so the early return "
            "leaves channel_up untouched"
        )
        assert m.heartbeats_seen == 6         # likewise not zeroed by a failed pass

    def test_heartbeat_query_failure_alone_is_fail_closed(self, tmp_path, monkeypatch):
        """Narrower variant: only the HEARTBEAT query fails (the other two
        succeed). The whole pass must still fail closed — no partial update that
        would publish a fresh last_reconcile_ts alongside a stale channel_up."""
        cfg = _cfg(tmp_path)
        m = rec.Metrics()
        m.channel_up = 1
        m.last_reconcile_ts = 500.0

        def _q(_url, query, **_k):
            if rec.EVENT_HEARTBEAT in query:
                raise urllib_error()
            return []

        monkeypatch.setattr(rec, "query_victorialogs", _q)
        rec.reconcile_once(cfg, m, now=1000.0)

        assert m.reconcile_errors_total == 1
        assert m.last_reconcile_ts == 500.0
        assert m.channel_up == 1


class TestReconcileOnceFailClosed:
    def test_query_failure_does_not_emit_all_clear(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        m = rec.Metrics()
        m.tamper_suspected = 3          # a prior suspicion must NOT be cleared by a failed pass
        m.last_reconcile_ts = 500.0

        def _boom(*_a, **_k):
            raise urllib_error()

        monkeypatch.setattr(rec, "query_victorialogs", _boom)
        rec.reconcile_once(cfg, m, now=1000.0)

        assert m.reconcile_errors_total == 1
        assert m.last_reconcile_ts == 500.0     # unchanged -> staleness alert fires
        assert m.tamper_suspected == 3          # not falsely reset to 0

    def test_missing_live_file_is_benign_empty(self, tmp_path, monkeypatch):
        # A never-written revoked.txt (fresh deploy, no revocations yet) is NOT
        # an error: there are no events either, so an empty live set reconciles
        # clean. A genuinely down mount/pod is caught by `up`, not this read.
        cfg = _cfg(tmp_path)  # revoked.txt does not exist
        m = rec.Metrics()
        monkeypatch.setattr(rec, "query_victorialogs", lambda *_a, **_k: [])
        rec.reconcile_once(cfg, m, now=1000.0)
        assert m.reconcile_errors_total == 0
        assert m.tamper_suspected == 0
        assert m.last_reconcile_ts == 1000.0

    def test_happy_path_updates_metrics(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        (tmp_path / "revoked.txt").write_text("ftk_a11ce\n", encoding="utf-8")
        m = rec.Metrics()

        ev_rows = [
            {"token_id": "ftk_a11ce", "expires_at": _future_rfc3339(3600)},
            {"token_id": "ftk_b0b", "expires_at": _future_rfc3339(3600)},
        ]

        def _query(_url, query, **_k):
            return ev_rows if "federation_token_revoked" in query else [{}, {}]  # 2 fail-open warns

        monkeypatch.setattr(rec, "query_victorialogs", _query)
        now = _now()
        rec.reconcile_once(cfg, m, now=now)

        assert m.tamper_suspected == 1          # ftk_b0b is logged-live but absent
        assert m.gateway_load_errors == 2
        assert m.last_reconcile_ts == now
        assert m.reconcile_errors_total == 0
        assert m.events_dropped == 0            # both rows parsed cleanly

    def test_contract_violating_live_line_still_raises_tamper(self, tmp_path, monkeypatch):
        """#1235 / TRK-349 — the DETECTION half of the deliberate asymmetry.

        The gateway, meeting a contract-violating line, discards the reload and
        keeps its previous set (enforcement safety). This end must do the
        opposite: the id the bad line would have carried has to be ABSENT from
        the live set, so a token the log says is revoked comes back suspected.
        If someone ever "unifies" the two ends by having the reconciler also
        hold a previous set, this assertion is what goes red — otherwise the
        detection would vanish with every other test still green."""
        cfg = _cfg(tmp_path)
        (tmp_path / "revoked.txt").write_text(
            "ftk_a11ce ftk_b0b\n",      # contract-violating line
            encoding="utf-8", newline="\n")
        m = rec.Metrics()
        ev_rows = [{"token_id": "ftk_a11ce", "expires_at": _future_rfc3339(3600)}]
        monkeypatch.setattr(
            rec, "query_victorialogs",
            lambda _u, query, **_k: ev_rows if "federation_token_revoked" in query else [],
        )
        rec.reconcile_once(cfg, m, now=_now())

        assert m.tamper_suspected == 1          # logged-live, absent from the live set
        assert m.reconcile_errors_total == 0    # a rejected line is not a failed pass
        assert m.last_reconcile_ts != 0.0       # ...so the pass still completes

    def test_malformed_event_rows_are_counted_not_silently_dropped(self, tmp_path, monkeypatch):
        # Schema drift: rows carry the event marker but can't be parsed. They must
        # be COUNTED (events_dropped), not silently absorbed — a fully-drifted feed
        # would otherwise reconcile to a clean, healthy zero while a real un-revoke
        # went unseen and last_reconcile_ts kept refreshing (ADR-028 D3).
        cfg = _cfg(tmp_path)
        (tmp_path / "revoked.txt").write_text("", encoding="utf-8")
        m = rec.Metrics()
        ev_rows = [
            {"token_id": "ftk_ok", "expires_at": _future_rfc3339(3600)},
            {"token_id": "ftk_no_exp"},                    # malformed: missing expires_at
            {"expires_at": _future_rfc3339(3600)},          # malformed: missing token_id
        ]
        monkeypatch.setattr(
            rec, "query_victorialogs",
            lambda _u, query, **_k: ev_rows if "federation_token_revoked" in query else [],
        )
        rec.reconcile_once(cfg, m, now=_now())

        assert m.events_checked == 1            # only ftk_ok reconciled
        assert m.events_dropped == 2            # the two malformed rows are made visible
        assert m.reconcile_errors_total == 0    # malformed != a failed pass (that's fail-closed)


def urllib_error():
    import urllib.error

    return urllib.error.URLError("connection refused")


def _now() -> float:
    import time

    return time.time()


def _future_rfc3339(seconds: int) -> str:
    import datetime as dt

    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestProjectionGraceWiring:
    """#1238. The projection grace must narrow the EVENT query and NOTHING else.

    The three gateway-side gauges read a ``[now-600s, now-settle]`` window.
    Raising their near edge SHRINKS that window, which makes a single refused
    reload / missing read even harder to hold above its `for:` — the exact
    opposite of what #1238 does for them. So the obvious-looking simplification
    ("just raise --settle, it is the same idea") is a regression, and it is one
    that no assertion elsewhere in this file would notice: every other test
    either calls the query builders directly with explicit arguments or ignores
    the query text entirely. Hence this class.
    """

    def _captured(self, tmp_path, monkeypatch, settle_s, grace_s):
        cfg = dataclasses.replace(
            _cfg(tmp_path), settle_s=settle_s, projection_grace_s=grace_s)
        seen: list[str] = []

        def _q(_url, query, **_k):
            seen.append(query)
            return []

        monkeypatch.setattr(rec, "query_victorialogs", _q)
        rec.reconcile_once(cfg, rec.Metrics(), now=_now())
        return seen

    def test_grace_moves_the_event_window_only(self, tmp_path, monkeypatch):
        seen = self._captured(tmp_path, monkeypatch, settle_s=60, grace_s=180)

        events = [q for q in seen if "federation_token_revoked" in q]
        others = [q for q in seen if "federation_token_revoked" not in q]
        assert len(events) == 1
        # fail-open, heartbeat, rejected, missing — if this count changes, a new
        # query was added and its near edge has to be decided deliberately.
        assert len(others) == 4, f"unexpected query set: {others}"

        assert "now-180s" in events[0] and "now-60s" not in events[0], (
            "the event query must settle on max(settle, projection_grace)")
        for q in others:
            assert "now-60s" in q, (
                "a gateway-side window must keep the bare --settle; raising its "
                f"near edge shrinks the ~600s window it depends on: {q!r}")
            assert "now-180s" not in q, (
                f"projection grace leaked into a gateway-side window: {q!r}")

    def test_zero_grace_reproduces_the_pre_1238_windows(self, tmp_path, monkeypatch):
        """The mitigation is revertible from values alone — an operator who
        decides the grace is mis-tuned for their cluster can set it to 0 and get
        exactly the old behaviour back, without waiting for a chart release."""
        seen = self._captured(tmp_path, monkeypatch, settle_s=60, grace_s=0)
        for q in seen:
            assert "now-60s" in q
