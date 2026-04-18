"""pytest style tests for validate_config.py — one-stop configuration validation。"""

import io
import json
import os
import sys
import tempfile

import pytest
import yaml

# Ensure scripts/tools is on the path

import validate_config as vc  # noqa: E402


class TestYAMLSyntax:
    """Check 1: YAML syntax validation."""

    def test_valid_yaml_passes(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "test.yaml"), "w") as f:
                yaml.dump({"tenants": {"t1": {"mysql_connections": "80"}}}, f)
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.PASS

    def test_invalid_yaml_fails(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.yaml"), "w") as f:
                f.write("key: [unclosed")
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.FAIL
            assert any("bad.yaml" in detail for detail in result["details"])

    def test_empty_dir_passes(self):
        with tempfile.TemporaryDirectory() as d:
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.PASS
            assert any("0 files" in detail for detail in result["details"])

    def test_non_yaml_files_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "readme.txt"), "w") as f:
                f.write("not yaml")
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.PASS


class TestSchemaCheck:
    """Check 2: Schema validation."""

    def test_valid_config_passes(self):
        """Config with known keys should pass."""
        conf_dir = os.path.join(os.path.dirname(__file__), "..",
                                "components", "threshold-exporter",
                                "config", "conf.d")
        if not os.path.isdir(conf_dir):
            pytest.skip("Config dir not found")
        result = vc.check_schema(conf_dir)
        assert result["status"] in (vc.PASS, vc.WARN)

    def test_unknown_key_warns(self):
        """Config with unknown reserved key should warn."""
        with tempfile.TemporaryDirectory() as d:
            # Defaults file
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            # Tenant with unknown key
            with open(os.path.join(d, "tenant-x.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-x": {
                    "_unknown_reserved": "foo"
                }}}, f)
            result = vc.check_schema(d)
            assert result["status"] == vc.WARN
            assert any("unknown" in detail.lower()
                       for detail in result["details"])


class TestRouteCheck:
    """Check 3: Route validation."""

    def test_valid_routing_passes(self):
        """Valid routing config should pass."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_routing": {
                        "receiver": {"type": "webhook",
                                     "url": "https://hooks.example.com/alert"},
                    }
                }}}, f)
            result = vc.check_routes(d)
            assert result["status"] in (vc.PASS, vc.WARN)

    def test_no_routing_passes(self):
        """Config with no routing should pass (no routes generated)."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "mysql_connections": "70"
                }}}, f)
            result = vc.check_routes(d)
            assert result["status"] == vc.PASS


class TestMakeResult:
    """Helper function tests."""

    def test_make_result_structure(self):
        r = vc._make_result("test_check", vc.PASS, ["detail1"])
        assert r["check"] == "test_check"
        assert r["status"] == vc.PASS
        assert r["details"] == ["detail1"]

    def test_make_result_default_details(self):
        r = vc._make_result("test_check", vc.FAIL)
        assert r["details"] == []


class TestReportOutput:
    """Report formatting tests."""

    def test_json_output(self):
        """JSON output should be parseable."""
        results = [vc._make_result("yaml_syntax", vc.PASS, ["ok"])]
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            vc.print_report(results, as_json=True)
        finally:
            sys.stdout = old_stdout
        parsed = json.loads(captured.getvalue())
        assert len(parsed) == 1
        assert parsed[0]["check"] == "yaml_syntax"

    def test_text_report_contains_summary(self):
        """Text report should contain summary line."""
        results = [
            vc._make_result("check1", vc.PASS, ["ok"]),
            vc._make_result("check2", vc.WARN, ["minor issue"]),
        ]
        old_stdout = sys.stdout
        sys.stdout = captured = io.StringIO()
        try:
            vc.print_report(results, as_json=False)
        finally:
            sys.stdout = old_stdout
        output = captured.getvalue()
        assert "2 checks" in output
        assert "1 pass" in output
        assert "1 warn" in output
        assert "WARN" in output


class TestPolicyCheck:
    """Check 4: Webhook domain allowlist."""

    def test_no_policy_file_skips(self):
        """Missing policy file should skip with PASS."""
        with tempfile.TemporaryDirectory() as d:
            result = vc.check_policy(d, "/nonexistent/policy.yaml")
            assert result["status"] == vc.PASS
            assert any("skipped" in detail.lower()
                       for detail in result["details"])

    def test_none_policy_skips(self):
        """None policy should skip with PASS."""
        with tempfile.TemporaryDirectory() as d:
            result = vc.check_policy(d, None)
            assert result["status"] == vc.PASS

    def test_valid_webhook_passes_policy(self):
        """Webhook URL matching allowed_domains should pass."""
        with tempfile.TemporaryDirectory() as d:
            # Create policy
            policy_path = os.path.join(d, "policy.yaml")
            with open(policy_path, "w") as f:
                yaml.dump({"allowed_domains": ["hooks.example.com"]}, f)
            # Create config with matching webhook
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_routing": {
                        "receiver": {"type": "webhook",
                                     "url": "https://hooks.example.com/alert"},
                    }
                }}}, f)
            result = vc.check_policy(d, policy_path)
            assert result["status"] in (vc.PASS, vc.WARN)


class TestCustomRulesCheck:
    """Check 5: Custom rule linting."""

    def test_no_dir_skips(self):
        """Missing rule-packs dir should skip with PASS."""
        result = vc.check_custom_rules(None)
        assert result["status"] == vc.PASS
        assert any("skipped" in detail.lower()
                   for detail in result["details"])

    def test_nonexistent_dir_skips(self):
        """Nonexistent dir should skip with PASS."""
        result = vc.check_custom_rules("/nonexistent/rule-packs")
        assert result["status"] == vc.PASS

    def test_real_rule_packs_runs(self):
        """Real rule-packs/ directory should run lint without crash.

        Note: Rule packs intentionally omit static 'tenant' label
        (tenant is injected via PromQL on(tenant) matching), so
        lint_custom_rules reports errors. This test verifies the
        check_custom_rules wrapper runs without exception, not that
        all rules pass lint.
        """
        rule_packs_dir = os.path.join(
            os.path.dirname(__file__), "..", "rule-packs")
        if not os.path.isdir(rule_packs_dir):
            pytest.skip("rule-packs dir not found")
        result = vc.check_custom_rules(rule_packs_dir)
        assert result["status"] in (vc.PASS, vc.WARN, vc.FAIL)
        assert "check" in result
        assert result["check"] == "custom_rules"


class TestVersionsCheck:
    """Check 6: Version consistency."""

    def test_version_check_runs(self):
        """Version check should complete without error."""
        result = vc.check_versions()
        # May PASS or FAIL depending on repo state, but should not crash
        assert result["status"] in (vc.PASS, vc.WARN, vc.FAIL)
        assert "check" in result
        assert result["check"] == "versions"


class TestIntegration:
    """Integration test with real config dir."""

    CONF_DIR = os.path.join(os.path.dirname(__file__), "..",
                            "components", "threshold-exporter",
                            "config", "conf.d")

    @pytest.mark.skipif(not os.path.isdir(CONF_DIR), reason="Config dir not found")
    def test_full_validation_passes(self):
        """Full validation on the real config dir should pass."""
        results = []
        results.append(vc.check_yaml_syntax(self.CONF_DIR))
        results.append(vc.check_schema(self.CONF_DIR))
        results.append(vc.check_routes(self.CONF_DIR))

        for r in results:
            assert r["status"] != vc.FAIL, \
                f"{r['check']} failed: {r['details']}"


class TestProfilesCheck:
    """Check 6: Profile validation (v1.12.0 deep validation)."""

    def test_valid_profile_passes(self):
        """Well-formed profile with metric keys should pass."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"standard": {
                    "mysql_connections": 80,
                    "redis_memory_used_bytes": 4294967296,
                }}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_profile": "standard"
                }}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.PASS

    def test_reserved_key_in_profile_warns(self):
        """Profile containing reserved keys should warn."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"bad-profile": {
                    "mysql_connections": 80,
                    "_routing": {"receiver": {"type": "webhook"}},
                }}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.WARN
            assert any("reserved key" in detail
                       for detail in result["details"])

    def test_empty_profile_warns(self):
        """Empty profile should warn."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"empty-profile": {}}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.WARN
            assert any("empty" in detail.lower()
                       for detail in result["details"])

    def test_unknown_profile_ref_warns(self):
        """Tenant referencing non-existent profile should warn."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"standard": {"mysql_connections": 80}}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_profile": "nonexistent"
                }}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.WARN
            assert any("unknown profile" in detail
                       for detail in result["details"])

    def test_no_profiles_file_passes(self):
        """Missing _profiles.yaml should still pass (no profiles defined)."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {"mysql_connections": "80"}}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.PASS
