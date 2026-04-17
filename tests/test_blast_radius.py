#!/usr/bin/env python3
"""Tests for blast_radius.py — Blast Radius diff engine for CI bot."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup (mirror conftest pattern)
# ---------------------------------------------------------------------------
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "tools", "ops"))

import blast_radius as br  # noqa: E402


# ---------------------------------------------------------------------------
# Test: flatten_dict()
# ---------------------------------------------------------------------------

class TestFlattenDict:
    """Tests for flatten_dict()."""

    def test_flat_simple(self):
        assert br.flatten_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_flat_nested(self):
        d = {"a": {"b": {"c": 3}}, "x": 1}
        assert br.flatten_dict(d) == {"a.b.c": 3, "x": 1}

    def test_flat_empty(self):
        assert br.flatten_dict({}) == {}

    def test_flat_mixed_types(self):
        d = {"a": [1, 2], "b": {"c": "hello"}}
        result = br.flatten_dict(d)
        assert result == {"a": [1, 2], "b.c": "hello"}


# ---------------------------------------------------------------------------
# Test: classify_field()
# ---------------------------------------------------------------------------

class TestClassifyField:
    """Tests for classify_field() — Tier A/B/C classification."""

    def test_tier_a_threshold(self):
        assert br.classify_field("alerts.threshold.MariaDBHighConnections") == "A"

    def test_tier_a_thresholds(self):
        assert br.classify_field("alerts.thresholds.DiskUsageHigh") == "A"

    def test_tier_a_receiver(self):
        assert br.classify_field("_routing.receiver.type") == "A"

    def test_tier_a_receivers(self):
        assert br.classify_field("receivers") == "A"

    def test_tier_a_routing_receivers(self):
        assert br.classify_field("_routing.receivers") == "A"

    def test_tier_b_alerts_generic(self):
        assert br.classify_field("alerts.enabled") == "B"

    def test_tier_b_routing_generic(self):
        assert br.classify_field("_routing.some_other_field") == "B"

    def test_tier_b_rules(self):
        assert br.classify_field("rules.cpu_high") == "B"

    def test_tier_b_severity(self):
        assert br.classify_field("severity.default") == "B"

    def test_tier_c_metadata(self):
        assert br.classify_field("_metadata.domain") == "C"

    def test_tier_c_comment(self):
        assert br.classify_field("_comment") == "C"

    def test_tier_c_unknown(self):
        assert br.classify_field("timezone") == "C"

    def test_tier_c_description(self):
        assert br.classify_field("_description.text") == "C"

    def test_tier_a_nested_threshold(self):
        """Nested path still matches Tier A."""
        assert br.classify_field("alerts.threshold") == "A"


# ---------------------------------------------------------------------------
# Test: diff_configs()
# ---------------------------------------------------------------------------

class TestDiffConfigs:
    """Tests for diff_configs() — effective config diffing."""

    def test_identical(self):
        cfg = {"a": 1, "b": {"c": 2}}
        result = br.diff_configs(cfg, cfg)
        assert result["added"] == {}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_added_key(self):
        base = {"a": 1}
        pr = {"a": 1, "b": 2}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"b": 2}
        assert result["removed"] == {}
        assert result["changed"] == {}

    def test_removed_key(self):
        base = {"a": 1, "b": 2}
        pr = {"a": 1}
        result = br.diff_configs(base, pr)
        assert result["removed"] == {"b": 2}
        assert result["added"] == {}

    def test_changed_value(self):
        base = {"a": {"b": 90}}
        pr = {"a": {"b": 95}}
        result = br.diff_configs(base, pr)
        assert result["changed"] == {"a.b": {"base": 90, "pr": 95}}

    def test_mixed_changes(self):
        base = {"x": 1, "y": {"a": 10}, "z": 3}
        pr = {"x": 1, "y": {"a": 20, "b": 30}}
        result = br.diff_configs(base, pr)
        assert result["added"] == {"y.b": 30}
        assert result["removed"] == {"z": 3}
        assert result["changed"] == {"y.a": {"base": 10, "pr": 20}}


# ---------------------------------------------------------------------------
# Test: classify_diff()
# ---------------------------------------------------------------------------

class TestClassifyDiff:
    """Tests for classify_diff() — tier classification of diffs."""

    def test_threshold_change_is_tier_a(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"alerts.threshold.MariaDBHighConnections": {"base": 90, "pr": 95}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["field"] == "alerts.threshold.MariaDBHighConnections"
        assert tiers["A"][0]["action"] == "changed"
        assert len(tiers["B"]) == 0
        assert len(tiers["C"]) == 0

    def test_receiver_added_is_tier_a(self):
        diff = {
            "added": {"_routing.receiver.type": "slack"},
            "removed": {},
            "changed": {},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1
        assert tiers["A"][0]["action"] == "added"

    def test_metadata_change_is_tier_c(self):
        diff = {
            "added": {},
            "removed": {},
            "changed": {"_metadata.domain": {"base": "old", "pr": "new"}},
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["C"]) == 1
        assert len(tiers["A"]) == 0
        assert len(tiers["B"]) == 0

    def test_mixed_tiers(self):
        diff = {
            "added": {"timezone": "UTC"},
            "removed": {"_metadata.old_field": "x"},
            "changed": {
                "alerts.threshold.DiskUsage": {"base": 80, "pr": 85},
                "severity.default": {"base": "warning", "pr": "critical"},
            },
        }
        tiers = br.classify_diff(diff)
        assert len(tiers["A"]) == 1  # threshold
        assert len(tiers["B"]) == 1  # severity
        assert len(tiers["C"]) == 2  # timezone + metadata


# ---------------------------------------------------------------------------
# Test: compute_blast_radius()
# ---------------------------------------------------------------------------

class TestComputeBlastRadius:
    """Tests for compute_blast_radius() — full blast radius computation."""

    @pytest.fixture()
    def base_data(self):
        return {
            "tenant-a": {
                "merged_hash": "aaaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "_routing": {"receiver": {"type": "slack"}},
                    "timezone": "UTC",
                },
            },
            "tenant-b": {
                "merged_hash": "bbbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 85}},
                    "timezone": "UTC",
                },
            },
            "tenant-c": {
                "merged_hash": "cccc",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                },
            },
        }

    def test_no_changes(self, base_data):
        report = br.compute_blast_radius(base_data, base_data)
        assert report["summary"]["affected_tenants"] == 0
        assert report["summary"]["total_tenants_scanned"] == 3

    def test_threshold_change_one_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 90

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["tenants"][0]["tenant_id"] == "tenant-a"
        assert report["tenants"][0]["highest_tier"] == "A"

    def test_format_only_change(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        pr_data["tenant-b"]["effective_config"]["timezone"] = "America/New_York"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1

    def test_new_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        pr_data["tenant-new"] = {
            "merged_hash": "newnew",
            "effective_config": {"alerts": {"threshold": {"DiskUsage": 80}}},
        }

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["new_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_removed_tenant(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)
        del pr_data["tenant-c"]

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["removed_tenants"] == 1
        assert report["summary"]["affected_tenants"] == 1

    def test_hash_match_skips_diff(self, base_data):
        """When merged_hash matches, tenant is skipped entirely."""
        import copy
        pr_data = copy.deepcopy(base_data)
        # Same hash, different config (shouldn't happen, but tests hash-first logic)
        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 0

    def test_multiple_tiers(self, base_data):
        import copy
        pr_data = copy.deepcopy(base_data)

        # tenant-a: Tier A (threshold change)
        pr_data["tenant-a"]["merged_hash"] = "aaaa-new"
        pr_data["tenant-a"]["effective_config"]["alerts"]["threshold"]["DiskUsage"] = 95

        # tenant-b: Tier B (severity change)
        pr_data["tenant-b"]["merged_hash"] = "bbbb-new"
        pr_data["tenant-b"]["effective_config"]["severity"] = {"default": "critical"}

        # tenant-c: Tier C (timezone change)
        pr_data["tenant-c"]["merged_hash"] = "cccc-new"
        pr_data["tenant-c"]["effective_config"]["timezone"] = "Asia/Tokyo"

        report = br.compute_blast_radius(base_data, pr_data)
        assert report["summary"]["affected_tenants"] == 3
        assert report["summary"]["tier_a_tenants"] == 1
        assert report["summary"]["tier_b_tenants"] == 1
        assert report["summary"]["tier_c_only_tenants"] == 1


# ---------------------------------------------------------------------------
# Test: generate_pr_comment()
# ---------------------------------------------------------------------------

class TestGeneratePRComment:
    """Tests for PR comment markdown generation."""

    def test_no_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 100,
                "affected_tenants": 0,
                "tier_a_tenants": 0,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [],
        }
        md = br.generate_pr_comment(report)
        assert "No effective tenant config changes" in md

    def test_with_changes(self):
        report = {
            "summary": {
                "total_tenants_scanned": 347,
                "affected_tenants": 347,
                "tier_a_tenants": 12,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 335,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [
                {
                    "tenant_id": f"tenant-{i}",
                    "status": "changed",
                    "highest_tier": "A",
                    "tiers": {
                        "A": [{"field": "alerts.threshold.DiskUsage", "action": "changed",
                               "detail": {"base": 80, "pr": 85}}],
                        "B": [],
                        "C": [],
                    },
                }
                for i in range(12)
            ] + [
                {
                    "tenant_id": f"format-{i}",
                    "status": "changed",
                    "highest_tier": "C",
                    "tiers": {"A": [], "B": [], "C": [
                        {"field": "timezone", "action": "changed",
                         "detail": {"base": "UTC", "pr": "UTC+0"}}
                    ]},
                }
                for i in range(335)
            ],
        }
        md = br.generate_pr_comment(report, changed_files="finance/_defaults.yaml")
        assert "finance/_defaults.yaml" in md
        assert "347" in md
        assert "12" in md
        assert "Substantive changes" in md
        assert "Format-only changes" in md
        assert "<details>" in md

    def test_changed_files_header(self):
        report = {
            "summary": {
                "total_tenants_scanned": 10,
                "affected_tenants": 1,
                "tier_a_tenants": 1,
                "tier_b_tenants": 0,
                "tier_c_only_tenants": 0,
                "new_tenants": 0,
                "removed_tenants": 0,
            },
            "tenants": [{
                "tenant_id": "tenant-a",
                "status": "changed",
                "highest_tier": "A",
                "tiers": {
                    "A": [{"field": "receivers", "action": "changed",
                           "detail": {"base": "slack", "pr": "pagerduty"}}],
                    "B": [], "C": [],
                },
            }],
        }
        md = br.generate_pr_comment(report, changed_files="domain-a/_defaults.yaml")
        assert "domain-a/_defaults.yaml" in md


# ---------------------------------------------------------------------------
# Test: CLI integration
# ---------------------------------------------------------------------------

class TestCLI:
    """CLI integration tests."""

    @pytest.fixture()
    def json_pair(self, tmp_path):
        base = {
            "tenant-a": {
                "merged_hash": "aaa",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 80}},
                    "timezone": "UTC",
                },
            },
        }
        pr = {
            "tenant-a": {
                "merged_hash": "bbb",
                "effective_config": {
                    "alerts": {"threshold": {"DiskUsage": 90}},
                    "timezone": "UTC",
                },
            },
        }
        base_path = tmp_path / "base.json"
        pr_path = tmp_path / "pr.json"
        base_path.write_text(json.dumps(base), encoding="utf-8")
        pr_path.write_text(json.dumps(pr), encoding="utf-8")
        return str(base_path), str(pr_path)

    def test_cli_json_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report["summary"]["tier_a_tenants"] == 1

    def test_cli_markdown_output(self, json_pair):
        base_path, pr_path = json_pair
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--format", "markdown"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "Blast Radius" in result.stdout

    def test_cli_output_file(self, json_pair, tmp_path):
        base_path, pr_path = json_pair
        out_path = str(tmp_path / "report.json")
        script = os.path.join(REPO_ROOT, "scripts", "tools", "ops", "blast_radius.py")
        result = subprocess.run(
            [sys.executable, script, "--base", base_path, "--pr", pr_path, "--output", out_path],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert os.path.exists(out_path)
        report = json.loads(Path(out_path).read_text(encoding="utf-8"))
        assert "summary" in report
