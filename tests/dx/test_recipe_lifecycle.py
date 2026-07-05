"""Recipe lifecycle status tests (ADR-024 §Custom Alerts governance, #741 item #6).

`status: [active|deprecated|eol]` is platform-authored recipe versioning (distinct
from capability-A APP versioning). The executable SSOT is shape.py::RECIPE_STATUS;
the human governance contracts rule-packs/recipes/*.yaml mirror a `status:` field.

Pinned here:
1. SSOT completeness — every shipped recipe has a status; every value is valid.
2. Drift guard — each governance yaml's `status:` equals the code SSOT (so a
   hand-edit to one without the other fails CI, mirroring the existing
   recipe-contract drift discipline).
3. Accessor — recipe_status() never raises and defaults unknown → "active".
4. Compiler notices — an all-active tree is silent; a deprecated recipe in use
   yields a non-fatal notice naming the affected tenant(s), and the recipe still
   compiles (no silent alert loss; the eol *write* rejection is tenant-api-side).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import compile_custom_alerts as cc  # noqa: E402
from custom_alerts import shape as shp  # noqa: E402
from custom_alerts import loader as ld  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"
_RECIPE_YAMLS = sorted((_REPO / "rule-packs" / "recipes").glob("*.yaml"))


# --- 1. SSOT completeness ---------------------------------------------------

def test_status_ssot_covers_every_recipe():
    assert set(shp.RECIPE_STATUS) == set(shp.RECIPES)


def test_status_ssot_values_are_valid():
    assert shp.RECIPE_LIFECYCLE == {"active", "deprecated", "eol"}
    for recipe, status in shp.RECIPE_STATUS.items():
        assert status in shp.RECIPE_LIFECYCLE, f"{recipe} has invalid status {status!r}"


def test_all_shipped_recipes_currently_active():
    # Baseline guard: nothing is deprecated/eol yet. Flipping a status is a
    # deliberate governance act that must also update the matching yaml (drift
    # test below) — this assertion documents the current baseline.
    assert all(s == "active" for s in shp.RECIPE_STATUS.values())


# --- 2. Drift guard: governance yaml <-> code SSOT --------------------------

def test_governance_yaml_status_matches_code_ssot():
    assert _RECIPE_YAMLS, "no governance recipe yamls found"
    for path in _RECIPE_YAMLS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        recipe = doc.get("recipe")
        assert recipe in shp.RECIPES, f"{path.name}: unknown recipe {recipe!r}"
        assert "status" in doc, f"{path.name}: missing `status:` field"
        assert doc["status"] == shp.recipe_status(recipe), (
            f"{path.name}: status {doc['status']!r} drifted from code SSOT "
            f"{shp.recipe_status(recipe)!r} (update shape.py RECIPE_STATUS + the yaml together)"
        )


# --- 3. Accessor ------------------------------------------------------------

def test_recipe_status_accessor_known_and_unknown():
    assert shp.recipe_status("threshold") == "active"
    # never raises; unknown recipe reports active (recipe_id() is the rejecter)
    assert shp.recipe_status("does_not_exist") == "active"


# --- 4. Compiler lifecycle notices ------------------------------------------

def test_notices_empty_when_all_active():
    assert ld.collect_lifecycle_notices(_EXAMPLES) == []


def test_notices_flag_deprecated_recipe_in_use(monkeypatch):
    used = {inst["recipe"] for _t, inst, _o, _own in ld.collect_instances(_EXAMPLES)[0]}
    used &= set(shp.RECIPES)
    assert used, "example conf.d declares no known recipes — fixture changed?"
    target = sorted(used)[0]

    monkeypatch.setitem(shp.RECIPE_STATUS, target, "deprecated")
    notices = ld.collect_lifecycle_notices(_EXAMPLES)
    assert notices, "a deprecated recipe in use must yield a notice"
    joined = "\n".join(notices)
    assert target in joined and "deprecated" in joined
    # migration-style wording, not the eol rejection wording
    assert "migrate away" in joined


def test_deprecated_recipe_still_compiles(monkeypatch, capsys):
    used = {inst["recipe"] for _t, inst, _o, _own in ld.collect_instances(_EXAMPLES)[0]}
    used &= set(shp.RECIPES)
    assert used, "example conf.d declares no known recipes — fixture changed?"
    target = sorted(used)[0]
    monkeypatch.setitem(shp.RECIPE_STATUS, target, "deprecated")
    # build_pack must NOT raise — deprecated keeps compiling (no silent loss)
    pack = cc.build_pack(_EXAMPLES)
    assert pack["groups"], "deprecated recipe still produces rule groups"


def test_eol_notice_uses_rejection_wording(monkeypatch):
    used = {inst["recipe"] for _t, inst, _o, _own in ld.collect_instances(_EXAMPLES)[0]}
    used &= set(shp.RECIPES)
    assert used, "example conf.d declares no known recipes — fixture changed?"
    target = sorted(used)[0]
    monkeypatch.setitem(shp.RECIPE_STATUS, target, "eol")
    joined = "\n".join(ld.collect_lifecycle_notices(_EXAMPLES))
    assert target in joined and "eol" in joined
    assert "rejected until SRE clears" in joined


# --- 5. Notice truncation (a deprecated SHARED recipe may have many tenants) --

def test_notice_truncates_large_tenant_list():
    # A deprecated/eol recipe shared by many tenants must not emit an unbounded
    # multi-kilobyte single-line warning (some log collectors truncate mid-line).
    # Lead with the count + a bounded sample of names.
    summary = ld._summarize_tenants({f"t{i:02d}" for i in range(25)})
    assert summary.startswith("25 tenant(s) (")
    assert "t00" in summary                          # first names shown
    assert f"and {25 - ld._NOTICE_TENANT_SAMPLE} more" in summary
    assert "t24" not in summary                       # beyond the sample → counted only
    assert summary.count(",") <= ld._NOTICE_TENANT_SAMPLE   # never the full 25-name join


def test_notice_small_list_not_truncated():
    assert ld._summarize_tenants({"beta", "alpha"}) == "2 tenant(s) (alpha, beta)"
    assert "more" not in ld._summarize_tenants({"alpha", "beta"})
