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
import re
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
    # alpha's optional tier is _critical-only, and it splits: alpha_conn_critical
    # has a chart-default base (settable today) while alpha_orphan_critical has
    # no base at all, so only the latter keeps the ⛔ precondition framing.
    assert "optional_overrides" in text
    assert _BASE_SHIPPED_MARK in text and _DORMANT_MARK in text
    assert "⛔ 啟用前提" in text
    assert "alpha_orphan_critical: 9" in text
    # a pack with no optional tier renders no warning
    beta = "\n".join(lib.render_pack_header_lines(doc, "beta"))
    assert "⛔" not in beta


# ── optional_overrides: warning is chosen PER KEY (#1310) ─────────────────
# PR-C (#1314) made the single legacy warning ("永久無法啟用") false for FLAT
# keys. A two-way split then replaced it — and was ALSO false, for the other
# direction: it told postgresql / mariadb readers their `_critical` keys emit
# nothing, when `resolveCriticalRows` only needs `defaults[base]` and those
# bases are shipped chart defaults. So the split is THREE-way and each heading
# is derived from what the resolver actually requires.
#
# ⛔ The three marks are mutually non-substring on purpose: two of the three
# headings share the phrase "不在出貨清單裡", and a substring probe would have
# reported the wrong section as "found" — the exact shape that lets a drift
# hide (the same reason `- alert:` name matching had to become exact).

_SHIPPED_MARK = "已出貨在清單裡——平台認得這個 key"
_BASE_SHIPPED_MARK = "但 base 已出貨——租戶今天就設得動"
_DORMANT_MARK = "base 也未出貨——documented-but-dormant"
_ALL_MARKS = (_SHIPPED_MARK, _BASE_SHIPPED_MARK, _DORMANT_MARK)


def test_the_three_warning_marks_cannot_alias_each_other():
    """Guard for the probes below: if one mark were a substring of another,
    every 'section X was chosen' assertion in this file would be unable to
    distinguish the two."""
    for a in _ALL_MARKS:
        for b in _ALL_MARKS:
            assert a == b or a not in b, (a, b)


def _synth_optional_packs():
    packs = copy.deepcopy(SYNTH_PACKS)
    # pure-flat optional tier (the oracle / db2 / clickhouse shape)
    packs["gamma"] = {
        "display": "Gamma Store",
        "exporter": "gamma_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-gamma.yaml",
        "defaults": {
            "gamma_base": {"value": 1, "unit": "count", "desc": "base",
                           "metric_class": "saturation"},
        },
        "optional_overrides": {
            "gamma_queue": {"value": 50, "unit": "count", "desc": "queue size",
                            "metric_class": "saturation"},
        },
    }
    # MIXED: flat AND _critical in the same pack — no such pack exists in the
    # registry today, which is exactly why the generator must be pinned on it.
    packs["delta"] = {
        "display": "Delta Cache",
        "exporter": "delta_exporter",
        "default_on": False,
        "rule_pack_file": "rule-packs/rule-pack-delta.yaml",
        "defaults": {
            "delta_conn": {"value": 10, "unit": "count", "desc": "conn",
                           "metric_class": "saturation"},
        },
        "optional_overrides": {
            "delta_lag": {"value": 7, "unit": "seconds", "desc": "lag",
                          "metric_class": "replication"},
            # base IS a chart default below -> settable by a tenant TODAY
            "delta_conn_critical": {"value": 20, "unit": "count",
                                    "desc": "conn critical"},
            # base is NOT a chart default -> genuinely dormant
            "delta_qsize_critical": {"value": 99, "unit": "count",
                                     "desc": "qsize critical"},
        },
    }
    packs["delta"]["defaults"]["delta_qsize"] = {
        "value": 40, "unit": "count", "desc": "qsize", "metric_class": "saturation"}
    return packs


def _synth_optional_doc():
    """All three optional populations in one doc — `delta_conn` is the only
    chart default, so delta carries flat + base-shipped + dormant at once."""
    return lib.build_registry_doc(
        _synth_optional_packs(), chart_default_keys=frozenset({"delta_conn"}))


def test_shipped_optional_predicate_excludes_critical_and_dimensional():
    """#1311: a `<base>_critical` in the list would be pure decoration —
    resolveDeclaredRows skips the suffix and resolveCriticalRows needs
    defaults[base]. Dimensional tokens are excluded for the same reason
    scaffold_tenant excludes them."""
    assert lib.is_shipped_optional_key("oracle_process_count")
    assert not lib.is_shipped_optional_key("pg_connections_critical")
    assert not lib.is_shipped_optional_key('oracle_tablespace{name="X"}')


def test_has_shipped_chart_base_asks_the_resolver_question():
    """Off the list is not the same as dormant: `resolveCriticalRows` gates on
    `defaults[base]`, so a `_critical` whose base is a chart default resolves
    today. A key with no base at all (no `critical_of`) never does."""
    doc = _synth_optional_doc()
    assert lib.has_shipped_chart_base(doc, "delta_conn_critical")
    assert not lib.has_shipped_chart_base(doc, "delta_qsize_critical")
    assert not lib.has_shipped_chart_base(doc, "alpha_orphan_critical")
    # a flat key has no base either — it is on the list instead
    assert not lib.has_shipped_chart_base(doc, "delta_lag")


def test_pack_header_warning_is_per_key_not_per_tier():
    doc = _synth_optional_doc()

    # 1. pure FLAT -> shipped warning only
    gamma = "\n".join(lib.render_pack_header_lines(doc, "gamma"))
    assert _SHIPPED_MARK in gamma
    assert _BASE_SHIPPED_MARK not in gamma and _DORMANT_MARK not in gamma
    # the sentence PR-C falsified must be gone everywhere
    assert "永久無法啟用" not in gamma
    assert "不是背書" in gamma, "the copy-me-not calibration warning is the point"

    # 2. `_critical` with no chart-default base -> dormant warning only
    alpha = "\n".join(lib.render_pack_header_lines(doc, "alpha"))
    assert _DORMANT_MARK in alpha
    assert _SHIPPED_MARK not in alpha and _BASE_SHIPPED_MARK not in alpha

    # 3. MIXED -> all THREE sub-sections, each key under the right one
    delta = lib.render_pack_header_lines(doc, "delta")
    text = "\n".join(delta)
    for mark in _ALL_MARKS:
        assert mark in text, (mark, delta)

    def _line(pred):
        return next(i for i, ln in enumerate(delta) if pred(ln))

    ship_ln = _line(lambda ln: _SHIPPED_MARK in ln)
    base_ln = _line(lambda ln: _BASE_SHIPPED_MARK in ln)
    dorm_ln = _line(lambda ln: _DORMANT_MARK in ln)
    flat_i = _line(lambda ln: "delta_lag:" in ln)
    live_crit_i = _line(lambda ln: "delta_conn_critical:" in ln)
    dead_crit_i = _line(lambda ln: "delta_qsize_critical:" in ln)
    assert ship_ln < flat_i < base_ln < live_crit_i < dorm_ln < dead_crit_i, (
        "each key must sit under ITS OWN warning", delta)

    # 4. no optional tier at all -> no warning of any kind
    beta = "\n".join(lib.render_pack_header_lines(doc, "beta"))
    assert not any(m in beta for m in _ALL_MARKS)


def test_warning_bodies_make_the_claims_their_headings_promise():
    """Content, not section choice. The previous version of this test only
    checked WHICH heading was selected — the same shape that let a false body
    ship under a correct-looking heading. Assert what each body actually says
    about emission, in both directions."""
    doc = _synth_optional_doc()
    bodies = {
        _SHIPPED_MARK: "\n".join(lib._OPTIONAL_SHIPPED_WARNING_LINES),
        _BASE_SHIPPED_MARK:
            "\n".join(lib._OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES),
        _DORMANT_MARK: "\n".join(lib._OPTIONAL_UNSHIPPED_WARNING_LINES),
    }
    # only the DORMANT body may claim a tenant-set key produces nothing
    for mark, body in bodies.items():
        claims_dead = "並不會發射任何 user_threshold" in body
        assert claims_dead == (mark == _DORMANT_MARK), (mark, body)
    # the two live bodies must say the tenant's value takes effect
    assert "填值並生效" in bodies[_SHIPPED_MARK]
    assert "產出一條真的" in bodies[_BASE_SHIPPED_MARK]
    # and the live-critical body must name the mechanism that makes it live
    assert "defaults[base]" in bodies[_BASE_SHIPPED_MARK]
    assert "resolveCriticalRows" in bodies[_BASE_SHIPPED_MARK]
    # every group the renderer can emit is covered above — no fourth body can
    # be added without this test noticing
    groups = lib.optional_warning_groups(
        doc, {k: e for k, e in doc["keys"].items()
              if e.get("tier") == "optional_overrides"})
    assert {w for w, _ in groups} == {
        lib._OPTIONAL_SHIPPED_WARNING_LINES,
        lib._OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES,
        lib._OPTIONAL_UNSHIPPED_WARNING_LINES,
    }


def test_optional_override_list_is_a_yaml_string_array():
    """The surface is `array of string` (platform-defaults.schema.json), not a
    mapping: a value here would arm the key for every tenant."""
    import yaml

    doc = _synth_optional_doc()
    body = lib.render_optional_overrides_lines(doc, 2)
    parsed = yaml.safe_load("\n".join(["optional_overrides:"] + body))
    assert parsed["optional_overrides"] == lib.optional_override_list_keys(doc)
    assert parsed["optional_overrides"] == ["gamma_queue", "delta_lag"]
    # ...and the _critical sibling of the mixed pack is NOT in it
    assert "delta_conn_critical" not in parsed["optional_overrides"]


def test_real_registry_optional_list_is_the_nine_flat_keys():
    """#1310/#1311 pin: the shipped list is the 9 FLAT keys, never the 25 the
    tier holds. 16 of those 25 are `_critical`; listing them would make 64% of
    the shipped list decoration."""
    doc = lib.build_registry_doc()
    tier = {k for k, e in doc["keys"].items()
            if e.get("tier") == "optional_overrides"}
    shipped = lib.optional_override_list_keys(doc)
    assert len(tier) == 25
    assert shipped == [
        "oracle_wait_time_rate", "oracle_process_count",
        "oracle_pga_allocated_bytes",
        "db2_log_usage_percent", "db2_deadlock_rate",
        "db2_tablespace_used_percent",
        "clickhouse_max_part_count", "clickhouse_replication_queue",
        "clickhouse_memory_tracking_bytes",
    ]
    assert not [k for k in shipped if k.endswith("_critical")]


# ── the header CLAIM vs the resolver's real entry condition (#1310 FIX-2) ──

_RESOLVE_GO = (REPO_ROOT / "components" / "threshold-exporter" / "app" / "pkg" /
               "config" / "resolve.go")
_CHART_VALUES = REPO_ROOT / "helm" / "threshold-exporter" / "values.yaml"


def _resolve_critical_rows_body() -> str:
    """The Go source of `resolveCriticalRows`, or a NAMED failure.

    `str.index` here raised a bare `ValueError: substring not found` when the
    function was renamed or moved — which says nothing about which string was
    missing or why anyone cares, while the caller's docstring promises it
    "fails FIRST, naming the reason". Same for the terminator: if
    `resolveCriticalRows` ever becomes the last function in the file there is
    no following `\\nfunc `, and the body simply runs to EOF.
    """
    src = _RESOLVE_GO.read_text(encoding="utf-8")
    sig = "func (c *ThresholdConfig) resolveCriticalRows("
    start = src.find(sig)
    assert start != -1, (
        f"{_RESOLVE_GO} no longer defines {sig!r}. Every three-way pack-header "
        "claim below is derived from that function's entry condition, so a "
        "rename/removal must be repaired here (repoint this reader) before "
        "those claims mean anything")
    end = src.find("\nfunc ", start + 1)
    if end == -1:  # last function in the file — body runs to EOF
        end = len(src)
    return src[start:end]


def test_critical_tier_entry_condition_is_still_defaults_of_the_base():
    """The premise every `_critical` header claim rests on, read from the Go
    source rather than assumed. If `resolveCriticalRows` ever stops keying off
    `defaults[base]`, the three-way split below is measuring the wrong thing and
    this fails FIRST, naming the reason."""
    body = _resolve_critical_rows_body()
    assert 'baseKey := strings.TrimSuffix(key, "_critical")' in body
    assert "if _, exists := defaults[baseKey]; !exists {" in body
    # ...and it never consults the declared list, which is why `_critical` keys
    # are deliberately off it (#1311)
    assert "OptionalOverrides" not in body and "declared" not in body.lower()


def test_every_real_pack_header_critical_claim_is_true_per_key():
    """Per-pack, per-key truth check against the SHIPPED artifact.

    The claim under test is not "the right heading was picked" but "the heading
    that was picked says something true about THIS key": a `_critical` renders
    under the settable-today warning IFF the chart actually ships its base, and
    under the dormant warning otherwise. Reads values.yaml (what an operator
    gets), while the renderer reads the registry's `chart_default` — so a
    divergence between declaration and artifact surfaces here.
    """
    import yaml

    doc = lib.build_registry_doc()
    chart_doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    chart_defaults = set(
        ((chart_doc.get("thresholdConfig") or {}).get("defaults") or {}))
    shipped_list = set(
        (chart_doc.get("thresholdConfig") or {}).get("optional_overrides") or [])
    assert chart_defaults and shipped_list, "values.yaml faces parsed as empty"

    checked = 0
    for pack in doc["packs"]:
        lines = lib.render_pack_header_lines(doc, pack)
        # section index each key falls under
        section = None
        current = {}
        for ln in lines:
            for mark in _ALL_MARKS:
                if mark in ln:
                    section = mark
            if ln.startswith("#   ") and ":" in ln:
                key = ln[4:].split(":", 1)[0].strip()
                if key in doc["keys"] and \
                        doc["keys"][key].get("tier") == "optional_overrides":
                    current[key] = section
        for key, sect in current.items():
            checked += 1
            base = doc["keys"][key].get("critical_of")
            if key in shipped_list:
                expected = _SHIPPED_MARK
            elif base and base in chart_defaults:
                expected = _BASE_SHIPPED_MARK
            else:
                expected = _DORMANT_MARK
            assert sect == expected, (
                f"{pack}/{key}: header claims {sect!r} but the shipped chart "
                f"says {expected!r} (base={base!r}, "
                f"base_in_chart={bool(base) and base in chart_defaults}, "
                f"on_declared_list={key in shipped_list})")
    assert checked == 25, (
        f"expected all 25 optional_overrides keys to be covered, saw {checked}")


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
    specs = lib.surface_specs(doc)
    ids = [s["id"] for s in specs]
    # the file-level surfaces are order-pinned: two `defaults` mappings, then
    # the `optional_overrides` lists — two of which share the SAME two files as
    # the mappings above (#1310), plus the try-local seed, which stopped being
    # a hand-maintained third copy in #1176/#1344 (its only gate compared the
    # PARSED list, so it never saw the per-key counter-example comments).
    assert ids[:5] == [
        "helm-defaults", "dev-defaults", "helm-optional", "dev-optional",
        "try-local-optional"]
    assert "pack-mariadb" in ids and "pack-kubernetes" in ids
    assert len(ids) == 5 + 13  # 13 threshold packs, non-threshold packs none
    # the two surfaces per file must target the same path but distinct blocks
    by_id = {s["id"]: s for s in specs}
    assert by_id["helm-defaults"]["path"] == by_id["helm-optional"]["path"]
    assert by_id["dev-defaults"]["path"] == by_id["dev-optional"]["path"]
    assert by_id["try-local-optional"]["path"] not in {
        by_id["helm-optional"]["path"], by_id["dev-optional"]["path"]}
    assert len({s["id"] for s in specs}) == len(specs)


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


# ── the TENANT-FACING doc's three-population claim (#1310) ────────────────
#
# `docs/getting-started/for-tenants.md` (+ `.en.md`) tells a CUSTOMER which of
# the three optional-tier populations a key belongs to, and it does so with
# hard numbers (9 / 5 / 11) and with the five settable-today `<base>_critical`
# keys spelled out by name. That paragraph is a HAND COPY of a derived
# contract: flip one tier in `scaffold_tenant.RULE_PACKS`, or ship one more
# chart default, and the page a customer reads goes quietly false — the same
# drift class this whole PR exists to delete, landing on the one surface with
# no generator and (until now) no gate.
#
# The derivation below reuses the SAME two predicates the pack-header renderer
# uses (`is_shipped_optional_key` for list membership, `has_shipped_chart_base`
# for the split among the rest), so the doc cannot disagree with the rule-pack
# headers either; and every predicate answer is cross-checked against the
# SHIPPED `helm/threshold-exporter/values.yaml`, so a registry that has drifted
# from the artifact fails here instead of being blessed by it.

_TENANT_DOC_RELS = (
    "docs/getting-started/for-tenants.md",
    "docs/getting-started/for-tenants.en.md",
)
_TENANT_DOCS = tuple(REPO_ROOT / rel for rel in _TENANT_DOC_RELS)

# ⛔ EXACT tokens, never substring/`in`: `mysql_connections_critical` contains
# `mysql_connections`, so a substring test reports "found" for precisely the
# stale spellings that most resemble a live key (feedback: 子字串比對會藏住漂移).
_DOC_CRITICAL_NAME_RE = re.compile(r"`([a-z][a-z0-9_]*_critical)`")
# glob-shaped family examples in the dormant bullet: `kafka_*_critical`
_DOC_CRITICAL_FAMILY_RE = re.compile(r"`([a-z][a-z0-9_]*?)_\*_critical`")
# "（目前 9 個）" / "(9 today)" — the ZH SSOT and the EN mirror
_DOC_COUNT_RE = re.compile(r"目前\s*(\d+)\s*個|\((\d+) today")


def _doc_group_bullets(path: Path) -> list[str]:
    """The three `- **…**` bullets of the three-population paragraph, in order.

    Every step FAILS LOUD rather than returning a short list. A silently empty
    or partial parse would make every assertion below vacuous — a gate that
    reports success forever is worse than no gate, because it also buys the
    reader's confidence (same discipline as
    `check_portal_rulepack_claims.parse_portal_claims`).
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    anchors = [i for i, ln in enumerate(lines)
               if "`thresholdConfig.defaults`" in ln]
    assert len(anchors) == 1, (
        f"{path.name}: expected exactly one paragraph mentioning "
        f"`thresholdConfig.defaults` to anchor the three-population bullets, "
        f"found {len(anchors)} at lines {[i + 1 for i in anchors]}. The doc's "
        "shape changed and this gate can no longer find the claims — repoint "
        "the anchor, do not delete the check")
    bullets: list[str] = []
    for ln in lines[anchors[0] + 1:]:
        if ln.startswith("- "):
            bullets.append(ln)
        elif bullets:
            break  # first non-bullet line after the block closes it
    assert len(bullets) == 3, (
        f"{path.name}: expected 3 population bullets after the anchor, parsed "
        f"{len(bullets)}. If the doc genuinely describes a different number of "
        "populations, update the derivation below too — do not loosen this")
    return bullets


def _derive_three_populations() -> tuple[set, set, set]:
    """(declared, base_shipped, dormant) — the optional tier split by TRUTH.

    Same predicates as `optional_warning_groups`, each answer re-asked of the
    shipped chart so declaration and artifact cannot silently disagree.
    """
    import yaml

    doc = lib.build_registry_doc()
    chart_doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    threshold_cfg = chart_doc.get("thresholdConfig") or {}
    chart_defaults = set(threshold_cfg.get("defaults") or {})
    shipped_list = set(threshold_cfg.get("optional_overrides") or [])
    assert chart_defaults and shipped_list, "values.yaml faces parsed as empty"

    tier = {k for k, e in doc["keys"].items()
            if e.get("tier") == "optional_overrides"}
    declared = {k for k in tier if lib.is_shipped_optional_key(k)}
    assert declared == shipped_list, (
        "the membership predicate and the SHIPPED optional_overrides list "
        f"disagree (predicate-only={sorted(declared - shipped_list)}, "
        f"artifact-only={sorted(shipped_list - declared)}) — the doc's group 1 "
        "would be describing something no operator ever received")

    base_shipped = set()
    for key in tier - declared:
        base = doc["keys"][key].get("critical_of")
        by_registry = lib.has_shipped_chart_base(doc, key)
        by_artifact = bool(base) and base in chart_defaults
        assert by_registry == by_artifact, (
            f"{key}: registry says base-shipped={by_registry} but "
            f"values.yaml says {by_artifact} (base={base!r})")
        if by_registry:
            base_shipped.add(key)
    return declared, base_shipped, tier - declared - base_shipped


@pytest.mark.parametrize("path", _TENANT_DOCS, ids=lambda p: p.name)
def test_tenant_doc_three_population_claim_matches_the_derivation(path):
    """The customer-facing counts AND the five spelled-out names, both langs."""
    declared, base_shipped, dormant = _derive_three_populations()
    bullets = _doc_group_bullets(path)
    expected = [len(declared), len(base_shipped), len(dormant)]

    counts = []
    for idx, bullet in enumerate(bullets):
        match = _DOC_COUNT_RE.search(bullet)
        assert match, (
            f"{path.name}: population bullet {idx + 1} states no count. Either "
            "restore the number (and it will be gated here) or drop the count "
            f"from all three bullets: {bullet[:120]!r}")
        counts.append(int(match.group(1) or match.group(2)))
    assert counts == expected, (
        f"{path.name}: the doc claims {counts} keys in "
        "(declared / base-shipped / dormant) but the registry + shipped "
        f"values.yaml derive {expected}. The derivation is the SSOT — fix the "
        "doc, in BOTH languages")

    # group 2 is the only bullet that spells its members out; EXACT set
    # equality, so a renamed or retired key cannot hide behind a live prefix.
    named = set(_DOC_CRITICAL_NAME_RE.findall(bullets[1]))
    assert named == base_shipped, (
        f"{path.name}: the doc names {sorted(named)} as the `<base>_critical` "
        f"keys a tenant can set today, the derivation says "
        f"{sorted(base_shipped)} (doc-only={sorted(named - base_shipped)}, "
        f"missing={sorted(base_shipped - named)})")

    # ...and the other two bullets must NOT name individual keys: a literal
    # name there would be a fourth ungated claim on this page.
    for idx in (0, 2):
        stray = _DOC_CRITICAL_NAME_RE.findall(bullets[idx])
        assert not stray, (
            f"{path.name}: population bullet {idx + 1} now spells out "
            f"{stray} — extend this gate to pin that bullet's membership too, "
            "do not ship an unchecked key list")

    # the dormant bullet gives glob-shaped families (`kafka_*_critical`);
    # each must still have at least one real member.
    families = _DOC_CRITICAL_FAMILY_RE.findall(bullets[2])
    assert families, (
        f"{path.name}: the dormant bullet no longer gives any `x_*_critical` "
        "example family — if that was deliberate, drop this assertion with it")
    for family in families:
        assert any(k.startswith(family + "_") for k in dormant), (
            f"{path.name}: the doc offers `{family}_*_critical` as an example "
            f"of the dormant group, but no dormant key starts with "
            f"'{family}_' (dormant={sorted(dormant)})")

# The `textwrap` entry points that can emit a wrapped line. ⛔ Enumerating THESE
# is legitimate where enumerating spellings was not: the list has an authority
# outside this repo — it is `textwrap`'s public API — and the other two members
# of that API (`dedent`, `indent`) cannot split anything, so admitting them
# would only produce false reports.
_TEXTWRAP_WRAPPING_API = ("wrap", "fill", "shorten", "TextWrapper")


def _textwrap_wrapping_calls(tree) -> list[int]:
    """Line of every call reaching a `textwrap` wrapping entry point, ANY spelling.

    ⛔ IT MATCHES SHAPES; IT DOES NOT RESOLVE ANYTHING, and every previous
    wording of this docstring overstated that. Four rounds of blind review each
    added the spelling it had just walked through — the literal `textwrap.wrap`,
    then `from textwrap import wrap`, then `import textwrap as tw`, then the
    stdlib's own `TextWrapper(...).wrap(...)` — and the wording after each round
    claimed a little more completeness than the round before. The last one
    claimed there was no fifth spelling. There is.

    ⚠️ MEASURED ESCAPES — all six get past THIS guard, each run with both
    keyword arguments removed as well:
      * binding the module to another name (`X = textwrap`, then `X.wrap(...)`)
      * `getattr(textwrap, "wrap")(...)`
      * `from textwrap import *`
      * subclassing `TextWrapper` and calling `.wrap()` on the subclass
      * `functools.partial(textwrap.wrap, ...)`
      * `importlib.import_module("textwrap")`
    They are latent — nothing in this module is written any of those ways — and
    they are listed rather than chased because closing them needs real binding
    analysis, which a test module is the wrong place for. ⇒ Treat this as a
    tripwire for the ordinary spellings, NOT as an invariant.

    ⚠️ ONE SECOND LINE OF DEFENCE, and it covers exactly one call site. Planting
    an escape at each of the six in turn, `check_threshold_registry --ci` stays
    green for five of them and goes RED for `render_chart_defaults_lines` —
    that renderer is the one whose bodies carry a token the escape actually
    splits today, so a changed wrap surfaces there as a stale generated block.
    ⛔ Do NOT read the other five greens as "nothing is watching them"; an
    earlier wording of this paragraph said exactly that and it was measured
    false. Perturbing each call site's width in turn and re-running `--regen`,
    `_entry_lines` rewrites 6 golden-compared blocks and `_counterexample_lines`
    rewrites 5 — including the same two files this paragraph used to hand to
    `render_chart_defaults_lines` alone. Those two sites are green because
    their bodies happen to wrap identically, NOT because a golden comparison
    cannot see them. The three that genuinely reach no golden surface, at 0
    each, are `annotate_defaults_counterexamples`, `_append_wrapped_comment`
    and `stub_counterexample_lines` — and the middle one is the very call site
    whose docstring claims a byte-for-byte revert, so that claim has no golden
    witness behind it either.
    """
    import ast

    module_names = {"textwrap"}
    from_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            module_names |= {a.asname or a.name
                             for a in node.names if a.name == "textwrap"}
        elif isinstance(node, ast.ImportFrom) and node.module == "textwrap":
            for alias in node.names:
                from_names[alias.asname or alias.name] = alias.name

    def reaches(func) -> bool:
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                return (func.value.id in module_names
                        and func.attr in _TEXTWRAP_WRAPPING_API)
            # `TextWrapper(...).wrap(...)` — the receiver is itself a call.
            if isinstance(func.value, ast.Call):
                return reaches(func.value.func)
            return False
        if isinstance(func, ast.Name):
            return from_names.get(func.id) in _TEXTWRAP_WRAPPING_API
        return False

    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and reaches(node.func)]


def test_no_call_site_wraps_a_comment_on_its_own() -> None:
    """⛔ `_wrap_comment` must stay the ONLY place that calls `textwrap`.

    The two flags it passes are not style: `textwrap` breaks on hyphens AND
    breaks long words by default, so either can cut an identifier or a path in
    half across the two comment lines this module emits — and a reference split
    that way is invisible to `git grep` (#1373 shipped exactly that).

    ⛔ This is a SET assertion, not a spot check, because the defect it guards
    is precisely "one call site did not get the fix". Before #1453 there were
    six call sites: three passed `break_on_hyphens=False`, three did not, and
    NONE passed `break_long_words=False`. Fixing the cited site would have left
    five. Measured at the time: two shipped artifacts carried live hyphen
    splits (`opt-in` and `lag-template` cut in half) — one of them a
    customer-facing `_defaults.yaml`.
    """
    import ast

    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    raw = _textwrap_wrapping_calls(tree)
    inside = [
        node.lineno
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name == "_wrap_comment"
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
    ]

    assert raw and set(raw) <= set(inside), (
        "every textwrap call in this module must sit inside `_wrap_comment`; "
        f"raw calls at lines {raw}, `_wrap_comment` spans {inside}. ⛔ Route the "
        "new call through the helper — do not copy the two keyword arguments, "
        "which is how three of the six original sites ended up with half the "
        "rule and three with none of it.")


def test_wrap_comment_never_splits_a_token() -> None:
    """⛔ The two keyword arguments themselves, asked as behaviour not spelling.

    `test_no_call_site_wraps_a_comment_on_its_own` proves the DECISION lives in
    one place. It does not prove the decision is still the right one: measured,
    deleting `break_on_hyphens=False` AND `break_long_words=False` together left
    that test — and the whole module — green, BEFORE THIS TEST EXISTED. (Today
    the same edit reds this test, which is the point of it; the sentence
    describes the state it was added to fix.) The centralisation was guarded and
    the thing centralised was not.

    ⚠️ WHAT EACH HALF IS ACTUALLY WORTH, because the first version of this
    docstring claimed both were unpinned and that was wrong. Deleting the hyphen
    flag alone reds `tests/lint/test_check_threshold_registry.py::test_real_repo_is_green`,
    which compares the regenerated artifact — so that half already had a witness,
    just an indirect one in another module. Deleting the long-word flag alone
    reds nothing but this test. The net new coverage here is the long-word half;
    the hyphen case is kept as a direct, local witness for a rule whose only
    other guard is a golden comparison three modules away.

    ⛔ THE WIDTH IS LOAD-BEARING AND WAS WRONG. At 24 the hyphen assertion is
    TAUTOLOGICAL — measured, `textwrap` produces byte-identical output with and
    without the flag at that width, so the case passed for a reason unrelated to
    what it claims to test. Only at 20 or below does the mutant split the token
    while the shipped helper does not. Do not raise it.
    """
    hyphenated = "see negative-db2.yaml for the counterexample and then stop"
    segments = lib._wrap_comment(hyphenated, 20)
    assert len(segments) > 1, "width too generous to exercise wrapping at all"
    assert any("negative-db2.yaml" in seg for seg in segments), (
        "`negative-db2.yaml` came back split across segments: "
        f"{segments}. ⛔ A reference broken that way is invisible to `git grep`, "
        "which is the accident this helper exists to prevent.")

    long_name = "test_the_index_listing_is_a_real_seam_on_both_derivations"
    segments = lib._wrap_comment(f"pinned by {long_name} today", 24)
    assert any(long_name in seg for seg in segments), (
        f"a token longer than the width came back split: {segments}. ⛔ The "
        "default breaks long words; that is the half none of the six original "
        "call sites had.")
