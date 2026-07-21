"""Tests for scripts/tools/lint/check_threshold_reachability.py (TRK-337).

The gate asserts every alert-demanded threshold key is producible by the
platform-defaults path, grandfathering the 18 keys already dead when it landed.
Each test below pins one branch of that contract (they are regression pins for a
declared-but-unwired guard, not decoration):
  - the live repo is green (18 grandfathered, 0 new drift)
  - a NEW dead key fails --ci
  - the _critical reachability rule (base in supply) is honoured
  - the KNOWN_UNWIRED exit-lock fires when a grandfathered key is fixed or removed
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_reachability.py"

_spec = importlib.util.spec_from_file_location("check_threshold_reachability", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_has_no_new_drift():
    """Grandfathered keys are INFO; there must be zero NEW unreachable keys and
    zero stale exemptions on the real artifacts."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


def test_grandfather_list_is_exactly_the_18_dead_keys():
    """Every KNOWN_UNWIRED key must actually be dead on the real repo (else it is
    a stale exemption). Guards against the list drifting from reality."""
    result = gate.run_check()
    # all 18 show up as info; none as a STALE-EXEMPTION error
    assert len(result["infos"]) == len(gate.KNOWN_UNWIRED) == 18
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


# ── new drift is caught ───────────────────────────────────────────────────

def test_new_unreachable_key_is_an_error():
    """A freshly-added alert demanding an unsupplied, non-grandfathered key fails."""
    result = gate.run_check(
        demand={"oracle_sessions_active", "newpack_brand_new_metric"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        known_unwired={},
    )
    assert any("newpack_brand_new_metric" in e and "UNREACHABLE" in e
               for e in result["errors"]), result


def test_supplied_key_is_not_flagged():
    result = gate.run_check(
        demand={"oracle_sessions_active"},
        supply={"oracle_sessions_active"},
        deferred=set(),
        known_unwired={},
    )
    assert result["errors"] == []


# ── _critical reachability rule ───────────────────────────────────────────

def test_critical_key_reachable_when_base_is_supplied():
    """A `_critical` key rides resolveCriticalRows: reachable iff its BASE is in
    supply, even though the `_critical` key itself is not."""
    result = gate.run_check(
        demand={"mysql_connections_critical"},
        supply={"mysql_connections"},          # base only
        deferred=set(),
        known_unwired={},
    )
    assert result["errors"] == [], result


def test_critical_key_dead_when_base_missing():
    result = gate.run_check(
        demand={"mysql_connections_critical"},
        supply=set(),                          # base absent
        deferred=set(),
        known_unwired={},
    )
    assert any("mysql_connections_critical" in e for e in result["errors"])


def test_known_deferred_is_exempt():
    """A key whose threshold lives in a :core recording rule is deferred, not dead."""
    result = gate.run_check(
        demand={"container_cpu"},
        supply=set(),
        deferred={"container_cpu"},
        known_unwired={},
    )
    assert result["errors"] == []


# ── the exit-lock: the allowlist must shrink as fixes land ────────────────

def test_grandfathered_key_that_became_reachable_is_a_stale_exemption():
    """Once the real fix lands (key now supplied), leaving it in KNOWN_UNWIRED is
    a hard error — forces the list to shrink so it can't rot into a silent
    permanent exemption."""
    result = gate.run_check(
        demand={"oracle_wait_time_rate"},
        supply={"oracle_wait_time_rate"},      # fixed: now supplied
        deferred=set(),
        known_unwired={"oracle_wait_time_rate": "A: ..."},
    )
    assert any("STALE-EXEMPTION" in e and "REACHABLE" in e
               for e in result["errors"]), result


def test_grandfathered_key_no_longer_demanded_is_a_stale_exemption():
    """If the alert (and its key) was deleted, the exemption must go too."""
    result = gate.run_check(
        demand=set(),                          # no alert demands it anymore
        supply=set(),
        deferred=set(),
        known_unwired={"redis_keyspace_misses_ratio": "E: ..."},
    )
    assert any("STALE-EXEMPTION" in e and "demands it" in e
               for e in result["errors"]), result
