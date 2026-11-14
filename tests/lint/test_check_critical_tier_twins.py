"""Tests for scripts/tools/lint/check_critical_tier_twins.py (#3658).

The gate ties the registry's `critical_of` declarations to the tenant rule
packs: one critical alert per declared tier, the twin of the warning alert.
The live repo is green; every check below has a synthetic case that turns
red when its rule is broken and green when it is restored (each case builds
on the same clean pack, so "restored" is the clean-pack test itself).
"""
from __future__ import annotations

import copy
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_critical_tier_twins.py"

_spec = importlib.util.spec_from_file_location("check_critical_tier_twins", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

PACK = "rule-packs/rule-pack-foo.yaml"
DECLARED = {"foo_load": "foo_load_critical", "foo_cpu": "foo_cpu_critical"}

_ANN = {"summary": "s", "description": "d"}


def _alert(name, expr, sev, mg, for_="5m", **extra_labels):
    labels = {"severity": sev, "maintenance": "mute", **extra_labels}
    if mg is not None:
        labels["metric_group"] = mg
    return {"alert": name, "expr": expr, "for": for_, "labels": labels,
            "annotations": dict(_ANN)}


def _thr(prefix, key, sev, metric):
    return {"record": f"{prefix}:alert_threshold:{key}",
            "expr": f'max by(tenant) (user_threshold{{component="foo", metric="{metric}", severity="{sev}"}})'}


def _core(sev):
    sel = f'tenant_version:alert_threshold:foo_cpu{{severity="{sev}"}}'
    dflt = f'tenant_version:alert_threshold:foo_cpu{{version="default", severity="{sev}"}}'
    return {"record": f"rp_foo:cpu_{sev}:core",
            "expr": f"(tenant_version:foo_cpu:v > on(tenant, version) group_left() {sel}) "
                    f"or (tenant_version:foo_cpu:v > on(tenant) group_left() {dflt})"}


def _clean_doc():
    """A pack with one (A)-form pair (foo_load) and one (B)-form pair (foo_cpu)."""
    w_load = ("tenant:foo_load:max > on(tenant) group_left tenant:alert_threshold:foo_load "
              "unless on(tenant) (user_state_filter{filter=\"maintenance\"} == 1)")
    c_load = w_load.replace("alert_threshold:foo_load ", "alert_threshold:foo_load_critical ")
    return {"groups": [
        {"name": "foo-normalization", "rules": [
            {"record": "tenant:foo_load:max", "expr": "max by(tenant) (foo_load)"},
            {"record": "tenant_version:foo_cpu:v", "expr": "max by(tenant, version) (foo_cpu)"},
        ]},
        {"name": "foo-threshold-normalization", "rules": [
            _thr("tenant", "foo_load", "warning", "load"),
            _thr("tenant", "foo_load_critical", "critical", "load"),
            {"record": "tenant_version:alert_threshold:foo_cpu",
             "expr": 'max by(tenant, version, severity) (user_threshold{component="foo", metric="cpu"})'},
            _core("warning"),
            _core("critical"),
        ]},
        {"name": "foo-alerts", "rules": [
            _alert("FooHighLoad", w_load, "warning", "foo_load"),
            _alert("FooHighLoadCritical", c_load, "critical", "foo_load"),
            _alert("FooHighCPU", "rp_foo:cpu_warning:core", "warning", "foo_cpu", "1m"),
            _alert("FooHighCPUCritical", "rp_foo:cpu_critical:core", "critical", "foo_cpu", "1m"),
            # a composite alert reading two warning tiers — no K's critical reader
            _alert("FooSystemBottleneck",
                   "(tenant:foo_load:max > tenant:alert_threshold:foo_load) and "
                   "(tenant_version:foo_cpu:v > tenant_version:alert_threshold:foo_cpu{severity=\"warning\"})",
                   "critical", None, "1m"),
        ]},
    ]}


def _find(doc, name):
    for g in doc["groups"]:
        for r in g["rules"]:
            if r.get("alert") == name or r.get("record") == name:
                return r
    raise KeyError(name)


def _run(doc, declared=None, for_exceptions=None):
    rules = gate.rules_of_document(PACK, doc)
    return gate.run_check(rules, dict(DECLARED if declared is None else declared),
                          {} if for_exceptions is None else for_exceptions)["errors"]


def _one_error(errors, *needles):
    assert errors, "expected a violation, got none"
    joined = "\n".join(errors)
    for n in needles:
        assert n in joined, joined
    return joined


# ---------------------------------------------------------------- live repo

def test_real_repo_is_green():
    result = gate.run_check()
    assert result["errors"] == []
    # anchors: the 3 kubernetes (B)-form pairs and the renamed mariadb groups
    for w in ("PodContainerHighCPU", "PodContainerHighMemory", "PodContainerCPUThrottled",
              "MariaDBHighConnections", "MariaDBHighThreadsRunning", "MariaDBReplicationLag"):
        assert w in result["twins"], result["twins"]


def test_real_repo_declarations_come_from_the_registry():
    declared = gate.declared_critical_bases()
    for base in ("container_cpu", "container_memory", "container_cpu_throttle",
                 "mysql_connection_usage_percent", "kafka_consumer_lag"):
        assert declared[base] == base + "_critical"


def test_for_exceptions_hold_on_the_real_repo():
    assert set(gate.FOR_EXCEPTIONS) == {
        "JVMHighGCPause", "JVMMemoryPressure", "JVMThreadPoolExhaustion",
        "NginxHighConnections", "NginxRequestRateSpike", "NginxConnectionBacklog"}


def test_cli_ci_exit_zero_on_repo():
    proc = subprocess.run([sys.executable, "-X", "utf8", str(_SCRIPT), "--ci"],
                          capture_output=True, text=True, encoding="utf-8",
                          cwd=REPO_ROOT, timeout=120)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------- synthetic

def test_clean_pack_is_green():
    assert _run(_clean_doc()) == []


def test_declared_without_rule():
    _one_error(_run(_clean_doc(), declared={**DECLARED, "foo_disk": "foo_disk_critical"}),
               "DECLARED-WITHOUT-RULE", "foo_disk_critical")


def test_rule_without_declaration_alert_and_record():
    errors = _run(_clean_doc(), declared={"foo_load": "foo_load_critical"})
    joined = _one_error(errors, "UNDECLARED-CRITICAL-TIER", "FooHighCPUCritical",
                        "rp_foo:cpu_critical:core")
    # undeclared is reported, and before anything else about that base
    assert errors[0].startswith("UNDECLARED-CRITICAL-TIER"), joined


def test_critical_threshold_record_without_declaration():
    doc = _clean_doc()
    doc["groups"][1]["rules"].append(_thr("tenant", "foo_orphan_critical", "critical", "orphan"))
    _one_error(_run(doc), "UNDECLARED-CRITICAL-TIER", "tenant:alert_threshold:foo_orphan_critical")


def test_undeclared_critical_reader_without_twin_reports_undeclared_first():
    doc = _clean_doc()
    doc["groups"][2]["rules"].append(_alert(
        "FooLoneCritical", "tenant:foo_load:max > tenant:alert_threshold:foo_lone_critical",
        "critical", "foo_lone"))
    errors = _run(doc)
    lone = [e for e in errors if "foo_lone" in e]
    assert lone and lone[0].startswith("UNDECLARED-CRITICAL-TIER"), errors
    assert any(e.startswith("NO-WARNING-TWIN") for e in lone), errors


def test_two_critical_readers_are_ambiguous():
    doc = _clean_doc()
    extra = copy.deepcopy(_find(doc, "FooHighLoadCritical"))
    extra["alert"] = "FooHighLoadCriticalToo"
    doc["groups"][2]["rules"].append(extra)
    _one_error(_run(doc), "AMBIGUOUS-CRITICAL-TIER", "foo_load")


def test_critical_reader_labelled_warning():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["labels"]["severity"] = "warning"
    _one_error(_run(doc), "CRITICAL-TIER-NOT-CRITICAL", "FooHighLoadCritical")


def test_no_warning_reader():
    doc = _clean_doc()
    _find(doc, "FooHighLoad")["labels"]["severity"] = "info"
    _one_error(_run(doc), "NO-WARNING-TWIN", "foo_load")


def test_a_form_threshold_record_wrong_shape():
    doc = _clean_doc()
    _find(doc, "tenant:alert_threshold:foo_load_critical")["expr"] = (
        'max by(tenant) (user_threshold{component="foo", metric="other", severity="critical"})')
    _one_error(_run(doc), "NOT-A-TWIN", "is not record 'tenant:alert_threshold:foo_load'")


def test_a_form_threshold_record_missing():
    doc = _clean_doc()
    rules = doc["groups"][1]["rules"]
    rules.remove(_find(doc, "tenant:alert_threshold:foo_load_critical"))
    _one_error(_run(doc), "NOT-A-TWIN", "is missing")


def test_a_form_base_record_needs_exactly_one_severity_warning():
    doc = _clean_doc()
    _find(doc, "tenant:alert_threshold:foo_load")["expr"] = (
        'max by(tenant) (user_threshold{component="foo", metric="load"})')
    _one_error(_run(doc), "NOT-A-TWIN", "0 `severity=\"warning\"` matcher")


def test_expr_with_one_more_difference():
    doc = _clean_doc()
    c = _find(doc, "FooHighLoadCritical")
    c["expr"] = c["expr"].replace("group_left", "group_right")
    _one_error(_run(doc), "NOT-A-TWIN", "group_left", "group_right")


def test_expr_with_another_length():
    doc = _clean_doc()
    c = _find(doc, "FooHighLoadCritical")
    c["expr"] = c["expr"] + " > 0"
    _one_error(_run(doc), "NOT-A-TWIN", "tokens vs")


def test_b_form_record_wrong():
    doc = _clean_doc()
    core = _find(doc, "rp_foo:cpu_critical:core")
    core["expr"] = core["expr"].replace('version="default", ', "")
    _one_error(_run(doc), "NOT-A-TWIN", "is not record 'rp_foo:cpu_warning:core'")


def test_b_form_record_without_a_warning_selector():
    doc = _clean_doc()
    core = _find(doc, "rp_foo:cpu_warning:core")
    core["expr"] = core["expr"].replace('severity="warning"', 'severity="info"')
    errors = _run(doc)
    _one_error(errors, "NOT-A-TWIN", "has no `severity=\"warning\"`")


def test_labels_differ_beyond_severity():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["labels"]["maintenance"] = "page"
    _one_error(_run(doc), "NOT-A-TWIN", "labels: differ beyond severity on maintenance")


def test_metric_group_is_not_the_key():
    doc = _clean_doc()
    for n in ("FooHighLoad", "FooHighLoadCritical"):
        _find(doc, n)["labels"]["metric_group"] = "load"
    _one_error(_run(doc), "NOT-A-TWIN", "metric_group", "'foo_load'")


def test_metric_group_missing():
    doc = _clean_doc()
    for n in ("FooHighLoad", "FooHighLoadCritical"):
        del _find(doc, n)["labels"]["metric_group"]
    _one_error(_run(doc), "NOT-A-TWIN", "carries None")


def test_for_differs_without_exception():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["for"] = "3m"
    _one_error(_run(doc), "NOT-A-TWIN", "for: '5m' vs '3m'")


def test_for_differs_with_exception_is_green():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["for"] = "3m"
    assert _run(doc, for_exceptions={"FooHighLoad": ("5m", "3m")}) == []


def test_for_exception_stale_when_for_is_equal():
    _one_error(_run(_clean_doc(), for_exceptions={"FooHighLoad": ("5m", "3m")}),
               "STALE-FOR-EXCEPTION", "FooHighLoad")


def test_for_exception_stale_when_pair_is_gone():
    _one_error(_run(_clean_doc(), for_exceptions={"BarGone": ("5m", "3m")}),
               "STALE-FOR-EXCEPTION", "BarGone")


def test_for_exception_mismatch():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["for"] = "2m"
    errors = _run(doc, for_exceptions={"FooHighLoad": ("5m", "3m")})
    _one_error(errors, "FOR-EXCEPTION-MISMATCH")
    _one_error(errors, "NOT-A-TWIN", "FOR_EXCEPTIONS['FooHighLoad'] says")


def test_annotation_keys_differ():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["annotations"]["runbook_url"] = "x"
    _one_error(_run(doc), "NOT-A-TWIN", "annotations", "runbook_url")


def test_annotation_values_may_differ():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["annotations"]["summary"] = "critical!"
    assert _run(doc) == []


def test_critical_name_is_not_warning_plus_critical():
    doc = _clean_doc()
    _find(doc, "FooHighLoadCritical")["alert"] = "FooLoadPage"
    _one_error(_run(doc), "NOT-A-TWIN", "must be named 'FooHighLoadCritical'")


def test_different_group():
    doc = _clean_doc()
    c = _find(doc, "FooHighLoadCritical")
    doc["groups"][2]["rules"].remove(c)
    doc["groups"].append({"name": "foo-critical-alerts", "rules": [c]})
    _one_error(_run(doc), "NOT-A-TWIN", "put both in the same group")


def test_composite_warning_candidate_does_not_misfire():
    """A composite warning alert reading foo_load (and foo_cpu) is a W
    candidate too; with the real twin present the gate stays green."""
    doc = _clean_doc()
    comp = _find(doc, "FooSystemBottleneck")
    comp["labels"]["severity"] = "warning"
    comp["labels"]["metric_group"] = "foo_system"
    assert _run(doc) == []


def test_composite_critical_alert_is_no_critical_reader():
    """FooSystemBottleneck is severity critical but reads only warning tiers."""
    rules = gate.rules_of_document(PACK, _clean_doc())
    ix = gate.Index(rules)
    comp = next(r for r in rules if r.name == "FooSystemBottleneck")
    assert ix.tiers(comp) == {("foo_load", "warning"), ("foo_cpu", "warning")}


def test_tier_classification():
    toks = gate.tokenize(
        'a > tenant:alert_threshold:k_critical or tenant_version:alert_threshold:k{severity="critical"} '
        'or tenant_version:alert_threshold:k{version="default", severity="warning"} '
        'or tenant:alert_threshold:k or tenant:alert_threshold:k_x')
    got = [(r.base, r.tier) for r in gate.threshold_reads(toks)]
    assert got == [("k", "critical"), ("k", "critical"), ("k", "warning"), ("k", "warning"),
                   ("k_x", "warning")]


@pytest.mark.parametrize("expr", [
    'tenant:alert_threshold:k{severity=~"crit.*"}',
    '{__name__="tenant:alert_threshold:k"}',
    'custom:alert_threshold:k',
    'tenant_version:alert_threshold:k{severity="\\x63ritical"}',
])
def test_unjudged_reference_fails_closed(expr):
    doc = _clean_doc()
    doc["groups"][2]["rules"].append(_alert("FooOdd", f"x > {expr}", "warning", "foo_odd"))
    _one_error(_run(doc), "UNJUDGED-REFERENCE", "FooOdd")


def test_transitive_record_read():
    doc = _clean_doc()
    doc["groups"][1]["rules"].append(
        {"record": "rp_foo:cpu_critical:outer", "expr": "rp_foo:cpu_critical:core > 0"})
    doc["groups"][2]["rules"].append(_alert(
        "FooOuter", "rp_foo:cpu_critical:outer", "critical", "foo_cpu", "1m"))
    _one_error(_run(doc), "AMBIGUOUS-CRITICAL-TIER", "FooOuter")


# ---------------------------------------------------------------- main()

def test_main_without_ci_is_report_only(monkeypatch):
    monkeypatch.setattr(gate, "run_check", lambda: {"errors": ["x"], "twins": []})
    assert gate.main([]) == 0


def test_main_ci_fails_on_errors(monkeypatch):
    monkeypatch.setattr(gate, "run_check", lambda: {"errors": ["x"], "twins": []})
    assert gate.main(["--ci"]) == 1


def test_main_crash_is_caller_error(monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(gate, "run_check", boom)
    assert gate.main(["--ci"]) == 2


# ------------------------------------------- adversarial-review regressions

def test_a_form_partial_swap_is_not_a_twin():
    """Every read of K in W must become K_critical in C: swapping only one of
    two reads left C on the warning threshold for the other."""
    doc = _clean_doc()
    w = _find(doc, "FooHighLoad")
    w["expr"] = ("tenant:foo_load:max > on(tenant) group_left tenant:alert_threshold:foo_load "
                 "and tenant:foo_load:max < on(tenant) group_left 2 * tenant:alert_threshold:foo_load")
    _find(doc, "FooHighLoadCritical")["expr"] = w["expr"].replace(
        "alert_threshold:foo_load ", "alert_threshold:foo_load_critical ", 1)
    _one_error(_run(doc), "NOT-A-TWIN", "still reads the warning tier of 'foo_load'")


def test_a_form_full_swap_of_two_reads_is_a_twin():
    doc = _clean_doc()
    w = _find(doc, "FooHighLoad")
    w["expr"] = ("tenant:foo_load:max > on(tenant) group_left tenant:alert_threshold:foo_load "
                 "and tenant:foo_load:max < on(tenant) group_left 2 * tenant:alert_threshold:foo_load")
    _find(doc, "FooHighLoadCritical")["expr"] = re.sub(
        r"alert_threshold:foo_load\b", "alert_threshold:foo_load_critical", w["expr"])
    assert _run(doc) == []


def test_a_both_tier_record_read_without_a_severity_matcher_is_unjudged():
    """`tenant_version:alert_threshold:foo_cpu` keeps severity as a dimension:
    read with no severity matcher it yields both tiers."""
    doc = _clean_doc()
    doc["groups"][2]["rules"].append(_alert(
        "FooCPUAny", "tenant_version:foo_cpu:v > on(tenant) group_left "
        "tenant_version:alert_threshold:foo_cpu", "critical", None))
    _one_error(_run(doc), "UNJUDGED-REFERENCE", "carries both tiers")


@pytest.mark.parametrize("order", ["ab", "ba"])
def test_a_record_cycle_is_judged_whatever_the_order(order):
    """A cycle's partial tier set is not cached: the extra critical-tier
    reader behind `r:b` is found whichever record comes first."""
    doc = _clean_doc()
    ra = {"record": "r:a", "expr": "r:b or tenant:alert_threshold:foo_load_critical"}
    rb = {"record": "r:b", "expr": "r:a"}
    doc["groups"][0]["rules"].extend([ra, rb] if order == "ab" else [rb, ra])
    doc["groups"][2]["rules"].append(_alert("FooViaB", "tenant:foo_load:max > r:b",
                                            "critical", "foo_load"))
    _one_error(_run(doc), "AMBIGUOUS-CRITICAL-TIER")


def test_a_record_of_another_pack_is_followed():
    """Record names are global: an alert in another pack reading a record
    that reads K's critical tier is a second critical-tier reader."""
    doc = _clean_doc()
    doc["groups"][0]["rules"].append(
        {"record": "r:crit", "expr": "tenant:alert_threshold:foo_load_critical"})
    other = {"groups": [{"name": "bar-alerts", "rules": [
        _alert("BarViaFoo", "tenant:foo_load:max > r:crit", "critical", "foo_load")]}]}
    rules = gate.rules_of_document(PACK, doc) + gate.rules_of_document(
        "rule-packs/rule-pack-bar.yaml", other)
    errors = gate.run_check(rules, dict(DECLARED), {})["errors"]
    _one_error(errors, "AMBIGUOUS-CRITICAL-TIER", "BarViaFoo")


def test_a_record_in_a_cycle_is_named_as_an_undeclared_reader():
    """The partial tier set a cycle cuts short is never stored: `r:b` reads
    the undeclared tier through `r:a`, and is named whichever is first."""
    doc = _clean_doc()
    doc["groups"][0]["rules"].extend([
        {"record": "r:a", "expr": "r:b or tenant:alert_threshold:bar_critical"},
        {"record": "r:b", "expr": "r:a"},
    ])
    joined = _one_error(_run(doc), "UNDECLARED-CRITICAL-TIER")
    assert "record 'r:b'" in joined, joined


@pytest.mark.parametrize("which", ["a-form", "b-form"])
def test_a_critical_record_with_other_labels_is_not_a_twin(which):
    """#3658 review: a record's `labels:` rewrites its series; a critical record
    that sets `tenant` (or anything) the warning record does not is no twin."""
    doc = _clean_doc()
    name = ("tenant:alert_threshold:foo_load_critical" if which == "a-form"
            else "rp_foo:cpu_critical:core")
    _find(doc, name)["labels"] = {"tenant": "other"}
    _one_error(_run(doc), "NOT-A-TWIN", "labels")
