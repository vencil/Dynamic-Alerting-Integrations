"""Tests for scripts/tools/lint/check_orphan_recordings.py (TRK-339 WS2a / #1200).

The gate asserts every recording rule across the two rule trees has a consumer
(rule exprs / Grafana dashboards / observed map), grandfathering the records
already orphaned when it landed (#1204; 1 left after D-del). Each test pins one branch of that
contract:
  - the live repo is green (1 grandfathered + 1 exempt, 0 new drift)
  - a NEW orphan fails --ci
  - consumption is exact full-token identity — the substring/colon-adjacency
    trap (old-name-is-a-prefix-of-new-name) must NOT count as consumption
  - the KNOWN_ORPHANS exit-lock fires when a grandfathered record is rewired
    or deleted; EXEMPT_ORPHANS is existence-locked
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_orphan_recordings.py"

_spec = importlib.util.spec_from_file_location("check_orphan_recordings", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_has_no_new_drift():
    """Grandfathered orphans are INFO; zero NEW orphans and zero stale
    exemptions on the real artifacts."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


def test_grandfather_list_is_empty_and_nothing_is_orphaned():
    """#3778 deleted the last grandfathered orphan
    (tenant:mysql_connection_usage:ratio), so the list is empty and the real
    repo reports no orphan at all. Guards the list against regrowing."""
    result = gate.run_check()
    assert len(result["infos"]) == len(gate.KNOWN_ORPHANS) == 0
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


def test_sanity_anchor_mysql_connection_usage_percent_is_consumed():
    """#3778 anchor: the old orphan tenant:mysql_connection_usage:ratio is
    gone, and its replacement tenant:mysql_connection_usage:percent is a record
    the MariaDB connection alerts consume (so the scanner reaches it and does
    not report it)."""
    records = gate.collect_record_names()
    consumed = gate.collect_consumed_tokens()
    assert "tenant:mysql_connection_usage:ratio" not in records
    assert "tenant:mysql_connection_usage:percent" in records
    assert "tenant:mysql_connection_usage:percent" in consumed
    result = gate.run_check()
    assert not any("tenant:mysql_connection_usage:percent" in m
                   for m in result["infos"] + result["errors"])


def test_sanity_anchor_db2_lock_wait_time_is_not_orphan():
    """WS2a scan anchor: tenant:db2_lock_wait_time:rate5m is consumed by the
    DB2HighLockWaitTime alert and must never be reported."""
    records = gate.collect_record_names()
    consumed = gate.collect_consumed_tokens()
    assert "tenant:db2_lock_wait_time:rate5m" in records
    assert "tenant:db2_lock_wait_time:rate5m" in consumed
    result = gate.run_check()
    assert not any("tenant:db2_lock_wait_time:rate5m" in m
                   for m in result["errors"] + result["infos"])


# ── token identity: the substring / colon-adjacency trap ──────────────────

def test_metric_tokens_keep_colons_inside_one_token():
    """`tenant:alert_threshold:db2_lock_wait_time` is ONE token — a consumer
    of the threshold must not accidentally 'consume' the rate5m record whose
    name embeds the same substring."""
    toks = gate.metric_tokens(
        "tenant:db2_lock_wait_time:rate5m > on(tenant) group_left "
        "tenant:alert_threshold:db2_lock_wait_time")
    assert "tenant:db2_lock_wait_time:rate5m" in toks
    assert "tenant:alert_threshold:db2_lock_wait_time" in toks
    # the bare prefix is NOT a token of this expr
    assert "tenant:db2_lock_wait_time" not in toks
    assert "db2_lock_wait_time" not in toks


def test_prefix_token_does_not_count_as_consumption():
    """A consumer referencing only the old/prefix name must leave the
    longer record orphaned (substring matching would hide this drift)."""
    result = gate.run_check(
        record_names={"tenant:db2_lock_wait_time:rate5m"},
        consumed=gate.metric_tokens("tenant:alert_threshold:db2_lock_wait_time > 5"),
        known_orphans={},
        exempt={},
    )
    assert any("tenant:db2_lock_wait_time:rate5m" in e and "ORPHAN-RECORDING" in e
               for e in result["errors"]), result


def test_stop_tokens_are_not_consumers():
    """PromQL keywords/functions never count as consumed series names.
    (Label names like `tenant` do surface as tokens — harmless over-approx on
    the consumer side, since record names are colon-namespaced.)"""
    toks = gate.metric_tokens("rate(vector(1)[5m]) or absent(x) and sum(foo_metric)")
    assert toks == {"x", "foo_metric"}


# ── new drift is caught / consumed records are clean ──────────────────────

def test_new_orphan_is_an_error():
    result = gate.run_check(
        record_names={"tenant:new_thing:rate5m"},
        consumed=set(),
        known_orphans={},
        exempt={},
    )
    assert any("tenant:new_thing:rate5m" in e and "ORPHAN-RECORDING" in e
               for e in result["errors"]), result


def test_consumed_record_is_clean():
    result = gate.run_check(
        record_names={"tenant:new_thing:rate5m"},
        consumed={"tenant:new_thing:rate5m"},
        known_orphans={},
        exempt={},
    )
    assert result["errors"] == [] and result["infos"] == []


def test_grandfathered_orphan_is_info_not_error():
    result = gate.run_check(
        record_names={"tenant:old_orphan:ratio"},
        consumed=set(),
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert result["errors"] == []
    assert any("tenant:old_orphan:ratio" in i for i in result["infos"])


# ── the exit-lock: the ledger must shrink as fixes land ───────────────────

def test_grandfathered_record_that_gained_a_consumer_is_stale_exemption():
    result = gate.run_check(
        record_names={"tenant:old_orphan:ratio"},
        consumed={"tenant:old_orphan:ratio"},   # rewired: now consumed
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert any("STALE-EXEMPTION" in e and "consumer" in e
               for e in result["errors"]), result


def test_grandfathered_record_that_was_deleted_is_stale_exemption():
    result = gate.run_check(
        record_names=set(),                     # record deleted
        consumed=set(),
        known_orphans={"tenant:old_orphan:ratio": "#1204"},
        exempt={},
    )
    assert any("STALE-EXEMPTION" in e and "no longer defined" in e
               for e in result["errors"]), result


def test_exempt_record_is_never_reported_while_it_exists():
    """custom_recipe_info-style inventory series: consumer-less by design."""
    result = gate.run_check(
        record_names={"custom_recipe_info"},
        consumed=set(),
        known_orphans={},
        exempt={"custom_recipe_info": "inventory"},
    )
    assert result["errors"] == [] and result["infos"] == []


def test_exempt_record_that_was_deleted_is_stale_exemption():
    result = gate.run_check(
        record_names=set(),
        consumed=set(),
        known_orphans={},
        exempt={"custom_recipe_info": "inventory"},
    )
    assert any("STALE-EXEMPTION" in e and "EXEMPT_ORPHANS" in e
               for e in result["errors"]), result


# ── the CLI exit-code contract (main), independent of real artifacts ──────

def test_main_without_ci_is_report_only(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["some drift"], "infos": []})
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["a breach"], "infos": []})
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": [], "infos": ["12 known-orphan"]})
    assert gate.main(["--ci"]) == gate.EXIT_OK


def test_strip_strings_removes_backtick_raw_strings():
    """PromQL backtick raw strings must not leak tokens (Gemini review of #1214)."""
    from check_orphan_recordings import metric_tokens
    toks = metric_tokens('label_replace(kube_pod_info, "tenant", `some_fake_metric`, "ns", `(.+)`)')
    assert "some_fake_metric" not in toks
    assert "kube_pod_info" in toks
