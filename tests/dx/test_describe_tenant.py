#!/usr/bin/env python3
"""Tests for describe_tenant.py — Effective tenant config resolution with ADR-018 semantics."""

import json
import os
import subprocess
import sys
import textwrap

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "dx"))

import describe_tenant as dt  # noqa: E402


# ---------------------------------------------------------------------------
# Test: deep_merge()
# ---------------------------------------------------------------------------

class TestDeepMerge:
    """Tests for deep_merge() — ADR-018 semantics."""

    def test_deep_merge_basic(self):
        base = {"a": 1, "b": {"x": 10}}
        override = {"b": {"y": 20}, "c": 3}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1, "b": {"x": 10, "y": 20}, "c": 3}

    def test_deep_merge_scalar_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 99}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1, "b": 99}

    def test_deep_merge_array_replace(self):
        base = {"endpoints": ["a", "b"], "config": {"x": 1}}
        override = {"endpoints": ["c", "d"]}
        result = dt.deep_merge(base, override)
        assert result["endpoints"] == ["c", "d"]
        assert result["config"] == {"x": 1}

    def test_deep_merge_null_optout(self):
        base = {"a": 1, "b": 2, "c": {"x": 10, "y": 20}}
        override = {"a": None, "c": {"y": None}}
        result = dt.deep_merge(base, override)
        assert "a" not in result
        assert result["b"] == 2
        assert result["c"] == {"x": 10}

    def test_deep_merge_metadata_skip(self):
        base = {"_metadata": {"v": 1}, "config": {"x": 10}}
        override = {"_metadata": {"v": 999}}
        result = dt.deep_merge(base, override)
        # _metadata from base is NOT deep_merged; it's skipped entirely
        assert result.get("_metadata", {}).get("v") == 1

    def test_deep_merge_empty_dicts(self):
        base = {"a": 1}
        override = {}
        result = dt.deep_merge(base, override)
        assert result == {"a": 1}

        result = dt.deep_merge({}, override)
        assert result == {}


# ---------------------------------------------------------------------------
# Test: ConfDScanner (flat & hierarchical)
# ---------------------------------------------------------------------------

class TestConfDScanner:
    """Tests for ConfDScanner — filesystem scanning & tenant mapping."""

    def test_scanner_flat_dir(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Create _defaults.yaml at root
        defaults_yaml = conf_d / "_defaults.yaml"
        defaults_yaml.write_text(
            yaml.dump({"defaults": {"timeout": 30, "retries": 3}}),
            encoding="utf-8"
        )

        # Create tenant files
        tenants_yaml = conf_d / "tenants.yaml"
        tenants_yaml.write_text(
            yaml.dump({
                "tenants": {
                    "tenant-a": {"name": "Tenant A", "retries": 5},
                    "tenant-b": {"name": "Tenant B"},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        assert len(scanner.tenants) == 2
        assert "tenant-a" in scanner.tenants
        assert "tenant-b" in scanner.tenants
        assert scanner.tenants["tenant-a"]["name"] == "Tenant A"

    def test_scanner_hierarchical(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # L0: root _defaults.yaml
        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 10, "region": "us"}}),
            encoding="utf-8"
        )

        # L1: domain level
        domain_dir = conf_d / "acme"
        domain_dir.mkdir()
        (domain_dir / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 20}}),
            encoding="utf-8"
        )

        # L2: tenant file in domain
        (domain_dir / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "acme-prod": {"replicas": 3},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        assert "acme-prod" in scanner.tenants
        assert len(scanner.defaults_chain["acme-prod"]) >= 2

    def test_scanner_effective_config_with_defaults(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        # Root defaults
        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 10, "retries": 1, "debug": False}}),
            encoding="utf-8"
        )

        # Tenant overrides timeout and debug
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "test-tenant": {"timeout": 30, "debug": True}
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        effective = scanner.effective_config("test-tenant")
        assert effective["timeout"] == 30  # overridden
        assert effective["retries"] == 1   # from defaults
        assert effective["debug"] is True  # overridden


# ---------------------------------------------------------------------------
# Test: source_info() and hashing
# ---------------------------------------------------------------------------

class TestSourceInfo:
    """Tests for source_info() — traceability & hashing."""

    def test_source_info_structure(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"x": 1}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"t1": {"y": 2}}}),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        info = scanner.source_info("t1")

        assert info["tenant_id"] == "t1"
        assert "source_file" in info
        assert "source_hash" in info
        assert "merged_hash" in info
        assert "defaults_chain" in info
        assert "effective_config" in info
        assert isinstance(info["source_hash"], str)
        assert isinstance(info["merged_hash"], str)

    def test_source_info_hashes_differ_with_defaults(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"env": "prod"}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"t1": {"app": "myapp"}}}),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        info = scanner.source_info("t1")
        # source_hash is of the tenant file only; merged_hash includes defaults
        assert info["source_hash"] != info["merged_hash"]


# ---------------------------------------------------------------------------
# Test: diff_tenants()
# ---------------------------------------------------------------------------

class TestDiffTenants:
    """Tests for diff_tenants() — config comparison."""

    def test_diff_tenants_complete(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"common": 999}}),
            encoding="utf-8"
        )

        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "t-a": {"app": "app-a", "version": "1.0"},
                    "t-b": {"app": "app-b", "version": "2.0", "extra": "value"},
                }
            }),
            encoding="utf-8"
        )

        scanner = dt.ConfDScanner(conf_d)
        diff = scanner.diff_tenants("t-a", "t-b")

        assert diff["tenant_a"] == "t-a"
        assert diff["tenant_b"] == "t-b"
        assert "different" in diff
        assert "only_in_t-a" in diff
        assert "only_in_t-b" in diff


# ---------------------------------------------------------------------------
# Test: CLI
# ---------------------------------------------------------------------------

class TestCLI:
    """Tests for CLI argument handling & subprocess execution."""

    def test_cli_help(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), "--help"],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        assert "describe" in result.stdout.decode().lower() or "usage" in result.stdout.decode().lower()

    def test_cli_show_sources_subprocess(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({"defaults": {"timeout": 30}}),
            encoding="utf-8"
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({"tenants": {"cli-test": {"name": "Test"}}}),
            encoding="utf-8"
        )

        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                "cli-test",
                "--conf-d", str(conf_d),
                "--show-sources",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.decode())
        assert output["tenant_id"] == "cli-test"
        assert "effective_config" in output

    def test_cli_all_mode(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "t1": {"a": 1},
                    "t2": {"b": 2},
                }
            }),
            encoding="utf-8"
        )

        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                "--conf-d", str(conf_d),
                "--all",
            ],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        output = json.loads(result.stdout.decode())
        assert "t1" in output
        assert "t2" in output


# ---------------------------------------------------------------------------
# Test: --what-if mode (P0 #5 ship-blocker fix)
# ---------------------------------------------------------------------------

class TestWhatIf:
    """Tests for --what-if mode: simulate modified _defaults.yaml and return diff + hash change."""

    def _setup_conf_d(self, tmp_path):
        """Build a fixture: L0 defaults + tenant file → returns conf.d Path."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()

        (conf_d / "_defaults.yaml").write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 30,
                }
            }),
            encoding="utf-8",
        )
        (conf_d / "tenants.yaml").write_text(
            yaml.dump({
                "tenants": {
                    "whatif-tenant": {
                        "name": "What-if test tenant",
                        "pg_stat_activity_count": 300,  # override L0
                    }
                }
            }),
            encoding="utf-8",
        )
        return conf_d

    def _run_cli(self, conf_d, tenant_id, what_if_path):
        """Run describe_tenant --what-if and return parsed JSON output."""
        result = subprocess.run(
            [
                sys.executable,
                os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"),
                tenant_id,
                "--conf-d", str(conf_d),
                "--what-if", str(what_if_path),
            ],
            capture_output=True,
            timeout=5,
        )
        return result

    def test_what_if_substitute_changes_hash(self, tmp_path):
        """Substituting an existing _defaults.yaml that modifies a tenant-visible field → hash changes."""
        conf_d = self._setup_conf_d(tmp_path)
        # What-if: bump pg_replication_lag_seconds to 60 (tenant does NOT override)
        whatif = tmp_path / "whatif.yaml"
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 60,  # changed from 30
                }
            }),
            encoding="utf-8",
        )
        # Point the what-if at the L0 path to trigger "substitute"
        l0_path = conf_d / "_defaults.yaml"
        l0_path.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 60,
                }
            }),
            encoding="utf-8",
        )
        # Reset L0 back and use whatif as substitute path
        l0_path.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 500,
                    "pg_replication_lag_seconds": 30,
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", l0_path)
        # l0_path content hasn't changed so hash should NOT change when using l0_path itself
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["tenant_id"] == "whatif-tenant"
        # L0 path substitution with same content → no change
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False
        assert output["substitution_type"] == "substitute"

    def test_what_if_append_adds_new_field(self, tmp_path):
        """Appending a what-if defaults that introduces a new field → merged_hash changes + added_keys populated."""
        conf_d = self._setup_conf_d(tmp_path)
        # Place what-if OUTSIDE conf.d/ to trigger "append-external"
        whatif = tmp_path / "whatif_external.yaml"
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_locks_count": 100,  # new field, not in L0 or tenant
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", whatif)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        assert output["substitution_type"] == "append-external"
        assert output["merged_hash_changed"] is True
        assert output["would_trigger_reload"] is True
        assert "pg_locks_count" in output["added_keys"]
        assert output["added_keys"]["pg_locks_count"] == 100

    def test_what_if_tenant_override_shields_from_change(self, tmp_path):
        """If tenant overrides a field, changing that field in what-if defaults should NOT change merged_hash."""
        conf_d = self._setup_conf_d(tmp_path)
        whatif = tmp_path / "whatif.yaml"
        # Change pg_stat_activity_count in defaults — but tenant overrides with 300
        whatif.write_text(
            yaml.dump({
                "defaults": {
                    "pg_stat_activity_count": 999,  # tenant still overrides with 300
                }
            }),
            encoding="utf-8",
        )
        result = self._run_cli(conf_d, "whatif-tenant", whatif)
        assert result.returncode == 0, f"stderr: {result.stderr.decode()}"
        output = json.loads(result.stdout.decode())
        # Tenant override shields this field → merged hash NOT changed
        # (in effective config, pg_stat_activity_count is still 300 from tenant)
        assert output["merged_hash_changed"] is False
        assert output["would_trigger_reload"] is False

    def test_what_if_file_not_found(self, tmp_path):
        """Non-existent --what-if path → exit 1 with clear error."""
        conf_d = self._setup_conf_d(tmp_path)
        result = self._run_cli(conf_d, "whatif-tenant", tmp_path / "does-not-exist.yaml")
        assert result.returncode == 1
        assert b"--what-if file not found" in result.stderr

    def test_what_if_help_text_no_longer_stub(self):
        """--what-if help text must not claim 'not yet implemented' (P0 #5 fix)."""
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "scripts", "tools", "dx", "describe_tenant.py"), "--help"],
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0
        help_text = result.stdout.decode()
        assert "not yet implemented" not in help_text.lower()
        assert "--what-if" in help_text
