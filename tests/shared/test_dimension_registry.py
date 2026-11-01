"""The threshold registry's `dimensions` ↔ what the rule packs read (#3475).

A dimensional key `<base>{label="v"}` is served only when its label is
declared under the runtime `dimensions:` (ADR-038 §11), and the shipped
declaration is generated from the registry's per-key `dimensions`. That
declaration is only true if the rule packs actually keep the label through
their threshold normalisation (`max by(tenant, <label>, …)(user_threshold{…})`)
— otherwise the label is dropped and the dimensional value collapses into one
tenant-wide threshold. This pins the two together, both ways:

  * every (key, label) the registry declares is kept by that key's
    normalisation in its pack;
  * every label a pack's normalisation keeps (besides `tenant` / `severity`,
    which the exporter itself sets) is declared by the registry — or named in
    `_KEPT_BUT_NOT_SERVED` with the reason it is deliberately not offered.

Both sides speak the label name the /metrics series carries: a regex
selector `x{db=~"a.*"}` is served as `db_re` (the exporter's
RegexLabelSuffix), so a pack reading it groups by `db_re` and the
registry declares `db_re` — declaring `db` covers only `db="…"`.

The custom-alerts pack (`component="custom"`) is out of scope: its labels are
the recipe compiler's, not a tenant's dimensional key.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

import _lib_consumer_read as cr

_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY = _ROOT / "rule-packs" / "threshold-registry.yaml"

# Labels the exporter sets on every user_threshold row itself.
_EXPORTER_LABELS = frozenset({"tenant", "severity"})

# A label a pack's normalisation keeps although the registry does not offer it
# to tenants, with why. ⛔ Each entry is re-checked below: it must still be
# kept by the pack and still be undeclared, so it cannot go stale silently.
_KEPT_BUT_NOT_SERVED = {
    ("container_cpu_throttle", "version"):
        "ADR-024 OQ-6 scopes version-aware thresholds to container cpu/memory; "
        "the throttle rule keeps the Route-2 shape so a later expansion is a "
        "registry edit, not a rule rewrite (rule-pack-kubernetes.yaml)",
}

_AGG = re.compile(
    r"\b(?:max|min|sum|avg)\s+by\s*\(([^)]*)\)\s*\(\s*user_threshold\s*\{([^}]*)\}",
    re.S)
_MATCHER = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=~?\s*"([^"]*)"')


def _registry() -> dict:
    return yaml.safe_load(_REGISTRY.read_text(encoding="utf-8"))


def _declared() -> "dict[str, set[str]]":
    return {k: set(e["dimensions"]) for k, e in (_registry().get("keys") or {}).items()
            if e.get("dimensions")}


def _kept_in(text: str, kept: "dict[str, set[str]]") -> None:
    for by, selector in _AGG.findall(text):
        m = dict(_MATCHER.findall(selector))
        if m.get("component") in (None, "custom") or "metric" not in m:
            continue
        key = f"{m['component']}_{m['metric']}"
        labels = {l.strip() for l in by.split(",") if l.strip()} - _EXPORTER_LABELS
        kept.setdefault(key, set()).update(labels)


def _kept_by_packs() -> "dict[str, set[str]]":
    """registry key -> labels some pack keeps when normalising its threshold."""
    kept: "dict[str, set[str]]" = {}
    for pack in cr.RULE_PACKS.files_under(_ROOT):
        _kept_in(pack.read_text(encoding="utf-8"), kept)
    return kept


def test_a_regex_dimension_is_its_served_label():
    """A pack reading regex selectors groups by `<label>_re`: that, not
    `<label>`, is what it keeps — and what the registry must declare."""
    kept: "dict[str, set[str]]" = {}
    _kept_in('max by(tenant, db_re, severity) (user_threshold{component="mysql", metric="x"})', kept)
    assert kept == {"mysql_x": {"db_re"}}
    declared = {"mysql_x": {"db"}}
    assert [l for l in sorted(kept["mysql_x"]) if l not in declared["mysql_x"]] == ["db_re"]


def test_the_exporter_serves_regex_labels_with_the_suffix_the_registry_assumes():
    """The suffix the docstring above relies on is the exporter's own."""
    go = (_ROOT / "components/threshold-exporter/app/pkg/config/dimensions.go").read_text(encoding="utf-8")
    assert re.search(r'const RegexLabelSuffix = "_re"', go), "RegexLabelSuffix moved or changed"


def test_the_scan_sees_the_normalisation_rules():
    """An empty scan is green forever."""
    kept = _kept_by_packs()
    assert len(kept) >= 30, sorted(kept)
    assert kept.get("container_cpu") == {"version"}, kept.get("container_cpu")


def test_every_declared_dimension_is_kept_by_its_pack():
    kept = _kept_by_packs()
    missing = [f"{k}{{{l}}}" for k, labels in sorted(_declared().items())
               for l in sorted(labels) if l not in kept.get(k, set())]
    assert not missing, (
        "the registry declares a dimension no rule pack keeps through its "
        "normalisation — a tenant's value for it would be merged into one "
        "tenant-wide threshold: " + ", ".join(missing))


def test_every_kept_dimension_is_declared():
    declared = _declared()
    undeclared = [f"{k}{{{l}}}" for k, labels in sorted(_kept_by_packs().items())
                  for l in sorted(labels)
                  if l not in declared.get(k, set()) and (k, l) not in _KEPT_BUT_NOT_SERVED]
    assert not undeclared, (
        "a rule pack keeps a label the registry does not declare under "
        "`dimensions` — declare it (scaffold_tenant.RULE_PACKS, then --regen) "
        "or name it in _KEPT_BUT_NOT_SERVED with why: " + ", ".join(undeclared))


def test_the_exemptions_are_still_true():
    kept, declared = _kept_by_packs(), _declared()
    for (key, label), why in _KEPT_BUT_NOT_SERVED.items():
        assert label in kept.get(key, set()), f"{key}{{{label}}} is no longer kept by its pack: drop the exemption ({why})"
        assert label not in declared.get(key, set()), f"{key}{{{label}}} is declared now: drop the exemption"


def test_the_shipped_declaration_is_the_registrys():
    """The runtime `dimensions:` on the shipped surfaces is the registry's,
    parsed (the regen gate compares the marker block's text)."""
    want = {k: sorted(v) for k, v in _declared().items()}
    assert want, "the registry declares no dimension at all"
    surfaces = {
        "helm/threshold-exporter/values.yaml": lambda d: (d.get("thresholdConfig") or {}).get("dimensions"),
        "components/threshold-exporter/config/conf.d/_defaults.yaml": lambda d: d.get("dimensions"),
        "try-local/seed/conf.d/_defaults.yaml": lambda d: d.get("dimensions"),
    }
    for rel, get in surfaces.items():
        got = get(yaml.safe_load((_ROOT / rel).read_text(encoding="utf-8")) or {}) or {}
        assert {k: sorted(v) for k, v in got.items()} == want, f"{rel}: dimensions {got} != registry {want}"
