"""Tests for check_percent_render_pinned.py (#978).

Pinned contracts
----------------
1. pinned by a firing case -> pass.
2. percent-rendered annotation asserted nowhere, not baselined -> fail (headline).
3. asserted only in a NO-fire case (exp_alerts: []) -> still unpinned -> fail.
4. a firing case that asserts a DIFFERENT annotation key -> unpinned -> fail.
5. every renderer shape is detected: humanizePercentage, a `}}%` suffix (ASCII or
   full-width ％), and a printf format that writes `%%` itself; a printf verb that
   prints no percent sign (`printf "%.1f" }} bytes`) is not a percent render.
5b. a fixture that loads some other rule file (not rule-packs/rule-pack-*) does not
   pin, even with the same alertname.
6. baseline entry that is now pinned / gone -> fail (stale).
7. --generate then check -> pass.
8. the shipped tree is clean (baseline == reality) — this is the test that
   looks at the real repo, not a tmp tree.
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_percent_render_pinned as prp  # noqa: E402

HUMANIZE = '"{{ $value | humanizePercentage }} used"'
PRINTF_PCT = '"{{ $value | printf \\"%.1f\\" }}% used"'
NO_PCT = '"{{ $value | printf \\"%.1f\\" }} bytes"'
PRINTF_DOUBLE = '"{{ $value | printf \\"%.1f%%\\" }} used"'
FULLWIDTH = '"{{ $value | printf \\"%.1f\\" }}％ 使用"'


def _pack(alerts: dict[str, str]) -> str:
    rules = "\n".join(
        f"      - alert: {a}\n        expr: up == 0\n        annotations:\n"
        f"          description: {d}" for a, d in alerts.items())
    return f"groups:\n  - name: g\n    rules:\n{rules}\n"


def _fixture(alert: str, *, firing: bool, key: str = "description",
             rule_file: str = "../../rule-packs/rule-pack-x.yaml") -> str:
    if firing:
        exp = (f"        exp_alerts:\n          - exp_labels: {{}}\n"
               f"            exp_annotations:\n              {key}: \"x\"\n")
    else:
        exp = "        exp_alerts: []\n"
    return (f"rule_files: [{rule_file}]\ntests:\n  - interval: 1m\n    input_series: []\n"
            f"    alert_rule_test:\n      - eval_time: 1m\n        alertname: {alert}\n{exp}")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    rp = tmp_path / "rule-packs"
    tr = tmp_path / "tests" / "rulepacks"
    rp.mkdir(parents=True)
    tr.mkdir(parents=True)
    monkeypatch.setattr(prp, "_REPO", tmp_path)
    monkeypatch.setattr(prp, "_RULE_PACKS", rp)
    monkeypatch.setattr(prp, "_TESTS", tr)
    monkeypatch.setattr(prp, "_BASELINE", tr / "percent_render_baseline.yaml")
    return rp, tr


def _baseline(tr, tree_: dict) -> None:
    (tr / "percent_render_baseline.yaml").write_text(
        yaml.safe_dump({"unpinned": tree_}), encoding="utf-8")


def test_pinned_by_firing_case_passes(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture("A", firing=True), encoding="utf-8")
    assert prp.check() == 0


def test_unpinned_not_baselined_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    assert prp.check() == 1


def test_no_fire_case_does_not_pin(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture("A", firing=False), encoding="utf-8")
    assert prp.check() == 1


def test_other_annotation_key_does_not_pin(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture("A", firing=True, key="summary"), encoding="utf-8")
    assert prp.check() == 1


def test_both_renderer_shapes_detected_and_plain_printf_ignored(tree):
    rp, _ = tree
    (rp / "rule-pack-x.yaml").write_text(
        _pack({"H": HUMANIZE, "P": PRINTF_PCT, "N": NO_PCT,
               "D": PRINTF_DOUBLE, "F": FULLWIDTH}), encoding="utf-8")
    assert prp.percent_rendered() == {"rule-pack-x.yaml": {
        "D": ["description"], "F": ["description"],
        "H": ["description"], "P": ["description"]}}


def test_fixture_loading_another_rule_file_does_not_pin(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "local.rules.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "x_test.yaml").write_text(
        _fixture("A", firing=True, rule_file="local.rules.yaml"), encoding="utf-8")
    assert prp.check() == 1


def test_stale_baseline_entry_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture("A", firing=True), encoding="utf-8")
    _baseline(tr, {"rule-pack-x.yaml": {"A": ["description"]}})
    assert prp.check() == 1


def test_generate_then_check_passes(tree):
    rp, _ = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": PRINTF_PCT}), encoding="utf-8")
    prp.generate()
    assert prp.check() == 0


def test_shipped_tree_is_clean():
    """The real rule packs, fixtures and baseline agree (needs da-read)."""
    assert prp.check() == 0


def test_same_alertname_twice_keeps_both_keys(tree):
    """CodeRabbit #4029: two rules may share an alertname; the second rule's
    percent-rendered key must not hide the first one's."""
    rp, tr = tree
    rules = ("groups:\n  - name: g\n    rules:\n"
             f"      - alert: A\n        expr: up == 0\n        annotations:\n          description: {HUMANIZE}\n"
             f"      - alert: A\n        expr: up == 1\n        annotations:\n          summary: {HUMANIZE}\n")
    (rp / "rule-pack-x.yaml").write_text(rules, encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture("A", firing=True, key="summary"), encoding="utf-8")
    assert prp.current_unpinned() == {"rule-pack-x.yaml": {"A": ["description"]}}


def test_fixture_pins_only_the_packs_it_loads(tree):
    """CodeRabbit #4029: a fixture loading pack Y plus a local rule file with an
    alert named like pack X's does not pin pack X's alert."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (rp / "rule-pack-y.yaml").write_text(_pack({"B": NO_PCT}), encoding="utf-8")
    (tr / "local.rules.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    fixture = _fixture("A", firing=True, rule_file="../../rule-packs/rule-pack-y.yaml")
    fixture = fixture.replace("rule_files: [../../rule-packs/rule-pack-y.yaml]",
                              "rule_files: [../../rule-packs/rule-pack-y.yaml, local.rules.yaml]")
    (tr / "y_test.yaml").write_text(fixture, encoding="utf-8")
    assert prp.check() == 1


def test_fixture_mixing_pack_and_local_rules_pins_nothing(tree):
    """CodeRabbit #4029 follow-up: a fixture loading pack X and a local rule file
    that also declares A cannot tell whose A rendered the asserted text."""
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    (tr / "local.rules.yaml").write_text(_pack({"A": HUMANIZE}), encoding="utf-8")
    fixture = _fixture("A", firing=True).replace(
        "rule_files: [../../rule-packs/rule-pack-x.yaml]",
        "rule_files: [../../rule-packs/rule-pack-x.yaml, local.rules.yaml]")
    (tr / "x_test.yaml").write_text(fixture, encoding="utf-8")
    assert prp.check() == 1
