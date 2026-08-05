"""Hermetic tests for check_threshold_unit_sanity.

Each test injects a synthetic registry + a tmp_path repo root, so no branch
depends on the real artifacts staying in a particular state. The final test is
the one exception: a smoke assertion that the gate is GREEN on the real repo,
which is what makes a future regression show up here rather than in production.

Two of these pin regressions the gate itself had while being written — the
kind that make a gate quietly wrong rather than loudly broken:

  * a blanket "threshold must be > 0" rule flagged the shipped-and-correct
    `kafka_under_replicated_partitions: 0` (`> 0` = alert if ANY);
  * feeding the recommendation file's PERCENTILES into tier ordering flagged
    `mysql_connections_critical` for sharing its base key's observed
    distribution — which is correct data, not an inversion.
"""
from __future__ import annotations

import io
import json
import os
import sys

import pytest
import yaml

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_ROOT, "scripts", "tools", "lint"))

import check_threshold_unit_sanity as gate  # noqa: E402


def _registry(**keys) -> dict:
    """Build a registry doc shaped like the real one (packs → key → spec)."""
    return {"version": 1, "packs": {"t": {k: v for k, v in keys.items()}}}


def _spec(value, unit, tier="defaults", **extra) -> dict:
    return {"value": value, "unit": unit, "tier": tier, **extra}


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _conf_d(root, body: dict):
    _write(root, "components/threshold-exporter/config/conf.d/_defaults.yaml",
           yaml.safe_dump(body, allow_unicode=True))


def _errors(registry, root):
    return gate.run_check(registry=registry, root=str(root))["errors"]


# ------------------------------------------------------------ classification


def test_unclassified_unit_is_a_violation_not_a_skip(tmp_path):
    """An unknown unit would silently drop a key from coverage — fail loud."""
    errs = _errors(_registry(foo_bar=_spec(1, "furlongs/fortnight")), tmp_path)
    assert any("UNCLASSIFIED-UNIT" in e and "furlongs/fortnight" in e for e in errs)


def test_unclassified_unit_short_circuits_other_checks(tmp_path):
    """Classification is a precondition: don't report domain errors computed
    from a unit whose dimension we do not actually know."""
    _conf_d(tmp_path, {"defaults": {"pct": 999}})
    errs = _errors(
        _registry(pct=_spec(50, "%"), weird=_spec(1, "??")), tmp_path)
    assert all("UNCLASSIFIED-UNIT" in e for e in errs)


# -------------------------------------------------------------- unit drift


def test_unit_drift_between_asset_and_registry(tmp_path):
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {"pct": {"recommended": 50, "unit": "count"}}}))
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("UNIT-DRIFT" in e and "'count'" in e and "'%'" in e for e in errs)


def test_matching_unit_is_clean(tmp_path):
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {"pct": {"recommended": 50, "unit": "%"}}}))
    assert _errors(_registry(pct=_spec(80, "%")), tmp_path) == []


# ------------------------------------------------------------ value domains


@pytest.mark.parametrize("unit,value,needle", [
    ("%", 300, "NEVER cross"),
    ("% of max_connections", 120, "NEVER cross"),
    ("ratio", 95, "0-1 quantity"),
    ("bytes (4GB)", 4, "unit slip"),
    ("0=green,1=yellow,2=red", 7, "enumerates"),
])
def test_out_of_domain_values(tmp_path, unit, value, needle):
    _conf_d(tmp_path, {"defaults": {"k": value}})
    errs = _errors(_registry(k=_spec(1 if unit == "ratio" else 50, unit)), tmp_path)
    assert any("OUT-OF-DOMAIN" in e and needle in e for e in errs), errs


def test_zero_is_legal_for_count_but_not_percent(tmp_path):
    """REGRESSION: `kafka_under_replicated_partitions: 0` (`> 0` = alert if ANY)
    is shipped and correct; a blanket `> 0` rule wrongly flagged it."""
    _conf_d(tmp_path, {"defaults": {"parts": 0, "pct": 0}})
    errs = _errors(
        _registry(parts=_spec(0, "count"), pct=_spec(80, "%")), tmp_path)
    assert not any("parts" in e for e in errs), errs
    assert any("pct" in e and "fires permanently" in e for e in errs), errs


def test_negative_threshold_is_rejected(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": -1}})
    errs = _errors(_registry(k=_spec(5, "count")), tmp_path)
    assert any("never crossable" in e for e in errs)


def test_unbounded_dimensions_are_not_second_guessed(tmp_path):
    """count / duration / rate have no static right magnitude — only sign."""
    _conf_d(tmp_path, {"defaults": {"c": 999999, "d": 86400, "r": 5000}})
    errs = _errors(_registry(
        c=_spec(80, "count"), d=_spec(30, "seconds"), r=_spec(1, "req/s")), tmp_path)
    assert errs == []


# ------------------------------------------------------------ value shapes


def test_nested_scheduled_form_is_reached(tmp_path):
    """#1217 blind spot: a line-oriented scan misses a value nested under the key."""
    _conf_d(tmp_path, {"defaults": {
        "pct": {"type": "scheduled",
                "default": {"during_business_hours": "300", "after_hours": "50"},
                "range": ["09:00", "18:00"]}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("300" in e for e in errs), errs
    assert not any("= 50 " in e for e in errs), errs


def test_inline_severity_suffix_and_disable_sentinel(tmp_path):
    """`"300:critical"` is a threshold; `"disable"` is not a number at all."""
    _conf_d(tmp_path, {"tenants": {"t": {"pct": "300:critical", "other": "disable"}}})
    errs = _errors(
        _registry(pct=_spec(80, "%"), other=_spec(5, "count")), tmp_path)
    assert any("pct = 300" in e for e in errs), errs
    assert not any("other" in e for e in errs), errs


def test_dimensional_key_resolves_to_its_base(tmp_path):
    _conf_d(tmp_path, {"tenants": {"t": {'pct{version="v2"}': "300"}}})
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("pct = 300" in e for e in errs), errs


# ---------------------------------------------------------- tier ordering


def test_tier_inversion_is_flagged(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": 90, "k_critical": 80}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e for e in errs), errs


def test_equal_tiers_are_flagged(tmp_path):
    _conf_d(tmp_path, {"defaults": {"k": 80, "k_critical": 80}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert any("TIER-INVERSION" in e for e in errs), errs


def test_percentiles_do_not_feed_tier_ordering(tmp_path):
    """REGRESSION: p50/p95/p99 are OBSERVATIONS — a critical key legitimately
    shares its base key's distribution, and that is not an inversion."""
    _write(tmp_path, "docs/assets/recommendation-data.json", json.dumps(
        {"recommendations": {
            "k": {"p50": 45, "p95": 78, "recommended": 80, "unit": "count"},
            "k_critical": {"p50": 45, "p95": 78, "recommended": 150,
                           "unit": "count"}}}))
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert errs == [], errs


def test_tier_ordering_is_per_surface(tmp_path):
    """A tenant may raise the warning above the PLATFORM critical default; only
    an inversion WITHIN one artifact is a real dedup hazard."""
    _conf_d(tmp_path, {"tenants": {"t": {"k": 200}}})
    errs = _errors(_registry(
        k=_spec(80, "count"),
        k_critical=_spec(120, "count", tier="optional_overrides", critical_of="k"),
    ), tmp_path)
    assert errs == [], errs


# ------------------------------------------------------- per-surface parsing

# `_collect` has five surface branches. conf.d YAML and recommendation-data are
# exercised throughout the tests above; these three pin the rest. Without them a
# regression in one parser (a broken `tpl.get("yaml")`, a changed surface label,
# the markdown fence regex) would only surface if the REAL repo happened to hold
# a violating example that day — green-on-present-corpus, which is precisely
# where omission bugs hide.


def test_template_data_embedded_yaml_is_scanned(tmp_path):
    """Each template's `yaml` string is parsed and labelled per template id.

    This is the branch that caught the shipped `pg_connections` defect in
    `saas-backend` / `event-driven` / `finance-compliance`.
    """
    _write(tmp_path, "docs/assets/template-data.json", json.dumps(
        {"templates": [
            {"id": "demo", "yaml": 'pg_connections: "300"'},
            {"id": "fine", "yaml": 'pg_connections: "80"'},
        ]}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("template-data.json[demo]" in e and "OUT-OF-DOMAIN" in e for e in errs), errs
    assert not any("[fine]" in e for e in errs), errs


def test_template_data_unparseable_yaml_is_skipped_not_fatal(tmp_path):
    """A malformed template must not take the whole gate down with it."""
    _write(tmp_path, "docs/assets/template-data.json", json.dumps(
        {"templates": [
            {"id": "broken", "yaml": "key: [unclosed"},
            {"id": "demo", "yaml": 'pg_connections: "300"'},
        ]}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("template-data.json[demo]" in e for e in errs), errs


def test_platform_data_value_and_declared_unit_are_scanned(tmp_path):
    """platform-data is the portal's RUNTIME rule-pack source — it carries both
    a value and a unit, so both invariants must reach it."""
    _write(tmp_path, "docs/assets/platform-data.json", json.dumps(
        {"rulePacks": {"pg": {"defaults": {
            "pg_connections": {"value": 300, "unit": "count"}}}}}))
    errs = _errors(_registry(pg_connections=_spec(80, "% of max_connections")), tmp_path)
    assert any("UNIT-DRIFT" in e and "platform-data.json" in e for e in errs), errs
    assert any("OUT-OF-DOMAIN" in e and "platform-data.json" in e for e in errs), errs


def test_markdown_fenced_yaml_is_scanned(tmp_path):
    """Doc examples are a real surface: #1217's residue lived mostly in them."""
    _write(tmp_path, "docs/getting-started/example.md",
           "設定範例：\n\n```yaml\ndefaults:\n  pct: 300\n```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("example.md" in e and "OUT-OF-DOMAIN" in e for e in errs), errs


def test_markdown_non_yaml_and_fragment_blocks_are_tolerated(tmp_path):
    """Docs legitimately contain YAML fragments that are not standalone
    documents, and fences in other languages. Neither may crash the gate nor
    stop the scan from reaching a later, valid block."""
    _write(tmp_path, "docs/getting-started/example.md",
           "```bash\nkubectl apply -f x.yaml\n```\n\n"
           "```yaml\n  - broken: [unclosed\n```\n\n"
           "```yaml\ndefaults:\n  pct: 300\n```\n")
    errs = _errors(_registry(pct=_spec(80, "%")), tmp_path)
    assert any("example.md" in e for e in errs), errs


# -------------------------------------------------------------- real repo


def test_registry_metadata_numbers_are_not_read_as_thresholds(monkeypatch):
    """A NUMBER stored *about* a key is not a threshold FOR that key.

    The walker's rule — "once inside a known registry key, every numeric leaf
    beneath it is a threshold literal" (#1217's nested scheduled form) — held
    only while every metadata field was non-numeric. #1176 added
    `value_counterexample: {issue: 1176, …}` and the gate promptly reported
    `oracle_pga_allocated_bytes = 1176 … < 1 MiB, looks like a unit slip`: a
    real false positive produced by a real contract change.

    Pinned with a bytes key on purpose — bytes is the one dimension with a
    lower bound, so a stray small integer is guaranteed to trip it if the
    exclusion regresses.
    """
    # `_REGISTRY_KEYS` is populated by run_check(); seed it so the walker
    # recognises the key (monkeypatch restores it, keeping this hermetic).
    monkeypatch.setattr(gate, "_REGISTRY_KEYS", {"oracle_pga_allocated_bytes"})
    entry = {
        "pack": "p", "tier": "defaults", "unit": "bytes (4GB)",
        "desc": "d", "metric_class": "capacity",
        "value": 4294967296,
        "value_counterexample": {
            "issue": 1176, "direction": "over_fire", "observed": "x",
        },
    }
    found = dict(gate._walk_values({"oracle_pga_allocated_bytes": entry}))
    assert found == {"oracle_pga_allocated_bytes": 4294967296}, found

    # ...and the exclusion is DERIVED from the registry's own field list, so a
    # future metadata field is covered without editing this gate.
    assert "value" not in gate._REGISTRY_METADATA_FIELDS
    assert "value_counterexample" in gate._REGISTRY_METADATA_FIELDS


def test_scheduled_nested_thresholds_still_collected(monkeypatch):
    """Counter-case for the exclusion above: the #1217 nested form carries REAL
    threshold literals under names that are not registry metadata, and must
    keep being collected. An over-broad skip would silently un-cover it."""
    monkeypatch.setattr(gate, "_REGISTRY_KEYS", {"mysql_threads_running"})
    nested = {"mysql_threads_running": {"default": {"during_business_hours": "30"}}}
    assert dict(gate._walk_values(nested)) == {"mysql_threads_running": 30}


def test_real_repo_is_clean():
    """Smoke: the shipped artifacts satisfy the gate. If this goes red, an
    asset drifted from the registry — read the message, don't relax the gate."""
    assert gate.run_check()["errors"] == []
