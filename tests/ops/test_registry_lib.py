"""Tests for scripts/tools/ops/_registry_lib.py (TRK-339 WS1a / #1200).

Since D-A PR-3 the registry file is the hand-authored SoT and
scaffold_tenant.RULE_PACKS's threshold half is generated from it. Pins:
  - extraction structure (packs / keys / tiers / derived critical_of)
  - loud failure on identity collisions (never silently shadow a contract key)
  - load + schema-validate on the real committed registry
  - the registry is a fixed point of project -> extract, and an authored file
    that breaks an extraction rule is not
  - the generated RULE_PACKS module is fresh, and a stale one is caught
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


def _vendor(value) -> dict:
    """A `validation` that holds for a key shipping *value*: one vendor source using it."""
    return {"basis": "vendor", "sources": [{
        "org": "Example Vendor", "url": "https://example.com/alerts.yaml",
        "quote": f"expr: metric > {value}", "value": value}]}


def _armed(*default_keys: str, packs: dict | None = None) -> dict:
    """An arming table for *packs* (SYNTH_PACKS by default): `default` on
    *default_keys*, `site_specific` with both reasons on every other key that
    is not a `<base>_critical` — what strict arming demands of a full doc."""
    packs = SYNTH_PACKS if packs is None else packs
    out: dict = {}
    for pack in packs.values():
        tiers = {**(pack.get("defaults") or {}),
                 **(pack.get("optional_overrides") or {})}
        for key in tiers:
            base = key[: -len("_critical")] if key.endswith("_critical") else None
            if base and base in tiers:
                continue
            out[key] = ({"arming": "default", "portability": "portable",
                         "validation": _vendor(tiers[key]["value"])}
                        if key in default_keys else
                        {"arming": "operator", "portability": "site_specific",
                         "portability_reason": f"{key} depends on the site",
                         "portability_reason_zh": f"{key} 依部署而定"})
    return out


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


# ── metric_class: inheritance / strict completeness ───────────────────────

def test_critical_inherits_base_metric_class():
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert doc["keys"]["alpha_conn_critical"]["metric_class"] == "saturation"


def test_strict_unclassified_key_fails_loud():
    packs = copy.deepcopy(SYNTH_PACKS)
    del packs["beta"]["defaults"]["beta_lag"]["metric_class"]
    with pytest.raises(ValueError, match="unclassified threshold keys"):
        lib.build_registry_doc(packs, strict_metric_class=True)


def test_real_registry_every_key_classified():
    """Every key carries a metric_class; the curated saturation set stays
    exactly the 22 bases (+ their _critical) it was before PR-3 folded the
    old backfill table into the file — the fold added NO new saturation."""
    doc = lib.build_registry_doc()
    classes = {k: e["metric_class"] for k, e in doc["keys"].items()}
    assert len(classes) == len(doc["keys"])  # no key unclassified
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


def test_committed_registry_is_a_fixed_point():
    """The invariant the gate enforces: project -> extract reproduces it."""
    assert lib.fixed_point_problems(lib.load_registry()) == []


def test_synthetic_doc_is_a_fixed_point():
    doc = lib.build_registry_doc(
        SYNTH_PACKS, arming=_armed("alpha_conn"),
        strict_metric_class=True, strict_arming=True)
    assert lib.fixed_point_problems(doc) == []
    assert lib.validate_registry(doc) == []


_BLOCKER = {"kind": "unwired", "tracking": 3770, "reason": "no source",
            "reason_zh": "沒有資料源"}


def _make_default(entry: dict) -> None:
    """Turn *entry* into a portable, unblocked `arming: default`."""
    entry["arming"] = "default"
    entry["portability"] = "portable"
    for f in (*lib.PORTABILITY_REASON_FIELDS, "blocker", "default_exception"):
        entry.pop(f, None)
    entry["validation"] = _vendor(entry["value"])


def _fixed_point_doc():
    return lib.build_registry_doc(
        SYNTH_PACKS, arming=_armed("alpha_conn"),
        deprecated_aliases={"alpha_conns": "alpha_conn"},
        strict_metric_class=True)


@pytest.mark.parametrize("label,mutate,expect", [
    # each one is an authored-file mistake the extraction used to make
    # impossible by construction; the fixed point must still refuse it
    ("critical_of on a key with no base",
     lambda d: d["keys"]["alpha_orphan_critical"].__setitem__("critical_of", "alpha_orphan"),
     "alpha_orphan_critical"),
    ("critical_of dropped where it derives",
     lambda d: d["keys"]["alpha_conn_critical"].pop("critical_of"),
     "alpha_conn_critical"),
    ("critical_of pointing at the wrong base",
     lambda d: d["keys"]["alpha_conn_critical"].__setitem__("critical_of", "beta_lag"),
     "alpha_conn_critical"),
    ("arming: default on an optional_overrides key",
     lambda d: _make_default(d["keys"]["alpha_orphan_critical"]),
     "chart-shippable"),
    ("arming authored on a critical tier",
     lambda d: d["keys"]["alpha_conn_critical"].__setitem__("arming", "default"),
     "follows its base"),
    ("a key with no arming or portability",
     lambda d: [d["keys"]["beta_lag"].pop(f) for f in lib.ARMING_FIELDS
                if f in d["keys"]["beta_lag"]],
     "has no arming / portability"),
    ("a key with arming but no portability",
     lambda d: [d["keys"]["beta_lag"].pop(f) for f in
                ("portability", *lib.PORTABILITY_REASON_FIELDS)],
     "has no portability"),
    ("an unknown arming state",
     lambda d: d["keys"]["beta_lag"].__setitem__("arming", "later"),
     "is not one of"),
    ("YAML 1.1 reading an unquoted off as false",
     lambda d: d["keys"]["beta_lag"].__setitem__("arming", False),
     "is not one of"),
    ("an unknown portability",
     lambda d: d["keys"]["beta_lag"].__setitem__("portability", "maybe"),
     "is not one of"),
    ("a non-portable key without its Chinese reason",
     lambda d: d["keys"]["beta_lag"].pop("portability_reason_zh"),
     "needs a non-blank portability_reason_zh"),
    ("a blank reason",
     lambda d: d["keys"]["beta_lag"].__setitem__("portability_reason", "  "),
     "needs a non-blank portability_reason"),
    ("a reason with a trailing newline",
     lambda d: d["keys"]["beta_lag"].__setitem__("portability_reason", "x\n"),
     "one line"),
    ("a reason with an embedded newline",
     lambda d: d["keys"]["beta_lag"].__setitem__("portability_reason_zh", "a\nb"),
     "one line"),
    ("a reason on a portable key",
     lambda d: d["keys"]["alpha_conn"].__setitem__("portability_reason", "why"),
     "remove ['portability_reason']"),
    ("a shipped key that is not portable, with no exception",
     lambda d: d["keys"]["alpha_conn"].update(
         portability="ratio_pending", portability_reason="r",
         portability_reason_zh="r"),
     "a shipped value must be portable"),
    ("a default_exception on a portable key",
     lambda d: d["keys"]["alpha_conn"].__setitem__(
         "default_exception", {"tracking": 1}),
     "remove default_exception"),
    ("a default_exception on an operator key",
     lambda d: d["keys"]["beta_lag"].__setitem__(
         "default_exception", {"tracking": 1}),
     "only applies to a shipped key"),
    ("a default_exception without an issue number",
     lambda d: d["keys"]["alpha_conn"].update(
         portability="site_specific", portability_reason="r",
         portability_reason_zh="r", default_exception={"tracking": "soon"}),
     "default_exception.tracking must be an issue number"),
    ("a blocker on a shipped key",
     lambda d: d["keys"]["alpha_conn"].__setitem__("blocker", dict(_BLOCKER)),
     "a blocked key cannot ship"),
    ("a blocker with an unknown kind",
     lambda d: d["keys"]["beta_lag"].__setitem__(
         "blocker", {**_BLOCKER, "kind": "later"}),
     "blocker.kind"),
    ("a blocker with a bool issue number",
     lambda d: d["keys"]["beta_lag"].__setitem__(
         "blocker", {**_BLOCKER, "tracking": True}),
     "blocker.tracking must be an issue number"),
    ("a blocker missing its Chinese reason",
     lambda d: d["keys"]["beta_lag"].__setitem__(
         "blocker", {k: v for k, v in _BLOCKER.items() if k != "reason_zh"}),
     "blocker must be exactly"),
    ("key under a pack the registry does not list",
     lambda d: d["keys"]["beta_lag"].__setitem__("pack", "gamma"),
     "not a valid contract"),
    ("alias onto no key",
     lambda d: d["deprecated_aliases"].__setitem__("old_x", "ghost"),
     "no threshold"),
    ("alias whose old spelling is still live",
     lambda d: d["deprecated_aliases"].__setitem__("beta_lag", "alpha_conn"),
     "ACTIVE"),
    ("key with no metric_class",
     lambda d: d["keys"]["beta_lag"].pop("metric_class"),
     "unclassified"),
])
def test_an_authored_mistake_breaks_the_fixed_point(label, mutate, expect):
    doc = _fixed_point_doc()
    assert lib.fixed_point_problems(doc) == [], "baseline must hold"
    mutate(doc)
    problems = lib.fixed_point_problems(doc)
    assert problems, label
    assert any(expect in p for p in problems), (label, problems)


@pytest.mark.parametrize("label,mutate", [
    ("a key entry that is not a mapping",
     lambda d: d["keys"].__setitem__("beta_lag", "oops")),
    ("a pack that is null", lambda d: d["packs"].__setitem__("beta", None)),
])
def test_a_misshapen_entry_is_reported_not_raised(label, mutate):
    """Hand-authored: a shape error is a contract problem reported next to
    the schema errors, never an exception that crashes the gate."""
    doc = _fixed_point_doc()
    mutate(doc)
    problems = lib.fixed_point_problems(doc)
    assert any(p.startswith("not a valid contract") for p in problems), (label, problems)


def test_committed_registry_doc_refuses_a_broken_file(tmp_path):
    doc = _fixed_point_doc()
    doc["keys"]["alpha_conn_critical"].pop("critical_of")
    path = tmp_path / "registry.yaml"
    path.write_text(lib.yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    with pytest.raises(ValueError, match="alpha_conn_critical"):
        lib.committed_registry_doc(str(path))


def test_a_key_written_twice_is_refused_not_last_wins(tmp_path):
    # Hand-authored now: a duplicated mapping key must not silently keep the
    # second entry (the extraction's two-packs check never sees it).
    text = lib.yaml.safe_dump(_fixed_point_doc(), sort_keys=False)
    dup = text.replace("keys:\n", "keys:\n  beta_lag:\n    pack: alpha\n", 1)
    assert dup != text
    path = tmp_path / "registry.yaml"
    path.write_text(dup, encoding="utf-8")
    with pytest.raises(Exception, match="duplicate key"):
        lib.load_registry(str(path))


# ── the generated RULE_PACKS module ───────────────────────────────────────

def test_packaged_doc_is_the_registry_minus_what_rule_packs_does_not_carry():
    committed = lib.load_registry()
    packaged = lib.packaged_registry_doc()
    expected = {k: {f: v for f, v in e.items() if f not in lib.ARMING_FIELDS}
                for k, e in committed["keys"].items()}
    assert packaged["keys"] == expected
    assert packaged["packs"] == committed["packs"]
    assert "deprecated_aliases" not in packaged


def test_a_critical_classed_apart_from_its_base_is_refused():
    doc = _fixed_point_doc()
    doc["keys"]["alpha_conn_critical"]["metric_class"] = "latency"
    problems = lib.fixed_point_problems(doc)
    assert any("critical tier carries its base" in p for p in problems), problems


def test_committed_module_is_fresh():
    assert lib.check_rule_packs_module(lib.load_registry()) is None


def test_a_stale_module_is_caught(tmp_path):
    doc = lib.load_registry()
    path = tmp_path / "_rule_packs_generated.py"
    path.write_text(lib.render_rule_packs_module(doc), encoding="utf-8")
    assert lib.check_rule_packs_module(doc, str(path)) is None
    key = next(iter(doc["keys"]))
    doc["keys"][key]["value"] = doc["keys"][key]["value"] + 1
    assert "not what the registry generates" in lib.check_rule_packs_module(doc, str(path))


def test_module_projects_back_to_the_registry():
    """The generated module IS the projection: executing it and extracting
    again gives the committed registry."""
    doc = lib.load_registry()
    ns: dict = {}
    exec(compile(lib.render_rule_packs_module(doc), "<module>", "exec"), ns)
    assert ns["THRESHOLD_PACKS"] == lib.project_rule_packs(doc)


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
    assert any("ghost_key" in d and "not derived" in d for d in diffs)


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


# ── arming / portability / blocker (D-A A1, #3773) ─────────────────────────

def test_synthetic_extraction_stays_pure():
    """Injected packs get NO enrichment unless the caller passes a table."""
    doc = lib.build_registry_doc(SYNTH_PACKS)
    assert all(f not in e for e in doc["keys"].values() for f in lib.ARMING_FIELDS)


def test_arming_injected_table():
    doc = lib.build_registry_doc(SYNTH_PACKS, arming=_armed("alpha_conn"))
    assert doc["keys"]["alpha_conn"]["arming"] == "default"
    assert doc["keys"]["beta_lag"]["arming"] == "operator"
    assert "arming" not in doc["keys"]["alpha_conn_critical"]


def test_arming_default_on_non_defaults_tier_fails_loud():
    with pytest.raises(ValueError, match="chart-shippable"):
        lib.build_registry_doc(
            SYNTH_PACKS, arming={"alpha_orphan_critical": {
                "arming": "default", "portability": "portable"}})


def test_arming_unknown_key_fails_loud():
    with pytest.raises(ValueError, match="not found in the packs"):
        lib.build_registry_doc(SYNTH_PACKS, arming={"ghost": {"arming": "default"}})


def test_arming_unknown_field_fails_loud():
    with pytest.raises(ValueError, match="only .* are arming fields"):
        lib.build_registry_doc(
            SYNTH_PACKS, arming={"alpha_conn": {"arming": "default", "why": "x"}})


def test_strict_arming_names_every_unarmed_key():
    with pytest.raises(ValueError) as exc:
        lib.build_registry_doc(SYNTH_PACKS, strict_arming=True)
    for key in ("alpha_conn", "beta_lag", "alpha_orphan_critical"):
        assert f"{key!r} has no arming / portability" in str(exc.value)
    # a <base>_critical is never asked for one
    assert "'alpha_conn_critical' has no" not in str(exc.value)


def test_a_blocked_key_is_not_a_validation_candidate():
    """Portable + operator in the defaults tier is the A2 input only while
    nothing blocks it; an optional_overrides key is not one (D-A A2a: the
    shipping gate refuses `default` there)."""
    entry = {"arming": "operator", "portability": "portable", "tier": "defaults"}
    assert lib.is_validation_candidate(entry)
    assert not lib.is_validation_candidate({**entry, "tier": "optional_overrides"})
    assert not lib.is_validation_candidate({**entry, "blocker": dict(_BLOCKER)})
    assert not lib.is_validation_candidate({**entry, "arming": "default"})
    assert not lib.is_validation_candidate({**entry, "portability": "site_specific"})
    assert not lib.is_validation_candidate({**entry, "critical_of": "x"})


def test_notes_say_why_and_render_nothing_for_a_clean_default():
    blocked = {"arming": "operator", "portability": "portable",
               "blocker": dict(_BLOCKER)}
    assert lib.arming_notes_zh(blocked) == ["未啟用（沒有資料源，#3770）：沒有資料源"]
    cand = {"arming": "operator", "portability": "portable", "tier": "defaults"}
    assert lib.arming_notes_zh(cand) == [
        f"未啟用（{lib.CANDIDATE_LABEL_ZH}）：{lib.CANDIDATE_NOTE_ZH}"]
    exc = {"arming": "default", "portability": "ratio_pending",
           "portability_reason_zh": "絕對值", "default_exception": {"tracking": 1320}}
    assert lib.arming_notes_zh(exc) == ["出貨例外（待改比值，#1320）：絕對值"]
    assert lib.arming_notes_zh({"arming": "default", "portability": "portable"}) == []


def test_a_portable_optional_tier_key_says_it_must_change_tier_first():
    """`default` is defaults-tier only, so a portable optional-tier key is not
    a candidate and its note does not say "ships once validated" (#3773
    review F3; D-A A2a made the derived set agree with the note)."""
    entry = {"arming": "operator", "portability": "portable",
             "tier": "optional_overrides"}
    assert lib.arming_notes_zh(entry) == [
        f"未啟用（{lib.DECLARED_ONLY_LABEL_ZH}）：{lib.DECLARED_ONLY_NOTE_ZH}"]
    assert "shadow" not in lib.CANDIDATE_NOTE_ZH + lib.DECLARED_ONLY_NOTE_ZH
    assert lib.arming_notes_zh({**entry, "tier": "defaults"}) == [
        f"未啟用（{lib.CANDIDATE_LABEL_ZH}）：{lib.CANDIDATE_NOTE_ZH}"]


def test_unarmed_block_names_the_key_on_every_reason():
    """A key with a blocker AND a non-portable reason renders both, each led
    by the key (#3773 review F6)."""
    packs = {"beta": {**SYNTH_PACKS["beta"]}}
    arming = {"beta_lag": {
        "arming": "operator", "portability": "site_specific",
        "portability_reason": "r", "portability_reason_zh": "依部署",
        "blocker": dict(_BLOCKER)}}
    doc = lib.build_registry_doc(packs, arming=arming)
    text = "\n".join(lib.render_unarmed_lines(doc, 4))
    assert "#   beta_lag 未啟用（沒有資料源，#3770）：沒有資料源" in text
    assert "#   beta_lag 未啟用（依部署而定）：依部署" in text


def test_a_shipped_exception_is_marked_in_the_chart_defaults():
    """A non-portable shipped value says so beside its value in helm values,
    not only in the rule-pack header (#3773 review F5)."""
    arming = _armed()
    arming["alpha_conn"] = {
        "arming": "default", "portability": "ratio_pending",
        "portability_reason": "r", "portability_reason_zh": "絕對值",
        "default_exception": {"tracking": 3778}, "validation": _vendor(80)}
    doc = lib.build_registry_doc(SYNTH_PACKS, arming=arming)
    text = "\n".join(lib.render_chart_defaults_lines(doc, 4))
    assert "出貨例外（待改比值，#3778）：絕對值" in text
    assert text.index("出貨例外") < text.index("alpha_conn: 80")


def test_notes_are_total_over_malformed_fields():
    """The renderers run after validation fails; a malformed field renders
    nothing rather than raising and hiding the violation (CodeRabbit #3741)."""
    for bad in ({"arming": ["operator"], "portability": "portable"},
                {"arming": "operator", "portability": {"x": 1}},
                {"arming": "operator", "portability": "portable", "blocker": "x"},
                {"arming": "operator", "portability": "site_specific",
                 "portability_reason_zh": 3}):
        lib.arming_notes_zh(bad)


def test_real_registry_chart_default_set():
    """The chart set is the behavior-preserving 6-key pre-rewire set plus the
    owner-decided pg_connections / pg_replication_lag promotion (#1200 Q3=C /
    D4 folding, 2026-07-25). A1 / A1.5 changed the fields, not the set.
    #3778 then took mysql_threads_running out (arming: operator — a healthy
    value scales with the host's CPU count) and swapped the absolute
    mysql_connections for mysql_connection_usage_percent: 8 -> 7.
    D-A A2b then promoted the four `%` candidates that carry a validation
    (es_disk_usage_percent, es_heap_usage_percent, jvm_memory,
    oracle_tablespace_used_percent): 7 -> 11. rabbitmq_node_mem_percent stayed
    operator: no source uses its 80 (blocker: unmeasured)."""
    doc = lib.build_registry_doc()
    tagged = {k for k, e in doc["keys"].items() if lib.is_chart_armed(e)}
    # Pin the DECISION, not just the mechanism: the owner-approved pg
    # promotion (#1232) must not silently vanish via an edit that keeps the
    # count intact (CodeRabbit review of #1232).
    assert {"pg_connections", "pg_replication_lag"} <= tagged
    assert "mysql_connection_usage_percent" in tagged
    assert "mysql_threads_running" not in tagged
    assert {"es_disk_usage_percent", "es_heap_usage_percent", "jvm_memory",
            "oracle_tablespace_used_percent"} <= tagged
    assert "rabbitmq_node_mem_percent" not in tagged
    assert len(tagged) == 11


def test_every_real_key_states_its_arming():
    """Every non-critical key states arming and portability; no
    `<base>_critical` states anything (it follows its base)."""
    doc = lib.build_registry_doc()
    for key, entry in doc["keys"].items():
        if entry.get("critical_of"):
            assert not any(f in entry for f in lib.ARMING_FIELDS), key
        else:
            assert entry.get("arming") in lib.ARMING_STATES, key
            assert entry.get("portability") in lib.PORTABILITY_STATES, key
    assert lib.arming_problems(doc["keys"], strict=True) == []


def test_real_shipped_non_portable_keys_are_the_recorded_exceptions():
    """The owner kept shipping the two absolute MariaDB values (#3773) under
    a recorded exception; #3778 resolved both (the count became a share of
    max_connections, threads_running stopped shipping a value), so no shipped
    key needs an exception any more."""
    doc = lib.build_registry_doc()
    exceptions = {k: e["default_exception"]["tracking"]
                  for k, e in doc["keys"].items() if "default_exception" in e}
    assert exceptions == {}


def test_real_validation_candidates_are_derived():
    """The A2 input set is derived: portable, operator-supplied, unblocked,
    in the defaults tier. Pinned so a reclassification shows up as a
    deliberate test edit. The two DB2 keys in optional_overrides are not in
    it (D-A A2a): the shipping gate refuses `default` outside the defaults
    tier, so they are not promotable as they stand (#1320 decides their tier).
    D-A A2b took five out: four `%` keys were promoted, and
    rabbitmq_node_mem_percent got `blocker: unmeasured` (no source uses 80).
    The three left are not `%` keys, which D-A does not promote."""
    doc = lib.build_registry_doc()
    assert lib.validation_candidates(doc) == {
        "redis_replication_lag", "mongodb_replication_lag",
        # #3770 fixed the URP sum, so its blocker is gone and 0 is portable
        "kafka_under_replicated_partitions"}


# #3783: the declared danielqsj/kafka_exporter exports neither metric (one is a
# JMX name that varies with the user's jmx_exporter config), so the two alerts
# and their three keys were removed outright — no alias, no shim.
_KAFKA_REMOVED_KEYS = {"kafka_active_controllers", "kafka_request_rate",
                       "kafka_request_rate_critical"}
_KAFKA_UNEXPORTED_METRICS = ("kafka_controller_active_controller_count",
                             "kafka_server_brokertopicmetrics_messagesin_total")


def test_real_kafka_unwired_keys_are_removed_not_blocked():
    doc = lib.build_registry_doc()
    keys = doc["keys"]
    assert _KAFKA_REMOVED_KEYS.isdisjoint(keys)
    assert not any(e.get("critical_of") in _KAFKA_REMOVED_KEYS for e in keys.values())
    assert {k for k, e in keys.items()
            if "blocker" in e and k.startswith("kafka_")} == set()


def test_real_kafka_pack_reads_no_unexported_metric():
    """Comments may name the metrics; no rule expr may read them (#3783)."""
    import yaml

    pack = yaml.safe_load(
        (REPO_ROOT / "rule-packs" / "rule-pack-kafka.yaml").read_text(encoding="utf-8"))
    rules = [r for g in pack["groups"] for r in g["rules"]]
    exprs = "\n".join(r["expr"] for r in rules)
    for metric in _KAFKA_UNEXPORTED_METRICS:
        assert metric not in exprs, metric
    names = {r.get("alert") or r.get("record") for r in rules}
    assert not names & {"KafkaNoActiveController", "KafkaHighRequestRate",
                        "KafkaHighRequestRateCritical"}
    for key in _KAFKA_REMOVED_KEYS - {"kafka_request_rate_critical"}:
        assert f"tenant:alert_threshold:{key}" not in names, key


def test_not_chart_armed_is_the_registry_derivation():
    """A defaults-tier key that is not `default`, and the `_critical` of one.

    The 61 the gate kept by hand before A1 (45, plus #3732's 16 criticals),
    less the three kafka keys #3783 removed — pinned so the derivation can
    only change with the registry, never with a rule edit here. #3778's
    mysql_threads_running is NOT here: it sits in the optional_overrides tier
    (declared, no value), so a tenant writing it alone is served — the
    defaults tier with `arming: operator` rendered it on no runtime face and
    served nothing."""
    doc = lib.build_registry_doc()
    derived = lib.not_chart_armed_keys(doc)
    keys = doc["keys"]
    for key in derived:
        owner = keys[keys[key].get("critical_of") or key]
        assert owner["tier"] == "defaults" and not lib.is_chart_armed(owner), key
    assert not {"mysql_threads_running", "mysql_threads_running_critical"} & derived
    # D-A A2b: four promoted keys and their four criticals left (58 -> 50).
    assert len(derived) == 50
    # optional_overrides flat keys belong to the unreachable face, not here
    assert not any(keys[k]["tier"] == "optional_overrides"
                   and not keys[k].get("critical_of") for k in derived)


def test_not_chart_armed_follows_a_promotion():
    """Promote one key to `default`: it and its critical leave the set."""
    doc = lib.build_registry_doc()
    before = lib.not_chart_armed_keys(doc)
    assert {"redis_replication_lag", "redis_replication_lag_critical"} <= before
    _make_default(doc["keys"]["redis_replication_lag"])
    assert before - lib.not_chart_armed_keys(doc) == {
        "redis_replication_lag", "redis_replication_lag_critical"}


# ── generated surfaces: render / splice / freshness ───────────────────────

def _synth_doc_with_chart():
    return lib.build_registry_doc(SYNTH_PACKS, arming=_armed("alpha_conn"))


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
_DEV_ONLY_MARK = "base 只在 repo 的 dev conf.d 範本供給，Helm chart 不出貨"
_DORMANT_MARK = "base 也未出貨——documented-but-dormant"
_BASE_OPTIONAL_MARK = "base 本身也是 optional_overrides 層"
_ALL_MARKS = (_SHIPPED_MARK, _BASE_SHIPPED_MARK, _DEV_ONLY_MARK, _DORMANT_MARK,
              _BASE_OPTIONAL_MARK)


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
            # base is itself optional-tier (#3778's mysql_threads_running
            # shape) -> no generator writes it under defaults:
            "gamma_queue_critical": {"value": 80, "unit": "count",
                                     "desc": "queue size critical"},
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


def test_pack_header_says_why_a_key_is_not_armed():
    """A non-default key's header line carries its state and Chinese reason;
    a chart default and a `<base>_critical` carry none."""
    doc = _synth_doc_with_chart()
    text = "\n".join(lib.render_pack_header_lines(doc, "beta"))
    assert "未啟用（依部署而定）：beta_lag 依部署而定" in text
    alpha = "\n".join(lib.render_pack_header_lines(doc, "alpha"))
    assert "alpha_conn: 80   [chart-default]" in alpha
    assert "未啟用" in alpha  # alpha_orphan_critical has no base: it states its own
    crit_block = alpha.split("alpha_conn_critical", 1)[1].split("alpha_orphan", 1)[0]
    assert "未啟用" not in crit_block


def test_unarmed_block_lists_exactly_the_unshipped_defaults():
    doc = _synth_doc_with_chart()
    text = "\n".join(lib.render_unarmed_lines(doc, 4))
    assert "#   beta_lag 未啟用（依部署而定）：beta_lag 依部署而定" in text
    # a chart default and an optional_overrides key are not in it
    assert "alpha_conn" not in text and "alpha_orphan" not in text
    assert all(line.startswith("    #") for line in text.split("\n"))


def test_unarmed_block_is_empty_when_everything_ships():
    doc = lib.build_registry_doc(
        SYNTH_PACKS, arming=_armed("alpha_conn", "beta_lag"))
    assert lib.render_unarmed_lines(doc, 4) == []


def test_real_helm_unarmed_block_matches_the_derivation():
    """The shipped values.yaml comment names every defaults-tier key the
    derivation calls not-chart-armed, and nothing else."""
    import re
    doc = lib.build_registry_doc()
    text = "\n".join(lib.render_unarmed_lines(doc, 4))
    named = set(re.findall(r"#   ([a-z0-9_]+) 未啟用", text))
    expected = {k for k in lib.not_chart_armed_keys(doc)
                if not doc["keys"][k].get("critical_of")}
    assert named == expected and len(named) == 28


def _synth_optional_doc():
    """All three optional populations in one doc — `delta_conn` is the only
    chart default, so delta carries flat + base-shipped + dormant at once."""
    packs = _synth_optional_packs()
    return lib.build_registry_doc(
        packs, arming=_armed("delta_conn", packs=packs))


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

    # 3. MIXED -> three sub-sections (no dev-only base here), each key under
    #    the right one
    delta = lib.render_pack_header_lines(doc, "delta")
    text = "\n".join(delta)
    for mark in (_SHIPPED_MARK, _BASE_SHIPPED_MARK, _DORMANT_MARK):
        assert mark in text, (mark, delta)
    assert _DEV_ONLY_MARK not in text

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


def test_dev_only_base_gets_its_own_section(monkeypatch):
    """#3730: a base the repo's dev conf.d template values (the example
    tenants' packs) and the chart does not is neither "settable today" nor
    "dormant" — it is each in one topology, and the header must say which.
    Before, the split asked the chart alone and called these dormant, while
    the dev tree serves them."""
    monkeypatch.setattr(lib, "DEV_EXAMPLE_PACKS", ("delta",))
    doc = _synth_optional_doc()
    assert lib.base_topologies(doc, "delta_qsize_critical") == {"dev"}
    assert lib.base_topologies(doc, "delta_conn_critical") == {"helm", "dev"}
    assert lib.base_topologies(doc, "alpha_orphan_critical") == frozenset()
    delta = lib.render_pack_header_lines(doc, "delta")

    def _line(pred):
        return next(i for i, ln in enumerate(delta) if pred(ln))

    base_ln = _line(lambda ln: _BASE_SHIPPED_MARK in ln)
    dev_ln = _line(lambda ln: _DEV_ONLY_MARK in ln)
    live_i = _line(lambda ln: "delta_conn_critical:" in ln)
    dev_i = _line(lambda ln: "delta_qsize_critical:" in ln)
    assert base_ln < live_i < dev_ln < dev_i, delta
    assert not any(_DORMANT_MARK in ln for ln in delta), (
        "delta has no key whose base neither topology values", delta)
    # the dormant pack stays dormant
    alpha = "\n".join(lib.render_pack_header_lines(doc, "alpha"))
    assert _DORMANT_MARK in alpha and _DEV_ONLY_MARK not in alpha


def test_topology_keeps_are_what_the_surfaces_carry():
    """TOPOLOGY_DEFAULTS_KEEPS is the header's claim about what each topology
    values; the generated files are what it does value. Held equal, over the
    registry's keys, so a block added to (or dropped from) either surface
    without the table — or the reverse — is red here."""
    import yaml

    doc = lib.build_registry_doc()
    registry = set(doc["keys"])
    chart = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    dev = yaml.safe_load(
        (REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
         / "_defaults.yaml").read_text(encoding="utf-8")) or {}
    carried = {
        "helm": set((chart.get("thresholdConfig") or {}).get("defaults") or {}),
        "dev": set(dev.get("defaults") or {}),
    }
    assert set(carried) == set(lib.TOPOLOGY_DEFAULTS_KEEPS)
    for topo, keeps in lib.TOPOLOGY_DEFAULTS_KEEPS.items():
        assert carried[topo], f"{topo}: the surface parsed as empty"
        by_table = {k for k, e in doc["keys"].items()
                    if any(keep(e.get("pack", "?"), e) for keep in keeps)}
        on_surface = carried[topo] & registry
        assert by_table == on_surface, (
            f"{topo}: table-only={sorted(by_table - on_surface)}, "
            f"surface-only={sorted(on_surface - by_table)}")


def test_warning_bodies_make_the_claims_their_headings_promise(monkeypatch):
    """Content, not section choice. The previous version of this test only
    checked WHICH heading was selected — the same shape that let a false body
    ship under a correct-looking heading. Assert what each body actually says
    about emission, in both directions."""
    doc = _synth_optional_doc()
    bodies = {
        _SHIPPED_MARK: "\n".join(lib._OPTIONAL_SHIPPED_WARNING_LINES),
        _BASE_SHIPPED_MARK:
            "\n".join(lib._OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES),
        _DEV_ONLY_MARK:
            "\n".join(lib._OPTIONAL_CRITICAL_BASE_DEV_ONLY_WARNING_LINES),
        _DORMANT_MARK: "\n".join(lib._OPTIONAL_UNSHIPPED_WARNING_LINES),
        _BASE_OPTIONAL_MARK:
            "\n".join(lib._OPTIONAL_CRITICAL_BASE_OPTIONAL_WARNING_LINES),
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
    # the dev-only body states both topologies: a row in the dev tree, none
    # under the chart
    dev_only = bodies[_DEV_ONLY_MARK]
    assert "產出一條真的 critical row" in dev_only
    assert "用 chart 部署時，租戶填一個並不會產出任何 row" in dev_only
    # #3778: an optional-tier base. The dormant body's remedy ("scaffold_tenant's
    # _defaults.yaml carries the base") is false for it; this body must name
    # the only remedy and must not repeat that one.
    base_optional = bodies[_BASE_OPTIONAL_MARK]
    assert "部署面必須自己在根層 `defaults:`" in base_optional
    # review: a subtree _defaults.yaml is not defaults[base] — say so
    assert "子樹 _defaults.yaml" in base_optional
    assert "_defaults.yaml 就含 base" not in base_optional
    # every group the renderer can emit is covered above — no fifth body can
    # be added without this test noticing (delta's qsize base is dev-only here)
    monkeypatch.setattr(lib, "DEV_EXAMPLE_PACKS", ("delta",))
    groups = lib.optional_warning_groups(
        doc, {k: e for k, e in doc["keys"].items()
              if e.get("tier") == "optional_overrides"})
    assert {w for w, _ in groups} == {
        lib._OPTIONAL_SHIPPED_WARNING_LINES,
        lib._OPTIONAL_CRITICAL_BASE_SHIPPED_WARNING_LINES,
        lib._OPTIONAL_CRITICAL_BASE_DEV_ONLY_WARNING_LINES,
        lib._OPTIONAL_UNSHIPPED_WARNING_LINES,
        lib._OPTIONAL_CRITICAL_BASE_OPTIONAL_WARNING_LINES,
    }


def test_critical_of_an_optional_tier_base_gets_its_own_warning():
    """#3778: `gamma_queue_critical`'s base is optional-tier, so the dormant
    warning's remedy does not apply; it renders under the base-optional
    warning, and nothing else does."""
    doc = _synth_optional_doc()
    gamma = lib.render_pack_header_lines(doc, "gamma")
    text = "\n".join(gamma)
    assert _BASE_OPTIONAL_MARK in text and _DORMANT_MARK not in text
    head = next(i for i, l in enumerate(gamma) if _BASE_OPTIONAL_MARK in l)
    assert any("gamma_queue_critical" in l for l in gamma[head:])
    for pack in ("alpha", "delta"):
        assert _BASE_OPTIONAL_MARK not in "\n".join(
            lib.render_pack_header_lines(doc, pack)), pack


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


def test_real_registry_optional_list_is_the_flat_keys():
    """#1310/#1311 pin: the shipped list is the 10 FLAT keys, never the 44 the
    tier holds (both asserted below; #3778 added mysql_threads_running). Most
    of the tier is `_critical` keys;
    listing them would make the shipped list mostly decoration."""
    doc = lib.build_registry_doc()
    tier = {k for k, e in doc["keys"].items()
            if e.get("tier") == "optional_overrides"}
    shipped = lib.optional_override_list_keys(doc)
    assert len(tier) == 44
    assert shipped == [
        "mysql_threads_running",
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
    # The body ends at the function's closing brace (a `}` in column 0), not
    # at the next `func`: the next function's doc comment is not this body.
    end = src.find("\n}\n", start + 1)
    if end == -1:  # unterminated: run to EOF
        end = len(src)
    return src[start:end]


def test_critical_tier_entry_condition_is_still_defaults_of_the_base():
    """The premise every `_critical` header claim rests on, read from the Go
    source rather than assumed. If `resolveCriticalRows` ever stops keying off
    `defaults[base]`, the three-way split below is measuring the wrong thing and
    this fails FIRST, naming the reason."""
    body = _resolve_critical_rows_body()
    src = _RESOLVE_GO.read_text(encoding="utf-8")

    def helper(sig):
        h_start = src.find(sig)
        assert h_start != -1, (
            f"{_RESOLVE_GO} no longer defines {sig!r}, which "
            "resolveCriticalRows uses for its entry test; repoint this reader")
        h_end = src.find("\nfunc ", h_start + 1)
        return src[h_start:h_end if h_end != -1 else len(src)]

    # #3046: the entry test lives in criticalRowRead, shared with da-guard's
    # "switched off with its base" reading; #2518: the base derivation in
    # criticalRowBase, shared with the build's root-null check.
    assert "baseKey, read := criticalRowRead(key, defaults)" in body
    assert "if !read {" in body
    read = helper("func criticalRowRead(key string, defaults map[string]float64) (base string, ok bool) {")
    assert "criticalRowBase(key)" in read and "_, valued := defaults[base]" in read
    # …and neither consults the declared list, which is why `_critical` keys
    # are deliberately off it (#1311). Code lines only: the helper's slice
    # runs into the next function's doc comment.
    code = "\n".join(ln for ln in (body + read).splitlines() if not ln.strip().startswith("//"))
    assert "OptionalOverrides" not in code and "declared" not in code.lower()
    base = helper("func criticalRowBase(key string) (string, bool) {")
    assert 'return strings.TrimSuffix(key, "_critical"), true' in base


def test_every_real_pack_header_critical_claim_is_true_per_key():
    """Per-pack, per-key truth check against the SHIPPED artifact.

    The claim under test is not "the right heading was picked" but "the heading
    that was picked says something true about THIS key": a `_critical` renders
    under the settable-today warning IFF the chart actually ships its base,
    under the dev-only warning IFF only the repo's dev conf.d template values
    it (#3730), and under the dormant warning otherwise. Reads values.yaml
    (what an operator gets) and the dev `_defaults.yaml`, while the renderer
    reads the registry — so a divergence between declaration and artifact
    surfaces here.
    """
    import yaml

    doc = lib.build_registry_doc()
    chart_doc = yaml.safe_load(_CHART_VALUES.read_text(encoding="utf-8")) or {}
    chart_defaults = set(
        ((chart_doc.get("thresholdConfig") or {}).get("defaults") or {}))
    shipped_list = set(
        (chart_doc.get("thresholdConfig") or {}).get("optional_overrides") or [])
    assert chart_defaults and shipped_list, "values.yaml faces parsed as empty"
    dev_doc = yaml.safe_load(
        (REPO_ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
         / "_defaults.yaml").read_text(encoding="utf-8")) or {}
    dev_defaults = set(dev_doc.get("defaults") or {})
    assert dev_defaults, "the dev _defaults.yaml parsed as empty"

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
            elif base and base in dev_defaults:
                expected = _DEV_ONLY_MARK
            elif base and base in shipped_list:
                # #3778: the base ships only as an optional_overrides list
                # entry, never under defaults:
                expected = _BASE_OPTIONAL_MARK
            else:
                expected = _DORMANT_MARK
            assert sect == expected, (
                f"{pack}/{key}: header claims {sect!r} but the shipped chart "
                f"says {expected!r} (base={base!r}, "
                f"base_in_chart={bool(base) and base in chart_defaults}, "
                f"base_in_dev={bool(base) and base in dev_defaults}, "
                f"on_declared_list={key in shipped_list})")
    assert checked == 44, (
        f"expected all 44 optional_overrides keys to be covered, saw {checked}")


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
    # `dev-example-defaults` (#3014) is the dev template's second `defaults`
    # block — the example tenants' packs — on the same file as `dev-defaults`.
    # The `dimensions:` blocks (#3475) follow, one per runtime carrier.
    assert ids[:10] == [
        "helm-defaults", "helm-unarmed", "dev-defaults", "dev-example-defaults",
        "helm-optional", "dev-optional", "try-local-optional",
        "helm-dimensions", "dev-dimensions", "try-local-dimensions"]
    assert "pack-mariadb" in ids and "pack-kubernetes" in ids
    assert len(ids) == 10 + 13  # 13 threshold packs, non-threshold packs none
    # the two surfaces per file must target the same path but distinct blocks
    by_id = {s["id"]: s for s in specs}
    assert by_id["helm-defaults"]["path"] == by_id["helm-optional"]["path"]
    assert by_id["helm-unarmed"]["path"] == by_id["helm-defaults"]["path"]
    assert by_id["dev-defaults"]["path"] == by_id["dev-optional"]["path"]
    assert by_id["dev-example-defaults"]["path"] == by_id["dev-defaults"]["path"]
    assert by_id["try-local-optional"]["path"] not in {
        by_id["helm-optional"]["path"], by_id["dev-optional"]["path"]}
    for carrier in ("helm", "dev", "try-local"):
        assert by_id[f"{carrier}-dimensions"]["path"] == by_id[f"{carrier}-optional"]["path"]
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

# ⛔ EXACT tokens, never substring/`in`: `mysql_connection_usage_percent_critical` contains
# `mysql_connection_usage_percent`, so a substring test reports "found" for precisely the
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
    reader's confidence.
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
    each, are `defaults_counterexample_head`, `_append_wrapped_comment`
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


# ── defaults_counterexample_head (#3206 10c-3) ──────────────────────────

def _a_counterexample_key(tier: str) -> str:
    import scaffold_tenant
    return next(k for pack in scaffold_tenant.RULE_PACKS.values()
                for k, info in (pack.get(tier) or {}).items()
                if isinstance(info, dict) and "value_counterexample" in info)


def test_counterexample_head_names_a_key_and_a_sequence_item() -> None:
    """Both shapes `_defaults.yaml` carries a measured key in: a `defaults:`
    entry (by its key) and an `optional_overrides:` item (by its value)."""
    key = _a_counterexample_key("defaults")
    item = _a_counterexample_key("optional_overrides")
    on_key = lib.defaults_counterexample_head(("defaults", key), 1)
    on_item = lib.defaults_counterexample_head(("optional_overrides", "[0]"), item)
    for text in (on_key, on_item):
        assert text and text.startswith("⚠ ") and lib.COUNTEREXAMPLE_MARK in text
    # A section (a non-empty mapping) is not annotated, nor a key with
    # nothing measured, nor a non-string item.
    assert lib.defaults_counterexample_head((key,), {"a": 1}) is None
    assert lib.defaults_counterexample_head(("defaults", "no_such_key"), 1) is None
    assert lib.defaults_counterexample_head(("x", "[0]"), 5) is None


def test_counterexample_head_wraps_at_the_width_left_at_its_indent() -> None:
    """da-guard writes each line `# <line>` at the node's indent (two columns
    a level): every written line stays within `_COMMENT_WIDTH`."""
    key = _a_counterexample_key("optional_overrides")
    for at in (("optional_overrides", "[0]"), ("a", "b", "c", "[0]")):
        indent = 2 * (len(at) - 1)
        text = lib.defaults_counterexample_head(at, key)
        assert all(indent + 2 + len(line) <= lib._COMMENT_WIDTH
                   for line in text.split("\n")), (at, text)


def test_tenant_declared_foot_is_the_stub_block_as_comment_text() -> None:
    keys = lib.shipped_optional_keys_for_packs(["oracle"])
    assert keys, "fixture assumption: oracle declares keys"
    lines = lib.render_tenant_declared_stub_lines(keys, lang="en")
    foot = lib.tenant_declared_foot(keys, lang="en")
    assert ["# " + t if t else "#" for t in foot.split("\n")] == lines
    assert lib.tenant_declared_foot([], lang="en") is None


# ── D-A A2a: validation (what a shipped value rests on) ─────────────────────

def _entry(value=80, unit="%", **validation):
    return {"arming": "default", "portability": "portable", "tier": "defaults",
            "value": value, "unit": unit, "validation": validation or _vendor(value)}


def _src(org, value, **extra):
    return {"org": org, "url": f"https://example.com/{org}.yaml",
            "quote": f"expr: metric > {value}", "value": value, **extra}


def _problems(entry):
    return lib.arming_problems({"k": entry}, strict=True)


def test_a_shipped_key_states_what_its_value_rests_on():
    assert _problems(_entry()) == []
    bare = _entry()
    del bare["validation"]
    assert any("has no validation" in p for p in _problems(bare))


def test_an_operator_key_may_carry_validation_and_is_held_to_it():
    """升級前可以先登記依據；登記了就照同一套規則檢查。"""
    op = {**_entry(), "arming": "operator"}
    assert _problems(op) == []
    op["validation"]["sources"][0].update(value=81, quote="expr: metric > 81")
    assert any("vendor source uses 81" in p for p in _problems(op))
    plain = {"arming": "operator", "portability": "portable", "tier": "defaults", "value": 80}
    assert _problems(plain) == []


def test_a_vendor_basis_is_the_vendors_own_value():
    """`vendor`：恰好一個來源，值等於 registry 的值——值改了、依據沒跟著改就轉紅。"""
    assert any("ships 85 but its vendor source uses 80" in p
               for p in _problems(_entry(85, basis="vendor", sources=[_src("a", 80)])))
    assert any("takes exactly one source" in p for p in _problems(
        _entry(80, basis="vendor", sources=[_src("a", 80), _src("b", 80)])))


def test_a_consensus_brackets_the_value_with_different_organisations():
    """`consensus`：至少兩個來源、組織與網址都不重複、registry 的值落在來源值的範圍內。"""
    ok = _entry(85, basis="consensus", sources=[_src("a", 80), _src("b", 90)])
    assert _problems(ok) == []
    edge = _entry(80, basis="consensus", sources=[_src("a", 80), _src("b", 80)])
    assert _problems(edge) == []
    assert any("needs two or more sources" in p for p in _problems(
        _entry(80, basis="consensus", sources=[_src("a", 80)])))
    assert any("outside what its consensus sources use (80–90)" in p for p in _problems(
        _entry(95, basis="consensus", sources=[_src("a", 80), _src("b", 90)])))
    assert any("outside what its consensus sources use" in p for p in _problems(
        _entry(70, basis="consensus", sources=[_src("a", 80), _src("b", 90)])))
    same_org = [_src("a", 80), {**_src("A", 90), "url": "https://example.com/other.yaml"}]
    assert any("repeat org" in p for p in _problems(_entry(85, basis="consensus", sources=same_org)))
    same_url = [_src("a", 80), {**_src("b", 90), "url": "https://example.com/a.yaml"}]
    assert any("repeat url" in p for p in _problems(_entry(85, basis="consensus", sources=same_url)))


@pytest.mark.parametrize("validation,needle", [
    ({"basis": "vendor"}, "must be exactly"),
    ({"basis": "vendor", "sources": [], "note": "x"}, "must be exactly"),
    ({"basis": "corpus", "sources": [_src("a", 80)]}, "is not one of"),
    ({"basis": "vendor", "sources": []}, "non-empty list"),
    ({"basis": "vendor", "sources": [{"org": "a", "url": "https://example.com/a", "value": 80}]},
     "must carry"),
    ({"basis": "vendor", "sources": [_src("a", 80, relation="equal")]}, "must carry"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "url": "http://example.com/a"}]}, "https://"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "url": "https://example.com/a b"}]}, "https://"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "quote": "two\nlines"}]}, "one line"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "quote": "  "}]}, "non-blank"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "value": "80"}]}, "finite number"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "value": True}]}, "finite number"),
    ({"basis": "vendor", "sources": [_src("a", 80, tier="page")]}, "tier"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "url": "https://"}]}, "https://"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "org": "  "}]}, "non-blank"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "org": "two\nlines"}]}, "one line"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "value": float("inf")}]}, "finite number"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "value": float("nan")}]}, "finite number"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "quote": "alert: 'HighMemory'"}]},
     "does not carry its value"),
    ({"basis": "vendor", "sources": [{**_src("a", 80), "quote": "expr: metric > 85"}]},
     "does not carry its value"),
], ids=["no-sources", "extra-field", "corpus-basis", "empty-sources", "source-without-quote",
        "authored-relation", "http-url", "url-with-space", "two-line-quote", "blank-quote",
        "string-value", "bool-value", "unknown-tier", "bare-scheme-url", "blank-org",
        "two-line-org", "infinite-value", "nan-value", "quote-without-a-number",
        "quote-with-another-number"])
def test_a_malformed_validation_is_reported(validation, needle):
    problems = _problems(_entry(80, **validation))
    assert any(needle in p for p in problems), problems


def test_a_measured_counterexample_keeps_a_value_from_shipping():
    """語料量到這個值會錯判（`value_counterexample`）的鍵不得預設出貨：語料是否決，不是依據。"""
    ce = {"issue": 1176, "direction": "over_fire", "observed": "x"}
    assert any("measured wrong cannot ship" in p for p in _problems({**_entry(), "value_counterexample": ce}))
    op = {"arming": "operator", "portability": "portable", "tier": "defaults", "value": 80,
          "value_counterexample": ce}
    assert _problems(op) == []


def test_a_critical_tier_carries_no_validation():
    crit = {"critical_of": "k", "value": 90, "validation": _vendor(90)}
    assert any("follows its base" in p for p in lib.arming_problems({"k_critical": crit}, strict=True))


def test_every_real_shipped_key_has_a_validation_and_no_counterexample():
    doc = lib.committed_registry_doc()
    shipped = {k: e for k, e in lib.registry_keys(doc).items() if lib.is_chart_armed(e)}
    assert shipped
    assert all("validation" in e and "value_counterexample" not in e for e in shipped.values())


@pytest.mark.parametrize("quote", ["> 80", "warn: 80,  // percentage", "> 0.8", "> .8",
                                   "> ( 80 / 100 )", "(70) : (80)", '"warning": 0.80'])
def test_a_quote_may_state_a_percentage_as_a_ratio(quote):
    """quote 要帶得出 value：數字本身，或把百分比寫成比值的那個數（`> 0.8` 對 80）。"""
    entry = _entry(80, basis="vendor", sources=[{**_src("a", 80), "quote": quote}])
    assert _problems(entry) == []


def test_two_spellings_of_one_page_are_one_source():
    """同一頁換個寫法（結尾斜線、fragment、大小寫）不算第二個來源。"""
    for other in ("https://example.com/a.yaml/", "https://example.com/a.yaml#L10",
                  "https://EXAMPLE.com/a.yaml"):
        sources = [_src("a", 80), {**_src("b", 90), "url": other}]
        assert any("repeat url" in p for p in _problems(
            _entry(85, basis="consensus", sources=sources))), other


@pytest.mark.parametrize("value", ["85", None, True, float("inf")])
def test_a_value_that_is_no_number_cannot_be_held_against_its_sources(value):
    """registry 的 value 不是有限數時，不能悄悄略過與來源的比較。"""
    entry = _entry(85, basis="consensus", sources=[_src("a", 80), _src("b", 90)])
    entry["value"] = value
    assert any("is not a number to hold the sources against" in p for p in _problems(entry))


_SCHEMA_CASES = [
    ("extra-field", {"basis": "vendor", "sources": [_src("a", 80)], "note": "x"}),
    ("corpus-basis", {"basis": "corpus", "sources": [_src("a", 80)]}),
    ("empty-sources", {"basis": "vendor", "sources": []}),
    ("source-without-quote", {"basis": "vendor", "sources": [
        {"org": "a", "url": "https://example.com/a", "value": 80}]}),
    ("authored-relation", {"basis": "vendor", "sources": [_src("a", 80, relation="equal")]}),
    ("http-url", {"basis": "vendor", "sources": [{**_src("a", 80), "url": "http://example.com/a"}]}),
    ("bare-scheme-url", {"basis": "vendor", "sources": [{**_src("a", 80), "url": "https://"}]}),
    ("two-line-quote", {"basis": "vendor", "sources": [{**_src("a", 80), "quote": "two\nlines"}]}),
    ("string-value", {"basis": "vendor", "sources": [{**_src("a", 80), "value": "80"}]}),
    ("unknown-tier", {"basis": "vendor", "sources": [_src("a", 80, tier="page")]}),
]


@pytest.mark.parametrize("name,validation", _SCHEMA_CASES, ids=[c[0] for c in _SCHEMA_CASES])
def test_the_schema_and_the_gate_refuse_the_same_shapes(name, validation):
    """形狀上的錯，JSON Schema 與 Python 閘門都要擋：只有一邊擋，另一邊的讀者就會收下。"""
    jsonschema = pytest.importorskip("jsonschema")
    import json
    schema = json.loads(Path(lib.SCHEMA_PATH).read_text(encoding="utf-8"))
    doc = copy.deepcopy(lib.committed_registry_doc())
    key = next(k for k, e in doc["keys"].items() if lib.is_chart_armed(e))
    assert not list(jsonschema.Draft7Validator(schema).iter_errors(doc))
    doc["keys"][key]["validation"] = validation
    assert list(jsonschema.Draft7Validator(schema).iter_errors(doc)), name
    assert _problems(_entry(80, **validation)), name


@pytest.mark.parametrize("unit,quote,ok", [
    ("%", "> 0.3", True),
    ("% of max_connections", "> .3", True),
    ("seconds", "lag > 0.3 seconds", False),
    ("count", "> 0.3", False),
    ("seconds", "lag > 30", True),
    ("seconds", "lag > -30", False),
    ("seconds", "lag > 3e1", True),
    ("seconds", "window[5m] - 30", True),
    ("seconds", "(lag)-30 > 0", True),
    ("seconds", "lag > 300", False),
], ids=["percent-ratio", "percent-of-ratio", "seconds-not-a-ratio", "count-not-a-ratio",
        "plain", "negative-is-not-the-value", "exponent", "minus-between-operands",
        "minus-after-paren", "longer-number"])
def test_a_quote_states_the_value_in_the_keys_own_unit(unit, quote, ok):
    """比值（`0.3` 對 30）只在百分比單位成立；帶號與指數是同一個數的一部分。"""
    entry = _entry(30, unit, basis="vendor", sources=[{**_src("a", 30), "quote": quote}])
    assert (_problems(entry) == []) is ok, _problems(entry)
