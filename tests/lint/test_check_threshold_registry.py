"""Tests for scripts/tools/lint/check_threshold_registry.py (TRK-339 WS1a / #1200).

The gate holds the transition-period invariant: the standalone registry (D2
SoT-to-be) must validate against its JSON Schema AND stay semantically equal to
the still-operative scaffold_tenant.RULE_PACKS. Pins:
  - the live repo is green (registry committed, schema-valid, drift-free)
  - a schema violation fails --ci
  - dual-SoT drift (either direction) fails --ci
  - missing/unparseable registry file is a violation with a --regen hint
  - the CLI exit-code contract (report-only without --ci)
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_threshold_registry.py"

_spec = importlib.util.spec_from_file_location("check_threshold_registry", _SCRIPT)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

lib = gate.registry_lib


# ── the live repo ─────────────────────────────────────────────────────────

def test_real_repo_is_green():
    """Committed registry: schema-valid AND semantically equal to scaffold."""
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


def test_value_drift_is_an_error(tmp_path):
    def bump_value(doc):
        key = next(iter(doc["keys"]))
        doc["keys"][key]["value"] = doc["keys"][key]["value"] + 1
    path = _write_registry_variant(tmp_path, bump_value)
    result = gate.run_check(registry_path=path)
    assert any(e.startswith("drift:") and "value" in e
               for e in result["errors"]), result


def test_registry_only_key_is_an_error(tmp_path):
    """Direction 2: a key hand-added to the registry (not in scaffold) fails —
    during the transition scaffold is operative, so a registry-only key would
    be a silent no-op contract entry."""
    def add_ghost(doc):
        doc["keys"]["ghost_metric"] = {
            "pack": "mariadb", "tier": "defaults", "value": 1,
            "unit": "count", "desc": "ghost"}
    path = _write_registry_variant(tmp_path, add_ghost)
    result = gate.run_check(registry_path=path)
    assert any("ghost_metric" in e and "missing from scaffold" in e
               for e in result["errors"]), result


def test_scaffold_only_key_is_an_error(tmp_path):
    """Direction 1: a scaffold key missing from the registry fails."""
    def drop_one(doc):
        key = next(iter(doc["keys"]))
        del doc["keys"][key]
    path = _write_registry_variant(tmp_path, drop_one)
    result = gate.run_check(registry_path=path)
    assert any("missing from registry" in e for e in result["errors"]), result


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
    assert "    mysql_cpu: 30" in text
    dst.write_text(text.replace("    mysql_cpu: 30", "    mysql_cpu: 80"),
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
    """The #1196 pending line (e.g. db2_lock_wait_time) and dimensional
    tokens stay legal in prose while their repair is pending."""
    f = tmp_path / "rule-pack-ok.yaml"
    f.write_text(
        "# header:\n"
        "#   db2_lock_wait_time: 10\n"
        "#   mysql_connections_critical: \"120\"\n"
        "#   \"oracle_tablespace_used_percent{tablespace_name=\\\"TEMP\\\"}\": \"disable\"\n"
        "groups: []\n",
        encoding="utf-8")
    result = gate.run_check(prose_files=[str(f)])
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
    """--regen delegates to the lib writer AND the surface splicer; pin the
    wiring, not the content."""
    calls = {}

    def fake_write():
        calls["write"] = True
        return {"path": str(tmp_path / "r.yaml"), "packs": 1, "keys": 2}

    def fake_surfaces():
        calls["surfaces"] = True
        return []

    monkeypatch.setattr(gate.registry_lib, "write_registry", fake_write)
    monkeypatch.setattr(gate.registry_lib, "regen_surfaces", fake_surfaces)
    assert gate.main(["--regen"]) == gate.EXIT_OK
    assert calls.get("write") and calls.get("surfaces")
