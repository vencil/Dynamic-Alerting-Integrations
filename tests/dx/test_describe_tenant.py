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
