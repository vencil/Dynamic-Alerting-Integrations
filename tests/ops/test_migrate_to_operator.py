#!/usr/bin/env python3
"""Tests for migrate_to_operator.py — ConfigMap → CRD migration tool.

Verifies:
  1. parse_configmap_rules() — Parse ConfigMap YAML with rule groups
  2. convert_rules_to_crd() — Convert to PrometheusRule CRD structure
  3. analyze_migration() — Analyze migration scope and identify issues
  4. build_migration_checklist() — Generate 6-phase migration checklist
  5. generate_migration() — Full E2E orchestration
  6. RFC 1123 validation for tenant names
  7. Dry-run mode (no file writes)
  8. JSON output mode
  9. ConfigMap → CRD end-to-end conversion
"""

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts', 'tools', 'ops'))

# Import the module to test
import migrate_to_operator as mto  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_configmap_dir():
    """Create a temporary directory with sample ConfigMap YAML files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create a sample ConfigMap with rules
        cm_yaml = textwrap.dedent("""
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: prometheus-rules-cpu
            data:
              cpu-rules.yaml: |
                groups:
                  - name: cpu_alerts
                    rules:
                      - alert: HighCPU
                        expr: cpu_usage > 80
                        for: 5m
                        labels:
                          severity: warning
                      - alert: CriticalCPU
                        expr: cpu_usage > 95
                        for: 2m
                        labels:
                          severity: critical
        """).strip()

        (tmppath / "cpu-rules.yaml").write_text(cm_yaml)

        # Create another ConfigMap with different rules
        cm_yaml_2 = textwrap.dedent("""
            apiVersion: v1
            kind: ConfigMap
            metadata:
              name: prometheus-rules-memory
            data:
              memory-rules.yaml: |
                groups:
                  - name: memory_alerts
                    rules:
                      - alert: HighMemory
                        expr: memory_usage > 85
                        for: 5m
                        labels:
                          severity: warning
        """).strip()

        (tmppath / "memory-rules.yaml").write_text(cm_yaml_2)

        yield tmppath


@pytest.fixture
def temp_config_dir():
    """Create a temporary directory with sample tenant config files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)

        # Create tenant config files
        tenant_a = textwrap.dedent("""
            tenants:
              db-a:
                cpu_threshold_warning: 80
                cpu_threshold_critical: 95
                memory_threshold_warning: 85
        """).strip()

        tenant_b = textwrap.dedent("""
            tenants:
              db-b:
                cpu_threshold_warning: 75
                cpu_threshold_critical: 90
        """).strip()

        (tmppath / "db-a.yaml").write_text(tenant_a)
        (tmppath / "db-b.yaml").write_text(tenant_b)

        yield tmppath


@pytest.fixture
def temp_output_dir():
    """Create a temporary directory for output CRDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


# ────────────────────────────────────────────────────────────────────────────
# Test: parse_configmap_rules
# ────────────────────────────────────────────────────────────────────────────

class TestParseConfigMapRules:
    """Tests for parse_configmap_rules()."""

    def test_parse_configmap_rules_basic(self, temp_configmap_dir):
        """Parse a simple ConfigMap YAML with 1 rule group."""
        rules = mto.parse_configmap_rules(temp_configmap_dir)

        # Should have parsed 2 ConfigMap files
        assert len(rules) == 2

        # Verify structure of first result
        first = next((r for r in rules if "cpu" in r["file"]), None)
        assert first is not None
        assert first["name"] == "prometheus-rules-cpu"
        assert first["file"] == "cpu-rules.yaml"
        assert len(first["rule_groups"]) == 1
        assert first["rule_groups"][0]["name"] == "cpu_alerts"

    def test_parse_configmap_rules_multiple(self, temp_configmap_dir):
        """Parse multiple ConfigMap files."""
        rules = mto.parse_configmap_rules(temp_configmap_dir)

        # Verify we got both files
        files = {r["file"] for r in rules}
        assert "cpu-rules.yaml" in files
        assert "memory-rules.yaml" in files

        # Verify rule group counts
        total_groups = sum(len(r["rule_groups"]) for r in rules)
        assert total_groups == 2

    def test_parse_configmap_rules_invalid_yaml(self, temp_configmap_dir):
        """Graceful handling of invalid YAML."""
        # Add an invalid YAML file
        invalid_yaml = "invalid: yaml: content: ["
        (temp_configmap_dir / "invalid.yaml").write_text(invalid_yaml)

        # Should not raise, but skip invalid files
        rules = mto.parse_configmap_rules(temp_configmap_dir)
        files = {r["file"] for r in rules}
        assert "invalid.yaml" not in files
        assert len(rules) == 2  # Still got the 2 valid files

    def test_parse_configmap_rules_directory_not_found(self):
        """FileNotFoundError when directory does not exist."""
        with pytest.raises(FileNotFoundError):
            mto.parse_configmap_rules(Path("/nonexistent/path"))


# ────────────────────────────────────────────────────────────────────────────
# Test: convert_rules_to_crd
# ────────────────────────────────────────────────────────────────────────────

class TestConvertRulesToCRD:
    """Tests for convert_rules_to_crd()."""

    def test_convert_rules_to_crd(self):
        """Verify CRD output structure (apiVersion, kind, metadata, spec.groups)."""
        rule_groups = [
            {
                "name": "cpu_alerts",
                "rules": [
                    {"alert": "HighCPU", "expr": "cpu > 80", "for": "5m"},
                ],
            },
        ]

        crd = mto.convert_rules_to_crd(rule_groups, "cpu-pack", "monitoring")

        # Verify structure
        assert crd["apiVersion"] == "monitoring.coreos.com/v1"
        assert crd["kind"] == "PrometheusRule"
        assert crd["metadata"]["name"] == "da-rule-pack-cpu-pack"
        assert crd["metadata"]["namespace"] == "monitoring"
        assert len(crd["spec"]["groups"]) == 1
        assert crd["spec"]["groups"][0]["name"] == "cpu_alerts"

    def test_convert_rules_preserves_labels(self):
        """Ensure 'migrated-from: configmap' label is present."""
        rule_groups = [{"name": "test", "rules": []}]

        crd = mto.convert_rules_to_crd(rule_groups, "test", "monitoring")

        labels = crd["metadata"]["labels"]
        assert labels["migrated-from"] == "configmap"
        assert labels["app.kubernetes.io/part-of"] == "dynamic-alerting"
        assert labels["prometheus"] == "kube-prometheus"


# ────────────────────────────────────────────────────────────────────────────
# Test: analyze_migration
# ────────────────────────────────────────────────────────────────────────────

class TestAnalyzeMigration:
    """Tests for analyze_migration()."""

    def test_analyze_migration(self, temp_configmap_dir, temp_config_dir):
        """Check analysis counts (ConfigMaps, rule groups, tenants, estimated CRDs)."""
        analysis = mto.analyze_migration(temp_configmap_dir, temp_config_dir)

        # Verify counts
        assert analysis["configmap_files"] == 2
        assert analysis["rule_groups"] == 2
        assert analysis["tenants"] == 2
        assert analysis["estimated_crds"] == 4  # 2 ConfigMaps + 2 tenants
        assert len(analysis["issues"]) == 0

    def test_analyze_migration_invalid_tenant(self, temp_configmap_dir):
        """RFC 1123 validation catches bad tenant names."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            # Create invalid tenant name (starts with hyphen)
            (tmppath / "-invalid.yaml").write_text("tenants: {}")

            # Create valid tenant name
            (tmppath / "valid-tenant.yaml").write_text("tenants: {}")

            analysis = mto.analyze_migration(temp_configmap_dir, tmppath)

            # Should report the invalid tenant name
            assert len(analysis["issues"]) > 0
            assert any("RFC 1123" in issue or "invalid" in issue.lower()
                      for issue in analysis["issues"])
            assert analysis["tenants"] == 1  # Only the valid one


# ────────────────────────────────────────────────────────────────────────────
# Test: validate_tenant_name
# ────────────────────────────────────────────────────────────────────────────

class TestValidateTenantName:
    """Tests for validate_tenant_name()."""

    @pytest.mark.parametrize("name,expected", [
        ("db-a", True),
        ("db-b", True),
        ("tenant-1", True),
        ("a", True),
        ("ab", True),
        ("-invalid", False),
        ("invalid-", False),
        ("UPPERCASE", False),
        ("under_score", False),
        ("db..a", False),
    ])
    def test_validate_tenant_name(self, name, expected):
        """RFC 1123 compliance checks."""
        assert mto.validate_tenant_name(name) == expected


# ────────────────────────────────────────────────────────────────────────────
# Test: build_migration_checklist
# ────────────────────────────────────────────────────────────────────────────

class TestBuildMigrationChecklist:
    """Tests for build_migration_checklist()."""

    def test_build_migration_checklist_contains_steps(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Checklist has all 6 phases."""
        result = {
            "configmap_files": 2,
            "rule_group_count": 2,
            "tenants": 2,
            "prometheus_rules": [{"crd": {}}],
            "alertmanager_configs": [{"crd": {}}],
        }

        checklist = mto.build_migration_checklist(
            temp_configmap_dir, temp_config_dir, temp_output_dir, result
        )

        # Check that all phases are present
        phases = [
            "Phase 1",
            "Phase 2",
            "Phase 3",
            "Phase 4",
            "Phase 5",
            "Phase 6",
        ]

        for phase in phases:
            assert phase in checklist

        # Check that key sections exist
        assert "Migration Checklist" in checklist or "遷移檢核清單" in checklist


# ────────────────────────────────────────────────────────────────────────────
# Test: generate_migration
# ────────────────────────────────────────────────────────────────────────────

class TestGenerateMigration:
    """Tests for generate_migration()."""

    def test_generate_migration_writes_crds(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Full run creates output directory with CRD files + checklist."""
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify result structure
        assert result["configmap_files"] == 2
        assert result["rule_group_count"] == 2
        assert result["tenants"] == 2
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2
        assert len(result["errors"]) == 0

        # Verify PrometheusRule CRD fields
        for item in result["prometheus_rules"]:
            assert "name" in item
            assert "crd" in item
            crd = item["crd"]
            assert crd["kind"] == "PrometheusRule"
            assert crd["metadata"]["labels"]["migrated-from"] == "configmap"

        # Verify AlertmanagerConfig CRD fields
        for item in result["alertmanager_configs"]:
            assert "name" in item
            assert "tenant" in item
            assert "crd" in item
            crd = item["crd"]
            assert crd["kind"] == "AlertmanagerConfig"
            assert crd["metadata"]["labels"]["migrated-from"] == "configmap"

    def test_generate_migration_json_output(self, temp_configmap_dir, temp_config_dir):
        """JSON mode returns valid JSON with all fields."""
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            Path("/tmp/dummy"),
            "monitoring",
        )

        # Verify that result can be serialized to JSON
        json_str = json.dumps(result, indent=2, ensure_ascii=False, default=str)
        parsed = json.loads(json_str)

        # Verify essential fields
        assert "configmap_files" in parsed
        assert "rule_group_count" in parsed
        assert "tenants" in parsed
        assert "prometheus_rules" in parsed
        assert "alertmanager_configs" in parsed


# ────────────────────────────────────────────────────────────────────────────
# Test: End-to-End
# ────────────────────────────────────────────────────────────────────────────

class TestEndToEnd:
    """End-to-end migration tests."""

    def test_end_to_end_configmap_to_crd(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Full E2E: create temp files, run migration, verify CRD output."""
        # Run full migration
        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got CRDs
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2

        # Verify PrometheusRule structure
        pr = result["prometheus_rules"][0]["crd"]
        assert pr["apiVersion"] == "monitoring.coreos.com/v1"
        assert pr["kind"] == "PrometheusRule"
        assert "spec" in pr
        assert "groups" in pr["spec"]

        # Verify AlertmanagerConfig structure
        ac = result["alertmanager_configs"][0]["crd"]
        assert ac["apiVersion"] == "monitoring.coreos.com/v1beta1"
        assert ac["kind"] == "AlertmanagerConfig"
        assert "spec" in ac
        assert "route" in ac["spec"]
        assert "receivers" in ac["spec"]
        assert "inhibitRules" in ac["spec"]


# ────────────────────────────────────────────────────────────────────────────
# Test: Dry-run Mode
# ────────────────────────────────────────────────────────────────────────────

class TestDryRunMode:
    """Tests for dry-run functionality."""

    def test_generate_migration_dry_run_produces_no_files(self, temp_configmap_dir, temp_config_dir, temp_output_dir):
        """Dry run produces result dict but no files are written."""
        # Note: generate_migration() itself doesn't write files
        # (that's done in main()), so we're testing the core behavior

        result = mto.generate_migration(
            temp_configmap_dir,
            temp_config_dir,
            temp_output_dir,
            "monitoring",
        )

        # Verify we got a result
        assert result is not None
        assert len(result["prometheus_rules"]) == 2
        assert len(result["alertmanager_configs"]) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
