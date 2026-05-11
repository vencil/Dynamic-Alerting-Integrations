#!/usr/bin/env python3
"""test_state_reconcile.py — tests for da-tools state-reconcile (#405 Cat A).

Coverage:
  - find_state_files: empty dir, multiple files, deterministic sort
  - read_state: valid / missing / invalid JSON
  - apply_migration_chain: pass-through (same version), no-path failure
  - build_manifest: shape + path canonicalisation
  - reconcile: idempotency (run twice = no change second time)
  - reconcile: manifest drift detection + rebuild
  - reconcile: schema drift (unresolvable) collection
  - dry_run flag: no file writes
  - compute_exit_code: drift / ci-pending / clean

Usage:
  pytest tests/ops/test_state_reconcile.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import state_reconcile as sr


# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Empty .da/state directory."""
    d = tmp_path / ".da" / "state"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / ".da" / "manifest.json"


def _write_state(state_dir: Path, cluster: str, schema_version: str = "1.0") -> Path:
    """Helper: write a minimal valid state file."""
    p = state_dir / f"{cluster}.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "generated_at": "2026-05-11T00:00:00Z",
                "generated_by": "test",
            }
        ),
        encoding="utf-8",
    )
    return p


# ─── find_state_files ─────────────────────────────────────────────────


def test_find_state_files_empty_dir(state_dir):
    assert sr.find_state_files(state_dir) == []


def test_find_state_files_missing_dir(tmp_path):
    assert sr.find_state_files(tmp_path / "nope") == []


def test_find_state_files_sorted(state_dir):
    _write_state(state_dir, "zebra")
    _write_state(state_dir, "alpha")
    _write_state(state_dir, "middle")
    result = [f.stem for f in sr.find_state_files(state_dir)]
    assert result == ["alpha", "middle", "zebra"]


def test_find_state_files_only_json(state_dir):
    _write_state(state_dir, "cluster-a")
    (state_dir / "README.md").write_text("not state")
    (state_dir / "ignore.txt").write_text("not state")
    result = [f.name for f in sr.find_state_files(state_dir)]
    assert result == ["cluster-a.json"]


# ─── read_state ───────────────────────────────────────────────────────


def test_read_state_valid(state_dir):
    p = _write_state(state_dir, "cluster-a")
    data, err = sr.read_state(p)
    assert err is None
    assert data["schema_version"] == "1.0"


def test_read_state_invalid_json(state_dir):
    p = state_dir / "broken.json"
    p.write_text("{not valid json")
    data, err = sr.read_state(p)
    assert data is None
    assert err is not None
    assert "invalid JSON" in err


def test_read_state_missing_file(state_dir):
    p = state_dir / "nonexistent.json"
    data, err = sr.read_state(p)
    assert data is None
    assert err is not None
    assert "cannot read" in err


# ─── normalize_schema_version ─────────────────────────────────────────


def test_normalize_schema_version_string_passthrough():
    assert sr.normalize_schema_version("1.0") == "1.0"
    assert sr.normalize_schema_version("1.1") == "1.1"


def test_normalize_schema_version_float_coerced():
    # User hand-edits JSON: "schema_version": 1.0 (numeric) → coerce to "1.0".
    # Without this, equality vs "1.0" string fails and we false-report drift.
    assert sr.normalize_schema_version(1.0) == "1.0"
    assert sr.normalize_schema_version(2.0) == "2.0"


def test_normalize_schema_version_int_coerced():
    # Bare int: treat as major-only.
    assert sr.normalize_schema_version(1) == "1.0"


def test_normalize_schema_version_non_integer_float():
    # Future minor like 1.5 → "1.5".
    assert sr.normalize_schema_version(1.5) == "1.5"


def test_normalize_schema_version_none():
    assert sr.normalize_schema_version(None) is None


def test_normalize_schema_version_bool_rejected():
    # bool is an int subclass; must NOT coerce True/False to "1.0"/"0.0".
    assert sr.normalize_schema_version(True) is None
    assert sr.normalize_schema_version(False) is None


def test_normalize_schema_version_collection_rejected():
    assert sr.normalize_schema_version([1, 0]) is None
    assert sr.normalize_schema_version({"major": 1}) is None


# ─── apply_migration_chain ────────────────────────────────────────────


def test_apply_migration_chain_same_version_pass_through():
    state = {"schema_version": "1.0", "x": 1}
    result, err = sr.apply_migration_chain(state, "1.0", "1.0")
    assert err is None
    assert result == state


def test_apply_migration_chain_no_path():
    state = {"schema_version": "0.9"}
    result, err = sr.apply_migration_chain(state, "0.9", "1.0")
    assert result is None
    assert err is not None
    assert "no registered migration" in err


def test_apply_migration_chain_registered_path(monkeypatch):
    """Verify the extension hook works when a migration is registered."""

    def _fake_migrate(state):
        state["schema_version"] = "1.1"
        state["gate_log"] = []
        return state

    monkeypatch.setitem(sr.MIGRATIONS, ("1.0", "1.1"), _fake_migrate)
    state = {"schema_version": "1.0"}
    result, err = sr.apply_migration_chain(state, "1.0", "1.1")
    assert err is None
    assert result["schema_version"] == "1.1"
    assert result["gate_log"] == []


# ─── build_manifest ───────────────────────────────────────────────────


def test_build_manifest_empty(state_dir):
    manifest = sr.build_manifest([], state_dir)
    assert manifest == {"schema_version": "1.0", "states": []}


def test_build_manifest_custom_state_dir_path_prefix(tmp_path):
    """Manifest `path` field must reflect caller-supplied state_dir, not
    a hard-coded `.da/<basename>`. This caught a real bug where custom
    locations like `custom/states/` produced manifest entries pointing
    to nonexistent `.da/states/<file>`."""
    custom = tmp_path / "custom" / "states"
    custom.mkdir(parents=True)
    _write_state(custom, "cluster-x")
    files = sr.find_state_files(custom)

    # Relative-form state_dir → preserve as-is in manifest
    rel = Path("custom/states")
    manifest = sr.build_manifest(files, rel)
    assert manifest["states"][0]["path"] == "custom/states/cluster-x.json"

    # Default `.da/state` still produces canonical shape
    manifest_default = sr.build_manifest(files, Path(".da/state"))
    assert manifest_default["states"][0]["path"] == ".da/state/cluster-x.json"


def test_build_manifest_uses_posix_separators_on_windows():
    """Path separators in manifest must be POSIX-style for cross-platform
    GitOps repos (state files committed on Windows + read on Linux CI)."""
    # Path-like input with backslashes (simulating Windows-style CWD form);
    # Path normalises via as_posix in our helper
    state_dir = Path("nested") / "states"
    manifest = sr.build_manifest([Path(state_dir / "c.json")], state_dir)
    assert manifest["states"][0]["path"] == "nested/states/c.json"
    assert "\\" not in manifest["states"][0]["path"]


def test_build_manifest_multiple(state_dir):
    _write_state(state_dir, "prod-us-east")
    _write_state(state_dir, "prod-us-west")
    files = sr.find_state_files(state_dir)
    manifest = sr.build_manifest(files, state_dir)
    assert manifest["schema_version"] == "1.0"
    assert {s["cluster"] for s in manifest["states"]} == {
        "prod-us-east",
        "prod-us-west",
    }
    # Path uses the actual state_dir prefix (POSIX form) + filename.
    expected_prefix = state_dir.as_posix().rstrip("/")
    for s in manifest["states"]:
        assert s["path"].startswith(f"{expected_prefix}/")
        assert s["path"].endswith(".json")


# ─── reconcile: end-to-end ────────────────────────────────────────────


def test_reconcile_idempotent(state_dir, manifest_path):
    """Running reconcile twice → second run is a no-op."""
    _write_state(state_dir, "cluster-a")
    _write_state(state_dir, "cluster-b")

    r1 = sr.reconcile(state_dir, manifest_path)
    assert r1["manifest_change"] is not None  # first run creates manifest
    assert manifest_path.exists()

    r2 = sr.reconcile(state_dir, manifest_path)
    assert r2["manifest_change"] is None  # second run is consistent
    assert r2["schema_migrations"] == []
    assert r2["schema_drift_unresolvable"] == []


def test_reconcile_manifest_drift(state_dir, manifest_path):
    """State file added but manifest missing it → rebuild."""
    _write_state(state_dir, "cluster-a")
    sr.reconcile(state_dir, manifest_path)  # initial manifest with 1 cluster
    _write_state(state_dir, "cluster-b")  # second cluster added

    r = sr.reconcile(state_dir, manifest_path)
    assert r["manifest_change"] is not None
    assert r["manifest_change"]["old_state_count"] == 1
    assert r["manifest_change"]["new_state_count"] == 2

    # Verify manifest content reflects both clusters
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    clusters = {s["cluster"] for s in manifest_data["states"]}
    assert clusters == {"cluster-a", "cluster-b"}


def test_reconcile_unresolvable_drift_missing_version(state_dir, manifest_path):
    """State file without schema_version field → unresolvable."""
    p = state_dir / "broken.json"
    p.write_text(json.dumps({"generated_at": "x"}), encoding="utf-8")

    r = sr.reconcile(state_dir, manifest_path)
    assert len(r["schema_drift_unresolvable"]) == 1
    assert "missing schema_version" in r["schema_drift_unresolvable"][0]["reason"]


def test_reconcile_numeric_schema_version_normalised(state_dir, manifest_path):
    """A hand-edited state file with numeric schema_version (1.0 not "1.0")
    must be treated as the equivalent string version. Without normalisation
    the equality vs CURRENT_SCHEMA_VERSION fails and we report bogus drift."""
    p = state_dir / "cluster-num.json"
    p.write_text(
        json.dumps(
            {
                "schema_version": 1.0,  # numeric, not string
                "generated_at": "2026-05-12T00:00:00Z",
                "generated_by": "test",
            }
        ),
        encoding="utf-8",
    )

    r = sr.reconcile(state_dir, manifest_path)
    # No drift reported — numeric 1.0 normalised to "1.0" matches CURRENT.
    assert r["schema_drift_unresolvable"] == []
    assert r["schema_migrations"] == []


def test_reconcile_unsupported_schema_version_type(state_dir, manifest_path):
    """schema_version of dict / bool / list → unresolvable with a typed reason."""
    p = state_dir / "cluster-weird.json"
    p.write_text(
        json.dumps({"schema_version": {"major": 1, "minor": 0}}),
        encoding="utf-8",
    )

    r = sr.reconcile(state_dir, manifest_path)
    assert len(r["schema_drift_unresolvable"]) == 1
    reason = r["schema_drift_unresolvable"][0]["reason"]
    assert "unsupported type" in reason
    assert "dict" in reason


def test_reconcile_unresolvable_drift_old_version(state_dir, manifest_path):
    """State file with old version + no migration → unresolvable."""
    _write_state(state_dir, "cluster-x", schema_version="0.9")

    r = sr.reconcile(state_dir, manifest_path)
    assert len(r["schema_drift_unresolvable"]) == 1
    drift = r["schema_drift_unresolvable"][0]
    assert drift["from"] == "0.9"
    assert "no registered migration" in drift["reason"]


def test_reconcile_dry_run_no_writes(state_dir, manifest_path):
    """--dry-run reports changes but does not touch the filesystem."""
    _write_state(state_dir, "cluster-a")

    r = sr.reconcile(state_dir, manifest_path, dry_run=True)
    assert r["manifest_change"] is not None  # reports change
    assert not manifest_path.exists()  # but no write happened


def test_reconcile_applies_migration(state_dir, manifest_path, monkeypatch):
    """When a migration is registered, the state file is rewritten."""

    def _migrate(state):
        state["schema_version"] = "1.1"
        state["new_field"] = "default"
        return state

    monkeypatch.setattr(sr, "CURRENT_SCHEMA_VERSION", "1.1")
    monkeypatch.setitem(sr.MIGRATIONS, ("1.0", "1.1"), _migrate)

    p = _write_state(state_dir, "cluster-a", schema_version="1.0")
    sr.reconcile(state_dir, manifest_path)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.1"
    assert data["new_field"] == "default"


# ─── compute_exit_code ────────────────────────────────────────────────


def test_exit_code_clean():
    report = {
        "schema_migrations": [],
        "schema_drift_unresolvable": [],
        "manifest_change": None,
    }
    assert sr.compute_exit_code(report, ci=False, dry_run=False) == 0
    assert sr.compute_exit_code(report, ci=True, dry_run=False) == 0


def test_exit_code_unresolvable_drift_always_fails():
    report = {
        "schema_migrations": [],
        "schema_drift_unresolvable": [{"file": "x", "reason": "y"}],
        "manifest_change": None,
    }
    # Unresolvable drift fails regardless of CI / dry-run mode
    assert sr.compute_exit_code(report, ci=False, dry_run=False) == 1
    assert sr.compute_exit_code(report, ci=True, dry_run=True) == 1


def test_exit_code_ci_dry_run_pending_changes():
    """--ci --dry-run with pending changes → exit 1 (CI gate signals work needed)."""
    report = {
        "schema_migrations": [],
        "schema_drift_unresolvable": [],
        "manifest_change": {"old_state_count": 1, "new_state_count": 2},
    }
    assert sr.compute_exit_code(report, ci=True, dry_run=True) == 1
    # Non-CI dry-run: pending changes are informational, exit 0
    assert sr.compute_exit_code(report, ci=False, dry_run=True) == 0


def test_exit_code_applied_changes_not_failing():
    """Successfully applied (non-dry-run) changes → exit 0 even with --ci."""
    report = {
        "schema_migrations": [{"file": "x", "from": "1.0", "to": "1.1"}],
        "schema_drift_unresolvable": [],
        "manifest_change": {"old_state_count": 0, "new_state_count": 1},
    }
    assert sr.compute_exit_code(report, ci=True, dry_run=False) == 0


# ─── main() end-to-end via argv ───────────────────────────────────────


def test_main_clean_run(state_dir, manifest_path, capsys):
    _write_state(state_dir, "cluster-a")
    rc = sr.main(
        [
            "--state-dir",
            str(state_dir),
            "--manifest-path",
            str(manifest_path),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Scanned 1 state" in out
    assert manifest_path.exists()


def test_main_json_output(state_dir, manifest_path, capsys):
    _write_state(state_dir, "cluster-a")
    rc = sr.main(
        [
            "--state-dir",
            str(state_dir),
            "--manifest-path",
            str(manifest_path),
            "--json",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["state_file_count"] == 1
    assert report["dry_run"] is False


def test_main_ci_dry_run_pending_exits_1(state_dir, manifest_path, capsys):
    _write_state(state_dir, "cluster-a")
    rc = sr.main(
        [
            "--state-dir",
            str(state_dir),
            "--manifest-path",
            str(manifest_path),
            "--dry-run",
            "--ci",
        ]
    )
    # First run: manifest doesn't exist → manifest_change → CI pending → exit 1
    assert rc == 1
    assert not manifest_path.exists()


def test_main_unresolvable_drift_exits_1(state_dir, manifest_path):
    p = state_dir / "broken.json"
    p.write_text("{}", encoding="utf-8")
    rc = sr.main(
        [
            "--state-dir",
            str(state_dir),
            "--manifest-path",
            str(manifest_path),
        ]
    )
    assert rc == 1
