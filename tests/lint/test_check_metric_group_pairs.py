"""Tests for scripts/tools/lint/check_metric_group_pairs.py (TRK-339 WS2a / #1200; bug #1199).

The gate asserts every X/XCritical alert pair carries a shared `metric_group`
label (ADR-001 severity-dedup key). The 4 kubernetes pairs grandfathered
when it landed were fixed by #1199 (TRK-338). Each test pins one branch:
  - the live repo is green (0 grandfathered, 0 new drift; the k8s and MariaDB
    pairs PASS)
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


def test_grandfather_list_is_empty():
    """#1199 fixed the 4 kubernetes pairs; the exit-locked list can only
    shrink, so it stays empty."""
    result = gate.run_check()
    assert gate.KNOWN_UNGROUPED == {}
    assert result["infos"] == []


def test_sanity_anchor_k8s_pairs_detected_and_pass():
    """WS2a scan anchor: the four #1199 pairs are still detected on the real
    artifacts (the scanner reaches them) and now verdict PASS."""
    verdicts = gate.detect_pairs(gate.collect_alerts())
    for base in ("PodContainerHighCPU", "PodContainerHighMemory",
                 "PodContainerCPUThrottled", "ContainerOOMKilled"):
        assert verdicts.get(base) == "PASS", (base, verdicts.get(base))


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


# ── #3692: the group namespace across rule packs ──────────────────────────

def _ns(by_pack):
    return gate.group_namespace_errors(by_pack)


def test_same_group_in_two_packs_is_a_collision():
    """The measured bug: `liveness` in mariadb and mongodb let one pack's
    critical mute the other's warning for a tenant running both."""
    errs = _ns({"rule-pack-a.yaml": {"liveness": ["warning", "critical"]},
                "rule-pack-b.yaml": {"liveness": ["warning", "critical"]}})
    assert [e for e in errs if e.startswith("GROUP-COLLISION")], errs


def test_distinct_groups_per_pack_are_clean():
    assert _ns({"rule-pack-a.yaml": {"a_liveness": ["warning", "critical"]},
                "rule-pack-b.yaml": {"b_liveness": ["warning", "critical"]}}) == []


def test_slo_prefix_is_reserved_for_the_custom_alert_compiler():
    errs = _ns({"rule-pack-a.yaml": {"slo_latency": ["warning", "critical"]}})
    assert [e for e in errs if e.startswith("RESERVED-PREFIX")], errs


def test_group_without_a_critical_or_a_warning_suppresses_nothing():
    only_crit = _ns({"rule-pack-a.yaml": {"g": ["critical"]}})
    only_warn = _ns({"rule-pack-a.yaml": {"g": ["warning"]}})
    assert [e for e in only_crit if e.startswith("NO-DEDUP-GROUP")], only_crit
    assert [e for e in only_warn if e.startswith("NO-DEDUP-GROUP")], only_warn
    # The inhibit mutes severity="warning" only: an info member is no pair.
    info_only = _ns({"rule-pack-a.yaml": {"g": ["info", "critical"]}})
    assert [e for e in info_only if e.startswith("NO-DEDUP-GROUP")], info_only
    assert _ns({"rule-pack-a.yaml": {"g": ["warning", "critical"]}}) == []


def test_real_rule_packs_carry_no_templated_or_colliding_group():
    """Read through the real loader: every group the source packs carry is
    owned by one pack and pairs a critical with a warning/info."""
    by_pack = gate.collect_groups_by_pack()
    assert by_pack, "no rule pack read — the check would pass over nothing"
    assert any(by_pack.values()), "no metric_group read at all"
    assert _ns(by_pack) == []


def test_templated_group_is_left_out(tmp_path):
    """The compiler's templated `slo_{{ $labels.name }}` exists per alert
    instance: no group check applies to it."""
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "rule-packs" / "rule-pack-custom-alerts.yaml").write_text(
        "groups:\n- name: g\n  rules:\n  - alert: SloBurn\n    expr: vector(1)\n"
        "    labels:\n      severity: critical\n"
        "      metric_group: 'slo_{{ $labels.name }}'\n", encoding="utf-8")
    by_pack = gate.collect_groups_by_pack(tmp_path)
    assert by_pack == {"rule-pack-custom-alerts.yaml": {}}
    assert _ns(by_pack) == []


def test_only_the_compilers_slo_template_is_left_out(tmp_path):
    """A templated value of a hand-written pack is checked as written."""
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "rule-packs" / "rule-pack-x.yaml").write_text(
        "groups:\n- name: g\n  rules:\n  - alert: X\n    expr: vector(1)\n"
        "    labels:\n      severity: critical\n"
        "      metric_group: 'x_{{ $labels.db }}'\n", encoding="utf-8")
    errs = _ns(gate.collect_groups_by_pack(tmp_path))
    assert [e for e in errs if e.startswith("NO-DEDUP-GROUP")], errs
