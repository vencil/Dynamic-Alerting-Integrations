#!/usr/bin/env python3
"""test_config_diff.py — Directory-level Config Diff 測試套件 (Wave 12 pytest 遷移)。"""

import json
import os
import tempfile

import pytest
import yaml


import config_diff as cd  # noqa: E402


# ── 1. Flatten Tenant Config ────────────────────────────────────────

class TestFlattenTenantConfig:

    def test_basic_flatten(self):
        raw = {"mysql_connections": 50, "redis_memory": 1024}
        result = cd.flatten_tenant_config(raw)
        assert result == {"mysql_connections": 50, "redis_memory": 1024}

    def test_skips_reserved_keys(self):
        raw = {"_routing": {"receiver": "slack"}, "_severity_dedup": "enable",
               "mysql_connections": 50}
        result = cd.flatten_tenant_config(raw)
        assert result == {"mysql_connections": 50}

    def test_empty_input(self):
        assert cd.flatten_tenant_config(None) == {}
        assert cd.flatten_tenant_config({}) == {}


# ── 2. Classify Change ──────────────────────────────────────────────

class TestClassifyChange:

    def test_added(self):
        assert cd.classify_change(None, 50) == "added"

    def test_removed(self):
        assert cd.classify_change(50, None) == "removed"

    def test_tighter(self):
        assert cd.classify_change(80, 50) == "tighter"

    def test_looser(self):
        assert cd.classify_change(50, 80) == "looser"

    def test_toggled_disable(self):
        assert cd.classify_change(50, "disable") == "toggled"

    def test_toggled_enable(self):
        assert cd.classify_change("disable", 50) == "toggled"

    def test_modified_dict(self):
        old = {"default": 50, "schedule": []}
        new = {"default": 70, "schedule": []}
        # Different dicts → modified (can't do simple numeric compare)
        result = cd.classify_change(old, new)
        assert result == "modified"

    def test_same_value_unchanged(self):
        assert cd.classify_change(50, 50) == "unchanged"


# ── 3. Compute Diff ──────────────────────────────────────────────────

class TestComputeDiff:

    def test_basic_diff(self):
        old = {"db-a": {"mysql_connections": 80, "redis_memory": 1024}}
        new = {"db-a": {"mysql_connections": 50, "redis_memory": 1024}}
        diffs = cd.compute_diff(old, new)
        assert "db-a" in diffs
        assert len(diffs["db-a"]) == 1
        assert diffs["db-a"][0]["change"] == "tighter"

    def test_new_tenant(self):
        old = {}
        new = {"db-c": {"pg_cache": 0.9}}
        diffs = cd.compute_diff(old, new)
        assert "db-c" in diffs
        assert diffs["db-c"][0]["change"] == "added"

    def test_removed_tenant(self):
        old = {"db-x": {"mysql_connections": 50}}
        new = {}
        diffs = cd.compute_diff(old, new)
        assert "db-x" in diffs
        assert diffs["db-x"][0]["change"] == "removed"

    def test_no_changes(self):
        old = {"db-a": {"mysql_connections": 50}}
        new = {"db-a": {"mysql_connections": 50}}
        diffs = cd.compute_diff(old, new)
        assert diffs == {}

    def test_multiple_tenants(self):
        old = {"db-a": {"mysql_connections": 50}, "db-b": {"redis_memory": 100}}
        new = {"db-a": {"mysql_connections": 70}, "db-b": {"redis_memory": 100}}
        diffs = cd.compute_diff(old, new)
        assert "db-a" in diffs
        assert "db-b" not in diffs


# ── 4. Load Configs From Dir ────────────────────────────────────────

class TestLoadConfigsFromDir:

    def test_basic_loading_flat(self):
        """Flat format (legacy): {metric: value} without tenants: wrapper."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 50, "_routing": {}}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "mysql_connections" in result["db-a"]
            assert "_routing" not in result["db-a"]

    def test_basic_loading_wrapped(self):
        """Wrapped format (actual conf.d/): {tenants: {name: {metric: value}}}."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {
                    "mysql_connections": "70",
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "mysql_connections" in result["db-a"]
            assert "_routing" not in result["db-a"]

    def test_wrapped_multi_tenant_in_file(self):
        """Multiple tenants in a single wrapped YAML file."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "teams.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {
                    "db-a": {"mysql_connections": "70"},
                    "db-b": {"redis_memory": "1024"},
                }}, f)
            result = cd.load_configs_from_dir(d)
            assert "db-a" in result
            assert "db-b" in result
            assert result["db-a"] == {"mysql_connections": "70"}

    def test_skips_defaults_and_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"mysql_connections": 99}, f)
            with open(os.path.join(d, ".hidden.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"x": 1}, f)
            result = cd.load_configs_from_dir(d)
            assert result == {}

    def test_missing_dir(self):
        result = cd.load_configs_from_dir("/nonexistent")
        assert result == {}


# ── 5. Estimate Affected Alerts ─────────────────────────────────────

class TestEstimateAffectedAlerts:

    def test_basic_conversion(self):
        assert cd.estimate_affected_alerts("mysql_connections") == "*MysqlConnections*"

    def test_single_word(self):
        assert cd.estimate_affected_alerts("cpu") == "*Cpu*"


# ── 6. Render Markdown ──────────────────────────────────────────────

class TestRenderMarkdown:

    def test_no_changes(self):
        md = cd.render_markdown({}, "old", "new")
        assert "No changes detected" in md

    def test_with_changes(self):
        diffs = {
            "db-a": [{"key": "mysql_connections", "old": 80, "new": 50, "change": "tighter"}]
        }
        md = cd.render_markdown(diffs, "old", "new")
        assert "db-a" in md
        assert "mysql_connections" in md
        assert "tighter" in md
        assert "Summary:" in md
        assert "1 tenant(s) changed" in md

    def test_format_value_disabled(self):
        assert cd._format_value("disable") == "disabled"
        assert cd._format_value(None) == "—"
        assert cd._format_value(50) == "50"
        assert cd._format_value({"schedule": []}) == "(scheduled)"


# ── 7. CLI ───────────────────────────────────────────────────────────

class TestCLI:

    def test_required_args(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b"])
        assert args.old_dir == "/a"
        assert args.new_dir == "/b"

    def test_json_flag(self):
        parser = cd.build_parser()
        args = parser.parse_args(["--old-dir", "/a", "--new-dir", "/b", "--json-output"])
        assert args.json_output

    def test_missing_required(self):
        parser = cd.build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])


# ── 8. End-to-End ────────────────────────────────────────────────────

class TestEndToEnd:

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

            assert len(diffs) == 1
            assert diffs["db-a"][0]["key"] == "mysql_connections"
            assert diffs["db-a"][0]["change"] == "tighter"

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

            assert len(diffs) == 1
            assert "db-a" in diffs
            assert diffs["db-a"][0]["key"] == "mysql_connections"
            assert diffs["db-a"][0]["change"] == "tighter"



# ── Exit Code Tests (v1.11.0 CI integration) ─────────────────────

class TestExitCode:
    """config_diff.py exit codes for CI pipeline integration."""

    def test_exit_0_no_changes(self):
        """Identical directories → exit 0."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "80"}}}, f)
            old = cd.load_configs_from_dir(d)
            new = cd.load_configs_from_dir(d)
            diffs = cd.compute_diff(old, new)
            # Exit code logic: 1 if diffs else 0
            assert (1 if diffs else 0) == 0

    def test_exit_1_changes_detected(self):
        """Different directories → exit 1 (signal to CI)."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            with open(os.path.join(old_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "80"}}}, f)
            with open(os.path.join(new_dir, "db-a.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"tenants": {"db-a": {"mysql_connections": "50"}}}, f)
            old = cd.load_configs_from_dir(old_dir)
            new = cd.load_configs_from_dir(new_dir)
            diffs = cd.compute_diff(old, new)
            assert (1 if diffs else 0) == 1


# ── 9. Profile Key Diff (v1.12.0 fine-grained) ───────────────────

class TestProfileKeyDiff:
    """Fine-grained profile content diff."""

    def test_added_profile(self):
        """New profile should show all keys as added."""
        diffs = cd.compute_profile_key_diff(None, {"mysql_connections": 80, "redis_memory": 1024})
        assert len(diffs) == 2
        assert all(d["change"] == "added" for d in diffs)

    def test_removed_profile(self):
        """Removed profile should show all keys as removed."""
        diffs = cd.compute_profile_key_diff({"mysql_connections": 80}, None)
        assert len(diffs) == 1
        assert diffs[0]["change"] == "removed"

    def test_modified_key(self):
        """Changed key should show tighter/looser."""
        diffs = cd.compute_profile_key_diff(
            {"mysql_connections": 80},
            {"mysql_connections": 50}
        )
        assert len(diffs) == 1
        assert diffs[0]["change"] == "tighter"

    def test_no_changes(self):
        """Identical profiles should produce no diffs."""
        diffs = cd.compute_profile_key_diff(
            {"mysql_connections": 80}, {"mysql_connections": 80})
        assert diffs == []


class TestProfileDiffEndToEnd:
    """End-to-end profile diff with directories."""

    def test_profile_modified_with_key_diffs(self):
        """Modified profile should include key_diffs in result."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # Old profile
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"standard": {
                    "mysql_connections": 80, "redis_memory": 1024
                }}}, f)
            # New profile — tighter mysql
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"standard": {
                    "mysql_connections": 50, "redis_memory": 1024
                }}}, f)
            # Tenant referencing profile
            for d in (old_dir, new_dir):
                with open(os.path.join(d, "db-a.yaml"), "w", encoding="utf-8") as f:
                    yaml.dump({"tenants": {"db-a": {"_profile": "standard"}}}, f)

            results = cd.compute_profile_diff(old_dir, new_dir)
            assert len(results) == 1
            assert results[0]["profile"] == "standard"
            assert results[0]["change"] == "modified"
            assert len(results[0]["key_diffs"]) == 1
            assert results[0]["key_diffs"][0]["key"] == "mysql_connections"
            assert results[0]["key_diffs"][0]["change"] == "tighter"

    def test_profile_added_with_key_diffs(self):
        """Added profile should list all keys as added."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            # No profile in old
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {}}, f)
            # New profile
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"new-profile": {
                    "mysql_connections": 80
                }}}, f)

            results = cd.compute_profile_diff(old_dir, new_dir)
            assert len(results) == 1
            assert results[0]["change"] == "added"
            assert len(results[0]["key_diffs"]) == 1
            assert results[0]["key_diffs"][0]["change"] == "added"

    def test_json_output_includes_key_diffs(self):
        """JSON output should include key_diffs in profile_diffs."""
        with tempfile.TemporaryDirectory() as old_dir, \
             tempfile.TemporaryDirectory() as new_dir:
            with open(os.path.join(old_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"s": {"x": 80}}}, f)
            with open(os.path.join(new_dir, "_profiles.yaml"), "w", encoding="utf-8") as f:
                yaml.dump({"profiles": {"s": {"x": 50}}}, f)

            profile_diffs = cd.compute_profile_diff(old_dir, new_dir)
            output = {"metric_diffs": {}, "profile_diffs": profile_diffs}
            j = json.dumps(output, default=str)
            parsed = json.loads(j)
            assert "key_diffs" in parsed["profile_diffs"][0]
