"""Tests for scripts/tools/lint/check_metric_group_pairs.py (TRK-339 WS2a / #1200; bug #1199).

The gate asserts every X/XCritical alert pair carries a shared `metric_group`
label (ADR-001 severity-dedup key), grandfathering the 4 kubernetes pairs
already violating when it landed (#1199 / TRK-338). Each test pins one branch:
  - the live repo is green (4 grandfathered, 0 new drift; MariaDB pair PASSes)
  - pairing needs BOTH the exact `Critical`-suffix name match AND severity
    corroboration (no false pairs from name coincidence)
  - MISSING_BOTH / MISSING_ONE / VALUE_MISMATCH all fail --ci when new
  - the KNOWN_UNGROUPED exit-lock fires when a grandfathered pair is fixed
    or stops being detected
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_metric_group_pairs.py"

_spec = importlib.util.spec_from_file_location("check_metric_group_pairs", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _pair(mg_w, mg_c, sev_w="warning", sev_c="critical", base="FooHigh"):
    """Synthetic alert map for one candidate pair."""
    labels_w = {"severity": sev_w}
    labels_c = {"severity": sev_c}
    if mg_w is not None:
        labels_w["metric_group"] = mg_w
    if mg_c is not None:
        labels_c["metric_group"] = mg_c
    return {base: labels_w, base + "Critical": labels_c}


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_has_no_new_drift():
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


def test_grandfather_list_is_exactly_the_4_k8s_pairs():
    result = gate.run_check()
    assert len(result["infos"]) == len(gate.KNOWN_UNGROUPED) == 4
    assert not any("STALE-EXEMPTION" in e for e in result["errors"])


def test_sanity_anchor_k8s_pairs_detected_missing_both():
    """WS2a scan anchor: the four #1199 pairs are detected AND verdicted
    MISSING_BOTH on the real artifacts (the scanner actually reaches them)."""
    verdicts = gate.detect_pairs(gate.collect_alerts())
    for base in ("PodContainerHighCPU", "PodContainerHighMemory",
                 "PodContainerCPUThrottled", "ContainerOOMKilled"):
        assert verdicts.get(base) == "MISSING_BOTH", (base, verdicts.get(base))
        assert base in gate.KNOWN_UNGROUPED


def test_sanity_anchor_mariadb_pair_passes():
    """WS2a scan anchor: MariaDBHighConnections / ...Critical share
    metric_group and must be verdicted PASS (never reported)."""
    verdicts = gate.detect_pairs(gate.collect_alerts())
    assert verdicts.get("MariaDBHighConnections") == "PASS"
    result = gate.run_check()
    assert not any("MariaDBHighConnections" in m
                   for m in result["errors"] + result["infos"])


# ── pairing detection: exact name + severity corroboration ────────────────

def test_pair_needs_critical_severity_on_the_critical_side():
    """Name coincidence without the severity convention is NOT an upgrade
    pair (keeps false pairs out of the contract)."""
    alerts = _pair("g", "g", sev_c="warning")
    assert gate.detect_pairs(alerts) == {}


def test_pair_needs_base_severity_warning_or_info():
    alerts = _pair("g", "g", sev_w="critical")
    assert gate.detect_pairs(alerts) == {}
    # info-severity base is a legitimate upgrade pair
    alerts = _pair("g", "g", sev_w="info")
    assert gate.detect_pairs(alerts) == {"FooHigh": "PASS"}


def test_single_alert_without_critical_twin_is_out_of_scope():
    """Singles (~114 on the real repo) are deliberately NOT checked or
    ledgered — the contract is 'paired but unlabelled', nothing more."""
    alerts = {"LonelyAlert": {"severity": "warning"}}  # no metric_group
    result = gate.run_check(alerts=alerts, known_ungrouped={})
    assert result["errors"] == [] and result["infos"] == []


# ── the three violating verdicts fail --ci when new ───────────────────────

def test_missing_both_is_an_error():
    result = gate.run_check(alerts=_pair(None, None), known_ungrouped={})
    assert any("MISSING_BOTH" in e and "FooHigh" in e for e in result["errors"])


def test_missing_one_is_an_error():
    result = gate.run_check(alerts=_pair("g", None), known_ungrouped={})
    assert any("MISSING_ONE" in e for e in result["errors"])


def test_value_mismatch_is_an_error():
    result = gate.run_check(alerts=_pair("g1", "g2"), known_ungrouped={})
    assert any("VALUE_MISMATCH" in e for e in result["errors"])


def test_matching_pair_is_clean():
    result = gate.run_check(alerts=_pair("g", "g"), known_ungrouped={})
    assert result["errors"] == [] and result["infos"] == []


def test_grandfathered_pair_is_info_not_error():
    result = gate.run_check(alerts=_pair(None, None),
                            known_ungrouped={"FooHigh": "#1199"})
    assert result["errors"] == []
    assert any("FooHigh" in i for i in result["infos"])


# ── the exit-lock: the ledger must shrink as fixes land ───────────────────

def test_grandfathered_pair_that_got_fixed_is_stale_exemption():
    result = gate.run_check(alerts=_pair("g", "g"),
                            known_ungrouped={"FooHigh": "#1199"})
    assert any("STALE-EXEMPTION" in e and "fix landed" in e
               for e in result["errors"]), result


def test_grandfathered_pair_no_longer_detected_is_stale_exemption():
    """Pair renamed/deleted (or severity convention changed) → the ledger
    entry must go too."""
    result = gate.run_check(alerts={}, known_ungrouped={"FooHigh": "#1199"})
    assert any("STALE-EXEMPTION" in e and "detected anymore" in e
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
                        lambda: {"errors": [], "infos": ["4 known-ungrouped"]})
    assert gate.main(["--ci"]) == gate.EXIT_OK
