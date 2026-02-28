"""
tests/test_offboard_deprecate.py — Unit tests for offboard_tenant.py,
deprecate_rule.py, and validate_migration.py pure logic.
Tests the filesystem-based lifecycle tools and vector comparison logic
introduced in v0.6.0.
"""

import os
import stat
import sys
import tempfile
import unittest

import yaml

# ---------------------------------------------------------------------------
# Import tools
# ---------------------------------------------------------------------------
TOOLS_DIR = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "tools")
sys.path.insert(0, os.path.abspath(TOOLS_DIR))

import offboard_tenant  # noqa: E402
import deprecate_rule  # noqa: E402
import validate_migration  # noqa: E402


# ===================================================================
# Helper: create temp conf.d directory with YAML files
# ===================================================================
def make_confdir(tmpdir, files):
    """Create YAML files in tmpdir. files = {filename: dict_content}."""
    for filename, content in files.items():
        path = os.path.join(tmpdir, filename)
        with open(path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(content, f, default_flow_style=False, allow_unicode=True)
        os.chmod(path, 0o600)


# ===================================================================
# 1. offboard_tenant — find_config_file
# ===================================================================
class TestFindConfigFile(unittest.TestCase):
    """Verify config file discovery."""

    def test_yaml_extension(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {"db-a.yaml": {"tenants": {"db-a": {}}}})
            path = offboard_tenant.find_config_file("db-a", d)
            self.assertIsNotNone(path)
            self.assertTrue(path.endswith("db-a.yaml"))

    def test_yml_extension(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {"db-b.yml": {"tenants": {"db-b": {}}}})
            # rename to .yml
            src = os.path.join(d, "db-b.yml")
            self.assertTrue(os.path.exists(src))
            path = offboard_tenant.find_config_file("db-b", d)
            self.assertIsNotNone(path)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            path = offboard_tenant.find_config_file("nonexistent", d)
            self.assertIsNone(path)


# ===================================================================
# 2. offboard_tenant — check_cross_references
# ===================================================================
class TestCrossReferences(unittest.TestCase):

    def test_no_cross_ref(self):
        configs = {
            "db-a.yaml": {"path": "/x/db-a.yaml", "data": {"tenants": {"db-a": {"m": 1}}}},
            "db-b.yaml": {"path": "/x/db-b.yaml", "data": {"tenants": {"db-b": {"m": 2}}}},
        }
        refs = offboard_tenant.check_cross_references("db-a", configs)
        self.assertEqual(refs, [])

    def test_found_cross_ref(self):
        configs = {
            "db-a.yaml": {"path": "/x/db-a.yaml", "data": {"tenants": {"db-a": {"m": 1}}}},
            "db-b.yaml": {"path": "/x/db-b.yaml", "data": {"note": "depends on db-a"}},
        }
        refs = offboard_tenant.check_cross_references("db-a", configs)
        self.assertIn("db-b.yaml", refs)


# ===================================================================
# 3. offboard_tenant — get_tenant_metrics
# ===================================================================
class TestGetTenantMetrics(unittest.TestCase):

    def test_found(self):
        configs = {
            "db-a.yaml": {
                "path": "/x/db-a.yaml",
                "data": {"tenants": {"db-a": {"mysql_connections": 70, "mysql_cpu": 80}}},
            },
        }
        metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
        self.assertEqual(len(metrics), 2)
        self.assertEqual(metrics["mysql_connections"], 70)

    def test_empty(self):
        configs = {
            "db-a.yaml": {
                "path": "/x/db-a.yaml",
                "data": {"tenants": {"db-a": {}}},
            },
        }
        metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
        self.assertEqual(metrics, {})

    def test_missing_tenant(self):
        configs = {
            "db-b.yaml": {
                "path": "/x/db-b.yaml",
                "data": {"tenants": {"db-b": {"m": 1}}},
            },
        }
        metrics = offboard_tenant.get_tenant_metrics("db-a", configs)
        self.assertEqual(metrics, {})


# ===================================================================
# 4. offboard_tenant — run_precheck
# ===================================================================
class TestRunPrecheck(unittest.TestCase):

    def test_pass(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": 70}}},
                "db-b.yaml": {"tenants": {"db-b": {"mysql_connections": 80}}},
            })
            can_proceed, report = offboard_tenant.run_precheck("db-a", d)
            self.assertTrue(can_proceed)
            report_text = "\n".join(report)
            self.assertIn("Pre-check", report_text)

    def test_fail_no_config(self):
        with tempfile.TemporaryDirectory() as d:
            can_proceed, report = offboard_tenant.run_precheck("nonexistent", d)
            report_text = "\n".join(report)
            self.assertIn("找不到", report_text)

    def test_warning_cross_ref(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {"m": 1}}},
                "db-b.yaml": {"tenants": {"db-b": {"note": "db-a related"}}},
            })
            can_proceed, report = offboard_tenant.run_precheck("db-a", d)
            # Cross-ref is a warning, can still proceed
            self.assertTrue(can_proceed)


# ===================================================================
# 5. deprecate_rule — scan_for_metric
# ===================================================================
class TestScanForMetric(unittest.TestCase):

    def test_found_in_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
            })
            findings = deprecate_rule.scan_for_metric("mysql_connections", d)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["filename"], "_defaults.yaml")

    def test_found_variants(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {
                    "mysql_connections": 70,
                    "custom_mysql_connections": 80,
                    "mysql_connections_critical": 90,
                }}},
            })
            findings = deprecate_rule.scan_for_metric("mysql_connections", d)
            self.assertEqual(len(findings), 1)
            total_occ = sum(len(f["occurrences"]) for f in findings)
            self.assertEqual(total_occ, 3)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"other_metric": 50}},
            })
            findings = deprecate_rule.scan_for_metric("mysql_connections", d)
            self.assertEqual(len(findings), 0)

    def test_dimensional_key(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {
                    'mysql_connections{db="orders"}': 100,
                }}},
            })
            findings = deprecate_rule.scan_for_metric("mysql_connections", d)
            self.assertEqual(len(findings), 1)


# ===================================================================
# 6. deprecate_rule — disable_in_defaults
# ===================================================================
class TestDisableInDefaults(unittest.TestCase):

    def test_preview_mode(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
            })
            ok, msg = deprecate_rule.disable_in_defaults(
                "mysql_connections", d, execute=False)
            self.assertTrue(ok)
            self.assertIn("disable", msg)
            # File should NOT be modified
            data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
            self.assertEqual(data["defaults"]["mysql_connections"], 70)

    def test_execute_mode(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
            })
            ok, msg = deprecate_rule.disable_in_defaults(
                "mysql_connections", d, execute=True)
            self.assertTrue(ok)
            data = deprecate_rule.load_yaml_file(os.path.join(d, "_defaults.yaml"))
            self.assertEqual(data["defaults"]["mysql_connections"], "disable")

    def test_already_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"mysql_connections": "disable"}},
            })
            ok, msg = deprecate_rule.disable_in_defaults(
                "mysql_connections", d, execute=True)
            self.assertTrue(ok)
            self.assertIn("已經是", msg)

    def test_missing_defaults_file(self):
        with tempfile.TemporaryDirectory() as d:
            ok, msg = deprecate_rule.disable_in_defaults("m", d, execute=False)
            self.assertFalse(ok)


# ===================================================================
# 7. deprecate_rule — remove_from_tenants
# ===================================================================
class TestRemoveFromTenants(unittest.TestCase):

    def test_preview(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": 70}}},
            })
            removed = deprecate_rule.remove_from_tenants(
                "mysql_connections", d, execute=False)
            self.assertEqual(len(removed), 1)
            # File should NOT be modified
            data = deprecate_rule.load_yaml_file(os.path.join(d, "db-a.yaml"))
            self.assertIn("mysql_connections", data["tenants"]["db-a"])

    def test_execute(self):
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "db-a.yaml": {"tenants": {"db-a": {
                    "mysql_connections": 70,
                    "mysql_cpu": 80,
                }}},
            })
            removed = deprecate_rule.remove_from_tenants(
                "mysql_connections", d, execute=True)
            self.assertEqual(len(removed), 1)
            data = deprecate_rule.load_yaml_file(os.path.join(d, "db-a.yaml"))
            self.assertNotIn("mysql_connections", data["tenants"]["db-a"])
            self.assertIn("mysql_cpu", data["tenants"]["db-a"])

    def test_skips_defaults(self):
        """_defaults.yaml should be skipped (handled by disable_in_defaults)."""
        with tempfile.TemporaryDirectory() as d:
            make_confdir(d, {
                "_defaults.yaml": {"defaults": {"mysql_connections": 70}},
            })
            removed = deprecate_rule.remove_from_tenants(
                "mysql_connections", d, execute=True)
            self.assertEqual(len(removed), 0)


# ===================================================================
# 8. validate_migration — extract_value_map
# ===================================================================
class TestExtractValueMap(unittest.TestCase):
    """Test Prometheus result → value dict conversion."""

    def test_normal(self):
        results = [
            {"metric": {"tenant": "db-a"}, "value": [1234567890, "42"]},
            {"metric": {"tenant": "db-b"}, "value": [1234567890, "99"]},
        ]
        vmap = validate_migration.extract_value_map(results)
        self.assertEqual(vmap["db-a"], 42.0)
        self.assertEqual(vmap["db-b"], 99.0)

    def test_no_tenant_label(self):
        results = [
            {"metric": {}, "value": [0, "10"]},
        ]
        vmap = validate_migration.extract_value_map(results)
        self.assertIn("__no_label__", vmap)

    def test_null_value(self):
        results = [
            {"metric": {"tenant": "db-a"}, "value": [0, None]},
        ]
        vmap = validate_migration.extract_value_map(results)
        self.assertIsNone(vmap["db-a"])

    def test_empty_results(self):
        vmap = validate_migration.extract_value_map([])
        self.assertEqual(vmap, {})


# ===================================================================
# 9. validate_migration — compare_vectors
# ===================================================================
class TestCompareVectors(unittest.TestCase):
    """Test vector comparison logic."""

    def test_match(self):
        old = {"db-a": 100.0}
        new = {"db-a": 100.0}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["status"], "match")

    def test_within_tolerance(self):
        old = {"db-a": 100.0}
        new = {"db-a": 100.05}
        diffs = validate_migration.compare_vectors(old, new, tolerance=0.001)
        self.assertEqual(diffs[0]["status"], "match")

    def test_mismatch(self):
        old = {"db-a": 100.0}
        new = {"db-a": 200.0}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(diffs[0]["status"], "mismatch")
        self.assertEqual(diffs[0]["delta"], 100.0)

    def test_old_missing(self):
        old = {}
        new = {"db-a": 50.0}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(diffs[0]["status"], "old_missing")

    def test_new_missing(self):
        old = {"db-a": 50.0}
        new = {}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(diffs[0]["status"], "new_missing")

    def test_both_empty(self):
        old = {"db-a": None}
        new = {"db-a": None}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(diffs[0]["status"], "both_empty")

    def test_zero_values_match(self):
        old = {"db-a": 0.0}
        new = {"db-a": 0.0}
        diffs = validate_migration.compare_vectors(old, new)
        self.assertEqual(diffs[0]["status"], "match")

    def test_multi_tenant(self):
        old = {"db-a": 10.0, "db-b": 20.0}
        new = {"db-a": 10.0, "db-b": 25.0}
        diffs = validate_migration.compare_vectors(old, new)
        statuses = {d["tenant"]: d["status"] for d in diffs}
        self.assertEqual(statuses["db-a"], "match")
        self.assertEqual(statuses["db-b"], "mismatch")


if __name__ == "__main__":
    unittest.main()
