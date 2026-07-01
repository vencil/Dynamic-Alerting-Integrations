"""Tests for check_vmalert_coverage.py — rule-pack alert firing-coverage baseline guard.

Pinned contracts
----------------
1. **clean → pass**: when the baseline == the current uncovered set, exit 0.
2. **new gap → fail** (headline): a declared alert with no alert_rule_test and NOT in the
   baseline exits 1 — it must not silently escape both firing gates.
3. **healed → fail**: a baseline entry that now HAS a firing test exits 1 (baseline must
   stay == reality; stale entries get removed).
4. **removed/renamed → fail**: an alert listed in the baseline but no longer declared exits
   1 (also a stale baseline entry).
5. **threshold-only fixture ≠ coverage**: an alert exercised only by a promql_expr_test (the
   value contract) is still "uncovered" — firing coverage needs alert_rule_test.
6. **--generate → reality**: generating then checking passes (baseline == tree).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_vmalert_coverage as cov  # noqa: E402


def _pack(alerts: list[str]) -> str:
    rules = "\n".join(f"      - alert: {a}\n        expr: up == 0" for a in alerts)
    return f"groups:\n  - name: g\n    rules:\n{rules}\n"


def _fixture_alert_test(alertnames: list[str]) -> str:
    arts = "\n".join(f"      - eval_time: 1m\n        alertname: {a}\n        exp_alerts: []"
                     for a in alertnames)
    return ("rule_files: []\ntests:\n  - interval: 1m\n    input_series: []\n"
            f"    alert_rule_test:\n{arts}\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """Minimal rule-packs/ + tests/rulepacks/ tree, module globals repointed at it."""
    rp = tmp_path / "rule-packs"
    tr = tmp_path / "tests" / "rulepacks"
    rp.mkdir(parents=True)
    tr.mkdir(parents=True)
    monkeypatch.setattr(cov, "_REPO", tmp_path)
    monkeypatch.setattr(cov, "_RULE_PACKS", rp)
    monkeypatch.setattr(cov, "_TESTS", tr)
    monkeypatch.setattr(cov, "_BASELINE", tr / "vmalert_coverage_baseline.yaml")
    return rp, tr


def _write_baseline(tr: Path, uncovered: dict) -> None:
    (tr / "vmalert_coverage_baseline.yaml").write_text(
        yaml.safe_dump({"uncovered": uncovered}), encoding="utf-8")


def test_clean_passes(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Buncovered"]})   # B grandfathered
    assert cov.check() == 0


def test_new_gap_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {})   # B is NOT grandfathered -> new silent gap
    assert cov.check() == 1


def test_healed_baseline_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Bnow"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested", "Bnow"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Bnow"]})   # stale: Bnow now tested
    assert cov.check() == 1


def test_removed_alert_stale_baseline_fails(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    _write_baseline(tr, {"rule-pack-x.yaml": ["Gone"]})   # Gone no longer declared
    assert cov.check() == 1


def test_threshold_only_fixture_is_not_firing_coverage(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Cthreshold"]), encoding="utf-8")
    # a promql_expr_test (value contract), NOT an alert_rule_test -> not firing coverage
    (tr / "x-threshold_test.yaml").write_text(
        "rule_files: []\ntests:\n  - interval: 1m\n    input_series: []\n"
        "    promql_expr_test:\n      - expr: tenant:alert_threshold:c\n        eval_time: 1m\n"
        "        exp_samples: []\n", encoding="utf-8")
    _write_baseline(tr, {})            # not grandfathered
    assert cov.check() == 1            # still counted as an uncovered firing decision


def test_generate_makes_reality(tree):
    rp, tr = tree
    (rp / "rule-pack-x.yaml").write_text(_pack(["Atested", "Buncovered"]), encoding="utf-8")
    (tr / "x_test.yaml").write_text(_fixture_alert_test(["Atested"]), encoding="utf-8")
    cov.generate()
    assert cov.load_baseline() == {"rule-pack-x.yaml": ["Buncovered"]}
    assert cov.check() == 0
