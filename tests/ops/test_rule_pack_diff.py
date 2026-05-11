#!/usr/bin/env python3
"""test_rule_pack_diff.py — tests for da-tools rule-pack-diff (#405 Cat D).

Coverage:
  - load_rule_pack: valid YAML / missing file / invalid YAML / non-dict top-level
  - extract_rules: alert + record indexing, group context, name-only de-dup
  - _label_keys / _label_value_diff: helpers
  - _classify_modification: all change types covered
  - _is_breaking: classification logic for each breaking category
  - diff_packs: added / removed / modified / breaking categorisation
  - render_text: human-readable output emission
  - compute_exit_code: --ci semantics
  - main() end-to-end via argv with file fixtures

Usage:
  pytest tests/ops/test_rule_pack_diff.py -v
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

import rule_pack_diff as rpd


# ─── Fixtures: minimal valid rule pack snippets ───────────────────────


@pytest.fixture
def empty_pack(tmp_path: Path) -> Path:
    p = tmp_path / "empty.yaml"
    p.write_text("groups: []\n", encoding="utf-8")
    return p


def _write_pack(path: Path, content: str) -> Path:
    """Write a dedented YAML pack snippet to path."""
    path.write_text(dedent(content).lstrip(), encoding="utf-8")
    return path


# ─── load_rule_pack ───────────────────────────────────────────────────


def test_load_rule_pack_valid(tmp_path):
    p = _write_pack(tmp_path / "v.yaml", """
        groups:
          - name: g1
            rules:
              - alert: A1
                expr: up == 0
                labels:
                  severity: warning
    """)
    data = rpd.load_rule_pack(p)
    assert data is not None
    assert data["groups"][0]["name"] == "g1"


def test_load_rule_pack_missing_file(tmp_path, capsys):
    data = rpd.load_rule_pack(tmp_path / "nonexistent.yaml")
    assert data is None
    err = capsys.readouterr().err
    assert "cannot read" in err


def test_load_rule_pack_invalid_yaml(tmp_path, capsys):
    p = tmp_path / "broken.yaml"
    p.write_text("groups: [unclosed list\n", encoding="utf-8")
    data = rpd.load_rule_pack(p)
    assert data is None
    err = capsys.readouterr().err
    assert "invalid YAML" in err


def test_load_rule_pack_top_level_not_mapping(tmp_path, capsys):
    """YAML that parses but to a list / string / None must be rejected."""
    p = tmp_path / "list.yaml"
    p.write_text("- just\n- a\n- list\n", encoding="utf-8")
    data = rpd.load_rule_pack(p)
    assert data is None
    err = capsys.readouterr().err
    assert "did not parse to a YAML mapping" in err


# ─── extract_rules ────────────────────────────────────────────────────


def test_extract_rules_indexes_alerts_and_records(tmp_path):
    p = _write_pack(tmp_path / "v.yaml", """
        groups:
          - name: g1
            rules:
              - alert: A1
                expr: up == 0
              - record: r1
                expr: rate(x[5m])
          - name: g2
            rules:
              - alert: A2
                expr: down == 1
    """)
    pack = rpd.load_rule_pack(p)
    index = rpd.extract_rules(pack)
    assert set(index.keys()) == {"A1", "r1", "A2"}
    # Group context preserved
    assert index["A1"][0]["_group"] == "g1"
    assert index["A2"][0]["_group"] == "g2"
    # Kind recorded
    assert index["A1"][0]["_kind"] == "alert"
    assert index["r1"][0]["_kind"] == "record"


def test_extract_rules_empty_groups(empty_pack):
    pack = rpd.load_rule_pack(empty_pack)
    assert rpd.extract_rules(pack) == {}


def test_extract_rules_handles_missing_groups_key(tmp_path):
    p = tmp_path / "v.yaml"
    p.write_text("other_key: stuff\n", encoding="utf-8")
    pack = rpd.load_rule_pack(p)
    assert rpd.extract_rules(pack) == {}


def test_extract_rules_handles_null_groups(tmp_path):
    p = tmp_path / "v.yaml"
    p.write_text("groups: null\n", encoding="utf-8")
    pack = rpd.load_rule_pack(p)
    assert rpd.extract_rules(pack) == {}


def test_extract_rules_skips_malformed_entries(tmp_path):
    """Non-dict entries in groups[] or rules[] are skipped, not crashed on."""
    p = _write_pack(tmp_path / "v.yaml", """
        groups:
          - name: g1
            rules:
              - alert: A1
                expr: up == 0
              - "not a dict, just a string"
              - record: r1
                expr: x
          - "junk top-level"
    """)
    pack = rpd.load_rule_pack(p)
    index = rpd.extract_rules(pack)
    assert set(index.keys()) == {"A1", "r1"}


def test_extract_rules_skips_rules_without_name(tmp_path):
    p = _write_pack(tmp_path / "v.yaml", """
        groups:
          - name: g1
            rules:
              - expr: up == 0
                annotations: {summary: 'has expr but no alert: nor record:'}
              - alert: A1
                expr: x
    """)
    pack = rpd.load_rule_pack(p)
    index = rpd.extract_rules(pack)
    assert set(index.keys()) == {"A1"}


# ─── _classify_modification / _is_breaking ────────────────────────────


def test_classify_no_change():
    r = {"expr": "up", "labels": {"severity": "warning"}}
    change = rpd._classify_modification(dict(r, _kind="alert"), dict(r, _kind="alert"))
    assert not any(
        [
            change["expr_changed"],
            change["labels_added"],
            change["labels_removed"],
            change["label_values_changed"],
            change["annotation_changed"],
            change["for_changed"],
            change["kind_changed"],
        ]
    )
    assert not rpd._is_breaking(change)


def test_classify_label_added_is_breaking():
    v1 = {"expr": "up", "labels": {"severity": "warning"}, "_kind": "alert"}
    v2 = {
        "expr": "up",
        "labels": {"severity": "warning", "team": "sre"},
        "_kind": "alert",
    }
    change = rpd._classify_modification(v1, v2)
    assert change["labels_added"] == ["team"]
    assert rpd._is_breaking(change), (
        "Adding a label key changes the matcher surface — breaking"
    )


def test_classify_label_removed_is_breaking():
    v1 = {"expr": "up", "labels": {"severity": "warning", "team": "sre"}, "_kind": "alert"}
    v2 = {"expr": "up", "labels": {"severity": "warning"}, "_kind": "alert"}
    change = rpd._classify_modification(v1, v2)
    assert change["labels_removed"] == ["team"]
    assert rpd._is_breaking(change)


def test_classify_label_value_changed_is_breaking():
    v1 = {"expr": "up", "labels": {"severity": "warning"}, "_kind": "alert"}
    v2 = {"expr": "up", "labels": {"severity": "critical"}, "_kind": "alert"}
    change = rpd._classify_modification(v1, v2)
    assert change["label_values_changed"] == {"severity": ("warning", "critical")}
    assert rpd._is_breaking(change), (
        "AM matchers using `severity=\"warning\"` will not match v2's `critical`"
    )


def test_classify_expr_only_change_not_breaking():
    """Expression edits are flagged for review but not auto-breaking
    (semantic equivalence is undecidable)."""
    v1 = {"expr": "up == 0", "labels": {"severity": "warning"}, "_kind": "alert"}
    v2 = {"expr": "absent(up)", "labels": {"severity": "warning"}, "_kind": "alert"}
    change = rpd._classify_modification(v1, v2)
    assert change["expr_changed"]
    assert not rpd._is_breaking(change), (
        "Expression change without label-schema change is informational, not breaking"
    )


def test_classify_annotation_change_not_breaking():
    v1 = {"expr": "up", "labels": {"severity": "warning"}, "annotations": {"summary": "A"}, "_kind": "alert"}
    v2 = {"expr": "up", "labels": {"severity": "warning"}, "annotations": {"summary": "B"}, "_kind": "alert"}
    change = rpd._classify_modification(v1, v2)
    assert change["annotation_changed"]
    assert not rpd._is_breaking(change)


def test_classify_kind_swap_is_breaking():
    """alert → record (or vice versa) is breaking: silencer matchers don't
    apply to recording rules at all."""
    v1 = {"expr": "up", "labels": {"severity": "warning"}, "_kind": "alert"}
    v2 = {"expr": "up", "labels": {"severity": "warning"}, "_kind": "record"}
    change = rpd._classify_modification(v1, v2)
    assert change["kind_changed"]
    assert rpd._is_breaking(change)


# ─── diff_packs end-to-end ────────────────────────────────────────────


def _pack_from(yaml_text: str) -> dict:
    """Helper: parse inline YAML string to dict for diff_packs input."""
    import yaml
    return yaml.safe_load(dedent(yaml_text).lstrip())


def test_diff_no_changes():
    pack = _pack_from("""
        groups:
          - name: g
            rules:
              - alert: A
                expr: up
                labels: {severity: warning}
    """)
    r = rpd.diff_packs(pack, pack)
    assert r["added"] == []
    assert r["removed"] == []
    assert r["modified"] == []
    assert r["breaking_modifications"] == []


def test_diff_added_alert():
    v1 = _pack_from("""
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: warning}}]}
    """)
    v2 = _pack_from("""
        groups:
          - {name: g, rules: [
              {alert: A1, expr: up, labels: {severity: warning}},
              {alert: A2, expr: down, labels: {severity: critical}}
          ]}
    """)
    r = rpd.diff_packs(v1, v2)
    assert r["added"] == ["A2"]
    assert r["added_alert_names"] == ["A2"]
    assert r["removed"] == []


def test_diff_removed_alert():
    v1 = _pack_from("""
        groups:
          - {name: g, rules: [
              {alert: A1, expr: up, labels: {severity: warning}},
              {alert: A2, expr: down, labels: {severity: critical}}
          ]}
    """)
    v2 = _pack_from("""
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: warning}}]}
    """)
    r = rpd.diff_packs(v1, v2)
    assert r["removed"] == ["A2"]
    assert r["removed_alert_names"] == ["A2"]


def test_diff_breaking_label_schema_change():
    v1 = _pack_from("""
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: warning}}]}
    """)
    v2 = _pack_from("""
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: warning, team: sre}}]}
    """)
    r = rpd.diff_packs(v1, v2)
    assert len(r["modified"]) == 1
    assert len(r["breaking_modifications"]) == 1
    assert r["counts"]["breaking"] == 1


def test_diff_record_rules_not_in_alert_subsets():
    """Recording rules tracked separately from alerts: appear in
    added_record_names (not added_alert_names) so customers can see
    both classes — silencer impact (alerts) vs downstream-expr-break
    impact (records) — without merging them."""
    v1 = _pack_from("""
        groups:
          - name: g
            rules:
              - record: r1
                expr: rate(x[5m])
    """)
    v2 = _pack_from("""
        groups:
          - name: g
            rules:
              - record: r1
                expr: rate(x[5m])
              - record: r2
                expr: rate(y[5m])
    """)
    r = rpd.diff_packs(v1, v2)
    assert r["added"] == ["r2"]
    assert r["added_alert_names"] == [], (
        "Recording rule additions should not appear in added_alert_names"
    )
    assert r["added_record_names"] == ["r2"], (
        "Recording rule additions should appear in added_record_names"
    )


def test_diff_empty_pack_pair_cold_start():
    """Both inputs are empty rule packs (groups: []) — the cold-start
    scenario for a brand-new pack being first-staged. Tool must handle
    gracefully without crashing."""
    empty1 = _pack_from("groups: []\n")
    empty2 = _pack_from("groups: []\n")
    r = rpd.diff_packs(empty1, empty2)
    assert r["added"] == []
    assert r["removed"] == []
    assert r["modified"] == []
    assert r["breaking_modifications"] == []
    assert r["added_alert_names"] == []
    assert r["removed_alert_names"] == []
    assert r["added_record_names"] == []
    assert r["removed_record_names"] == []
    assert r["counts"]["v1_total_rules"] == 0
    assert r["counts"]["v2_total_rules"] == 0


def test_diff_removed_recording_rule_in_record_subset():
    """Removed recording rule must appear in removed_record_names so
    customers see it in the upgrade audit — without this, removed
    records are invisible to text-output users and CI gate alike."""
    v1 = _pack_from("""
        groups:
          - name: g
            rules:
              - record: tenant:foo:rate5m
                expr: rate(x[5m])
              - alert: A
                expr: up
                labels: {severity: warning}
    """)
    v2 = _pack_from("""
        groups:
          - name: g
            rules:
              - alert: A
                expr: up
                labels: {severity: warning}
    """)
    r = rpd.diff_packs(v1, v2)
    assert "tenant:foo:rate5m" in r["removed"]
    assert r["removed_alert_names"] == []
    assert r["removed_record_names"] == ["tenant:foo:rate5m"]


def test_main_text_output_lists_removed_records(tmp_path, capsys):
    """Regression guard: removed recording rules must appear in text
    output. Previously they were silently invisible (only alerts were
    listed), so customers reading the report would think v2 was safer
    than it is."""
    v1 = _write_pack(tmp_path / "v1.yaml", """
        groups:
          - name: g
            rules:
              - record: tenant:dropped:rate5m
                expr: rate(x[5m])
    """)
    v2 = _write_pack(tmp_path / "v2.yaml", """
        groups:
          - name: g
            rules: []
    """)
    rc = rpd.main(["--from", str(v1), "--to", str(v2)])
    out = capsys.readouterr().out
    assert "Removed recording rules" in out
    assert "tenant:dropped:rate5m" in out
    assert "downstream alerts referencing these expressions will break" in out


def test_diff_counts_summary():
    v1 = _pack_from("""
        groups:
          - {name: g, rules: [
              {alert: A1, expr: up, labels: {severity: warning}},
              {alert: A2, expr: down}
          ]}
    """)
    v2 = _pack_from("""
        groups:
          - {name: g, rules: [
              {alert: A1, expr: up, labels: {severity: critical}},
              {alert: A3, expr: stuck}
          ]}
    """)
    r = rpd.diff_packs(v1, v2)
    c = r["counts"]
    assert c["v1_total_rules"] == 2
    assert c["v2_total_rules"] == 2
    assert c["added"] == 1  # A3
    assert c["removed"] == 1  # A2
    assert c["modified"] == 1  # A1 (label value changed)
    assert c["breaking"] == 1  # A1


# ─── compute_exit_code ────────────────────────────────────────────────


def test_exit_code_default_zero_with_breaking():
    """Without --ci, exit code is 0 even with breaking changes —
    reporting mode for human review."""
    report = {"counts": {"breaking": 3, "removed": 2}}
    assert rpd.compute_exit_code(report, ci=False) == 0


def test_exit_code_ci_breaking_exits_1():
    report = {"counts": {"breaking": 1, "removed": 0}}
    assert rpd.compute_exit_code(report, ci=True) == 1


def test_exit_code_ci_removed_exits_1():
    """Removed alerts are treated as breaking in --ci even without
    label-schema change. Silencer matchers on the alertname will silently
    miss in v2."""
    report = {"counts": {"breaking": 0, "removed": 1}}
    assert rpd.compute_exit_code(report, ci=True) == 1


def test_exit_code_ci_clean_exits_0():
    report = {"counts": {"breaking": 0, "removed": 0}}
    assert rpd.compute_exit_code(report, ci=True) == 0


# ─── main() end-to-end ────────────────────────────────────────────────


@pytest.fixture
def v1_path(tmp_path):
    return _write_pack(tmp_path / "v1.yaml", """
        groups:
          - name: g
            rules:
              - alert: A1
                expr: up
                labels: {severity: warning}
    """)


@pytest.fixture
def v2_path(tmp_path):
    return _write_pack(tmp_path / "v2.yaml", """
        groups:
          - name: g
            rules:
              - alert: A1
                expr: up
                labels: {severity: warning, team: sre}  # breaking: label added
    """)


def test_main_text_output(v1_path, v2_path, capsys):
    rc = rpd.main(["--from", str(v1_path), "--to", str(v2_path)])
    out = capsys.readouterr().out
    assert "Breaking modifications" in out
    assert "A1" in out
    assert rc == 0  # no --ci → exit 0 even with breaking


def test_main_json_output_includes_breaking(v1_path, v2_path, capsys):
    rc = rpd.main(["--from", str(v1_path), "--to", str(v2_path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["counts"]["breaking"] == 1
    assert len(report["breaking_modifications"]) == 1
    assert rc == 0


def test_main_ci_exits_1_on_breaking(v1_path, v2_path):
    rc = rpd.main(["--from", str(v1_path), "--to", str(v2_path), "--ci"])
    assert rc == 1


def test_main_missing_input_exits_2(tmp_path):
    rc = rpd.main(
        ["--from", str(tmp_path / "nope.yaml"), "--to", str(tmp_path / "also.yaml")]
    )
    assert rc == 2


def test_main_clean_diff_text(v1_path, capsys):
    """Same file vs itself → 'No differences detected'."""
    rc = rpd.main(["--from", str(v1_path), "--to", str(v1_path)])
    out = capsys.readouterr().out
    assert "No differences detected" in out
    assert rc == 0


def test_diff_same_name_count_mismatch_recorded(tmp_path):
    """If the same alertname appears 2× in v1 but 1× in v2 (or vice versa),
    the count mismatch must be captured in count_anomalies — not silently
    swallowed by the zip pairing. Catches pathological rule packs that
    duplicate alertnames (well-formed packs don't, but this defends).
    """
    v1 = _pack_from("""
        groups:
          - name: g1
            rules:
              - alert: FooAlert
                expr: up
                labels: {severity: warning}
          - name: g2
            rules:
              - alert: FooAlert
                expr: down
                labels: {severity: warning}
    """)
    v2 = _pack_from("""
        groups:
          - name: g1
            rules:
              - alert: FooAlert
                expr: up
                labels: {severity: warning}
    """)
    r = rpd.diff_packs(v1, v2)
    assert len(r["count_anomalies"]) == 1
    anomaly = r["count_anomalies"][0]
    assert anomaly["name"] == "FooAlert"
    assert anomaly["v1_count"] == 2
    assert anomaly["v2_count"] == 1
    assert r["counts"]["count_anomalies"] == 1


def test_main_json_includes_input_paths(v1_path, v2_path, capsys):
    """JSON output must include from_path / to_path so automation can
    correlate diff reports with the inputs that produced them."""
    rc = rpd.main(["--from", str(v1_path), "--to", str(v2_path), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["from_path"] == str(v1_path)
    assert report["to_path"] == str(v2_path)


def test_main_json_roundtrip_for_label_value_tuple(tmp_path, capsys):
    """JSON output's label_values_changed tuples must round-trip as
    arrays (not blow up serialisation)."""
    v1 = _write_pack(tmp_path / "v1.yaml", """
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: warning}}]}
    """)
    v2 = _write_pack(tmp_path / "v2.yaml", """
        groups:
          - {name: g, rules: [{alert: A1, expr: up, labels: {severity: critical}}]}
    """)
    rc = rpd.main(["--from", str(v1), "--to", str(v2), "--json"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    breaking = report["breaking_modifications"][0]["change"]["label_values_changed"]
    # Tuple round-trips as a 2-element list
    assert breaking == {"severity": ["warning", "critical"]}
