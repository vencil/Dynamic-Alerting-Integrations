#!/usr/bin/env python3
"""test_runtime_audit.py — tests for da-tools runtime-audit (#747).

Coverage:
  - parse_declared_rules: alert/record names, type, multi-group, malformed
  - parse_runtime_rules: /api/v1/rules shape, non-success raises, skips unnamed
  - diff_rules: MISSING / UNHEALTHY / ORPHAN classification + group-scoping
                (orphan only within DECLARED groups) + clean case
  - RuntimeAuditor.exit_code: caller-error=2, --ci gating, --strict-orphan
  - RuntimeAuditor.run end-to-end via --runtime-json fixture (no live cluster)

Usage:
  pytest tests/ops/test_runtime_audit.py -v
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from textwrap import dedent

import pytest

import runtime_audit as ra


# ─── Helpers ──────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(content).lstrip(), encoding="utf-8")
    return path


def _rule_pack(tmp_path: Path, name: str, body: str) -> Path:
    return _write(tmp_path / f"rule-pack-{name}.yaml", body)


def _runtime(*rules: dict) -> dict:
    """Build a minimal /api/v1/rules success response from rule dicts.

    Each rule dict needs at least group/name; type/health/lastError default.
    """
    groups: dict[str, list] = {}
    for r in rules:
        groups.setdefault(r["group"], []).append({
            "name": r["name"],
            "type": r.get("type", "alerting"),
            "health": r.get("health", "ok"),
            "lastError": r.get("lastError", ""),
            "query": r.get("query", "vector(1)"),
        })
    return {
        "status": "success",
        "data": {"groups": [{"name": g, "rules": rs} for g, rs in groups.items()]},
    }


def _args(**kw) -> argparse.Namespace:
    base = dict(rule_packs_dir="rule-packs/", prometheus=None, runtime_json=None,
                strict_orphan=False, json=False, ci=False)
    base.update(kw)
    return argparse.Namespace(**base)


# ─── parse_declared_rules ─────────────────────────────────────────────


def test_parse_declared_alert_and_record(tmp_path):
    p = _rule_pack(tmp_path, "k8s", """
        groups:
          - name: k8s-normalization
            rules:
              - record: container:cpu:ratio
                expr: rate(x[5m])
          - name: k8s-alerts
            rules:
              - alert: HighCpu
                expr: container:cpu:ratio > 0.8
    """)
    declared = ra.parse_declared_rules([p])
    assert declared[("k8s-normalization", "container:cpu:ratio")]["type"] == "recording"
    assert declared[("k8s-alerts", "HighCpu")]["type"] == "alerting"
    assert len(declared) == 2


# #3206: the packs are read the way Prometheus' rule loader reads them — the
# loader this audit compares the runtime against. A pack it refuses is not
# "skipped" any more (Prometheus does not load ANY rule of it): ValueError,
# naming the pack and the loader's reason, the caller-error path.
@pytest.mark.parametrize("name, text, reason", [
    ("weird", "groups:\n- name: g\n  rules:\n  - expr: vector(1)\n"
              "  - alert: Real\n    expr: up == 0\n",
     "one of 'record' or 'alert' must be set"),
    ("list", "- just\n- a\n- list\n", "cannot unmarshal !!seq"),
    ("strgroups", 'groups: "not-a-list"\n', "cannot unmarshal !!str"),
    ("mixed", "groups:\n- not-a-dict-group\n- name: g\n  rules:\n"
              "  - not-a-dict-rule\n  - alert: Real\n    expr: up == 0\n",
     "cannot unmarshal !!str"),
    ("bad", "groups: [unterminated\n", "did not find expected"),
])
def test_parse_declared_a_pack_prometheus_refuses_raises_valueerror(tmp_path, name, text,
                                                                    reason):
    p = _write(tmp_path / f"rule-pack-{name}.yaml", text)
    with pytest.raises(ValueError) as exc_info:
        ra.parse_declared_rules([p])
    assert f"rule-pack-{name}.yaml" in str(exc_info.value)
    assert reason in str(exc_info.value)


# ─── parse_runtime_rules ──────────────────────────────────────────────


def test_parse_runtime_basic():
    rt = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "A", "type": "alerting", "health": "ok"},
        {"group": "g", "name": "B", "type": "recording", "health": "err",
         "lastError": "boom"},
    ))
    assert rt[("g", "A")]["health"] == "ok"
    assert rt[("g", "B")]["lastError"] == "boom"


def test_parse_runtime_non_success_raises():
    with pytest.raises(ValueError):
        ra.parse_runtime_rules({"status": "error", "error": "bad query"})


def test_parse_runtime_non_dict_raises():
    with pytest.raises(ValueError):
        ra.parse_runtime_rules(["not", "a", "dict"])


@pytest.mark.parametrize("body", [
    {"status": "success", "data": None},
    {"status": "success", "data": {}},
    {"status": "success", "data": {"groups": None}},
    {"status": "success", "data": {"groups": ["not-a-dict"]}},
    {"status": "success", "data": {"groups": [{"name": "g"}]}},
    {"status": "success", "data": {"groups": [{"name": "g", "rules": ["x", {"name": "A"}]}]}},
], ids=["data-null", "no-groups", "groups-null", "group-not-object", "no-rules",
        "rule-not-object"])
def test_parse_runtime_malformed_is_value_error(body):
    """#3133: an answer that is not a rules list used to read as "no rule
    loaded" (every declared rule then reported missing)."""
    with pytest.raises(ValueError):
        ra.parse_runtime_rules(body)


def test_parse_runtime_group_with_rules_null_is_empty():
    """#3331: `rules: null` is an empty group (a Go nil slice), not a
    malformed answer; a group without `rules` is malformed (above)."""
    rt = ra.parse_runtime_rules({"status": "success", "data": {"groups": [{"name": "g", "rules": None}]}})
    assert rt == {}


def test_parse_runtime_skips_unnamed_rule():
    rt = ra.parse_runtime_rules({
        "status": "success",
        "data": {"groups": [{"name": "g", "rules": [{"type": "alerting"}]}]},
    })
    assert rt == {}


# ─── diff_rules — the core ────────────────────────────────────────────


def test_diff_clean_when_all_declared_loaded_and_healthy():
    declared = {("g", "A"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime({"group": "g", "name": "A"}))
    assert ra.diff_rules(declared, runtime) == []


def test_diff_missing_when_declared_not_loaded():
    declared = {("g", "A"): {"type": "alerting"},
                ("g", "B"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime({"group": "g", "name": "A"}))
    findings = ra.diff_rules(declared, runtime)
    assert [(f.category, f.name) for f in findings] == [(ra.MISSING, "B")]


def test_diff_unhealthy_when_loaded_with_error():
    declared = {("g", "A"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "A", "health": "err", "lastError": "parse fail"}))
    findings = ra.diff_rules(declared, runtime)
    assert len(findings) == 1
    assert findings[0].category == ra.UNHEALTHY
    assert findings[0].detail == "parse fail"


def test_diff_unknown_health_not_flagged():
    # health=="unknown" is transient (loaded, not yet evaluated, e.g. just
    # after a reload). It must NOT be reported as UNHEALTHY — only "err" gates.
    declared = {("g", "A"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "A", "health": "unknown"}))
    assert ra.diff_rules(declared, runtime) == []


def test_diff_ok_health_not_flagged():
    declared = {("g", "A"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "A", "health": "ok"}))
    assert ra.diff_rules(declared, runtime) == []


def test_diff_orphan_only_within_declared_groups():
    # 'A' declared in group g; runtime also has a stale 'Stale' in g AND an
    # unrelated infra rule in group 'node-exporter' (NOT declared) — only the
    # in-declared-group orphan should be flagged.
    declared = {("g", "A"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "A"},
        {"group": "g", "name": "Stale"},
        {"group": "node-exporter", "name": "NodeDown"},
    ))
    findings = ra.diff_rules(declared, runtime)
    assert [(f.category, f.group, f.name) for f in findings] == [
        (ra.ORPHAN, "g", "Stale"),
    ]


def test_diff_combined_missing_unhealthy_orphan():
    declared = {("g", "Keep"): {"type": "alerting"},
                ("g", "Gone"): {"type": "alerting"},
                ("g", "Broken"): {"type": "alerting"}}
    runtime = ra.parse_runtime_rules(_runtime(
        {"group": "g", "name": "Keep"},
        {"group": "g", "name": "Broken", "health": "err", "lastError": "x"},
        {"group": "g", "name": "Orphan"},
    ))
    cats = sorted((f.category, f.name) for f in ra.diff_rules(declared, runtime))
    assert cats == [
        (ra.MISSING, "Gone"),
        (ra.ORPHAN, "Orphan"),
        (ra.UNHEALTHY, "Broken"),
    ]


# ─── RuntimeAuditor.exit_code ─────────────────────────────────────────


def test_exit_code_caller_error_is_2_even_without_ci():
    a = ra.RuntimeAuditor(_args(ci=False))
    a.caller_error = "cannot reach Prometheus"
    assert a.exit_code() == ra.EXIT_CALLER_ERROR


def test_exit_code_findings_do_not_gate_without_ci():
    a = ra.RuntimeAuditor(_args(ci=False))
    a.findings = [ra.Finding(ra.MISSING, "g", "A")]
    assert a.exit_code() == ra.EXIT_OK


def test_exit_code_missing_gates_in_ci():
    a = ra.RuntimeAuditor(_args(ci=True))
    a.findings = [ra.Finding(ra.MISSING, "g", "A")]
    assert a.exit_code() == ra.EXIT_VIOLATION


def test_exit_code_orphan_does_not_gate_by_default():
    a = ra.RuntimeAuditor(_args(ci=True))
    a.findings = [ra.Finding(ra.ORPHAN, "g", "A")]
    assert a.exit_code() == ra.EXIT_OK


def test_exit_code_orphan_gates_with_strict_orphan():
    a = ra.RuntimeAuditor(_args(ci=True, strict_orphan=True))
    a.findings = [ra.Finding(ra.ORPHAN, "g", "A")]
    assert a.exit_code() == ra.EXIT_VIOLATION


# ─── RuntimeAuditor.run end-to-end (offline via --runtime-json) ───────


def test_run_end_to_end_offline(tmp_path, capsys):
    _rule_pack(tmp_path, "k8s", """
        groups:
          - name: g
            rules:
              - alert: Keep
                expr: up == 0
              - alert: Gone
                expr: up == 1
    """)
    rt_file = tmp_path / "rules.json"
    rt_file.write_text(json.dumps(_runtime(
        {"group": "g", "name": "Keep"},
        {"group": "g", "name": "Orphan"},
    )), encoding="utf-8")

    a = ra.RuntimeAuditor(_args(
        rule_packs_dir=str(tmp_path), runtime_json=str(rt_file), ci=True))
    a.run()
    cats = sorted((f.category, f.name) for f in a.findings)
    assert cats == [(ra.MISSING, "Gone"), (ra.ORPHAN, "Orphan")]
    # MISSING gates in --ci; ORPHAN does not (no --strict-orphan).
    assert a.exit_code() == ra.EXIT_VIOLATION

    report = a.json_document()
    assert report["summary"]["missing"] == 1
    assert report["summary"]["orphan"] == 1
    assert report["summary"]["declared"] == 2


def test_main_json_exit_is_the_verdict(tmp_path, capsys, monkeypatch):
    """#3216: under --json the document goes out through run() and the
    run's exit is still the --ci verdict (MISSING gates: rc 1)."""
    import sys
    from _lib_exitcodes import emit_result
    _rule_pack(tmp_path, "k8s", """
        groups:
          - name: g
            rules:
              - alert: Gone
                expr: up == 1
    """)
    rt_file = tmp_path / "rules.json"
    rt_file.write_text(json.dumps(_runtime({"group": "g", "name": "Other"})), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["runtime_audit.py", "--rule-packs-dir", str(tmp_path),
                                      "--runtime-json", str(rt_file), "--json", "--ci"])
    assert emit_result(ra.main()) == ra.EXIT_VIOLATION
    assert json.loads(capsys.readouterr().out)["summary"]["missing"] == 1


@pytest.mark.parametrize("answer", [
    (None, "connection refused"),
    ({"status": "success", "data": None}, None),
], ids=["refused", "data-null"])
def test_run_prometheus_not_answered_is_caller_error(tmp_path, monkeypatch, answer):
    """#3133: --prometheus reads /api/v1/rules through the lib; an answer not
    read is exit 2, and ``data: null`` is no longer "no rule loaded"."""
    import _lib_prometheus
    _rule_pack(tmp_path, "k8s", """
        groups:
          - name: g
            rules:
              - alert: A
                expr: up == 0
    """)
    seen = []
    monkeypatch.setattr(_lib_prometheus, "http_get_json",
                        lambda url, timeout=15: (seen.append(url) or answer[0], answer[1]))
    a = ra.RuntimeAuditor(_args(rule_packs_dir=str(tmp_path), prometheus="http://p", ci=True))
    a.run()
    assert seen == ["http://p/api/v1/rules"]
    assert a.findings == [] and a.caller_error.startswith("Prometheus query failed: ")
    assert a.exit_code() == ra.EXIT_CALLER_ERROR


def test_run_no_runtime_source_is_caller_error(tmp_path):
    _rule_pack(tmp_path, "k8s", """
        groups:
          - name: g
            rules:
              - alert: A
                expr: up == 0
    """)
    a = ra.RuntimeAuditor(_args(rule_packs_dir=str(tmp_path)))
    a.run()
    assert a.caller_error is not None
    assert a.exit_code() == ra.EXIT_CALLER_ERROR


def test_run_empty_rule_packs_dir_is_caller_error(tmp_path):
    a = ra.RuntimeAuditor(_args(rule_packs_dir=str(tmp_path), runtime_json="x.json"))
    a.run()
    assert a.caller_error is not None
    assert a.exit_code() == ra.EXIT_CALLER_ERROR
