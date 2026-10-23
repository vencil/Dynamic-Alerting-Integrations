"""Recipe lifecycle status tests (ADR-024 §Custom Alerts governance, #741 item #6).

`status: [active|deprecated|eol]` is platform-authored recipe versioning (distinct
from capability-A APP versioning). It is written once, in the exporter's recipe
contract (pkg/config/recipe_contract.json); the `status:` field of
rule-packs/recipes/*.yaml is generated from it (gen_recipe_contract.py), and
da-guard compile-custom-alerts names the non-active recipes in use
(`lifecycle`; Go's TestCompileCustomAlerts_LifecycleNamesNonActiveRecipesInUse
pins which tenants are named).

Pinned here:
1. SSOT completeness — every shipped recipe has a status; every value is valid.
2. Drift guard — each governance yaml's `status:` equals the contract's.
3. Compiler notices — an all-active tree is silent; a deprecated / eol recipe in
   use yields a non-fatal notice naming the affected tenant(s) (no silent alert
   loss; the eol *write* rejection is tenant-api-side).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

_DX = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx")
sys.path.insert(0, _DX)

import compile_custom_alerts as cc  # noqa: E402
import _lib_tenant_values as tv  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_EXAMPLES = _REPO / "rule-packs" / "recipes" / "examples" / "conf.d"
_RECIPE_YAMLS = sorted((_REPO / "rule-packs" / "recipes").glob("*.yaml"))
_CONTRACT = json.loads((_REPO / "components" / "threshold-exporter" / "app" / "pkg" / "config"
                        / "recipe_contract.json").read_text(encoding="utf-8"))
_STATUS = {r["name"]: r["status"] for r in _CONTRACT["recipes"]}


# --- 1. SSOT completeness ---------------------------------------------------

def test_status_ssot_values_are_valid():
    assert set(_CONTRACT["lifecycle"]) == {"active", "deprecated", "eol"}
    for recipe, status in _STATUS.items():
        assert status in _CONTRACT["lifecycle"], f"{recipe} has invalid status {status!r}"


def test_all_shipped_recipes_currently_active():
    # Baseline guard: nothing is deprecated/eol yet. Flipping a status is a
    # deliberate governance act that must also update the matching yaml (drift
    # test below) — this assertion documents the current baseline.
    assert all(s == "active" for s in _STATUS.values())


# --- 2. Drift guard: governance yaml <-> contract ----------------------------

def test_governance_yaml_status_matches_code_ssot():
    assert _RECIPE_YAMLS, "no governance recipe yamls found"
    for path in _RECIPE_YAMLS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        recipe = doc.get("recipe")
        assert recipe in _STATUS, f"{path.name}: unknown recipe {recipe!r}"
        assert "status" in doc, f"{path.name}: missing `status:` field"
        assert doc["status"] == _STATUS[recipe], (
            f"{path.name}: status {doc['status']!r} drifted from the contract "
            f"{_STATUS[recipe]!r} (edit recipe_contract.json, then `make recipe-contract`)"
        )


# --- 3. Compiler lifecycle notices ------------------------------------------

@pytest.mark.usefixtures("da_guard_env")
def test_an_all_active_tree_names_no_lifecycle():
    pack = cc.build_pack(_EXAMPLES)
    assert pack.groups, "the example tree compiled nothing — the check is vacuous"
    assert pack.lifecycle == []
    assert cc.lifecycle_notices(pack.lifecycle) == []


def test_notices_flag_deprecated_recipe_in_use():
    notices = cc.lifecycle_notices([tv.LifecycleUse("rate", "deprecated", ("ta", "tb"))])
    assert notices == [
        "recipe 'rate' is deprecated: 2 tenant(s) (ta, tb); migrate away — it still compiles."]


def test_eol_notice_uses_rejection_wording():
    joined = "\n".join(cc.lifecycle_notices([tv.LifecycleUse("rate", "eol", ("ta",))]))
    assert "'rate' is eol" in joined
    assert "rejected until SRE clears" in joined


# --- 4. Notice truncation (a deprecated SHARED recipe may have many tenants) --

def test_notice_truncates_large_tenant_list():
    # A deprecated/eol recipe shared by many tenants must not emit an unbounded
    # multi-kilobyte single-line warning (some log collectors truncate mid-line).
    # Lead with the count + a bounded sample of names.
    summary = cc._summarize_tenants({f"t{i:02d}" for i in range(25)})
    assert summary.startswith("25 tenant(s) (")
    assert "t00" in summary                          # first names shown
    assert f"and {25 - cc._NOTICE_TENANT_SAMPLE} more" in summary
    assert "t24" not in summary                       # beyond the sample → counted only
    assert summary.count(",") <= cc._NOTICE_TENANT_SAMPLE   # never the full 25-name join


def test_notice_small_list_not_truncated():
    assert cc._summarize_tenants({"beta", "alpha"}) == "2 tenant(s) (alpha, beta)"
    assert "more" not in cc._summarize_tenants({"alpha", "beta"})
