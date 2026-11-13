"""Tests for scripts/tools/lint/check_threshold_registry.py (TRK-339 WS1a / #1200).

Since D-A PR-3 the registry is the hand-authored SoT and every other copy of
a threshold — scaffold_tenant.RULE_PACKS's threshold half included — is
generated from it. Pins:
  - the live repo is green (schema-valid, a fixed point, every surface fresh)
  - a schema violation fails --ci
  - an authored mistake the extraction forbids fails --ci ("inconsistent:")
  - a registry edit without --regen leaves the RULE_PACKS module stale
  - missing/unparseable registry file is a violation with a --regen hint
  - the CLI exit-code contract (report-only without --ci)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_registry.py"

_spec = importlib.util.spec_from_file_location("check_threshold_registry", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

lib = gate.registry_lib


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_is_green():
    """Committed registry: schema-valid, a fixed point, every surface fresh."""
    result = gate.run_check()
    assert result["errors"] == [], result["errors"]


# ── violations are caught (synthetic artifacts on tmp_path) ───────────────

def _write_registry_variant(tmp_path, mutate):
    """Write the real registry with `mutate(doc)` applied; returns the path."""
    import yaml
    doc = lib.load_registry()
    mutate(doc)
    p = tmp_path / "threshold-registry.yaml"
    p.write_text(
        yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
        encoding="utf-8")
    return str(p)


def test_schema_violation_is_an_error(tmp_path):
    def bad_tier(doc):
        key = next(iter(doc["keys"]))
        doc["keys"][key]["tier"] = "not-a-tier"
    path = _write_registry_variant(tmp_path, bad_tier)
    result = gate.run_check(registry_path=path)
    assert any(e.startswith("schema:") for e in result["errors"]), result


def test_missing_metric_class_is_a_schema_error(tmp_path):
    """metric_class is REQUIRED on every key (PR-2 schema promotion)."""
    def drop_class(doc):
        key = next(iter(doc["keys"]))
        doc["keys"][key].pop("metric_class", None)
    path = _write_registry_variant(tmp_path, drop_class)
    result = gate.run_check(registry_path=path)
    assert any(e.startswith("schema:") and "metric_class" in e
               for e in result["errors"]), result


@pytest.mark.parametrize("bad", [["ratio_pending"], {"state": "x"}, 3])
def test_a_malformed_arming_is_reported_not_raised(tmp_path, bad):
    """The surfaces are rendered after validation finds problems; a
    non-string arming must come back as a violation, not crash the gate
    on an unhashable lookup (CodeRabbit #3741)."""
    def malform(doc):
        doc["keys"]["redis_memory_used_bytes"]["arming"] = bad
    path = _write_registry_variant(tmp_path, malform)
    result = gate.run_check(registry_path=path)
    assert any("redis_memory_used_bytes" in e for e in result["errors"]), result


def _module_stale(result):
    return any(e.startswith("stale-surface:") and "rule-packs-module" in e
               for e in result["errors"])


def test_value_edit_without_regen_leaves_the_module_stale(tmp_path):
    """The registry is authored now, so a changed value is not drift — but
    RULE_PACKS (what scaffold generates configs from) must follow it."""
    def bump_value(doc):
        key = next(iter(doc["keys"]))
        doc["keys"][key]["value"] = doc["keys"][key]["value"] + 1
    path = _write_registry_variant(tmp_path, bump_value)
    result = gate.run_check(registry_path=path)
    assert _module_stale(result), result
    assert not any(e.startswith("inconsistent:") for e in result["errors"]), result


def test_added_key_without_regen_leaves_the_module_stale(tmp_path):
    def add_key(doc):
        doc["keys"]["ghost_metric"] = {
            "pack": "mariadb", "tier": "defaults", "value": 1,
            "unit": "count", "desc": "ghost", "metric_class": "state"}
    path = _write_registry_variant(tmp_path, add_key)
    assert _module_stale(gate.run_check(registry_path=path))


def test_dropped_key_without_regen_leaves_the_module_stale(tmp_path):
    def drop_one(doc):
        key = next(iter(doc["keys"]))
        del doc["keys"][key]
    path = _write_registry_variant(tmp_path, drop_one)
    assert _module_stale(gate.run_check(registry_path=path))


def test_a_wrong_critical_of_is_inconsistent(tmp_path):
    """The fixed point, through the gate: critical_of is derived, so an
    authored one that disagrees is refused before anything is generated."""
    def wrong(doc):
        key = next(k for k, e in doc["keys"].items() if "critical_of" in e)
        doc["keys"][key].pop("critical_of")
    path = _write_registry_variant(tmp_path, wrong)
    result = gate.run_check(registry_path=path)
    assert any(e.startswith("inconsistent:") and "critical_of" in e
               for e in result["errors"]), result


def test_missing_registry_file_is_a_violation(tmp_path):
    result = gate.run_check(registry_path=str(tmp_path / "nope.yaml"))
    assert any("--regen" in e for e in result["errors"]), result


def test_unparseable_registry_is_a_violation(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("keys: [unclosed", encoding="utf-8")
    result = gate.run_check(registry_path=str(p))
    assert any("unparseable" in e for e in result["errors"]), result


# ── generated-surface freshness (PR-2 rewire) ─────────────────────────────

def _helm_values_copy(tmp_path):
    src = Path(lib.HELM_VALUES_PATH)
    dst = tmp_path / "values.yaml"
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dst


def test_hand_edit_inside_generated_block_bites(tmp_path):
    """Red→green proof: mutate a value INSIDE the helm generated block →
    stale-surface hard error; re-splice → green again."""
    dst = _helm_values_copy(tmp_path)
    text = dst.read_text(encoding="utf-8")
    assert "    mysql_threads_running: 30" in text
    dst.write_text(
        text.replace("    mysql_threads_running: 30",
                     "    mysql_threads_running: 80"),
        encoding="utf-8")
    result = gate.run_check(surface_paths={"helm-defaults": str(dst)})
    assert any("stale-surface" in e and "helm-defaults" in e
               for e in result["errors"]), result

    # green again after a fresh splice (what --regen does)
    doc = lib.load_registry()
    spec = next(s for s in lib.surface_specs(doc) if s["id"] == "helm-defaults")
    dst.write_text(
        lib.splice_surface(dst.read_text(encoding="utf-8"), spec),
        encoding="utf-8")
    result = gate.run_check(surface_paths={"helm-defaults": str(dst)})
    assert not any("helm-defaults" in e for e in result["errors"]), result


def test_deleted_markers_are_a_hard_error(tmp_path):
    dst = _helm_values_copy(tmp_path)
    lines = [
        ln for ln in dst.read_text(encoding="utf-8").split("\n")
        if "GENERATED:threshold-registry:helm-defaults" not in ln
    ]
    dst.write_text("\n".join(lines), encoding="utf-8")
    result = gate.run_check(surface_paths={"helm-defaults": str(dst)})
    assert any("markers" in e for e in result["errors"]), result


def test_missing_surface_file_is_a_violation(tmp_path):
    result = gate.run_check(
        surface_paths={"dev-defaults": str(tmp_path / "gone.yaml")})
    assert any("stale-surface" in e and "does not exist" in e
               for e in result["errors"]), result


# ── header-prose key membership (F6) ──────────────────────────────────────

def test_prose_claiming_nonexistent_key_is_a_violation(tmp_path):
    bogus = tmp_path / "rule-pack-bogus.yaml"
    bogus.write_text(
        "# header:\n#   db2_totally_fake_key: 42\ngroups: []\n",
        encoding="utf-8")
    result = gate.run_check(prose_files=[str(bogus)])
    assert any("membership" in e and "db2_totally_fake_key" in e
               for e in result["errors"]), result


def test_prose_known_unwired_and_dimensional_are_allowed(tmp_path):
    """The #1196 pending-line exemption (extra_allowed = KNOWN_UNWIRED on the
    real path) and dimensional tokens stay legal in prose while a repair is
    pending. Uses a SYNTHETIC pending key: after the #1231 B/C/D/E identity
    repairs every remaining KNOWN_UNWIRED key (A-class) is also a registry key
    (optional_overrides tier), so only a synthetic injection still exercises
    the pending-line branch hermetically (db2_lock_wait_time, the old fixture,
    is a shipped defaults key now)."""
    f = tmp_path / "rule-pack-ok.yaml"
    f.write_text(
        "# header:\n"
        "#   zzz_pending_key: 10\n"
        "#   mysql_connections_critical: \"120\"\n"
        "#   \"oracle_tablespace_used_percent{tablespace_name=\\\"TEMP\\\"}\": \"disable\"\n"
        "groups: []\n",
        encoding="utf-8")
    result = gate.run_check(
        prose_files=[str(f)], extra_allowed={"zzz_pending_key"})
    assert not any("membership" in e for e in result["errors"]), result


def test_real_pack_headers_membership_clean():
    """All 16 shipped pack headers pass the membership scan (F6)."""
    result = gate.run_check()
    assert not any(e.startswith("membership") for e in result["errors"]), [
        e for e in result["errors"] if e.startswith("membership")]


# ── the CLI exit-code contract (main), independent of the real artifacts ──

def test_main_without_ci_is_report_only(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["some drift"]})
    assert gate.main([]) == gate.EXIT_OK


def test_main_ci_fails_on_errors(monkeypatch):
    monkeypatch.setattr(gate, "run_check",
                        lambda: {"errors": ["a breach"]})
    assert gate.main(["--ci"]) == gate.EXIT_VIOLATION


def test_main_ci_passes_when_clean(monkeypatch):
    monkeypatch.setattr(gate, "run_check", lambda: {"errors": []})
    assert gate.main(["--ci"]) == gate.EXIT_OK


def test_regen_writes_registry_and_surfaces(monkeypatch, tmp_path):
    """--regen delegates to the lib's all-or-none `regen` (registry AND
    surfaces); pin the wiring, not the content."""
    calls = {}

    def fake_regen():
        calls["regen"] = True
        return {"path": str(tmp_path / "r.yaml"), "packs": 1, "keys": 2}, []

    monkeypatch.setattr(gate.registry_lib, "regen", fake_regen)
    assert gate.main(["--regen"]) == gate.EXIT_OK
    assert calls.get("regen")


def test_regen_surfaces_rejects_path_escaping_repo_root(tmp_path, monkeypatch):
    """A registry-derived surface path carrying `..` must fail loud, not
    splice a file outside the repo (CodeRabbit review of #1222).

    ⛔ HERMETIC, and that is load-bearing rather than tidy. This test used to
    call `regen_surfaces` on the REAL registry with only one pack's
    `rule_pack_file` poisoned. `regen_surfaces` then wrote each surface as it
    went and only raised when it reached the bad one (it now splices all in
    memory and writes none on a refusal — this test still keeps the real
    files out of reach rather than lean on that), so the earlier surfaces —
    helm values and the dev `conf.d/_defaults.yaml` — were rewritten to a
    freshly regenerated state BEFORE the expected ValueError. In a whole-suite
    run `tests/lint` goes first, so it silently repaired the working tree and
    any drift a later `tests/ops` test was supposed to catch was already gone
    (measured: the same mutation gave 4 failures scoped, 2 whole-suite).

    Everything the test was written to prove is unchanged: a `..` in a
    registry-derived surface path raises before writing. It now proves it
    against a tmp repo root, so nothing outside tmp_path is touched.

    ⛔ The escape TARGET is derived, not guessed. `surface_specs` joins
    `rule_pack_file` onto `_REPO_ROOT`, so `../../outside.yaml` climbs two
    levels out of the patched root — an earlier revision asserted about
    `tmp_path/outside.yaml`, a location the code can never reach, so the check
    was vacuous. The fake root is nested one level deeper here (`sandbox/repo`)
    precisely so the real escape target lands back inside `tmp_path` and the
    test keeps its hermeticity.

    ⛔ And the target is PRE-SEEDED with a stale-but-spliceable copy of the
    poisoned pack. Without that, "the file was not written" would hold for the
    wrong reason: an unguarded `regen_surfaces` would die on FileNotFoundError
    before writing anything. With it, dropping the containment check really
    does rewrite the file, so the assertion below has something to catch.
    """
    import pytest
    from _registry_lib import regen_surfaces, surface_specs

    fake_root = tmp_path / "sandbox" / "repo"
    fake_root.mkdir(parents=True)
    helm = fake_root / "values.yaml"
    dev = fake_root / "_defaults.yaml"
    # Copies of the real host documents, so the two file-level surfaces splice
    # exactly as they do in production (markers, parent keys, indent) — the
    # earlier surfaces must be genuinely spliceable, or reaching the poisoned
    # pack spec would prove nothing about ordering.
    helm.write_text(Path(lib.HELM_VALUES_PATH).read_text(encoding="utf-8"),
                    encoding="utf-8")
    dev.write_text(Path(lib.DEV_DEFAULTS_PATH).read_text(encoding="utf-8"),
                   encoding="utf-8")

    doc = lib.build_registry_doc()
    # Redirect BOTH file-level surfaces and the repo-root containment anchor
    # into tmp_path, so the loop reaches the poisoned pack spec having touched
    # nothing real.
    monkeypatch.setattr(lib, "_REPO_ROOT", str(fake_root))
    monkeypatch.setattr(lib, "HELM_VALUES_PATH", str(helm))
    monkeypatch.setattr(lib, "DEV_DEFAULTS_PATH", str(dev))
    assert len(surface_specs(doc)) > 4, "expected pack surfaces after the files"

    victim = next(iter(doc["packs"]))
    # A STALE copy of the real pack: the markers are intact so `splice_surface`
    # succeeds, and the extra line inside the block makes the render differ, so
    # an unguarded run reaches `write_text_secure` instead of no-op'ing.
    marker = lib.begin_marker(f"pack-{victim}", "")
    original = (REPO_ROOT / doc["packs"][victim]["rule_pack_file"]).read_text(
        encoding="utf-8")
    assert marker in original, f"pack {victim} lost its generated-block marker"
    escape = (fake_root / ".." / ".." / "outside.yaml").resolve()
    escape.write_text(
        original.replace(marker, marker + "\n#   zzz_stale_probe: 1", 1),
        encoding="utf-8")
    seeded = escape.read_text(encoding="utf-8")

    doc["packs"][victim]["rule_pack_file"] = "../../outside.yaml"
    with pytest.raises(ValueError, match="escapes repo root"):
        regen_surfaces(doc)
    assert escape.read_text(encoding="utf-8") == seeded, (
        f"regen_surfaces spliced {escape}, which is outside the (patched) repo "
        "root — the containment check let a `..`-carrying registry path through")


# ── deprecated_aliases (#1231) — SSOT section ───────────────────────────────

def test_alias_targets_are_active_keys_and_old_spellings_are_retired():
    doc = lib.load_registry()
    aliases = doc.get("deprecated_aliases", {}) or {}
    assert aliases, "expected at least one open alias window (#1231)"
    for old, new in aliases.items():
        assert new in doc["keys"], (old, new)
        assert old not in doc["keys"], old


def test_an_alias_onto_no_key_is_inconsistent(tmp_path):
    """An alias must target a live key. Removing or retargeting an alias onto
    a live key is a legitimate authored edit here; Go's aliases.go follows it
    or pkg/config/aliases_registry_test.go goes red."""
    def retarget_alias(doc):
        aliases = doc.get("deprecated_aliases") or {}
        old = next(iter(aliases))
        aliases[old] = "no_such_key"
    path = _write_registry_variant(tmp_path, retarget_alias)
    result = gate.run_check(registry_path=path)
    assert any(e.startswith("inconsistent:") and "no threshold" in e
               for e in result["errors"]), result


def test_build_rejects_bad_alias_entries():
    """build_registry_doc fail-loud contract: alias old-spelling must not be
    an active key; alias target must be an active key; old != new."""
    import pytest
    packs = {
        "p1": {
            "display": "P1", "exporter": "e", "default_on": True,
            "rule_pack_file": "rule-packs/rule-pack-p1.yaml",
            "defaults": {
                "aaa_bbb": {"value": 1, "unit": "u", "desc": "d",
                             "metric_class": "state"},
                "aaa_ccc": {"value": 2, "unit": "u", "desc": "d",
                             "metric_class": "state"},
            },
        },
    }
    with pytest.raises(ValueError, match="still an ACTIVE registry key"):
        lib.build_registry_doc(
            packs, deprecated_aliases={"aaa_bbb": "aaa_ccc"})
    with pytest.raises(ValueError, match="not a\\s+registry key"):
        lib.build_registry_doc(
            packs, deprecated_aliases={"aaa_zzz": "aaa_gone"})
    with pytest.raises(ValueError, match="maps to itself"):
        lib.build_registry_doc(
            packs, deprecated_aliases={"aaa_zzz": "aaa_zzz"})
    # and the happy path emits the section
    doc = lib.build_registry_doc(
        packs, deprecated_aliases={"aaa_zzz": "aaa_bbb"})
    assert doc["deprecated_aliases"] == {"aaa_zzz": "aaa_bbb"}


# ── retired_keys (#3829) ──────────────────────────────────────────────────

def _retire(entry_for):
    """A registry mutation adding one `retired_keys:` entry, built from the doc."""
    def mutate(doc):
        doc["retired_keys"] = dict(entry_for(doc))
    return mutate


def _live_key(doc):
    return next(iter(doc["keys"]))


_OK_ENTRY = {"tracking": 3829, "hint": "fixture hint"}


@pytest.mark.parametrize("name, entry_for, needle", [
    ("an active key",
     lambda d: {_live_key(d): {"replaced_by": None, **_OK_ENTRY}}, "still an ACTIVE registry key"),
    ("an alias's old spelling",
     lambda d: {next(iter(d["deprecated_aliases"])): {"replaced_by": None, **_OK_ENTRY}},
     "also in deprecated_aliases"),
    ("an alias's target",
     lambda d: {next(iter(d["deprecated_aliases"].values())): {"replaced_by": None, **_OK_ENTRY}},
     "also in deprecated_aliases"),
    ("replaced by no key",
     lambda d: {"fixture_retired": {"replaced_by": "no_such_key", **_OK_ENTRY}},
     "not an active registry key"),
    ("no tracking",
     lambda d: {"fixture_retired": {"replaced_by": None, "hint": "fixture hint"}}, "needs tracking"),
])
def test_an_inconsistent_retired_key_is_reported(tmp_path, name, entry_for, needle):
    """The four consistency rules of `retired_keys:`, one counter-example each."""
    path = _write_registry_variant(tmp_path, _retire(entry_for))
    errors = gate.run_check(registry_path=path)["errors"]
    assert any(e.startswith("inconsistent:") and needle in e for e in errors), (name, errors)


def test_a_retired_base_with_an_active_critical_tier_is_reported():
    """#3829 review: the Go lookup retires `<old>_critical` with `<old>` (as
    an alias canonicalizes it), so retiring a base whose critical tier is
    still an active key is a contradiction."""
    keys = {"aaa_new": {}, "aaa_old_critical": {}}
    problems = lib.retired_key_problems(
        {"aaa_old": {"replaced_by": "aaa_new", **_OK_ENTRY}}, keys, {})
    assert any("still has an ACTIVE critical tier 'aaa_old_critical'" in p for p in problems), problems
    # Without the critical tier the same entry is consistent.
    assert lib.retired_key_problems(
        {"aaa_old": {"replaced_by": "aaa_new", **_OK_ENTRY}},
        {"aaa_new": {}, "aaa_new_critical": {}}, {}) == []


def test_a_derived_critical_replacement_must_be_a_registry_key():
    """#3829 second review: `<old>_critical` is derived to `<repl>_critical`.
    When repl has no critical tier that hint would name no key, so the
    critical spelling needs its own entry — which then wins over the
    derivation."""
    entry = {"replaced_by": "aaa_new", **_OK_ENTRY}
    problems = lib.retired_key_problems({"aaa_old": entry}, {"aaa_new": {}}, {})
    assert any("'aaa_new_critical', not a registry key" in p for p in problems), problems
    own = {"replaced_by": None, **_OK_ENTRY}
    assert lib.retired_key_problems(
        {"aaa_old": entry, "aaa_old_critical": own}, {"aaa_new": {}}, {}) == []
    # A null replacement derives no critical key, so nothing to check.
    assert lib.retired_key_problems(
        {"aaa_old": {"replaced_by": None, **_OK_ENTRY}}, {"aaa_new": {}}, {}) == []


def test_a_missing_tracking_is_also_a_schema_error(tmp_path):
    path = _write_registry_variant(tmp_path, _retire(
        lambda d: {"fixture_retired": {"replaced_by": None, "hint": "fixture hint"}}))
    errors = gate.run_check(registry_path=path)["errors"]
    assert any(e.startswith("schema: retired_keys/fixture_retired") and "tracking" in e for e in errors), errors


def test_a_retired_key_without_regen_leaves_the_go_table_stale(tmp_path):
    """A consistent entry is no violation of the contract — but the Go table the
    key verdict reads must follow it (--regen), or --ci is red."""
    path = _write_registry_variant(tmp_path, _retire(
        lambda d: {"fixture_retired": {"replaced_by": _live_key(d), **_OK_ENTRY}}))
    errors = gate.run_check(registry_path=path)["errors"]
    assert not [e for e in errors if e.startswith(("inconsistent:", "schema:"))], errors
    assert any(e.startswith("stale-surface:") and lib.RETIRED_KEYS_GO_ID in e for e in errors), errors


def test_the_go_table_renders_every_entry_and_names_the_replacement(tmp_path):
    """The generated Go table for a doc with entries: one line per key, sorted,
    the replacement ("" for null), the issue and the hint; and a fresh copy
    passes the freshness check while a copy missing one entry does not."""
    doc = {"retired_keys": {
        "zz_old": {"replaced_by": None, "tracking": 2, "hint": "gone"},
        "aa_old": {"replaced_by": "aa_new", "tracking": 1, "hint": "use the \"new\" one"},
    }}
    text = lib.render_retired_keys_go(doc)
    assert ('\t{"aa_old", "aa_new", 1, "use the \\"new\\" one"},\n'
            '\t{"zz_old", "", 2, "gone"},\n}\n') in text, text
    go = tmp_path / "gen.go"
    go.write_text(text, encoding="utf-8")
    assert lib.check_retired_keys_go(doc, str(go)) is None
    go.write_text(text.replace('\t{"zz_old", "", 2, "gone"},\n', ""), encoding="utf-8")
    assert lib.RETIRED_KEYS_GO_ID in (lib.check_retired_keys_go(doc, str(go)) or "")


# ── value_counterexample (#1176) ──────────────────────────────────────────
#
# The field records that ADR-030's reference library produced a case a shipped
# value gets wrong, so every face that hands the number over can hand over what
# we know about it. It is NOT a verdict — that library is a sampling probe, and
# its README's anti-Goodhart clause forbids treating it as a spec.

_CE_MARK = lib.COUNTEREXAMPLE_MARK


def _ce_keys() -> dict:
    doc = lib.build_registry_doc()
    return {k: e["value_counterexample"]
            for k, e in doc["keys"].items() if "value_counterexample" in e}


def test_real_repo_counterexamples_are_well_formed():
    """Shape is schema-pinned; this also fails loud if the set empties out,
    which would otherwise make every assertion below vacuously true."""
    ce = _ce_keys()
    assert ce, "no key carries value_counterexample — the rest of this file "\
               "would pass vacuously"
    for key, e in ce.items():
        # `observed_zh` is schema-optional and gate-required (the ZH surfaces
        # include the one file a tenant opens; owner decision, #1344), so it is
        # allowed but not part of the minimum shape asserted here — the
        # "everything ships it" assertion lives in
        # tests/lint/test_counterexample_coverage.py, next to the surfaces that
        # would otherwise fall back to English.
        assert set(e) <= {"issue", "direction", "observed", "observed_zh"}, (key, e)
        assert {"issue", "direction", "observed"} <= set(e), (key, e)
        assert isinstance(e["issue"], int) and e["issue"] > 0, (key, e)
        assert e["direction"] in ("over_fire", "under_fire"), (key, e)
        assert e["observed"].strip(), key
        if "observed_zh" in e:
            assert e["observed_zh"].strip(), key


def test_pack_header_renders_exactly_its_own_counterexamples():
    """One ⚠ line per counter-example key IN THAT PACK — and none for a pack
    that has measured none.

    ⛔ The regression this pins: the three cases used to be spelled out as
    prose inside the GENERATED optional-tier warning, so the Oracle and
    ClickHouse headers both asserted DB2's counter-example. ClickHouse has
    never had one measured at all. A pack header must not claim a fact about a
    pack it does not own — re-tuning db2_deadlock_rate would have left two
    other packs quietly citing the old number.
    """
    doc = lib.build_registry_doc()
    ce = _ce_keys()
    packs_with_ce = set()
    for pack_name in doc["packs"]:
        own = {k for k, e in doc["keys"].items() if e["pack"] == pack_name}
        body = "\n".join(lib.render_pack_header_lines(doc, pack_name))
        expected = len(own & set(ce))
        assert body.count(_CE_MARK) == expected, (
            f"{pack_name}: rendered {body.count(_CE_MARK)} counter-example "
            f"lines, registry has {expected}")
        if expected:
            packs_with_ce.add(pack_name)
        foreign = [k for k in ce if k not in own and k in body]
        assert not foreign, (
            f"{pack_name} header names another pack's key: {foreign}")
    assert len(packs_with_ce) >= 2, (
        "fewer than two packs render one — the cross-pack assertion above "
        "would not be exercised")


def test_the_two_directions_do_not_share_wording():
    """`over_fire` and `under_fire` are opposite failures — a single blanket
    caveat ("this value is too low") is FALSE for the under_fire one. That is
    why direction is an enum and the wording is derived from it."""
    over = lib._counterexample_lines(
        {"value_counterexample": {"issue": 1, "direction": "over_fire",
                                  "observed": "X"}})
    under = lib._counterexample_lines(
        {"value_counterexample": {"issue": 1, "direction": "under_fire",
                                  "observed": "X"}})
    assert over and under
    assert "\n".join(over) != "\n".join(under)
    # and the repo really does ship both, so the distinction is load-bearing
    assert {e["direction"] for e in _ce_keys().values()} == {"over_fire", "under_fire"}


def test_absence_renders_nothing_at_all():
    """No field ⇒ no line. "Silent" must read as "nothing measured", never as
    "validated" — an unconditional footer would say the opposite."""
    assert lib._counterexample_lines({"value": 1}) == []


def test_prose_may_name_deprecated_alias_spellings(tmp_path):
    """During an open alias window the OLD spelling is still valid conf.d
    input, so pack-header prose may legitimately name it (F6 membership)."""
    f = tmp_path / "rule-pack-alias-prose.yaml"
    f.write_text(
        "# header:\n#   mysql_cpu: \"45\"\n#   mysql_cpu_critical: \"50\"\n"
        "groups: []\n",
        encoding="utf-8")
    result = gate.run_check(prose_files=[str(f)])
    assert not any("membership" in e for e in result["errors"]), result
