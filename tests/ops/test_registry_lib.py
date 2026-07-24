"""Tests for scripts/tools/ops/_registry_lib.py (TRK-339 WS1a / #1200).

The lib is the single mechanical bridge between scaffold_tenant.RULE_PACKS and
the standalone threshold registry (D2). Pins:
  - extraction structure (packs / keys / tiers / derived critical_of)
  - loud failure on identity collisions (never silently shadow a contract key)
  - load + schema-validate roundtrip on the real committed registry
  - the semantic diff catches every field-level divergence, both directions
  - query helpers
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "ops" / "_registry_lib.py"

_spec = importlib.util.spec_from_file_location("_registry_lib", _SCRIPT)
lib = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lib)


# A minimal synthetic RULE_PACKS covering every extraction branch.
SYNTH_PACKS = {
    "alpha": {
        "display": "Alpha DB",
        "exporter": "alpha_exporter",
        "default_on": True,
        "rule_pack_file": "rule-packs/rule-pack-alpha.yaml",
        "defaults": {
            "alpha_conn": {"value": 80, "unit": "count", "desc": "conn warning",
                           "metric_class": "saturation"},
        },
        "optional_overrides": {
            "alpha_conn_critical": {"value": 120, "unit": "count",
                                    "desc": "conn critical"},
            "alpha_orphan_critical": {"value": 9, "unit": "count",
                                      "desc": "no base key in this pack"},
        },
    },
    "beta": {
        "display": "Beta MQ",
        "exporter": "beta_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-beta.yaml",
        "defaults": {
            "beta_lag": {"value": 0.5, "unit": "seconds", "desc": "lag warning"},
        },
    },
}


# ── extraction ────────────────────────────────────────────────────────────

def test_build_registry_doc_structure():
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert doc["version"] == 1
    assert set(doc["packs"]) == {"alpha", "beta"}
    assert doc["packs"]["alpha"] == {
        "display": "Alpha DB",
        "exporter": "alpha_exporter",
        "default_on": True,
        "rule_pack_file": "rule-packs/rule-pack-alpha.yaml",
    }
    assert doc["keys"]["alpha_conn"] == {
        "pack": "alpha", "tier": "defaults", "value": 80, "unit": "count",
        "desc": "conn warning", "metric_class": "saturation",
    }
    # float values survive as-is
    assert doc["keys"]["beta_lag"]["value"] == 0.5


def test_critical_of_derived_only_when_base_exists_in_pack():
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert doc["keys"]["alpha_conn_critical"]["critical_of"] == "alpha_conn"
    # _critical WITHOUT a same-pack base key gets NO critical_of (never guess)
    assert "critical_of" not in doc["keys"]["alpha_orphan_critical"]


def test_same_key_in_both_tiers_of_one_pack_fails_loud():
    packs = copy.deepcopy(SYNTH_PACKS)
    packs["alpha"]["optional_overrides"]["alpha_conn"] = {
        "value": 1, "unit": "count", "desc": "shadow"}
    with pytest.raises(ValueError, match="BOTH tiers"):
        lib.build_registry_doc(packs)


def test_same_key_in_two_packs_fails_loud():
    packs = copy.deepcopy(SYNTH_PACKS)
    packs["beta"]["defaults"]["alpha_conn"] = {
        "value": 1, "unit": "count", "desc": "shadow"}
    with pytest.raises(ValueError, match="ambiguous"):
        lib.build_registry_doc(packs)


# ── the real committed registry ───────────────────────────────────────────

def test_committed_registry_loads_and_validates():
    doc = lib.load_registry()
    assert lib.validate_registry(doc) == []
    assert len(doc["packs"]) >= 13
    assert len(doc["keys"]) >= 60


def test_committed_registry_equals_scaffold():
    """The transition-period invariant itself (also enforced by the gate)."""
    assert lib.diff_vs_scaffold(lib.load_registry()) == []


def test_write_registry_roundtrip(tmp_path):
    out = tmp_path / "registry.yaml"
    summary = lib.write_registry(str(out), SYNTH_PACKS)
    assert summary == {"path": str(out), "packs": 2, "keys": 4}
    doc = lib.load_registry(str(out))
    assert lib.diff_docs(doc, lib.build_registry_doc(SYNTH_PACKS)) == []
    # the written file validates against the committed schema
    assert lib.validate_registry(doc) == []


# ── schema validation catches shape violations ────────────────────────────

def _valid_doc():
    return lib.build_registry_doc(SYNTH_PACKS)


def test_schema_rejects_bad_tier():
    doc = _valid_doc()
    doc["keys"]["alpha_conn"]["tier"] = "defalts"  # typo
    assert any("tier" in e for e in lib.validate_registry(doc))


def test_schema_rejects_non_numeric_value():
    doc = _valid_doc()
    doc["keys"]["alpha_conn"]["value"] = "80"  # string, not number
    assert any("value" in e for e in lib.validate_registry(doc))


def test_schema_rejects_missing_required_field():
    doc = _valid_doc()
    del doc["keys"]["alpha_conn"]["unit"]
    assert any("unit" in e for e in lib.validate_registry(doc))


def test_schema_rejects_unknown_field():
    doc = _valid_doc()
    doc["keys"]["alpha_conn"]["surprise"] = 1
    assert lib.validate_registry(doc) != []


# ── semantic diff (both directions, per-field) ────────────────────────────

def test_diff_value_change_is_reported():
    committed = _valid_doc()
    committed["keys"]["alpha_conn"]["value"] = 99
    diffs = lib.diff_docs(committed, lib.build_registry_doc(SYNTH_PACKS))
    assert any("alpha_conn" in d and "value" in d for d in diffs)


def test_diff_missing_and_extra_key_are_reported():
    committed = _valid_doc()
    del committed["keys"]["beta_lag"]
    committed["keys"]["ghost_key"] = {
        "pack": "beta", "tier": "defaults", "value": 1, "unit": "x", "desc": "y"}
    diffs = lib.diff_docs(committed, lib.build_registry_doc(SYNTH_PACKS))
    assert any("beta_lag" in d and "missing from registry" in d for d in diffs)
    assert any("ghost_key" in d and "missing from scaffold" in d for d in diffs)


def test_diff_pack_metadata_change_is_reported():
    committed = _valid_doc()
    committed["packs"]["beta"]["default_on"] = True
    diffs = lib.diff_docs(committed, lib.build_registry_doc(SYNTH_PACKS))
    assert any("beta" in d and "default_on" in d for d in diffs)


def test_diff_equal_docs_is_empty():
    assert lib.diff_docs(_valid_doc(), lib.build_registry_doc(SYNTH_PACKS)) == []


# ── query helpers ─────────────────────────────────────────────────────────

def test_query_helpers():
    doc = _valid_doc()
    assert lib.default_value(doc, "alpha_conn") == 80
    assert lib.default_value(doc, "nope") is None
    assert set(lib.keys_by_pack(doc)) == {"alpha", "beta"}
    assert set(lib.keys_in_tier(doc, "defaults")) == {"alpha_conn", "beta_lag"}
    assert "alpha_conn_critical" in lib.keys_in_tier(doc, "optional_overrides")
