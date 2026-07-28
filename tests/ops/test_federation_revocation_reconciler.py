"""Tests for federation_revocation_reconciler.py (ADR-028 D1, #924).

The reconcile logic is pure (events + live set + now -> suspected list), so the
correctness core — un-revoke detection, clock-skew tolerance, expiry skip,
dedup — is unit-tested here. The fail-closed I/O contract (G1: a failed pass
never emits an all-clear) is tested by monkeypatching the I/O seams to raise.
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops")
sys.path.insert(0, _TOOLS_DIR)

import _federation_revocation_reconciler as rec  # noqa: E402

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
        assert "now-86400s" in q and "now-60s" in q

    def test_failopen_query_uses_recent_window(self):
        # The fail-open gauge must reflect RECENT failures, not a 24h-old blip.
        q = rec.build_failopen_query(lookback_s=600, settle_s=60)
        assert "now-600s" in q and "now-60s" in q
        assert rec.GATEWAY_FAILOPEN_PHRASE in q

    def test_rejected_query_is_stream_field_qualified(self):
        """#1235. The fail-open query is a bare phrase against the whole
        message and is therefore forgeable by anything that can get a matching
        string into the stream. A new query must not inherit that: qualify on
        the class Vector assigns to non-JSON gateway output, which
        request-derived content cannot reach."""
        q = rec.build_rejected_query(lookback_s=600, settle_s=60)
        assert 'log_type:"gateway_operational"' in q
        assert rec.GATEWAY_REJECTED_PHRASE in q
        assert "now-600s" in q and "now-60s" in q

    def test_the_two_gateway_phrases_can_never_be_confused(self):
        """⛔ The gateway's two revoked-set warnings mean OPPOSITE things.

        `reload failed` feeds the fail-open gauge and means "we could not read
        the list, traffic is going through". `rejected` means "we read it,
        refused it, and are still enforcing the previous one". If either
        phrase were a substring of the other, one query would count both and
        an incident responder would read the posture backwards.
        """
        a, b = rec.GATEWAY_FAILOPEN_PHRASE, rec.GATEWAY_REJECTED_PHRASE
        assert a != b
        assert a not in b and b not in a

    def test_both_phrases_are_actually_emitted_by_the_shipped_lua(self):
        """The queries above treat log text as an API. Nothing else binds them
        to the producer, so a reworded `logWarn` would silently zero both
        gauges — the failure mode where monitoring looks healthy because it
        stopped asking. Pin the literals against the shipped filter."""
        lua = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "helm", "federation-gateway", "files", "revoked_check.lua")
        src = open(os.path.abspath(lua), encoding="utf-8").read()
        assert rec.GATEWAY_FAILOPEN_PHRASE in src, \
            "the gateway no longer emits the fail-open phrase the reconciler counts"
        assert rec.GATEWAY_REJECTED_PHRASE in src, \
            "the gateway no longer emits the reload-refused phrase the reconciler counts"

    def test_new_gauges_are_exposed(self):
        """Both enforcement-staleness gauges must reach /metrics — a gauge that
        is computed but never rendered is the same as no gauge."""
        m = rec.Metrics()
        m.live_set_rejected_lines = 3
        m.gateway_reload_rejected = 2
        out = m.render()
        assert "federation_revocation_live_set_rejected_lines 3" in out
        assert "federation_gateway_revoked_set_reload_rejected 2" in out

    def test_heartbeat_query_is_source_qualified(self):
        """#1234: the canary query filters on the event field AND the stream
        class it arrives on. Source qualification is the point — the pre-existing
        revocation query keys on `event` alone across the whole store (the #1237
        debt), so any producer that ever logs that field name can spoof it. The
        heartbeat query starts qualified, and `log_type` is written by the
        platform inside the Vector transform (from pre-merge locals), so a log
        payload cannot forge it."""
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


def _query_router(*, events=(), failopen=(), heartbeats=(), rejected=()):
    """Route each of reconcile_once's LogsQL queries to its own rows.

    Dispatches on the EVENT NAME each query filters on (not a loose substring of
    the whole query) so a future query can't silently be fed another's fixture —
    the sibling failure mode of substring matching that hides drift. The two
    gateway phrases are checked most-specific-first and asserted elsewhere to be
    non-overlapping, so neither can absorb the other's rows."""
    def _q(_url, query, **_k):
        if rec.EVENT_HEARTBEAT in query:
            return list(heartbeats)
        if "federation_token_revoked" in query:
            return list(events)
        if rec.GATEWAY_REJECTED_PHRASE in query:
            return list(rejected)
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
