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


def test_regen_writes_registry(monkeypatch, tmp_path):
    """--regen delegates to the lib writer; pin the wiring, not the content."""
    calls = {}

    def fake_write():
        calls["hit"] = True
        return {"path": str(tmp_path / "r.yaml"), "packs": 1, "keys": 2}

    monkeypatch.setattr(gate.registry_lib, "write_registry", fake_write)
    assert gate.main(["--regen"]) == gate.EXIT_OK
    assert calls.get("hit")
