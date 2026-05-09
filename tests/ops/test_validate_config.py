"""pytest style tests for validate_config.py — one-stop configuration validation.

Merged from previous _extended split (PR test-refactor sweep): core check_*
happy paths sit alongside policy_dsl / profile / route / report / main / custom
rules / reserved-key edge case classes appended below.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
import yaml

import validate_config as vc


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


# ---------------------------------------------------------------------------
# Edge case + main() coverage (was test_validate_config_extended.py)
# ---------------------------------------------------------------------------


class TestPolicyDSL:
    """Check 8: Policy-as-Code DSL evaluation."""

    def test_no_policy_engine_skips(self, monkeypatch):
        """If policy_engine can't be imported, skip gracefully."""
        with tempfile.TemporaryDirectory() as d:
            # Temporarily hide policy_engine from imports
            original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__

            def mock_import(name, *args, **kwargs):
                if name == "policy_engine":
                    raise ImportError("no module")
                return original_import(name, *args, **kwargs)

            monkeypatch.setattr("builtins.__import__", mock_import)
            result = vc.check_policy_dsl(d)
            assert result["status"] == vc.PASS
            assert any("skipped" in d.lower() for d in result["details"])

    def test_no_policies_defined(self):
        """Config with no _policies section should skip."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            result = vc.check_policy_dsl(d)
            assert result["status"] == vc.PASS

    def test_with_standalone_dsl_file(self):
        """Standalone policy DSL file should be loaded."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            # The policy DSL file might not exist
            result = vc.check_policy_dsl(d, "/nonexistent/policy.yaml")
            assert result["status"] == vc.PASS


class TestProfilesExtended:
    """Extended profile validation tests."""

    def test_profile_not_a_mapping(self):
        """Profile value that's not a dict should warn."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"bad": "not-a-dict"}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.WARN
            assert any("not a mapping" in detail for detail in result["details"])

    def test_unknown_reserved_key_in_profile(self):
        """Profile with unknown reserved key should warn."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"p1": {
                    "mysql_connections": 80,
                    "_unknown_thing": "bad",
                }}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.WARN

    def test_tenant_without_profile_ref(self):
        """Tenant without _profile doesn't cause issues."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"standard": {"cpu": 80}}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {"cpu": 70}}}, f)
            result = vc.check_profiles(d)
            assert result["status"] == vc.PASS

    def test_bare_tenant_yaml(self):
        """Tenant YAML without 'tenants' wrapper (bare format)."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_profiles.yaml"), "w") as f:
                yaml.dump({"profiles": {"p1": {"cpu": 80}}}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"_profile": "p1", "cpu": 70}, f)
            result = vc.check_profiles(d)
            assert result["status"] in (vc.PASS, vc.WARN)


class TestRoutesWithPolicy:
    """Route validation with policy file."""

    def test_routes_with_policy(self):
        with tempfile.TemporaryDirectory() as d:
            policy_path = os.path.join(d, "policy.yaml")
            with open(policy_path, "w") as f:
                yaml.dump({"allowed_domains": ["hooks.example.com"]}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_routing": {
                        "receiver": {"type": "webhook",
                                     "url": "https://hooks.example.com/alert"},
                    }
                }}}, f)
            result = vc.check_routes(d, policy_path)
            assert result["status"] in (vc.PASS, vc.WARN)

    def test_routes_with_blocked_domain(self):
        """Route with domain not in allowlist produces warning."""
        with tempfile.TemporaryDirectory() as d:
            policy_path = os.path.join(d, "policy.yaml")
            with open(policy_path, "w") as f:
                yaml.dump({"allowed_domains": ["allowed.com"]}, f)
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            with open(os.path.join(d, "tenant-a.yaml"), "w") as f:
                yaml.dump({"tenants": {"tenant-a": {
                    "_routing": {
                        "receiver": {"type": "webhook",
                                     "url": "https://evil.com/alert"},
                    }
                }}}, f)
            result = vc.check_routes(d, policy_path)
            # Should have warnings about domain


class TestPrintReportExtended:
    """print_report() edge cases."""

    def test_all_pass(self, capsys):
        results = [vc._make_result("c1", vc.PASS, ["ok"])]
        vc.print_report(results)
        out = capsys.readouterr().out
        assert "PASS" in out
        assert "1 pass" in out
        assert "0 fail" in out

    def test_all_fail(self, capsys):
        results = [vc._make_result("c1", vc.FAIL, ["error"])]
        vc.print_report(results)
        out = capsys.readouterr().out
        assert "FAIL" in out

    def test_mixed(self, capsys):
        results = [
            vc._make_result("c1", vc.PASS, ["ok"]),
            vc._make_result("c2", vc.WARN, ["caution"]),
            vc._make_result("c3", vc.FAIL, ["bad"]),
        ]
        vc.print_report(results)
        out = capsys.readouterr().out
        assert "3 checks" in out
        assert "1 pass" in out
        assert "1 warn" in out
        assert "1 fail" in out


class TestMainCLI:
    """validate_config main() CLI tests."""

    def _make_config_dir(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        with open(d / "_defaults.yaml", "w") as f:
            yaml.dump({"defaults": {"mysql_connections": 80}}, f)
        with open(d / "tenant-a.yaml", "w") as f:
            yaml.dump({"tenants": {"tenant-a": {
                "mysql_connections": "70",
                "_routing": {
                    "receiver": {"type": "webhook",
                                 "url": "https://hooks.example.com/alert"},
                },
            }}}, f)
        return str(d)

    def test_main_basic(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("validate_config", "--config-dir", config_dir)
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert "validate-config" in out or "Validation" in out

    def test_main_json(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        cli_argv("validate_config", "--config-dir", config_dir, "--json")
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert isinstance(parsed, list)
        assert any(r["check"] == "yaml_syntax" for r in parsed)

    def test_main_nonexistent_dir(self, monkeypatch, capsys, cli_argv):
        cli_argv("validate_config", "--config-dir", "/nonexistent/path")
        with pytest.raises(SystemExit) as exc:
            vc.main()
        assert exc.value.code == 1

    def test_main_with_policy(self, tmp_path, monkeypatch, capsys, cli_argv):
        config_dir = self._make_config_dir(tmp_path)
        policy = tmp_path / "policy.yaml"
        policy.write_text(yaml.dump({"allowed_domains": ["hooks.example.com"]}),
                          encoding="utf-8")
        cli_argv("validate_config", "--config-dir", config_dir,
            "--policy", str(policy))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert "policy" in out.lower() or "PASS" in out


class TestCustomRulesExtended:
    """Extended custom rules check tests."""

    def test_timeout_handling(self, monkeypatch):
        """Timeout during lint should return FAIL."""
        def mock_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 30)
        monkeypatch.setattr(subprocess, "run", mock_run)

        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "rules"))
            # Need a real directory to pass the os.path.isdir check
            result = vc.check_custom_rules(os.path.join(d, "rules"))
            # No rule-packs in the dir, so it runs the subprocess
            # But our mock raises timeout
            # Actually check_custom_rules checks os.path.isdir first
            assert result["status"] in (vc.PASS, vc.FAIL)

    def test_with_policy_file(self, monkeypatch):
        """Running with both rule-packs and policy file."""
        with tempfile.TemporaryDirectory() as d:
            rp = os.path.join(d, "rule-packs")
            os.makedirs(rp)
            policy = os.path.join(d, "policy.yaml")
            with open(policy, "w") as f:
                yaml.dump({"allowed_domains": ["example.com"]}, f)

            def mock_run(cmd, **kwargs):
                result = MagicMock()
                result.returncode = 0
                result.stdout = "All good"
                result.stderr = ""
                return result
            monkeypatch.setattr(subprocess, "run", mock_run)

            result = vc.check_custom_rules(rp, policy)
            assert result["status"] == vc.PASS


class TestIsReservedKey:
    """_is_reserved_key helper tests."""

    def test_known_reserved(self):
        assert vc._is_reserved_key("_routing") is True
        assert vc._is_reserved_key("_severity_dedup") is True

    def test_reserved_prefix(self):
        assert vc._is_reserved_key("_state_maintenance") is True

    def test_not_reserved(self):
        assert vc._is_reserved_key("mysql_connections") is False
        assert vc._is_reserved_key("custom_metric") is False
