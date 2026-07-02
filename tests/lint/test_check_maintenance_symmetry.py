"""Tests for check_maintenance_symmetry.py — per-arm maintenance-clause symmetry guard.

Pinned contracts
----------------
1. **canonical two-arm (2 copies, one per arm) → pass** — the current 76-alert shape.
2. **bare arm missing its copy → fail** (headline: the #973/#977 bug class — delete the
   bare copy and only an e-tenant fixture would notice; this lint notices statically).
3. **enriched arm missing its copy → fail** (mirror asymmetry).
4. **typo'd clause → fail** — a near-miss spelling doesn't match the canonical regex,
   drops the count to 1-inside-an-arm, and fails. That IS the typo detection.
5. **factored form → pass** — ``((enriched) or (bare)) unless on(tenant) (...)`` with a
   single top-level trailing clause (the target shape of the #947 refactor).
6. **precedence footgun → fail** — ``(A) or (B) unless M`` without the union parens:
   PromQL ``or`` binds looser than ``unless``, so the enriched arm silently loses
   suppression while the expr LOOKS factored.
7. **two-arm, zero clauses → pass** — suppression may live upstream in a shared ``:core``
   recording rule (kubernetes pack) or be a deliberate no-opt-out; policy is out of scope.
8. **single-arm (no bare marker) → out of scope, pass** regardless of clause count.
9. **both copies inside one arm → fail** — count alone isn't symmetry; position matters.
10. **.yml pack scanned too** — a ``.yml`` suffix must not escape the gate (fail-open
    hole class, Gemini #969).
11. **live repo → pass** — integration teeth: the real rule-packs/ tree is compliant.
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint")
sys.path.insert(0, _TOOLS_DIR)

import check_maintenance_symmetry as sym  # noqa: E402

MAINT = 'unless on(tenant)\n  (user_state_filter{filter="maintenance"} == 1)'
ENRICH = '* on(tenant) group_left(runbook_url, owner, tier)\n  tenant_metadata_info'
BARE = 'unless on(tenant) tenant_metadata_info'
BREACH = '(tenant:x:avg1m > on(tenant) group_left tenant:alert_threshold:x)'


def _two_arm(enriched_maint: bool = True, bare_maint: bool = True) -> str:
    left = f"(\n  (\n    {BREACH}\n    {MAINT if enriched_maint else ''}\n  )\n  {ENRICH}\n)"
    right = f"(\n  (\n    {BREACH}\n    {MAINT if bare_maint else ''}\n  )\n  {BARE}\n)"
    return f"{left}\nor\n{right}"


def _pack(exprs: dict[str, str]) -> str:
    return yaml.safe_dump(
        {"groups": [{"name": "g",
                     "rules": [{"alert": a, "expr": e, "labels": {"severity": "warning"}}
                               for a, e in exprs.items()]}]})


@pytest.fixture
def tree(tmp_path, monkeypatch):
    rp = tmp_path / "rule-packs"
    rp.mkdir()
    monkeypatch.setattr(sym, "_REPO", tmp_path)
    monkeypatch.setattr(sym, "_RULE_PACKS", rp)
    return rp


def test_canonical_two_arm_passes(tree):
    (tree / "rule-pack-x.yaml").write_text(_pack({"A": _two_arm()}), encoding="utf-8")
    assert sym.check() == 0


def test_bare_arm_missing_copy_fails(tree, capsys):
    (tree / "rule-pack-x.yaml").write_text(
        _pack({"A": _two_arm(bare_maint=False)}), encoding="utf-8")
    assert sym.check() == 1
    assert "INSIDE one arm" in capsys.readouterr().out


def test_enriched_arm_missing_copy_fails(tree):
    (tree / "rule-pack-x.yaml").write_text(
        _pack({"A": _two_arm(enriched_maint=False)}), encoding="utf-8")
    assert sym.check() == 1


def test_typoed_clause_fails(tree):
    typo = _two_arm().replace('filter="maintenance"} == 1)\n  )\n  unless on(tenant) tenant',
                              'filter="maintenence"} == 1)\n  )\n  unless on(tenant) tenant')
    assert 'maintenence' in typo  # the bare-arm copy really was typo'd
    (tree / "rule-pack-x.yaml").write_text(_pack({"A": typo}), encoding="utf-8")
    assert sym.check() == 1


def test_factored_form_passes(tree):
    enriched = f"(\n  {BREACH}\n  {ENRICH}\n)"
    bare = f"(\n  {BREACH}\n  {BARE}\n)"
    expr = f"(\n{enriched}\nor\n{bare}\n)\n{MAINT}"
    (tree / "rule-pack-x.yaml").write_text(_pack({"A": expr}), encoding="utf-8")
    assert sym.check() == 0


def test_precedence_footgun_fails(tree, capsys):
    enriched = f"(\n  {BREACH}\n  {ENRICH}\n)"
    bare = f"(\n  {BREACH}\n  {BARE}\n)"
    expr = f"{enriched}\nor\n{bare}\n{MAINT}"       # union NOT parenthesised
    (tree / "rule-pack-x.yaml").write_text(_pack({"A": expr}), encoding="utf-8")
    assert sym.check() == 1
    assert "binds looser" in capsys.readouterr().out


def test_two_arm_zero_clauses_passes(tree):
    (tree / "rule-pack-x.yaml").write_text(
        _pack({"A": _two_arm(enriched_maint=False, bare_maint=False)}), encoding="utf-8")
    assert sym.check() == 0


def test_single_arm_out_of_scope(tree):
    (tree / "rule-pack-x.yaml").write_text(
        _pack({"A": f"(mysql_up == 0)\n{MAINT}"}), encoding="utf-8")
    assert sym.check() == 0


def test_both_copies_in_one_arm_fails(tree):
    left = f"(\n  (\n    {BREACH}\n    {MAINT}\n    {MAINT}\n  )\n  {ENRICH}\n)"
    right = f"(\n  {BREACH}\n  {BARE}\n)"
    (tree / "rule-pack-x.yaml").write_text(
        _pack({"A": f"{left}\nor\n{right}"}), encoding="utf-8")
    assert sym.check() == 1


def test_yml_suffix_scanned(tree):
    (tree / "rule-pack-x.yml").write_text(
        _pack({"A": _two_arm(bare_maint=False)}), encoding="utf-8")
    assert sym.check() == 1


def test_live_repo_is_compliant():
    """Integration teeth: the actual rule-packs/ tree passes (76 two-arm alerts symmetric,
    :core-factored kubernetes alerts at 0 copies, single-arm liveness out of scope)."""
    assert sym.check() == 0
