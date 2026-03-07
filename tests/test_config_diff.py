#!/usr/bin/env python3
"""test_config_diff.py — Directory-level Config Diff 測試套件。"""

import json
import os
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import config_diff as cd  # noqa: E402


# ── 1. Flatten Tenant Config ────────────────────────────────────────

class TestFlattenTenantConfig(unittest.TestCase):

    def test_basic_flatten(self):
        raw = {"mysql_connections": 50, "redis_memory": 1024}
        result = cd.flatten_tenant_config(raw)
        self.assertEqual(result, {"mysql_connections": 50, "redis_memory": 1024})

    def test_skips_reserved_keys(self):
        raw = {"_routing": {"receiver": "slack"}, "_severity_dedup": "enable",
               "mysql_connections": 50}
        result = cd.flatten_tenant_config(raw)
        self.assertEqual(result, {"mysql_connections": 50})

    def test_empty_input(self):
        self.assertEqual(cd.flatten_tenant_config(None), {})
        self.assertEqual(cd.flatten_tenant_config({}), {})


# ── 2. Classify Change ──────────────────────────────────────────────

class TestClassifyChange(unittest.TestCase):

    def test_added(self):
        self.assertEqual(cd.classify_change(None, 50), "added")

    def test_removed(self):
        self.assertEqual(cd.classify_change(50, None), "removed")

    def test_tighter(self):
        self.assertEqual(cd.classify_change(80, 50), "tighter")

    def test_looser(self):
        self.assertEqual(cd.classify_change(50, 80), "looser")

    def test_toggled_disable(self):
        self.assertEqual(cd.classify_change(50, "disable"), "toggled")

    def test_toggled_enable(self):
        self.assertEqual(cd.classify_change("disable", 50), "toggled")

    def test_modified_dict(self):
        old = {"default": 50, "schedule": []}
        new = {"default": 70, "schedule": []}
        # Different dicts → modified (can't do simple numeric compare)
        result = cd.classify_change(old, new)
        self.assertEqual(result, "modified")

    def test_same_value_unchanged(self):
        self.assertEqual(cd.classify_change(50, 50), "unchanged")


# ── 3. Compute Diff ──────────────────────────────────────────────────

class TestComputeDiff(unittest.TestCase):

    def test_basic_diff(self):
        old = {"db-a": {"mysql_connections": 80, "redis_memory": 1024}}
        new = {"db-a": {"mysql_connections": 50, "redis_memory": 1024}}
        diffs = cd.compute_diff(old, new)
        self.assertIn("db-a", diffs)
        self.assertEqual(len(diffs["db-a"]), 1)
        self.assertEqual(diffs["db-a"][0]["change"], "tighter")

    def test_new_tenant(self):
        old = {}
        new = {"db-c": {"pg_cache": 0.9}}
        diffs = cd.compute_diff(old, new)
        self.assertIn("db-c", diffs)
        self.assertEqual(diffs["db-c"][0]["change"], "added")

    def test_removed_tenant(self):
        old = {"db-x": {"mysql_connections": 50}}
        new = {}
        diffs = cd.compute_diff(old, new)
        self.assertIn("db-x", diffs)
        self.assertEqual(diffs["db-x"][0]["change"], "removed")

    def test_no_changes(self):
        old = {"db-a": {"mysql_connections": 50}}
        new = {"db-a": {"mysql_connections": 50}}
        diffs = cd.compute_diff(old, new)
        self.assertEqual(diffs, {})

    def test_multiple_tenants(self):
        old = {"db-a": {"mysql_connections": 50}, "db-b": {"redis_memory": 100}}
        new = {"db-a": {"mysql_connections": 70}, "db-b": {"redis_memory": 100}}
        diffs = cd.compute_diff(old, new)
        self.assertIn("db-a", diffs)
        self.assertNotIn("db-b", diffs)


# ── 4. Load Configs From Dir ────────────────────────────────────────

class TestLoadConfigsFromDir(unittest.TestCase):

    def test_basic_loading_flat(self):
        """Flat format (legacy): {metric: value} without tenants: wrapper."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "_routing": {}}, f)
            result = cd.load_configs_from_dir(d)
            self.assertIn("db-a", result)
            self.assertIn("mysql_connections", result["db-a"])
            self.assertNotIn("_routing", result["db-a"])

    def test_basic_loading_wrapped(self):
        """Wrapped format (actual conf.d/): {tenants: {name: {metric: value}}}."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "70",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            result = cd.load_configs_from_dir(d)
            self.assertIn("db-a", result)
            self.assertIn("mysql_connections", result["db-a"])
            self.assertNotIn("_routing", result["db-a"])

    def test_wrapped_multi_tenant_in_file(self):
        """Multiple tenants in a single wrapped YAML file."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "teams.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {
                    "db-a": {"mysql_connections": "70"},
                    "db-b": {"redis_memory": "1024"},
                }}, f)
            result = cd.load_configs_from_dir(d)
            self.assertIn("db-a", result)
            self.assertIn("db-b", result)
            self.assertEqual(result["db-a"], {"mysql_connections": "70"})

    def test_skips_defaults_and_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 99}, f)
            with open(os.path.join(d, ".hidden.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"x": 1}, f)
            result = cd.load_configs_from_dir(d)
            self.assertEqual(result, {})

    def test_missing_dir(self):
        result = cd.load_configs_from_dir("/nonexistent")
        self.assertEqual(result, {})


# ── 5. Estimate Affected Alerts ─────────────────────────────────────

class TestEstimateAffectedAlerts(unittest.TestCase):

    def test_basic_conversion(self):
        self.assertEqual(cd.estimate_affected_alerts("mysql_connections"),
                         "*MysqlConnections*")

    def test_single_word(self):
        self.assertEqual(cd.estimate_affected_alerts("cpu"), "*Cpu*")


# ── 6. Render Markdown ──────────────────────────────────────────────

class TestRenderMarkdown(unittest.TestCase):

    def test_no_changes(self):
        md = cd.render_markdown({}, "old", "new")
        self.assertIn("No changes detected", md)

    def test_with_changes(self):
        diffs = {
            "db-a": [{"key": "mysql_connections", "old": 80, "new": 50, "change": "tighter"}]
        }
        md = cd.render_markdown(diffs, "old", "new")
        self.assertIn("db-a", md)
        self.assertIn("mysql_connections", md)
        self.assertIn("tighter", md)
        self.assertIn("Summary:", md)
        self.assertIn("1 tenant(s) changed", md)

    def test_format_value_disabled(self):
        self.assertEqual(cd._format_value("disable"), "disabled")
        self.assertEqual(cd._format_value(None), "—")
        self.assertEqual(cd._format_value(50), "50")
        self.assertEqual(cd._format_value({"schedule": []}), "(scheduled)")


# ── 7. CLI ───────────────────────────────────────────────────────────

class TestCLI(unittest.TestCase):

    def test_required_args(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b"])
        self.assertEqual(args.old_dir, "/a")
        self.assertEqual(args.new_dir, "/b")

    def test_json_flag(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b", "--json-output"])
        self.assertTrue(args.json_output)

    def test_missing_required(self):
        parser = cd.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])


# ── 8. End-to-End ────────────────────────────────────────────────────

class TestEndToEnd(unittest.TestCase):

    def test_directory_comparison_flat(self):
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old config (flat)
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 80, "redis_memory": 1024}, f)
            # New config — tighter mysql, same redis
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "redis_memory": 1024}, f)

            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)

            self.assertEqual(len(diffs), 1)
            self.assertEqual(diffs["db-a"][0]["key"], "mysql_connections")
            self.assertEqual(diffs["db-a"][0]["change"], "tighter")

    def test_directory_comparison_wrapped(self):
        """End-to-end test with actual conf.d/ format (tenants: wrapper)."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old config (wrapped format)
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "80",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            # New config — tighter mysql
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "50",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)

            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)

            self.assertEqual(len(diffs), 1)
            self.assertIn("db-a", diffs)
            self.assertEqual(diffs["db-a"][0]["key"], "mysql_connections")
            self.assertEqual(diffs["db-a"][0]["change"], "tighter")


if __name__ == "__main__":
    unittest.main()
