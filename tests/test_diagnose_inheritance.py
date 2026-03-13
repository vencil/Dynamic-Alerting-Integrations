#!/usr/bin/env python3
"""test_diagnose_inheritance.py — Four-layer inheritance chain tests (v1.12.0).

Tests for diagnose.py resolve_inheritance_chain() and _format_chain_summary().
"""

import os
import sys
import tempfile
import unittest

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools"))

import diagnose  # noqa: E402


class TestResolveInheritanceChain(unittest.TestCase):
    """resolve_inheritance_chain() tests."""

    def _make_config_dir(self, tmpdir, defaults=None, profiles=None, tenants=None):
        """Helper to create a conf.d/ directory with given config files."""
        if defaults:
            with open(os.path.join(tmpdir, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"defaults": defaults}, f)
        if profiles:
            with open(os.path.join(tmpdir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": profiles}, f)
        if tenants:
            for t_name, t_data in tenants.items():
                with open(os.path.join(tmpdir, f"{t_name}.yaml"), "w", encoding="utf-8") as f:
                    yaml.dump({"tenants": {t_name: t_data}}, f)

    def test_defaults_only(self):
        """Tenant with only defaults should show single layer."""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80, "container_cpu": 70},
                tenants={"db-a": {}})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            self.assertIsNotNone(result)
            self.assertEqual(len(result["chain"]), 1)
            self.assertEqual(result["chain"][0]["layer"], "defaults")
            self.assertEqual(result["resolved"]["mysql_connections"], 80)
            self.assertIsNone(result["profile_name"])

    def test_defaults_plus_tenant_override(self):
        """Tenant override should win over defaults."""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80},
                tenants={"db-a": {"mysql_connections": "50"}})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            self.assertEqual(len(result["chain"]), 2)  # defaults + tenant
            self.assertEqual(result["resolved"]["mysql_connections"], "50")

    def test_full_chain_with_profile(self):
        """Full four-layer chain: defaults + profile + tenant."""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"mysql_connections": 80, "container_cpu": 70},
                profiles={"standard": {
                    "mysql_connections": 60,
                    "redis_memory": 1024,
                }},
                tenants={"db-a": {
                    "_profile": "standard",
                    "mysql_connections": "50",
                }})
            result = diagnose.resolve_inheritance_chain("db-a", d)
            self.assertEqual(result["profile_name"], "standard")

            # Should have 3 layers: defaults, profile (fill-in), tenant
            self.assertEqual(len(result["chain"]), 3)
            layers = [c["layer"] for c in result["chain"]]
            self.assertEqual(layers, ["defaults", "profile", "tenant"])

            # Tenant override wins for mysql_connections
            self.assertEqual(result["resolved"]["mysql_connections"], "50")
            # Profile fills in redis_memory (tenant didn't set it)
            self.assertEqual(result["resolved"]["redis_memory"], 1024)
            # Defaults provide container_cpu
            self.assertEqual(result["resolved"]["container_cpu"], 70)

    def test_profile_fillin_only_missing_keys(self):
        """Profile should only fill keys not set by tenant."""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={},
                profiles={"p": {"a": 10, "b": 20}},
                tenants={"t": {"_profile": "p", "a": 99}})
            result = diagnose.resolve_inheritance_chain("t", d)
            # Profile's effective keys = only "b" (tenant has "a")
            profile_layer = [c for c in result["chain"] if c["layer"] == "profile"]
            self.assertEqual(len(profile_layer), 1)
            self.assertIn("b", profile_layer[0]["keys"])
            self.assertNotIn("a", profile_layer[0]["keys"])

    def test_nonexistent_tenant(self):
        """Non-existent tenant should return empty chain."""
        with tempfile.TemporaryDirectory() as d:
            self._make_config_dir(d,
                defaults={"x": 1},
                tenants={"db-a": {}})
            result = diagnose.resolve_inheritance_chain("nonexistent", d)
            # Should still resolve defaults
            self.assertIsNotNone(result)
            self.assertEqual(len(result["chain"]), 1)

    def test_no_config_dir(self):
        """None config_dir should return None."""
        result = diagnose.resolve_inheritance_chain("db-a", None)
        self.assertIsNone(result)


class TestFormatChainSummary(unittest.TestCase):
    """_format_chain_summary() tests."""

    def test_summary_structure(self):
        """Summary should include layers, resolved_count, profile."""
        inheritance = {
            "chain": [
                {"layer": "defaults", "source": "_defaults.yaml", "keys": {"a": 1, "b": 2}},
                {"layer": "tenant", "source": "db-a.yaml", "keys": {"a": 10}},
            ],
            "resolved": {"a": 10, "b": 2},
            "profile_name": None,
        }
        summary = diagnose._format_chain_summary(inheritance)
        self.assertEqual(len(summary["layers"]), 2)
        self.assertEqual(summary["layers"][0]["key_count"], 2)
        self.assertEqual(summary["layers"][1]["key_count"], 1)
        self.assertEqual(summary["resolved_count"], 2)
        self.assertIsNone(summary["profile"])


if __name__ == "__main__":
    unittest.main()
