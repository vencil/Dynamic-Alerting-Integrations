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
        assert rec.parse_revoked_file("ftk_1\n ftk_2 \n\n") == {"ftk_1", "ftk_2"}

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
        assert "revoked-set reload failed" in q


class TestMetrics:
    def test_render_has_all_series(self):
        text = rec.Metrics().render()
        for name in (
            "federation_revocation_tamper_suspected",
            "federation_revocation_last_reconcile_timestamp_seconds",
            "federation_revocation_reconcile_errors_total",
            "federation_gateway_revocation_load_errors",
        ):
            assert name in text


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
    )


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
        (tmp_path / "revoked.txt").write_text("ftk_present\n", encoding="utf-8")
        m = rec.Metrics()

        ev_rows = [
            {"token_id": "ftk_present", "expires_at": _future_rfc3339(3600)},
            {"token_id": "ftk_gone", "expires_at": _future_rfc3339(3600)},
        ]

        def _query(_url, query, **_k):
            return ev_rows if "federation_token_revoked" in query else [{}, {}]  # 2 fail-open warns

        monkeypatch.setattr(rec, "query_victorialogs", _query)
        now = _now()
        rec.reconcile_once(cfg, m, now=now)

        assert m.tamper_suspected == 1          # ftk_gone is logged-live but absent
        assert m.gateway_load_errors == 2
        assert m.last_reconcile_ts == now
        assert m.reconcile_errors_total == 0


def urllib_error():
    import urllib.error

    return urllib.error.URLError("connection refused")


def _now() -> float:
    import time

    return time.time()


def _future_rfc3339(seconds: int) -> str:
    import datetime as dt

    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
