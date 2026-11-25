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
  - within one metric_group the warning and critical members have one
    cluster scope (#1200): cluster/cluster and flat/flat pass, mixed is
    SCOPE-MISMATCH, an undeterminable scope is SCOPE-NOT-JUDGED, and the
    shipped tree is judged non-vacuously
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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


# ── #1200: one metric_group, one cluster scope ────────────────────────────
#
# The two dedup inhibit rules pair "both alerts carry da_cluster" and
# "neither does"; a per-cluster warning and a tenant-wide critical never
# pair. The scope of a member is check_cluster_identity's judgement of its
# RESULT, never the `# da_cluster: tenant-wide` declarations.

_C, _F = gate.check_cluster_identity.CLUSTER, gate.check_cluster_identity.FLAT


def _rows(*members, pack="rule-pack-x.yaml", group="x_load"):
    """(alert, severity, scopes) → the rows `collect_group_scopes` returns."""
    return [(pack, group, a, sev, frozenset(sc)) for a, sev, sc in members]


def _verdicts(rows):
    return [e.split(":", 1)[0] for e in gate.group_scope_errors(rows)]


def test_members_all_per_cluster_are_clean():
    assert _verdicts(_rows(("XHigh", "warning", {_C}), ("XDown", "critical", {_C}))) == []


def test_members_all_tenant_wide_are_clean():
    """flat/flat is allowed: one Prometheus gives both alerts the same
    external label (or none), and one of the two rules pairs them."""
    assert _verdicts(_rows(("XHigh", "warning", {_F}), ("XDown", "critical", {_F}))) == []


def test_per_cluster_warning_with_tenant_wide_critical_is_a_mismatch():
    errs = gate.group_scope_errors(
        _rows(("XHigh", "warning", {_C}), ("XDown", "critical", {_F})))
    assert len(errs) == 1 and errs[0].startswith("SCOPE-MISMATCH"), errs
    # the message names the pack, the group, and each alert with its scope
    for part in ("rule-pack-x.yaml", "'x_load'", "XHigh [warning] = cluster",
                 "XDown [critical] = flat", "da_cluster"):
        assert part in errs[0], (part, errs[0])
    assert _verdicts(_rows(("XHigh", "warning", {_F}),
                           ("XDown", "critical", {_C}))) == ["SCOPE-MISMATCH"]


def test_alert_whose_or_sides_differ_is_a_mismatch_by_itself():
    """Every member reads {cluster, flat}: the set over the group is one
    value, and the group is still broken — each alert fires both ways."""
    rows = _rows(("XHigh", "warning", {_C, _F}), ("XDown", "critical", {_C, _F}))
    errs = gate.group_scope_errors(rows)
    assert _verdicts(rows) == ["SCOPE-MISMATCH"] and "cluster+flat" in errs[0], errs


@pytest.mark.parametrize("unjudged", [{None}, set(), {_C, None}, {"something-new"}])
def test_scope_that_could_not_be_judged_is_a_failure_that_says_so(unjudged):
    """Paired with a member of ANY scope, and with itself: never "same"."""
    for other in ({_C}, {_F}, unjudged):
        rows = _rows(("XHigh", "warning", other), ("XDown", "critical", unjudged))
        errs = gate.group_scope_errors(rows)
        assert _verdicts(rows) == ["SCOPE-NOT-JUDGED"], errs
        assert "XDown [critical] = not judged" in errs[0] and "not a pass" in errs[0]


def test_info_member_is_not_held_to_the_scope():
    """The inhibit's source is critical and its target warning: an info
    alert neither mutes nor is muted, whatever its result carries."""
    rows = _rows(("XHigh", "warning", {_C}), ("XDown", "critical", {_C}),
                 ("XNote", "info", {_F}))
    assert _verdicts(rows) == []
    assert gate.dedup_members(rows) == {
        ("rule-pack-x.yaml", "x_load"): [("XHigh", "warning", "cluster"),
                                         ("XDown", "critical", "cluster")]}


def test_groups_are_judged_per_pack_and_per_group():
    rows = (_rows(("AHigh", "warning", {_C}), ("ADown", "critical", {_C}), group="a")
            + _rows(("BHigh", "warning", {_F}), ("BDown", "critical", {_F}), group="b"))
    assert _verdicts(rows) == []


def test_run_check_reports_scope_errors_and_what_it_judged():
    rows = _rows(("XHigh", "warning", {_C}), ("XDown", "critical", {_F}))
    result = gate.run_check(alerts={}, known_ungrouped={}, groups_by_pack={},
                            group_scopes=rows)
    assert [e.split(":", 1)[0] for e in result["errors"]] == ["SCOPE-MISMATCH"]
    assert result["scope_judged"] == (1, 2)


# Through the real readers: the rule loader, Prometheus' parser, the lint's judge.

_PER_CLUSTER = "max by (tenant, da_cluster) (db_up) == 0"
_TENANT_WIDE = 'max by (tenant) (user_threshold{metric="x"}) > 1'


def _pack(tmp_path, warning, critical, above_critical=""):
    (tmp_path / "rule-packs").mkdir()
    (tmp_path / "rule-packs" / "rule-pack-x.yaml").write_text(
        "groups:\n- name: g\n  rules:\n"
        "  - record: x:helper\n    expr: max by (tenant, da_cluster) (db_up)\n"
        f"  - alert: XHigh\n    expr: {warning}\n"
        "    labels:\n      severity: warning\n      metric_group: x_load\n"
        f"{above_critical}"
        f"  - alert: XDown\n    expr: {critical}\n"
        "    labels:\n      severity: critical\n      metric_group: x_load\n",
        encoding="utf-8")
    return gate.collect_group_scopes(tmp_path)


@pytest.mark.usefixtures("da_read_env")
@pytest.mark.parametrize("warning, critical, want", [
    (_PER_CLUSTER, _PER_CLUSTER, []),
    (_TENANT_WIDE, _TENANT_WIDE, []),
    (_PER_CLUSTER, _TENANT_WIDE, ["SCOPE-MISMATCH"]),
    (_TENANT_WIDE, _PER_CLUSTER, ["SCOPE-MISMATCH"]),
    # a recorded per-cluster series aggregated to the tenant
    (_PER_CLUSTER, "max by (tenant) (x:helper) == 0", ["SCOPE-MISMATCH"]),
    # one `or` side without the cluster
    (_PER_CLUSTER, f"({_PER_CLUSTER}) or ({_TENANT_WIDE})", ["SCOPE-MISMATCH"]),
    # a scalar has no labels to judge
    (_PER_CLUSTER, "time()", ["SCOPE-NOT-JUDGED"]),
])
def test_scope_of_a_synthetic_pack(tmp_path, warning, critical, want):
    rows = _pack(tmp_path, warning, critical)
    assert [r[2:4] for r in rows] == [("XHigh", "warning"), ("XDown", "critical")]
    assert _verdicts(rows) == want, gate.group_scope_errors(rows)


@pytest.mark.usefixtures("da_read_env")
def test_scope_is_the_result_not_the_tenant_wide_declaration(tmp_path):
    """A declaration excuses an inner grouping for check_cluster_identity; it
    says nothing about what the rule OUTPUTS. Both directions:

    * the MariaDBNoPrimary shape — an inner `count by (tenant)` declared, the
      result `by (tenant, da_cluster)` — is per cluster and pairs;
    * a critical declared tenant-wide whose result has lost the cluster does
      not pair with a per-cluster warning, declared or not."""
    declared = ("  # da_cluster: tenant-wide by-drops-cluster (tenant) — "
                "asked of all the tenant's clusters on purpose\n")
    inner = ("count by (tenant, da_cluster) (db_up) "
             "and on (tenant) count by (tenant) (db_up == 1)")
    rows = _pack(tmp_path, _PER_CLUSTER, inner, above_critical=declared)
    assert gate.check_cluster_identity.findings(tmp_path) == {}, "the declaration must be live"
    assert _verdicts(rows) == [], gate.group_scope_errors(rows)

    other = tmp_path / "other"
    other.mkdir()
    rows = _pack(other, _PER_CLUSTER, "max by (tenant) (db_up) == 0", above_critical=declared)
    assert gate.check_cluster_identity.findings(other) == {}, "the declaration must be live"
    assert _verdicts(rows) == ["SCOPE-MISMATCH"], gate.group_scope_errors(rows)


@pytest.mark.usefixtures("da_read_env")
def test_rule_whose_scope_row_does_not_line_up_is_not_judged(tmp_path, monkeypatch):
    """The scopes are attached by a rule's place in its file. A row of
    another name there, or none, is "not judged" — never a borrowed scope."""
    monkeypatch.setattr(gate.check_cluster_identity, "alert_result_scopes",
                        lambda root=None: [("rule-pack-x.yaml", 1, "Other", frozenset({_C}))])
    rows = _pack(tmp_path, _PER_CLUSTER, _PER_CLUSTER)
    assert [r[4] for r in rows] == [frozenset({None}), frozenset({None})]
    assert _verdicts(rows) == ["SCOPE-NOT-JUDGED"]


def test_shipped_tree_scope_check_is_not_vacuous():
    """Guard of the guard: the check judged real groups of the shipped packs,
    each member got a scope of its own (cluster or flat — none "not judged"),
    and every group the namespace check reads was judged here too."""
    rows = gate.collect_group_scopes()
    judged = gate.dedup_members(rows)
    n_members = sum(len(m) for m in judged.values())
    assert len(judged) > 0 and n_members > 0, "no metric_group judged for cluster scope"
    assert gate.run_check()["scope_judged"] == (len(judged), n_members)
    texts = {text for members in judged.values() for _a, _s, text in members}
    assert texts and texts <= {"cluster", "flat"}, texts
    by_pack = gate.collect_groups_by_pack()
    assert set(judged) == {(pack, mg) for pack, groups in by_pack.items() for mg in groups}
    assert gate.group_scope_errors(rows) == []


def test_shipped_declared_tenant_wide_alert_still_outputs_the_cluster():
    """MariaDBNoPrimary declares an inner tenant-wide read and is a critical
    member of `mariadb_liveness`: its RESULT is per cluster, like the
    warning it suppresses. Reading the declaration instead would flag it."""
    members = gate.dedup_members(gate.collect_group_scopes())[
        ("rule-pack-mariadb.yaml", "mariadb_liveness")]
    assert ("MariaDBNoPrimary", "critical", "cluster") in members, members
    declared, _malformed = gate.check_cluster_identity.declarations()
    assert any(f == "rule-pack-mariadb.yaml" for f, _n in declared)
