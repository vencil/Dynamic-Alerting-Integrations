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
            # no base key in this pack -> no critical_of, no inheritance, so
            # it must be classified explicitly (schema requires metric_class)
            "alpha_orphan_critical": {"value": 9, "unit": "count",
                                      "desc": "no base key in this pack",
                                      "metric_class": "state"},
        },
    },
    "beta": {
        "display": "Beta MQ",
        "exporter": "beta_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-beta.yaml",
        "defaults": {
            "beta_lag": {"value": 0.5, "unit": "seconds", "desc": "lag warning",
                         "metric_class": "replication"},
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


# ── metric_class: inheritance / backfill / strict completeness ────────────

def test_critical_inherits_base_metric_class():
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert doc["keys"]["alpha_conn_critical"]["metric_class"] == "saturation"


def test_backfill_classifies_unclassified_key():
    packs = copy.deepcopy(SYNTH_PACKS)
    del packs["beta"]["defaults"]["beta_lag"]["metric_class"]
    doc = lib.build_registry_doc(
        packs, metric_class_backfill={"beta_lag": "latency"})
    assert doc["keys"]["beta_lag"]["metric_class"] == "latency"


def test_backfill_shadowing_scaffold_class_fails_loud():
    with pytest.raises(ValueError, match="shadows scaffold-authored"):
        lib.build_registry_doc(
            SYNTH_PACKS, metric_class_backfill={"beta_lag": "latency"})


def test_backfill_unknown_key_fails_loud():
    with pytest.raises(ValueError, match="not found in RULE_PACKS"):
        lib.build_registry_doc(
            SYNTH_PACKS, metric_class_backfill={"ghost": "latency"})


def test_strict_unclassified_key_fails_loud():
    packs = copy.deepcopy(SYNTH_PACKS)
    del packs["beta"]["defaults"]["beta_lag"]["metric_class"]
    with pytest.raises(ValueError, match="unclassified threshold keys"):
        lib.build_registry_doc(packs, strict_metric_class=True)


def test_real_extraction_every_key_classified_and_enum_bounded():
    """65/65 keys carry a metric_class from the schema taxonomy; the curated
    saturation set stays exactly the 22 scaffold-authored bases (+ inherited
    _critical) — the backfill adds NO new saturation (behavior-preserving)."""
    doc = lib.build_registry_doc()
    classes = {k: e["metric_class"] for k, e in doc["keys"].items()}
    assert len(classes) == len(doc["keys"])  # no key unclassified
    assert "saturation" not in set(lib.METRIC_CLASS_BACKFILL.values())
    saturation_bases = {
        k for k, e in doc["keys"].items()
        if e["metric_class"] == "saturation" and "critical_of" not in e
    }
    assert len(saturation_bases) == 22


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


# ── chart_default (PR-2 rewire) ───────────────────────────────────────────

def test_synthetic_extraction_stays_pure():
    """Injected packs get NO enrichment unless the caller passes a table."""
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert all("chart_default" not in e for e in doc["keys"].values())


def test_chart_default_injected_table():
    doc = lib.build_registry_doc(
        SYNTH_PACKS, chart_default_keys=frozenset({"alpha_conn"}))
    assert doc["keys"]["alpha_conn"]["chart_default"] is True
    assert "chart_default" not in doc["keys"]["beta_lag"]


def test_chart_default_non_defaults_tier_fails_loud():
    with pytest.raises(ValueError, match="chart-shippable"):
        lib.build_registry_doc(
            SYNTH_PACKS, chart_default_keys=frozenset({"alpha_conn_critical"}))


def test_chart_default_unknown_key_fails_loud():
    with pytest.raises(ValueError, match="not found in RULE_PACKS"):
        lib.build_registry_doc(
            SYNTH_PACKS, chart_default_keys=frozenset({"ghost"}))


def test_real_extraction_chart_default_set_matches_table():
    """The real doc carries chart_default on exactly CHART_DEFAULT_KEYS —
    the behavior-preserving 6-key pre-rewire set plus the owner-decided
    pg_connections / pg_replication_lag promotion (#1200 Q3=C / D4 folding,
    2026-07-25)."""
    doc = lib.build_registry_doc()
    tagged = {k for k, e in doc["keys"].items() if e.get("chart_default")}
    assert tagged == set(lib.CHART_DEFAULT_KEYS)
    # Pin the DECISION, not just the mechanism: the owner-approved pg
    # promotion (#1232) must not silently vanish via a table edit that
    # keeps the count intact (CodeRabbit review of #1232).
    assert {"pg_connections", "pg_replication_lag"} <= tagged
    assert len(tagged) == 8


# ── generated surfaces: render / splice / freshness ───────────────────────

def _synth_doc_with_chart():
    return lib.build_registry_doc(
        SYNTH_PACKS, chart_default_keys=frozenset({"alpha_conn"}))


def test_render_chart_defaults_only_chart_keys():
    doc = _synth_doc_with_chart()
    body = lib.render_chart_defaults_lines(doc, 4)
    text = "\n".join(body)
    assert "    alpha_conn: 80" in text
    # non-chart keys never render into the chart surfaces
    assert "beta_lag" not in text
    # the registry's critical sibling renders as an opt-in hint
    assert "alpha_conn_critical" in text and "120" in text


def test_render_pack_header_defaults_and_optional_sections():
    doc = _synth_doc_with_chart()
    text = "\n".join(lib.render_pack_header_lines(doc, "alpha"))
    assert "#   alpha_conn: 80" in text
    assert "[chart-default]" in text
    # optional tier renders under the ⛔ activation-precondition warning
    assert "optional_overrides" in text and "⛔ 啟用前提" in text
    assert "alpha_orphan_critical: 9" in text
    # a pack with no optional tier renders no warning
    beta = "\n".join(lib.render_pack_header_lines(doc, "beta"))
    assert "⛔" not in beta


def test_splice_check_roundtrip_and_staleness():
    doc = _synth_doc_with_chart()
    spec = {
        "id": "unit-test",
        "path": "unit-test.yaml",
        "indent": "  ",
        "body": lib.render_chart_defaults_lines(doc, 2),
    }
    spec["block"] = lib.render_block(spec["id"], spec["body"], spec["indent"])
    text = "\n".join([
        "defaults:",
        lib.begin_marker("unit-test", "  "),
        "  stale_content: 1",
        lib.end_marker("unit-test", "  "),
        "rest: true",
    ])
    assert lib.check_surface(text, spec) is not None  # stale
    fresh = lib.splice_surface(text, spec)
    assert lib.check_surface(fresh, spec) is None
    # idempotent
    assert lib.splice_surface(fresh, spec) == fresh
    # hand-written lines outside the block survive
    assert fresh.startswith("defaults:") and fresh.endswith("rest: true")


def test_missing_markers_is_reported_not_silent():
    doc = _synth_doc_with_chart()
    spec = lib.surface_specs(doc)[0]
    assert "markers" in (lib.check_surface("no markers here", spec) or "")
    with pytest.raises(ValueError, match="markers"):
        lib.splice_surface("no markers here", spec)


def test_surface_specs_cover_helm_dev_and_threshold_packs():
    doc = lib.build_registry_doc()
    ids = [s["id"] for s in lib.surface_specs(doc)]
    assert ids[0] == "helm-defaults" and ids[1] == "dev-defaults"
    assert "pack-mariadb" in ids and "pack-kubernetes" in ids
    assert len(ids) == 2 + 13  # 13 threshold packs, non-threshold packs none


# ── header-prose key membership (F6) ──────────────────────────────────────

HEADER = """# ============================================================
# Rule Pack: demo
#
# 手寫 prose：
#   alpha_conn: 80
#   alpha_conn_critical: "120"
#   totally_bogus_key: 1
#   "alpha_dim_metric{label=\\"x\\"}": "5"
# recording 名 tenant:alert_threshold:alpha_conn 不算 key claim
# >>> GENERATED:threshold-registry:pack-demo — generated block, DO NOT EDIT（改 scaffold_tenant.RULE_PACKS 後跑 check_threshold_registry.py --regen）
#   generated_only_key: 9
# <<< GENERATED:threshold-registry:pack-demo
# ============================================================
groups:
  - name: below-header
    rules: []
#   after_groups_token: 1
"""


def test_prose_key_tokens_extraction_rules():
    tokens = {t for _, t in lib.prose_key_tokens(HEADER)}
    assert "alpha_conn" in tokens
    assert "alpha_conn_critical" in tokens
    assert "totally_bogus_key" in tokens
    # dimensional tokens are exempt
    assert "alpha_dim_metric" not in tokens
    # generated block is not prose
    assert "generated_only_key" not in tokens
    # scan stops at the first non-comment line
    assert "after_groups_token" not in tokens
    # recording-rule segments are not key claims
    assert "alert_threshold" not in tokens


def test_membership_universe_composition():
    doc = _valid_doc()
    uni = lib.membership_universe(doc, extra_allowed={"pending_key"})
    assert "alpha_conn" in uni
    assert "alpha_conn_critical" in uni          # explicit registry entry
    assert "beta_lag_critical" in uni            # derived _critical opt-in
    assert "pending_key" in uni and "pending_key_critical" in uni
    assert "state_filters" in uni                # structural prose token
    assert "totally_bogus_key" not in uni
