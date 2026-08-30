"""pytest style tests for validate_config.py — one-stop configuration validation.

Merged from previous _extended split (PR test-refactor sweep): core check_*
happy paths sit alongside policy_dsl / profile / route / report / main / custom
rules / reserved-key edge case classes appended below.
"""

import fnmatch
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock

import pytest
import yaml

import validate_config as vc
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


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
        # ⛔ TWO levels: `dirname(__file__)` is `tests/ops`. With one,
        # this resolved to `tests/components/...`, which has never
        # existed, and the skip guard below hid that on every machine
        # including CI — a permanently-skipped test reads exactly like
        # a passing one in the summary line.
        conf_dir = os.path.join(os.path.dirname(__file__), "..", "..",
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

    def test_supplied_but_missing_policy_file_is_caller_error(self):
        """A policy path that is not a file is a caller error, not a skip.

        ⛔ Renamed from `test_no_policy_file_skips`, which asserted PASS +
        "skipped" (#1556). That row is exactly what a customer copying the
        documented `--policy "webhook.company.com,slack.com"` example saw:
        `[PASS] policy — No policy file — skipped`, exit 0, and the webhook
        domain allowlist never ran. Omitting the flag is still a legitimate
        skip — `test_none_policy_skips` below is the other half of the
        distinction this check now draws.
        """
        with tempfile.TemporaryDirectory() as d:
            result = vc.check_policy(d, "/nonexistent/policy.yaml")
            assert result["status"] == vc.FAIL
            assert result["caller_error"] is True

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

    def test_supplied_but_nonexistent_dir_is_caller_error(self):
        """A --rule-packs path that is not a directory is a caller error.

        ⛔ Renamed from `test_nonexistent_dir_skips` (#1556): asserting PASS
        here meant a typo or a renamed directory removed the custom rule lint
        from the run and still reported success. `test_no_dir_skips` above
        keeps the legitimate case (flag omitted).
        """
        result = vc.check_custom_rules("/nonexistent/rule-packs")
        assert result["status"] == vc.FAIL
        assert result["caller_error"] is True

    def test_real_rule_packs_runs(self):
        """Real rule-packs/ directory should run lint without crash.

        Note: Rule packs intentionally omit static 'tenant' label
        (tenant is injected via PromQL on(tenant) matching), so
        lint_custom_rules reports errors. This test verifies the
        check_custom_rules wrapper runs without exception, not that
        all rules pass lint.
        """
        rule_packs_dir = os.path.join(  # two levels: see TestSchemaCheck
            os.path.dirname(__file__), "..", "..", "rule-packs")
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

    def test_it_actually_finds_the_tool_it_shells_out_to(self):
        """⛔ The check above accepts PASS *or* FAIL, so it passed for years
        while this check had never once run: `_THIS_DIR / "bump_docs.py"`
        pointed at `scripts/tools/ops/`, and the tool has always lived in
        `scripts/tools/dx/`. Every developer's `make validate-config` was red
        with

            [FAIL] versions
                   can't open file '.../scripts/tools/ops/bump_docs.py':
                   [Errno 2] No such file or directory

        Reverting the path fix leaves the whole suite green again unless
        something asserts the tool was *found*, which is what this does.
        """
        result = vc.check_versions()
        detail = " ".join(str(v) for v in result.values())
        assert "No such file or directory" not in detail, detail
        assert "can't open file" not in detail, detail
        assert result["status"] in (vc.PASS, vc.WARN), (
            "the versions check reported FAIL — if that is real drift, fix the "
            f"drift; if it is a missing file, fix the path.\n{result}")

    def test_a_missing_bump_docs_degrades_instead_of_leaking_interpreter_output(
            self, monkeypatch, tmp_path):
        """⛔ The tool is genuinely ABSENT in the published da-tools image.

        `build.sh` flattens the tool set into `/opt/da-tools/` and
        `bump_docs.py` is not in the shipped list at all, so no path resolves
        there. The failure mode that shipped for years is subtle:
        `subprocess.run([sys.executable, "<missing>.py"])` does NOT raise
        `FileNotFoundError` — the INTERPRETER exists, so it starts, prints
        `can't open file ...` to stderr and exits 2. The `except
        FileNotFoundError` branch is therefore unreachable, and a caller sees
        a bare interpreter error dressed up as `[FAIL] versions` (#1461).

        ⛔ Reverting the pre-flight existence check leaves the whole suite
        green — measured: the guard was added with no control, and disabling
        it scored `44 passed, 3 skipped`. This is that missing control.

        Asserts the SHAPE of a degraded answer, not the wording: not FAIL,
        no interpreter noise, and an actionable command the reader can run.
        """
        monkeypatch.setattr(vc, "_THIS_DIR", tmp_path / "ops")
        result = vc.check_versions()
        detail = " ".join(str(v) for v in result.values())

        # ⛔ Assert the degraded branch was ACTUALLY TAKEN before asserting
        # anything about its wording. This patch works by rebinding
        # `_THIS_DIR`, which only bites while the path is resolved INSIDE the
        # function; hoisting it to a module-level constant — a behaviour-
        # preserving refactor this repo does everywhere — silently stops the
        # patch from applying. Without this check the run reports `pass` and
        # then fails further down with "the degraded answer does not say WHAT
        # is missing", a message that is simply false when no degraded answer
        # was produced at all.
        assert result["status"] == vc.WARN, (
            "the degraded branch was not exercised — `check_versions` did not "
            "see a missing bump_docs.py. If the path is now resolved at import "
            "time, patch that constant instead of `_THIS_DIR`; this test is "
            f"about the MISSING-TOOL behaviour, not about where the path is "
            f"computed.\n{result}")
        for leak in ("can't open file", "No such file or directory",
                     "Traceback"):
            assert leak not in detail, (
                f"raw interpreter/OS output leaked into the check result "
                f"({leak!r}) — that is #1461's shape.\n{result}")
        assert "bump_docs.py" in detail, (
            f"the degraded answer does not say WHAT is missing.\n{result}")
        # ⛔ "gives the reader something runnable", not a literal flag. The
        # earlier form pinned `--check`, so redirecting the reader at
        # `make version-check` — which this branch made the correct answer by
        # teaching that target BOTH halves of the gate — failed with "gives the
        # reader no command to run" while a command sat in the detail. The
        # original rationale ("must not depend on make, the image has no
        # Makefile") does not hold against that wording either: the sentence
        # already says "In a repo checkout run:", i.e. it has redirected the
        # reader somewhere a Makefile exists.
        assert any(tok in detail for tok in ("bump_docs.py", "version-check")), (
            "the degraded answer gives the reader nothing runnable — name the "
            f"command or the make target that performs this check.\n{result}")


class TestIntegration:
    """Integration test with real config dir."""

    CONF_DIR = os.path.join(os.path.dirname(__file__), "..", "..",
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

    def test_missing_standalone_dsl_file_is_caller_error(self):
        """A --policy-dsl path that is not a file is a caller error.

        ⛔ Renamed from `test_with_standalone_dsl_file`, whose NAME said the
        file "should be loaded" while its BODY asserted PASS for a file that
        does not exist, under the comment "The policy DSL file might not
        exist" (#1556). Measured before this changed: `--policy-dsl <typo>`
        produced output BYTE-IDENTICAL to passing no flag at all, at exit 0 —
        the operator asked for a Policy-as-Code file and the whole check left
        the run silently, reported as `[PASS] policy_dsl`.
        """
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            result = vc.check_policy_dsl(d, "/nonexistent/policy.yaml")
            assert result["status"] == vc.FAIL
            assert result["caller_error"] is True

    def test_standalone_dsl_file_that_exists_is_loaded(self):
        """The case the old NAME claimed to cover, which nothing covered."""
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w") as f:
                yaml.dump({"defaults": {"mysql_connections": 80}}, f)
            dsl = os.path.join(d, "policies.yaml")
            with open(dsl, "w", encoding="utf-8") as f:
                yaml.dump({"policies": [
                    {"name": "p1",
                     "rule": "thresholds.mysql_connections.warning < 1",
                     "severity": "error"}]}, f)
            result = vc.check_policy_dsl(d, dsl)
            assert result["details"], (
                "a real DSL file produced no detail lines at all, so nothing "
                "here shows the file was actually read")


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
        assert exc.value.code == EXIT_CALLER_ERROR

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


# ============================================================
# #1447 — what the report points at has to be openable by its reader.
# ============================================================

_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".."))


class TestCheckHintsPointSomewhereCustomersCanOpen:
    """The report used to name files only this repository has.

    Its audience runs `da-tools validate-config` against a repo holding
    `conf.d/` and whatever `da-tools init` wrote. `-> See: docs/scenarios/…`
    named a path that does not exist over there.
    """

    @staticmethod
    def _exclude_patterns():
        """Patterns mkdocs holds back, read from mkdocs.yml's exclude_docs.

        Read textually rather than with `yaml.safe_load`: mkdocs.yml carries
        `!!python/name:` tags for the emoji extension, which safe_load
        refuses. `generate_nav.py` reads the nav the same way for the same
        reason.
        """
        lines = open(os.path.join(_REPO_ROOT, "mkdocs.yml"),
                     encoding="utf-8").read().splitlines()
        patterns, collecting = [], False
        for line in lines:
            if line.startswith("exclude_docs:"):
                collecting = True
                continue
            if not collecting:
                continue
            if line.strip() and not line[0].isspace():
                break
            entry = line.strip()
            if entry and not entry.startswith("#"):
                # Tolerate either YAML spelling. Measured: rewriting
                # `exclude_docs` as a `- `-prefixed list — same meaning to
                # mkdocs — turned this red, because the `- ` rode along
                # into the pattern and matched nothing.
                patterns.append(entry[2:].strip()
                                if entry.startswith("- ") else entry)
        return patterns

    @classmethod
    def _is_excluded(cls, rel_to_docs):
        """⚠️ Models the literal / fnmatch subset of .gitignore syntax only.

        Directory-form entries (`internal/`) are NOT modelled and would be
        read as "not excluded" — a fail-OPEN direction. `test_exclude_...`
        below pins the shapes that are modelled so the gap stays visible
        rather than being assumed away.
        """
        patterns = cls._exclude_patterns()
        assert patterns, (
            "read no exclude_docs patterns from mkdocs.yml — the block moved "
            "or was renamed, so this check is no longer looking at anything")
        return any(
            fnmatch.fnmatch(rel_to_docs, pat)
            or fnmatch.fnmatch(os.path.basename(rel_to_docs), pat)
            for pat in patterns
        )

    def test_every_hint_target_is_reachable(self):
        assert vc._CHECK_HINTS, "no hints defined"
        for check, (hint, docs) in vc._CHECK_HINTS.items():
            assert hint, f"{check}: empty hint"
            if docs.startswith(("http://", "https://")):
                # Absolute URLs are already openable, but they still have to
                # look like one — an earlier draft simply `continue`d here,
                # which meant a typo'd scheme skipped every check below.
                assert "://" in docs and " " not in docs, (
                    f"{check}: {docs!r} is not a usable absolute URL")
                continue
            path = docs.split("#", 1)[0]
            assert path.startswith("docs/") and path.endswith(".md"), (
                f"{check}: hint target {docs!r} is neither a docs/ page nor "
                "an absolute URL, so there is nothing the reader can open")
            assert os.path.isfile(os.path.join(_REPO_ROOT, path)), (
                f"{check}: {path} does not exist")
            assert not self._is_excluded(path[len("docs/"):]), (
                f"{check}: {path} matches an mkdocs.yml exclude_docs "
                "pattern, so the URL printed for it would 404")

    def test_exclude_matching_is_answerable(self):
        """⛔ Control for the half that never fires on real data.

        `exclude_docs` holds back only two README files today, so
        `_is_excluded` returns False for every hint and could be replaced
        with `return False` without turning anything red.
        """
        assert self._is_excluded("README-root.md"), (
            "a pattern mkdocs.yml really lists was not matched — if the "
            "exclude_docs block changed, update this literal rather than "
            "deleting the assertion; it is this layer's only control")
        assert not self._is_excluded("scenarios/alert-routing-split.md")

    @pytest.mark.parametrize("rel,expected", [
        ("docs/scenarios/alert-routing-split.md",
         "scenarios/alert-routing-split/"),
        # A deliberately precise pointer must not be coarsened to the page.
        ("docs/scenarios/alert-routing-split.md#guardrails",
         "scenarios/alert-routing-split/#guardrails"),
        # mkdocs folds index/README onto the directory; keeping the filename
        # yields a 404 that a file-exists check cannot see (measured on the
        # live site: /adr/README/ → 404, /adr/ → 200).
        ("docs/adr/README.md", "adr/"),
        ("docs/index.md", ""),
    ])
    def test_docs_url_matches_how_mkdocs_serves_the_page(self, rel, expected):
        assert vc._docs_url(rel) == vc.DOCS_SITE_BASE + expected

    @pytest.mark.parametrize("value", [
        "", "https://example.invalid/x", "scripts/tools/ops/diagnose.py",
        "docs/notes.txt",
    ])
    def test_non_docs_values_are_returned_unchanged(self, value):
        """Must-tolerate side: don't mangle something that isn't a docs page."""
        assert vc._docs_url(value) == value

    def test_printed_report_carries_the_url_not_the_repo_path(self):
        captured = io.StringIO()
        original, sys.stdout = sys.stdout, captured
        try:
            vc.print_report([vc._make_result("routes", vc.FAIL, ["boom"])])
        finally:
            sys.stdout = original
        out = captured.getvalue()
        assert "-> See: https://vencil.github.io/" in out
        assert "-> See: docs/" not in out

    def test_json_mode_carries_the_url_too(self):
        captured = io.StringIO()
        original, sys.stdout = sys.stdout, captured
        try:
            vc.print_report([vc._make_result("routes", vc.FAIL, ["boom"])],
                            as_json=True)
        finally:
            sys.stdout = original
        entry = json.loads(captured.getvalue())[0]
        assert entry["docs_link"].startswith("https://vencil.github.io/")


# ============================================================
# #1448 — the report has to survive a customer file this tool cannot read,
# and it must not invent facts while surviving.
# ============================================================
class TestNoCheckCanKillTheReport:
    """#1448 stated as a property of the run, not of how ``main()`` is written.

    ⛔ This replaces two AST guards that asked what the source *looks like*
    ("is every ``check_*`` reference lexically inside a ``_run_check(...)``
    call?"). Blind review broke them with five edits that are ordinary
    rather than wrong, each measured to leave behaviour byte-identical:
    replacing the eight dispatch lines with a ``(name, fn, args, kwargs)``
    table and a loop; extracting the dispatch into ``_collect_results()``;
    hoisting the checks into module-level tuples; exporting a ``check_*``
    that ``main()`` deliberately does not run; and splitting the module so
    the checks arrive by import. The first turned the guard red while every
    end-to-end test stayed green — the guard's own message ("a customer file
    escapes as a traceback and no report is printed") was false as it was
    printed, and the cheapest way back to green was to revert the refactor.

    Asking the question at runtime removes the whole class: force one check
    to fail the way an unreadable customer file makes it fail, and require
    that the report still comes out. Every dispatch shape that keeps that
    true passes; a check wired up so its failure kills the process does not,
    however it is written, and however it got into the module.

    ⚠️ What this deliberately no longer catches: a ``check_*`` that is
    defined but never dispatched. Forcing it to raise changes nothing, so
    it stays green. That is not #1448's defect — dead code does not stop
    the report — and pinning it was the source of two of the false reds
    above (an exported helper, and the population going empty on a module
    split).
    """

    # Derived from the module, so a ninth check answers for itself — and
    # unlike an AST walk over `def` statements, an imported or re-exported
    # check is in here too.
    CHECK_NAMES = sorted(n for n in dir(vc)
                         if n.startswith("check_") and callable(getattr(vc, n)))

    @staticmethod
    def _conf_d(tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db_a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        return str(d)

    def _run_with(self, name, tmp_path, monkeypatch, capsys, cli_argv):
        """Force `name` to fail the way a bad customer file makes it fail."""
        def boom(*_a, **_kw):
            raise yaml.scanner.ScannerError(
                None, None, "while scanning a quoted scalar", None)

        # The two checks that shell out are stubbed to something fast unless
        # they are the target: this test is about the dispatch surviving, not
        # about bump_docs.py being installed.
        for slow in ("check_versions", "check_custom_rules"):
            if slow != name and hasattr(vc, slow):
                monkeypatch.setattr(
                    vc, slow,
                    lambda *_a, **_kw: vc._make_result("stub", vc.PASS, ["ok"]))
        monkeypatch.setattr(vc, name, boom)

        policy = tmp_path / "policy.yaml"
        policy.write_text("allowed_domains: [hooks.example.com]\n",
                          encoding="utf-8")
        packs = tmp_path / "rule-packs"
        packs.mkdir()
        cli_argv("validate_config", "--config-dir", self._conf_d(tmp_path),
                 "--policy", str(policy), "--rule-packs", str(packs),
                 "--version-check")
        with pytest.raises(SystemExit) as exc:
            vc.main()
        return exc.value.code, capsys.readouterr().out

    @pytest.mark.parametrize("name", CHECK_NAMES)
    def test_the_report_survives(self, name, tmp_path, monkeypatch, capsys,
                                 cli_argv):
        rc, out = self._run_with(name, tmp_path, monkeypatch, capsys, cli_argv)
        assert "Unified Validation Report" in out, (
            f"forcing {name} to fail on its input took the whole report with "
            f"it — that is #1448. stdout was {len(out)} bytes.")
        assert "Total:" in out, out[-400:]
        assert rc != vc.EXIT_CALLER_ERROR, (
            f"{name} failing on unreadable input is a finding about the "
            f"config (exit 1), not a defect in this tool (exit 2)")

    def test_the_probe_is_observed(self, tmp_path, monkeypatch, capsys,
                                   cli_argv):
        """Must-observe control.

        Every assertion above is satisfied by a run in which the injection
        never happened. This one takes a check that is always dispatched and
        requires the report to show it failing — so the harness is known to
        be able to reach the thing it claims to be testing.
        """
        rc, out = self._run_with("check_yaml_syntax", tmp_path, monkeypatch,
                                 capsys, cli_argv)
        assert "[FAIL] yaml_syntax" in out, out
        assert "could not read the config" in out, out

    def test_a_clean_run_is_untouched(self, tmp_path, capsys, cli_argv):
        """Must-succeed control: the same fixture with nothing injected."""
        cli_argv("validate_config", "--config-dir", self._conf_d(tmp_path))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert exc.value.code == 0, out
        assert "[FAIL]" not in out, out


class TestRunCheckContract:
    """Both directions: what _run_check absorbs, and how it classifies it."""

    def test_yaml_error_becomes_a_report_row(self):
        def boom(_arg):
            raise yaml.scanner.ScannerError(
                None, None, "while scanning a quoted scalar", None)

        result = vc._run_check("profiles", boom, "some-dir")
        assert result["check"] == "profiles"
        assert result["status"] == vc.FAIL
        assert not result.get("caller_error")
        assert any("could not read the config" in d
                   for d in result["details"])

    def test_undecodable_input_is_a_finding_not_a_caller_error(self):
        """⛔ An encoding problem in the customer's file is *their* config.

        Measured before this: one tenant file saved in the local code page
        made every check raise ``UnicodeDecodeError``, which fell into the
        catch-all below — so the run exited **2** ("this tool broke") and
        told the customer "Please report this along with the file it names",
        in a message that names no file, because ``UnicodeDecodeError``
        carries a codec and an offset and no path. ``origin/main`` exited 1.
        A fix that made the report appear must not also change what the exit
        code means.
        """
        def boom(_arg):
            b"\xb4\xfa".decode("utf-8")

        result = vc._run_check("schema", boom, "some-dir")
        assert result["status"] == vc.FAIL
        assert not result.get("caller_error"), (
            "an undecodable input file must exit 1 like any other finding "
            "about the customer's config, not 2")

    def test_os_error_is_a_caller_error(self):
        def boom():
            raise PermissionError(13, "Permission denied")

        result = vc._run_check("schema", boom)
        assert result["status"] == vc.FAIL
        assert result["caller_error"] is True

    def test_other_exceptions_still_produce_a_report_row(self, capsys):
        """⛔ The contract that replaced "let it propagate".

        The first draft asserted the opposite — that anything which is not a
        YAMLError keeps propagating, on the theory that it is our bug and
        should stay loud. Blind review measured six *syntactically valid*
        customer configs against it: three still died with zero bytes on
        stdout, one of them inside ``check_profiles``, the function #1448
        named. "Whose bug is it" and "does the customer get a report" are
        different questions and the ticket asks the second.
        """
        def boom():
            raise RuntimeError("something we did not foresee")

        result = vc._run_check("routes", boom)
        assert result["status"] == vc.FAIL
        assert result["caller_error"] is True
        assert "Traceback" in capsys.readouterr().err

    def test_a_healthy_check_is_passed_through_untouched(self):
        """Must-succeed control.

        Without one, a wrapper that swallowed every call and returned a FAIL
        row would satisfy every assertion above — a group made only of
        must-fail cases cannot tell "classified correctly" from "the thing
        under test was never reached".

        ⛔ The keyword argument is REQUIRED on purpose. An earlier version
        of this control gave it a default, so a wrapper that dropped
        ``**kwargs`` — which is how ``--strict`` would silently stop
        reaching ``check_schema`` — kept it green.
        """
        seen = {}

        def probe(a, *, strict):
            seen["args"] = (a, strict)
            return vc._make_result("routes", vc.PASS, ["all good"])

        row = vc._run_check("routes", probe, "d", strict=True)
        assert seen["args"] == ("d", True), seen
        assert row["status"] == vc.PASS
        assert row["details"] == ["all good"]


class TestUnusableFilesAreDataNotProse:
    """#1448: the skipped-file list is bookkeeping, never re-parsed prose."""

    def test_a_crashed_yaml_syntax_row_names_no_files(self):
        """⛔ The regression this replaced.

        The first draft recovered the list by splitting the detail lines it
        had just printed on ``:``. When ``check_yaml_syntax`` itself died,
        its row's details are three English sentences — so the report
        announced "⚠️ 3 file(s) could not be parsed and were skipped (this
        check could not complete on your input, The report below is
        incomplete …, Please report this …)" and put the same three
        sentences into the ``skipped_unusable_files`` JSON field. Measured
        on a conf.d holding exactly one bad file.
        """
        crashed = vc._make_result(
            "yaml_syntax", vc.FAIL,
            ["this check could not complete on your input: X: y",
             "The report below is incomplete — a traceback was written.",
             "Please report this along with the traceback."],
            caller_error=True)
        assert vc._unusable_files([crashed]) == []

    def test_the_list_comes_from_the_checks_own_bookkeeping(self):
        row = vc._make_result("yaml_syntax", vc.FAIL,
                              ["a.yaml: bad", "b.yaml: worse"],
                              unusable_files=["a.yaml", "b.yaml"])
        assert vc._unusable_files([row]) == ["a.yaml", "b.yaml"]

    def test_no_caveat_is_printed_when_nothing_was_skipped(self):
        """Must-tolerate side: a clean run must not grow a warning."""
        captured = io.StringIO()
        original, sys.stdout = sys.stdout, captured
        try:
            vc.print_report([vc._make_result("yaml_syntax", vc.PASS, ["ok"]),
                             vc._make_result("routes", vc.WARN, ["hmm"])])
        finally:
            sys.stdout = original
        assert _caveat_fragment() not in captured.getvalue()


class TestCustomerRunsTheCommandInTheTroubleshootingTable:
    """End to end, as the GitOps guide §7 tells a customer to run it.

    ⛔ The unit tests above all reach into the module. #1448 is about what
    the *process* does: on ``origin/main`` a single unreadable file in
    ``conf.d/`` produced **zero bytes on stdout** and a Python traceback,
    measured both in-tree and against the shipped ``ghcr.io/vencil/da-tools``
    image (rc=1, 0 bytes). A report that exists only when every file happens
    to be readable is not a report the troubleshooting table can point at.
    """

    _SCRIPT = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        "scripts", "tools", "ops", "validate_config.py")

    def _conf_d(self, d, bad_name, bad_bytes):
        conf = os.path.join(d, "conf.d")
        os.makedirs(conf)
        with open(os.path.join(conf, "_defaults.yaml"), "wb") as f:
            f.write(b"defaults:\n  mysql_threads_running: 80\n")
        with open(os.path.join(conf, bad_name), "wb") as f:
            f.write(bad_bytes)
        return conf

    def _run(self, conf, *extra):
        p = subprocess.run(
            [sys.executable, self._SCRIPT, "--config-dir", conf, *extra],
            capture_output=True, timeout=300)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    # A tenant file an editor saved in the local code page, and one whose
    # top level is a list. Both are valid *files*; neither is readable by
    # this tool, and neither used to leave a report behind.
    BAD = [
        ("not-utf8", b"tenants:\n  db_a:\n    # \xb4\xfa\xb8\xd5\n    x: 90\n"),
        ("not-a-mapping", b"- tenants:\n    db_a:\n      x: 90\n"),
    ]

    @pytest.mark.parametrize("label,body", BAD, ids=[i for i, _ in BAD])
    def test_the_report_is_printed_and_names_the_file(self, label, body):
        with tempfile.TemporaryDirectory() as d:
            conf = self._conf_d(d, "db-a.yaml", body)
            rc, out, err = self._run(conf)
            assert out, f"{label}: zero bytes on stdout — #1448 all over again"
            assert "Unified Validation Report" in out
            assert "db-a.yaml" in out, (
                f"{label}: the report never names the file that stopped it; "
                f"the customer has nothing to act on.\n{out}")
            assert "Traceback" not in err, err[-800:]

    @pytest.mark.parametrize("label,body", BAD, ids=[i for i, _ in BAD])
    def test_an_unreadable_file_is_a_finding_not_a_tool_failure(self, label,
                                                                body):
        """⛔ exit 2 says "this tool broke, report it". It is their file.

        ``origin/main`` exited 1 here (by accident — an uncaught exception).
        The first draft of the fix exited **2** and printed "Please report
        this along with the file it names", naming no file.
        """
        with tempfile.TemporaryDirectory() as d:
            conf = self._conf_d(d, "db-a.yaml", body)
            rc, out, _ = self._run(conf)
            assert rc == 1, f"{label}: expected a validation finding\n{out}"
            assert "Please report this" not in out, out

    @pytest.mark.parametrize("label,body", BAD, ids=[i for i, _ in BAD])
    def test_json_lists_real_filenames_only(self, label, body):
        """⛔ The ``skipped_unusable_files`` field is consumed by machines.

        The first draft filled it by splitting the report's own prose on
        ``:``, so a run that crashed early published three English sentences
        there — and the text report said "3 file(s) could not be parsed"
        about a conf.d holding one bad file.
        """
        with tempfile.TemporaryDirectory() as d:
            conf = self._conf_d(d, "db-a.yaml", body)
            rc, out, _ = self._run(conf, "--json")
            entries = json.loads(out)
            names = {n for e in entries
                     for n in e.get("skipped_unusable_files", [])}
            assert names == {"db-a.yaml"}, (
                f"{label}: the machine-readable skip list is not a list of "
                f"files: {sorted(names)}")

    def test_a_readable_conf_d_is_unchanged(self):
        """Must-succeed control.

        Every case above asserts on a broken input; a build of this tool
        that reported "unreadable" for *everything* would satisfy all of
        them. This one fails if the fix made a healthy conf.d noisier.
        """
        with tempfile.TemporaryDirectory() as d:
            conf = self._conf_d(
                d, "db-a.yaml",
                b"tenants:\n  db_a:\n    mysql_threads_running: 90\n")
            rc, out, err = self._run(conf)
            assert _caveat_fragment() not in out, out
            assert "[FAIL] yaml_syntax" not in out, out
            assert "Traceback" not in err


class TestNullDefaultsBlockDoesNotKillTheReport:
    """``defaults:`` with every entry commented out parses to ``None``.

    #1448 named ``check_profiles``; this is the line inside it that raised.
    ``.get("defaults", {})`` returns ``None`` for a key that exists with
    nothing under it — the default only fires when the key is *absent*.
    """

    @pytest.mark.parametrize("body", [
        "defaults:\n",
        "defaults:\n  # mysql_threads_running: 80\n",
        "defaults: null\n",
    ], ids=["bare-key", "all-commented-out", "explicit-null"])
    def test_check_profiles_survives(self, body):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(body)
            result = vc.check_profiles(d)
            assert result["status"] in (vc.PASS, vc.WARN, vc.FAIL)

    def test_a_populated_defaults_block_is_still_read(self):
        """Must-succeed control: `or {}` must not swallow real keys.

        A declared key used by a tenant profile must still count as known —
        otherwise "survives" could be bought by reading nothing at all.
        """
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "_defaults.yaml"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write("defaults:\n  mysql_threads_running: 80\n")
            result = vc.check_profiles(d)
            assert result["status"] == vc.PASS


class TestYamlSyntaxNamesWhatItCannotRead:
    """The one check that opens every customer file is the one that can
    name it — ``UnicodeDecodeError`` carries a codec and an offset, no path.
    """

    def test_non_utf8_file_is_reported_with_its_name(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "wb") as f:
                f.write(b"tenants:\n  db_a:\n    # \xb4\xfa\xb8\xd5\n")
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.FAIL
            assert result["unusable_files"] == ["db-a.yaml"]
            assert any("UTF-8" in x for x in result["details"]), result

    def test_non_mapping_top_level_is_reported_with_its_name(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "db-a.yaml"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write("- tenants:\n    db_a:\n      x: 90\n")
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.FAIL
            assert result["unusable_files"] == ["db-a.yaml"]
            assert any("mapping" in x for x in result["details"]), result

    @pytest.mark.parametrize("body", ["", "# only a comment\n", "---\n"],
                             ids=["empty", "comment-only", "doc-marker"])
    def test_a_file_holding_nothing_is_not_an_error(self, body):
        """Must-tolerate side.

        ``yaml.safe_load`` returns ``None`` for these, which is *not* a
        wrong shape — placeholder files are ordinary. Rejecting them would
        be the cheapest way to make the checks above pass.
        """
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "placeholder.yaml"), "w",
                      encoding="utf-8", newline="\n") as f:
                f.write(body)
            result = vc.check_yaml_syntax(d)
            assert result["status"] == vc.PASS, result
            assert result["unusable_files"] == []




def _caveat_fragment() -> str:
    """The run-independent part of the skipped-file caveat.

    ⛔ Derived from `vc.UNUSABLE_CAVEAT`, never retyped. Retyped prose is
    how the negative controls below went vacuous: `"<old wording>" not in
    output` is satisfied by any reword, so a reword plus a real defect left
    them green while the positive assertions went red — measured.
    """
    return vc.UNUSABLE_CAVEAT.split("{listed}")[-1].strip()



def _generic_schema_hint() -> str:
    """The stock advice for the `schema` row, read from the shipped table.

    ⛔ Never retyped. Blind review reworded `_CHECK_HINTS["schema"]` — an
    ordinary copy edit — and measured two positive assertions going red
    while **four** `"Remove unknown keys" not in …` controls silently became
    vacuous, because a reworded string satisfies a `not in` on the old
    wording. The same trap the caveat constants were hoisted for.
    """
    return vc._CHECK_HINTS["schema"][0].split(".")[0]


def _report_row(out: str, check: str) -> str:
    """The block of the text report belonging to one check, status-agnostic.

    Slicing on a hard-coded `[FAIL] <name>` breaks the moment the check
    passes (which is the point of several of these fixtures: a skipped file
    leaves other checks PASSing), and slicing to the next `---` runs past
    into the following row.
    """
    lines = out.splitlines(True)
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().endswith("] " + check)
                  and ln.strip().startswith("[")), None)
    assert start is not None, f"no row for {check!r} in report:\n{out}"
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].strip().startswith("[")
                or lines[i].startswith("-" * 10)), len(lines))
    return "".join(lines[start:end])


class TestTheCaveatIsActuallyPrinted:
    """⛔ Re-added after a rewrite dropped it.

    An earlier draft pinned this end to end; rewriting the skipped-file
    plumbing kept only the *negative* half ("no caveat on a clean run") plus
    two unit tests on the helper. Measured: deleting the caveat entirely
    (``caveat = ""``) left **148 passed** — the ⚠️ line and the ordering
    line, both of which the CHANGELOG calls load-bearing, had no coverage at
    all. A fix that narrows coverage looks exactly like a fix that narrows
    false positives.
    """

    def _report(self, tmp_path, capsys, cli_argv, **kw):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        (d / "db-a.yaml").write_bytes(b"tenants:\n  db_a: [90\n")
        cli_argv("validate_config", "--config-dir", str(d), *kw.get("extra", ()))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        return exc.value.code, capsys.readouterr().out

    def test_rows_that_skipped_a_file_say_so_and_say_it_first(
            self, tmp_path, capsys, cli_argv):
        rc, out = self._report(tmp_path, capsys, cli_argv)
        assert vc.UNUSABLE_CAVEAT.format(n=1, listed="db-a.yaml") in out, out
        assert vc.READ_ERRORS_FIRST in out, (
            "the 'Remove unknown keys'-style hint must be sequenced after "
            "the read errors — following it first deletes a working config")
        # Ordering is a within-row guarantee, so compare inside one row.
        # (Comparing global offsets was wrong: the FAIL row that *reports*
        # the unreadable file carries a Suggested action of its own, and it
        # is printed first.)
        row = _report_row(out, "profiles")
        assert (row.index(vc.READ_ERRORS_FIRST)
                < row.index("-> Suggested action:")), row

    def test_the_row_that_reported_them_does_not_warn_about_itself(
            self, tmp_path, capsys, cli_argv):
        rc, out = self._report(tmp_path, capsys, cli_argv)
        head = _report_row(out, "yaml_syntax")
        assert _caveat_fragment() not in head, head

    def test_a_check_that_never_reads_config_dir_is_not_blamed(
            self, tmp_path, capsys, cli_argv, monkeypatch):
        """⛔ `versions` shells out to bump_docs.py; `custom_rules` reads
        `--rule-packs`. Neither has ever opened a file in `conf.d/`, and an
        earlier draft gated the caveat on ``check != "yaml_syntax"`` — so
        both were told that a broken tenant file was why they failed.
        """
        monkeypatch.setattr(
            vc, "check_versions",
            lambda: vc._make_result("versions", vc.FAIL, ["bump_docs blew up"]))
        rc, out = self._report(tmp_path, capsys, cli_argv,
                               extra=("--version-check",))
        block = _report_row(out, "versions")
        assert _caveat_fragment() not in block, block

    def test_a_check_that_does_read_config_dir_still_is(
            self, tmp_path, capsys, cli_argv):
        """Must-observe side of the pair above: without it, "attach the
        caveat to nothing" would satisfy that test."""
        rc, out = self._report(tmp_path, capsys, cli_argv)
        block = _report_row(out, "routes")
        assert _caveat_fragment() in block, block


class TestUnreadableInputIsNeverOurBug:
    """The exit code has to keep saying whose problem it is.

    ⛔ Measured before this: a `conf.d/` file nested ~500 levels deep makes
    (roughly half ``sys.getrecursionlimit()``, and ⚠️ **not a constant even
    on one host** — it shifts with how many frames are already on the stack.
    Measured three ways on this machine: flow-style 495/495/493, block-style
    493/492/491. Never assert on the number; assert on the behaviour.)
    ``yaml.safe_load`` raise ``RecursionError`` — not a ``YAMLError`` — so
    it fell through to the catch-all and the run exited **2** with "Please
    report this", naming no file. An enumeration of exception types cannot
    close that; reading each file by name, and deciding the exit code from
    whether the input was readable at all, can.
    """

    @staticmethod
    def _tree(tmp_path, body: bytes):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        (d / "db-a.yaml").write_bytes(body)
        return str(d)

    UNREADABLE = [
        ("not-utf8", b"tenants:\n  db_a:\n    # \xb4\xfa\xb8\xd5\n    x: 1\n"),
        ("not-a-mapping", b"- tenants:\n    db_a:\n      x: 1\n"),
        ("syntax-error", b"tenants:\n  db_a: [1\n"),
        ("deeply-nested",
         ("".join("  " * i + "k%d:\n" % i for i in range(520))).encode()),
    ]

    @pytest.mark.parametrize("label,body", UNREADABLE,
                             ids=[i for i, _ in UNREADABLE])
    def test_exit_1_and_the_file_is_named(self, label, body, tmp_path,
                                          capsys, cli_argv):
        cli_argv("validate_config", "--config-dir", self._tree(tmp_path, body))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert exc.value.code == 1, (
            f"{label}: exit {exc.value.code} tells the customer this tool "
            f"broke; their file did.\n{out}")
        assert "db-a.yaml" in out, f"{label}: the report names no file\n{out}"

    def test_a_real_tool_defect_still_exits_2(self, tmp_path, capsys,
                                              cli_argv, monkeypatch):
        """Must-distinguish control.

        Without it, "always exit 1" passes every case above and the tool
        loses the ability to say "this one is on us".
        """
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")

        def boom(*_a, **_kw):
            raise RuntimeError("a defect in this tool")

        monkeypatch.setattr(vc, "check_routes", boom)
        cli_argv("validate_config", "--config-dir", str(d))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert exc.value.code == vc.EXIT_CALLER_ERROR, out
        assert "please report it with the traceback" in out, out


class TestEmptyPlatformDefaultsSaysSoInsteadOfAdvisingDeletion:
    """⛔ "Remove unknown keys" is destructive when nothing is declared.

    Measured on ``origin/main``: a ``_defaults.yaml`` with no ``defaults:``
    key at all makes ``check_schema`` report every tenant override as
    ``unknown key … not in defaults``, the row prints "Remove unknown keys
    or add them to the schema", and the run exits **0**. Those keys are
    legal and in force; deleting them silently drops the tenant to platform
    values that do not exist.

    Only the advice changes. The warnings come from ``validate_tenant_keys``,
    whose verdicts are pinned key-for-key against the Go twin.
    """

    @staticmethod
    def _tree(tmp_path, defaults_body: str):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(defaults_body, encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db_a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        return str(d)

    @pytest.mark.parametrize("body", [
        "# nothing declared here\n",
        "defaults:\n",
        "defaults: {}\n",
    ], ids=["no-defaults-key", "null-defaults", "empty-defaults"])
    def test_the_destructive_hint_is_replaced(self, body, tmp_path, capsys,
                                              cli_argv):
        cli_argv("validate_config", "--config-dir", self._tree(tmp_path, body))
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "unknown key" in out, out
        assert _generic_schema_hint() not in out, (
            "the report is still telling the customer to delete overrides "
            f"that are in force\n{out}")
        assert vc.NO_DECLARED_DEFAULTS_HINT in out, out

    def test_a_real_typo_still_gets_the_normal_advice(self, tmp_path, capsys,
                                                     cli_argv):
        """Must-tolerate side: when defaults ARE declared, an unknown key is
        a typo and "remove it or add it to the schema" is the right thing to
        say. Without this, suppressing the hint unconditionally would pass."""
        d = self._tree(tmp_path, "defaults:\n  mysql_threads_running: 80\n")
        (pathlib.Path(d) / "db-b.yaml").write_text(
            "tenants:\n  db_b:\n    mysql_thredas_running: 90\n",
            encoding="utf-8")
        cli_argv("validate_config", "--config-dir", d)
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "unknown key" in out, out
        assert _generic_schema_hint() in out, out
        assert vc.NO_DECLARED_DEFAULTS_HINT not in out, out


class TestExitCodeAttributionIsPerRow:
    """Whose fault the run was is decided per check, not for the whole run.

    ⛔ The first version asked one global question — "was anything under
    `--config-dir` unreadable?" — and downgraded EVERY caller-error to exit
    1. So a genuine `bump_docs.py not found` from `versions`, which never
    opens anything in that tree, silently became "your config has a
    finding" because an unrelated tenant file was saved in the wrong code
    page. That is the same false attribution `_caveat_applies` exists to
    prevent, made in the exit code instead of in the text — measured as all
    four cells below.
    """

    @staticmethod
    def _tree(tmp_path, unreadable: bool):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        if unreadable:
            (d / "db-a.yaml").write_bytes(
                b"tenants:\n  db_a:\n    # \xb4\xfa\xb8\xd5\n    x: 1\n")
        return str(d)

    def _run(self, tmp_path, monkeypatch, capsys, cli_argv, *,
             unreadable, caller_error):
        if caller_error:
            monkeypatch.setattr(vc, "check_versions", lambda: vc._make_result(
                "versions", vc.FAIL, ["bump_docs.py not found"],
                caller_error=True))
        else:
            monkeypatch.setattr(vc, "check_versions", lambda: vc._make_result(
                "versions", vc.PASS, ["ok"]))
        cli_argv("validate_config", "--config-dir",
                 self._tree(tmp_path, unreadable), "--version-check")
        with pytest.raises(SystemExit) as exc:
            vc.main()
        capsys.readouterr()
        return exc.value.code

    @pytest.mark.parametrize("unreadable,caller_error,expected", [
        (False, False, 0),
        (False, True, 2),
        (True, False, 1),
        (True, True, 2),
    ], ids=["clean", "tool-broke", "bad-file", "bad-file+tool-broke"])
    def test_the_four_cells(self, unreadable, caller_error, expected,
                            tmp_path, monkeypatch, capsys, cli_argv):
        rc = self._run(tmp_path, monkeypatch, capsys, cli_argv,
                       unreadable=unreadable, caller_error=caller_error)
        assert rc == expected, (
            f"unreadable={unreadable} caller_error={caller_error}: got "
            f"exit {rc}, expected {expected}. `versions` never reads "
            f"--config-dir, so an unreadable file there cannot explain its "
            f"failure.")

    def test_a_caller_error_from_a_check_that_does_read_the_tree_is_downgraded(
            self, tmp_path, monkeypatch, capsys, cli_argv):
        """The other side of the same rule — without it, "never downgrade"
        passes every cell above and the customer's own encoding problem is
        reported as our bug again."""
        def boom(config_dir, *_a, **_kw):
            raise RuntimeError("downstream of the unreadable file")

        monkeypatch.setattr(vc, "check_routes", boom)
        rc = self._run(tmp_path, monkeypatch, capsys, cli_argv,
                       unreadable=True, caller_error=False)
        assert rc == 1, rc


class TestTheCaveatFollowsTheValueNotTheParameterName:
    """⛔ A customer-facing promise must not hang on an identifier.

    The first version answered "does this check read the config tree?" with
    ``"config_dir" in inspect.signature(fn).parameters``. Blind review
    renamed ``check_routes(config_dir, …)`` to ``conf_root`` — a pure
    rename, no behaviour change — and measured the ⚠️ caveat and the "fix
    the read errors first" line vanishing from that row, with the cheapest
    repair being to rename it back. A decorator without ``functools.wraps``
    collapses the signature the same way.
    """

    def test_a_renamed_parameter_keeps_the_caveat(self):
        def check_renamed(conf_root):
            return vc._make_result("routes", vc.PASS, ["ok"])

        row = vc._run_check("routes", check_renamed, "/some/dir",
                            _config_dir="/some/dir")
        assert row["reads_config_dir"] is True

    def test_a_decorated_check_keeps_the_caveat(self):
        def undecorated(config_dir):
            return vc._make_result("routes", vc.PASS, ["ok"])

        def wrap(fn):
            def inner(*a, **kw):        # deliberately no functools.wraps
                return fn(*a, **kw)
            return inner

        row = vc._run_check("routes", wrap(undecorated), "/some/dir",
                            _config_dir="/some/dir")
        assert row["reads_config_dir"] is True

    def test_a_check_handed_something_else_does_not_get_it(self):
        """Must-exclude control: without it, "always True" passes both
        cases above and `versions` is blamed for `conf.d/` again."""
        def check_versions():
            return vc._make_result("versions", vc.PASS, ["ok"])

        row = vc._run_check("versions", check_versions,
                            _config_dir="/some/dir")
        assert row["reads_config_dir"] is False

    def test_an_unknown_config_dir_keeps_the_caveat(self):
        """A caller that does not say gets the over-warning default: a
        missing caveat over-claims coverage, an extra one only over-warns."""
        row = vc._run_check("routes", lambda: vc._make_result(
            "routes", vc.PASS, ["ok"]))
        assert row["reads_config_dir"] is True


class TestTheDestructiveHintIsReplacedOnBothExits:
    """⛔ `check_schema` returns from two places and only one was wired.

    ``--strict`` plus an emptied ``defaults:`` plus any domain-policy
    violation takes the FAIL branch, where the generic "Remove unknown
    keys" survived — the exact advice the replacement exists to stop, on
    the path where the reader is most likely to be in a hurry.
    """

    @staticmethod
    def _tree(tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n\n_routing_defaults:\n"
            "  group_by: [tenant, alertname]\n  group_wait: 30s\n"
            "  repeat_interval: 4h\n  receiver:\n    type: slack\n"
            "    api_url: https://hooks.example/a\n", encoding="utf-8")
        (d / "_domain_policy.yaml").write_text(
            "domain_policies:\n  fin:\n    tenants: [db-a]\n"
            "    constraints:\n      forbidden_receiver_types: [slack]\n",
            encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db-a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        return str(d)

    def test_the_fail_branch_too(self, tmp_path, capsys, cli_argv):
        cli_argv("validate_config", "--config-dir", self._tree(tmp_path),
                 "--strict")
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "[FAIL] schema" in out, out
        assert _generic_schema_hint() not in out, (
            "the FAIL path still tells the customer to delete overrides "
            f"that are in force\n{out}")
        assert vc.NO_DECLARED_DEFAULTS_HINT.split(".")[0] in out, out


class TestUnwatchedHalvesFoundByMutation:
    """Layers a mutation sweep found with no control on them.

    Each of these survived a single-point weakening of the shipped code
    while the whole suite stayed green — which is the definition of a layer
    nobody is watching. The sweep ran 39 mutations; these are its survivors
    that turned out to be reachable.
    """

    def test_the_hint_counts_declared_optional_overrides_too(self, tmp_path):
        """⛔ A platform can declare keys without giving them values.

        ``optional_override_keys`` (#1189) is the platform's declared but
        valueless surface. Dropping it from the union survived the whole
        suite: a platform that declares only optional overrides would be
        told "This config declares no platform defaults at all … ⛔ Do NOT
        remove the keys named above" — the wrong advice, on the one path
        this helper exists to make right. Every other test here ships a
        `defaults:` block, so `defaults_keys` alone satisfied them.
        """
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "optional_overrides:\n  - mysql_threads_running\n",
            encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db_a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        assert vc._no_declared_defaults_hint(str(d)) is None, (
            "the platform DOES declare this key, just without a value — "
            "telling the customer nothing is declared is wrong here")

    def test_a_platform_declaring_nothing_at_all_still_gets_the_hint(
            self, tmp_path):
        """Must-fire control for the test above: without it, "always return
        None" would satisfy it and the destructive advice comes back."""
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text("# nothing\n", encoding="utf-8")
        assert vc._no_declared_defaults_hint(str(d)) == \
            vc.NO_DECLARED_DEFAULTS_HINT

    def test_config_dir_passed_by_keyword_is_recognised(self):
        """⛔ The kwargs half of the comparison was dead code.

        No dispatch passes ``config_dir=`` by keyword today, so deleting
        that half survived — and the docstring promises "comparing the
        argument survives both". A later
        ``_run_check("routes", check_routes, config_dir=…)`` would silently
        drop the ⚠️ caveat and the read-errors-first line from that row.
        """
        row = vc._run_check("routes",
                            lambda config_dir: vc._make_result(
                                "routes", vc.PASS, ["ok"]),
                            config_dir="/some/dir", _config_dir="/some/dir")
        assert row["reads_config_dir"] is True

    def test_the_caveat_truncates_a_long_file_list(self, tmp_path, capsys,
                                                   cli_argv):
        """⛔ ``unusable[:5]`` and its ``…`` had no fixture with six files.

        A conf.d where everything is unreadable is exactly when the report
        matters most, and an untruncated join puts an unbounded list inside
        every row.
        """
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        # ⛔ SEVEN, not six, and the assertion below is <= 5, not <= 6.
        # With six files the test could not fail: removing the truncation
        # leaves six names, and `count(".yaml") <= 5 + 1` accepts six. Six
        # was exactly one short of discriminating — measured.
        for i in range(7):
            (d / f"t{i}.yaml").write_bytes(b"tenants:\n  x: [1\n")
        cli_argv("validate_config", "--config-dir", str(d))
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "…" in out, out
        row = _report_row(out, "routes")
        assert row.count(".yaml") <= 5, (
            "the caveat is listing every unreadable file instead of the "
            f"first five\n{row}")

    def test_two_files_with_the_same_name_at_different_depths(self, tmp_path):
        """⛔ The relative-path label had no fixture.

        Since #1339 the scan is recursive, so two levels can hold the same
        `_defaults.yaml`; a bare filename makes the report name the same
        thing twice and the reader cannot tell which one to open.
        """
        d = tmp_path / "conf.d"
        (d / "team").mkdir(parents=True)
        (d / "_defaults.yaml").write_bytes(b"defaults: [1\n")
        (d / "team" / "_defaults.yaml").write_bytes(b"defaults: [2\n")
        result = vc.check_yaml_syntax(str(d))
        assert sorted(result["unusable_files"]) == [
            "_defaults.yaml", "team/_defaults.yaml"], result["unusable_files"]


class TestTheJsonDocumentCarriesNoInternalBookkeeping:
    """⛔ Everything published in `--json` becomes a contract.

    Measured on a clean `conf.d`: an earlier revision put
    `"unusable_files": []`, `"hint": null` and `"reads_config_dir": true` on
    every row of a perfectly healthy run (750 B → 970 B for nothing). All
    three are internal — they exist so the report can decide where the
    skipped-file caveat belongs and which advice line to print — and `hint`
    duplicates `suggested_action`, which the same function already emits.

    The version before that popped only `reads_config_dir`, under a comment
    stating the rule that the other two also broke.
    """

    @staticmethod
    def _json(tmp_path, cli_argv, capsys, broken=False):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db_a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        if broken:
            (d / "db-b.yaml").write_bytes(b"tenants:\n  db_b: [1\n")
        cli_argv("validate_config", "--config-dir", str(d), "--json")
        with pytest.raises(SystemExit):
            vc.main()
        return json.loads(capsys.readouterr().out)

    INTERNAL = ("reads_config_dir", "hint")

    def test_a_healthy_run_publishes_none_of_it(self, tmp_path, cli_argv,
                                                capsys):
        rows = self._json(tmp_path, cli_argv, capsys)
        assert rows, "no rows at all"
        for row in rows:
            leaked = sorted(k for k in self.INTERNAL if k in row)
            assert not leaked, (row["check"], leaked)
            assert "unusable_files" not in row, (
                f"{row['check']}: an empty bookkeeping list is still a "
                f"published key")

    def test_the_file_list_is_still_published_where_it_says_something(
            self, tmp_path, cli_argv, capsys):
        """Must-still-publish control: the point of dropping the empty ones
        is that the non-empty one keeps meaning something. Without this,
        popping the key unconditionally would satisfy the test above."""
        rows = self._json(tmp_path, cli_argv, capsys, broken=True)
        by = {r["check"]: r for r in rows}
        assert by["yaml_syntax"]["unusable_files"] == ["db-b.yaml"], rows
        # ⛔ NOT `all(... for r in rows if check != "yaml_syntax")`. That
        # form contradicts `_caveat_applies`, which deliberately withholds
        # the caveat from a check that never read the tree — measured: add
        # one correctly-classified check that takes no `config_dir` and the
        # assertion goes red, with the cheapest repair being to fake the
        # dispatch so the new row DOES carry it, i.e. print the false
        # attribution the contract exists to prevent. Assert the shape of
        # what is carried, plus that something carries it, and leave which
        # rows to the contract.
        carriers = {r["check"] for r in rows if "skipped_unusable_files" in r}
        # ⛔ An exact set, not `all(...)` over every non-`yaml_syntax` row and
        # not merely "somebody carries it".
        #   * `all(...)` contradicts `_caveat_applies`, which deliberately
        #     withholds the caveat from a check that never read the tree.
        #     Measured: add one correctly-classified check taking no
        #     `config_dir` and it goes red, the cheapest repair being to fake
        #     the dispatch so the new row DOES carry it — printing exactly the
        #     false attribution the contract exists to prevent.
        #   * "somebody carries it" is too weak the other way: drop the caveat
        #     from one row and the rest keep the assertion green.
        # An exact set fails in both directions. Adding a check that reads the
        # tree turns this red on purpose — add its name here, and only if it
        # really does read `--config-dir`.
        assert carriers == {
            "policy_dsl",
            "profiles",
            "routes",
            "schema",
            # #1577. It reads `--config-dir` and skips what it cannot parse,
            # so the caveat belongs on its row for the same reason as the
            # four above: without it the row asserts "no duplicate
            # declaration" about a tree it did not finish reading.
            "tenant_uniqueness",
        }, sorted(carriers)
        assert all(r["skipped_unusable_files"] == ["db-b.yaml"]
                   for r in rows if r["check"] in carriers), rows
        for row in rows:
            assert not [k for k in self.INTERNAL if k in row], row["check"]

    def test_the_advice_still_reaches_json_under_its_public_name(
            self, tmp_path, cli_argv, capsys):
        """`hint` was popped, not silenced.

        ⛔ Asserting only that `suggested_action` is non-empty proves
        nothing: every non-PASS row gets a non-empty value from the static
        `_CHECK_HINTS` table regardless. Measured — deleting the JSON
        branch's `hint` override silently reverted this field to the
        generic "Remove unknown keys", the very advice this branch exists
        to stop, and the suite stayed green. The fixture therefore has to be
        a tree with NO declared defaults, and the assertion has to be on the
        content.
        """
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text("# nothing declared\n",
                                          encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db_a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        cli_argv("validate_config", "--config-dir", str(d), "--json")
        with pytest.raises(SystemExit):
            vc.main()
        rows = json.loads(capsys.readouterr().out)
        schema = next(r for r in rows if r["check"] == "schema")
        assert schema["suggested_action"] == vc.NO_DECLARED_DEFAULTS_HINT, (
            schema.get("suggested_action"))
        assert _generic_schema_hint() not in schema["suggested_action"]


class TestACheckThatExitsTheProcessStillLeavesAReport:
    """⛔ `SystemExit` is not an `Exception`, and both catchers had to learn it.

    `_parse_config_files` calls `sys.exit(EXIT_CALLER_ERROR)` when the config
    directory has gone; `check_schema` / `check_routes` / `check_policy` all
    reach it, and so does `_no_declared_defaults_hint`. `main()` checks
    `isdir` first, so firing it needs a TOCTOU — a GitOps ConfigMap projected
    volume swaps `..data` atomically — and then the process dies before
    `print_report`: #1448's own symptom, through the wrapper written to stop
    it.

    Both catchers were fixed a round apart, and **neither fix had a test**:
    deleting `_run_check`'s clause, or narrowing the hint helper's back to
    `except Exception`, left the whole suite green. A fix nothing pins is a
    fix that leaves with the next refactor.
    """

    def test_run_check_turns_it_into_a_row(self):
        def boom():
            sys.exit(2)

        row = vc._run_check("schema", boom)
        assert row["status"] == vc.FAIL
        assert row["caller_error"] is True
        assert any("exited the process" in d for d in row["details"]), row

    def test_the_report_survives_it_end_to_end(self, tmp_path, monkeypatch,
                                               capsys, cli_argv):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")

        def gone(*_a, **_kw):
            sys.exit(2)

        monkeypatch.setattr(vc, "check_routes", gone)
        cli_argv("validate_config", "--config-dir", str(d))
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert "Unified Validation Report" in out, (
            f"a check exiting the process took the report with it "
            f"({len(out)} bytes on stdout)")
        assert exc.value.code == vc.EXIT_CALLER_ERROR, out

    def test_the_hint_helper_survives_it_too(self, tmp_path, monkeypatch):
        """The sibling catcher. Without this, narrowing it back to
        `except Exception` is invisible."""
        import generate_alertmanager_routes as gen

        def gone(_d):
            sys.exit(2)

        monkeypatch.setattr(gen, "_parse_config_files", gone)
        assert vc._no_declared_defaults_hint(str(tmp_path)) is None

    def test_a_check_that_returns_normally_is_untouched(self):
        """Must-succeed control: catching SystemExit must not swallow a
        healthy return, which is what "always build a row" would look like."""
        sentinel = vc._make_result("routes", vc.PASS, ["all good"])
        assert vc._run_check("routes", lambda: sentinel) is sentinel


class TestTheSchemaRowDoesNotAdviseDeletingKeysItNeverReported:
    """⛔ `check_schema` is a multiplexer; the advice line is keyed on its name.

    Its details carry unknown-key WARNs *and* ADR-007 policy ERRORs, so a run
    whose only finding is a broken `_domain_policy.yaml` — zero keys reported
    — still ended with "Remove unknown keys or add them to the schema". That
    is the destructive advice this branch exists to stop, on a path that has
    nothing to do with keys.
    """

    @staticmethod
    def _tree(tmp_path, policy_body):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_threads_running: 80\n", encoding="utf-8")
        (d / "db-a.yaml").write_text(
            "tenants:\n  db-a:\n    mysql_threads_running: 90\n",
            encoding="utf-8")
        (d / "_domain_policy.yaml").write_text(policy_body, encoding="utf-8")
        return str(d)

    def test_a_policy_only_failure_gets_policy_advice(self, tmp_path, capsys,
                                                      cli_argv):
        cli_argv("validate_config", "--config-dir",
                 self._tree(tmp_path, "domain_policies:\n"), "--strict")
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "[FAIL] schema" in out, out
        assert _generic_schema_hint() not in out, (
            "the row reported no keys at all and still told the customer to "
            f"remove some\n{out}")
        assert vc.POLICY_ONLY_SCHEMA_HINT in out, out

    def test_an_unknown_key_still_gets_key_advice(self, tmp_path, capsys,
                                                  cli_argv):
        """Must-still-fire control: when the row DOES report keys, the
        generic advice is the right one. Without this, suppressing it
        unconditionally would pass the test above."""
        d = self._tree(
            tmp_path,
            "domain_policies:\n  fin:\n    tenants: [db-a]\n")
        (pathlib.Path(d) / "db-b.yaml").write_text(
            "tenants:\n  db-b:\n    mysql_thredas_running: 90\n",
            encoding="utf-8")
        cli_argv("validate_config", "--config-dir", d)
        with pytest.raises(SystemExit):
            vc.main()
        out = capsys.readouterr().out
        assert "unknown key" in out, out
        assert _generic_schema_hint() in out, out
        assert vc.POLICY_ONLY_SCHEMA_HINT not in out, out


class TestTenantUniqueness:
    """Check 9 (#1577): one tenant id declared by two files.

    The exporter answers this with a hard reject of the WHOLE config dir
    (``DuplicateTenantError`` -> ``config rejected (mixed-mode duplicate
    tenant)``), so a tree in this state loses alerting for every tenant, not
    just the duplicated one. Before this check the same tree came out of this
    command as ``Result: PASS`` / exit 0, naming neither file.

    ⚠️ Every PASS assertion below is paired with a case where the same helper
    reports, so "no findings" cannot be satisfied by the check quietly
    scanning less: ``test_one_file_per_tenant_passes`` is only meaningful
    beside ``test_two_spellings_of_one_stem`` and
    ``test_nested_directories_are_walked``.
    """

    _A = "tenants:\n  shared:\n    mysql_connections: 11\n"
    _B = "tenants:\n  shared:\n    mysql_connections: 99\n"

    @staticmethod
    def _tree(tmp_path, files):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 70\n", encoding="utf-8")
        for name, body in files.items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return str(d)

    def test_one_file_per_tenant_passes(self, tmp_path):
        d = self._tree(tmp_path, {
            "a.yaml": self._A,
            "b.yaml": "tenants:\n  other:\n    mysql_connections: 33\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r
        assert "2 tenant(s)" in " ".join(r["details"]), r

    def test_two_spellings_of_one_stem(self, tmp_path):
        """The accident this shipped as: an editor leaving a ``.yml`` behind."""
        d = self._tree(tmp_path, {"same.yaml": self._A, "same.yml": self._B})
        # The fixture really produced two files. Windows folds CASE, not the
        # extension, so this stays a two-file tree on every platform.
        assert len(list(pathlib.Path(d).glob("same.*"))) == 2
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.FAIL, r
        detail = " ".join(r["details"])
        assert "same.yaml" in detail and "same.yml" in detail, detail
        assert "shared" in detail, detail

    def test_nested_directories_are_walked(self, tmp_path):
        """⛔ Also the predicate test: no extension is involved here, and the
        exporter rejects this tree exactly as hard. A version of this check
        keyed on ``.yaml`` vs ``.yml`` would call it clean."""
        d = self._tree(tmp_path, {"x.yaml": self._A, "archive/y.yaml": self._B})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.FAIL, r
        assert "archive/y.yaml" in " ".join(r["details"]), r

    def test_uppercase_extension_counts(self, tmp_path):
        """Selection is case-insensitive because the exporter's walker is."""
        d = self._tree(tmp_path, {"lower.yaml": self._A, "upper.YAML": self._B})
        names = sorted(p.name for p in pathlib.Path(d).iterdir() if p.is_file())
        assert "upper.YAML" in names, names
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.FAIL, r

    def test_a_lone_yml_tenant_is_seen(self, tmp_path):
        """Discriminating control for the both-spellings selection: if ``.yml``
        were invisible here this would report zero tenants and still say
        PASS."""
        d = self._tree(tmp_path, {"solo.yml": self._A})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r
        assert "1 tenant(s)" in " ".join(r["details"]), r

    def test_reserved_files_do_not_declare_tenants(self, tmp_path):
        """False-positive guard mirroring the walker's ``_``-prefix gate: it
        returns before parsing ``tenants:`` in a reserved file at all."""
        d = self._tree(tmp_path, {
            "t.yaml": self._A,
            "_profiles.yaml": "tenants:\n  shared:\n    mysql_connections: 1\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r

    def test_hidden_files_do_not_declare_tenants(self, tmp_path):
        """Same shape for the dot-prefix skip — an editor backup beside a
        tenant file must not turn a valid tree red."""
        d = self._tree(tmp_path, {"a.yaml": self._A, ".a.backup.yaml": self._B})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r

    def test_unreadable_file_is_a_coverage_limit_not_a_pass(self, tmp_path):
        """"Could not examine" and "examined, found nothing" are different
        answers, and the second declaration can be hiding in the file that
        will not parse."""
        d = self._tree(tmp_path, {
            "a.yaml": self._A, "bad.yaml": "tenants:\n  x: [unclosed\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.WARN, r
        detail = " ".join(r["details"])
        assert "bad.yaml" in detail, detail
        assert "not a clean result" in detail, detail
        # ⛔ Its own advice. `_CHECK_HINTS` is keyed by check name, so this row
        # used to print the FAIL advice — "decide which single file owns each
        # tenant listed above" — directly under a row saying nothing is listed.
        assert "listed above" not in (r.get("hint") or ""), r
        assert "could not open" in (r.get("hint") or ""), r

    def test_message_does_not_prescribe_deleting_a_file(self, tmp_path):
        """The cheapest way to a green run is to delete one of the two files
        at random, which silently drops whatever only that file declared. The
        row must not name that as the move — a guard whose message recommends
        the worse fix gets dismantled by the people who follow it.

        ⛔ BOTH CARRIERS, and the first version of this pinned only the first.
        Blind review swapped the ``_CHECK_HINTS`` entry for "Delete the
        duplicate file to make this green." and the whole file stayed green
        (143 passed) — while that string is what the report prints under
        ``-> Suggested action:``, i.e. the line an operator actually reads as
        the instruction. The detail row was pinned and the instruction was
        not."""
        d = self._tree(tmp_path, {"same.yaml": self._A, "same.yml": self._B})
        detail = " ".join(vc.check_tenant_uniqueness(d)["details"]).lower()
        assert "delete" not in detail, detail
        assert "decision this tool cannot make" in detail, detail
        hint = vc._CHECK_HINTS["tenant_uniqueness"][0].lower()
        assert "delete" not in hint, hint

    def test_end_to_end_exits_1_and_prints_both_files(self, tmp_path, capsys,
                                                      cli_argv):
        """Wired into ``main()``, not merely importable: the defect this closes
        is that the command printed PASS on exactly this tree."""
        d = self._tree(tmp_path, {"same.yaml": self._A, "same.yml": self._B})
        cli_argv("validate_config", "--config-dir", d)
        with pytest.raises(SystemExit) as exc:
            vc.main()
        out = capsys.readouterr().out
        assert exc.value.code == 1, out
        assert "tenant_uniqueness" in out, out
        assert "same.yml" in out, out
        # Whole printed page, so a future carrier (a new hint line, a caveat,
        # a `-> See:` blurb) is covered without anyone remembering to add it.
        assert "delete" not in out.lower(), out


class TestTenantIdParity:
    """What counts as ONE tenant id has to be what the exporter counts.

    PyYAML implements YAML **1.1** implicit typing; the exporter's
    ``gopkg.in/yaml.v3`` keys a ``map[string]...`` on the scalar's text. Left
    alone, ``yaml.safe_load`` disagrees with the oracle in BOTH directions and
    silently — the first version of check 9 shipped with both, and two blind
    reviewers on different lenses found them independently.

    The Go column below was measured on this repo's own module (it needs no
    container: ``components/threshold-exporter/app`` is a standalone module),
    with this program — re-run it rather than trusting this docstring::

        var doc struct{ Tenants map[string]yaml.Node `yaml:"tenants"` }
        yaml.Unmarshal([]byte(src), &doc)   // then print sorted keys

        src                                          -> Go keys
        "tenants:\\n on: {}\\n yes: {}\\n true: {}\\n True: {}\\n 010: {}\\n null: {}\\n"
                                                     -> ["010" "True" "on" "true" "yes"]
        "common: &c\\n  db-m: {}\\ntenants:\\n  <<: *c\\n  db-x: {}\\n"
                                                     -> ["db-m" "db-x"]   (merge expanded)
        "tenants:\\n  null: {}\\n  b: {}\\n"           -> ["b"]             (null key dropped)

    ⚠️ One measured divergence is deliberately NOT closed here: Go rejects a
    file that repeats the ``tenants:`` key outright, where PyYAML takes the
    last block. That is a whole-file parse verdict and belongs to
    ``yaml_syntax``; it is written down so it does not have to be rediscovered.
    """

    @staticmethod
    def _tree(tmp_path, files):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 70\n", encoding="utf-8")
        for name, body in files.items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return str(d)

    def test_quoting_a_bareword_id_does_not_make_it_a_different_tenant(
            self, tmp_path):
        """The MISS direction. ``no:`` and ``"no":`` are one id to the exporter
        — it rejects the whole dir — and were two to ``safe_load``, so the
        check said PASS about a tree that cannot be deployed. This is the most
        natural way to reach the state at all: two people writing the same
        tenant, one of them quoting."""
        d = self._tree(tmp_path, {
            "x.yaml": "tenants:\n  no:\n    mysql_connections: 1\n",
            "archive/y.yaml": "tenants:\n  \"no\":\n    mysql_connections: 2\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.FAIL, r
        detail = " ".join(r["details"])
        assert 'tenant "no"' in detail, detail

    def test_yaml_11_booleans_are_distinct_tenants(self, tmp_path):
        """The FALSE-RED direction. ``on``/``yes``/``true``/``True`` are four
        ids to the exporter and were one (``True``) to ``safe_load`` — a FAIL
        naming a tenant that appears in none of the four files, so the operator
        could not even grep for it."""
        d = self._tree(tmp_path, {
            "f1.yaml": "tenants:\n  on:\n    mysql_connections: 1\n",
            "f2.yaml": "tenants:\n  yes:\n    mysql_connections: 2\n",
            "f3.yaml": "tenants:\n  true:\n    mysql_connections: 3\n",
            "f4.yaml": "tenants:\n  True:\n    mysql_connections: 4\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r
        assert "4 tenant(s)" in " ".join(r["details"]), r

    def test_a_leading_zero_id_keeps_its_zero(self, tmp_path):
        """``010`` is octal to YAML 1.1 — ``safe_load`` renamed it to ``8``."""
        d = self._tree(tmp_path, {
            "a.yaml": "tenants:\n  010:\n    mysql_connections: 1\n",
            "b.yaml": "tenants:\n  8:\n    mysql_connections: 2\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r

    def test_merge_keys_are_expanded_because_the_exporter_expands_them(
            self, tmp_path):
        """⛔ Why this is a loader subclass and not a ``yaml.compose`` walk:
        compose would report a tenant literally named ``<<`` and miss the ids
        the anchor actually contributes — measured, Go yields the anchor's
        ids."""
        d = self._tree(tmp_path, {
            "a.yaml": "common: &c\n  db-m: {}\ntenants:\n  <<: *c\n  db-x: {}\n",
            "b.yaml": "tenants:\n  db-m:\n    mysql_connections: 2\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.FAIL, r
        assert 'tenant "db-m"' in " ".join(r["details"]), r

    def test_a_null_key_is_dropped_because_the_exporter_drops_it(self, tmp_path):
        """Must-stay-green control for the same loader: without the null rule
        two trees each carrying a ``null:`` key would read as a duplicate the
        exporter never sees."""
        d = self._tree(tmp_path, {
            "a.yaml": "tenants:\n  null:\n    m: 1\n  b:\n    m: 1\n",
            "c.yaml": "tenants:\n  null:\n    m: 2\n  d:\n    m: 2\n"})
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r

    def test_the_loader_cannot_construct_python_objects(self):
        """⛔ The safety property, measured — not the spelling of the call.

        The custom loader exists to change how mapping KEYS are read; it must
        not have widened what YAML is allowed to construct.
        ``tests/shared/test_sast.py::TestNoUnsafeYamlLoad`` cannot answer that:
        it accepts a ``yaml.load`` call only when ``Loader=`` is written as
        ``<something>.SafeLoader``, so a subclass is indistinguishable to it
        from ``yaml.UnsafeLoader``. This feeds the real payload instead."""
        import io

        assert issubclass(vc._ExporterKeyLoader, yaml.SafeLoader)
        payload = "tenants:\n  t: !!python/object/apply:os.system ['echo pwned']\n"
        with pytest.raises(yaml.YAMLError):
            vc._load_with_exporter_keys(io.StringIO(payload))
        # Must-still-work control: an ordinary document still loads, so the
        # assertion above cannot be satisfied by a loader that refuses
        # everything.
        ok = vc._load_with_exporter_keys(io.StringIO("tenants:\n  t: {a: 1}\n"))
        assert ok == {"tenants": {"t": {"a": 1}}}, ok

    def test_a_defaults_carrier_declaring_a_tenant_is_still_not_a_declaration(
            self, tmp_path):
        """The reserved-name rule stated on the shape the docstring names.
        The sibling test in ``TestTenantUniqueness`` uses ``_profiles.yaml``;
        ``_defaults.yaml`` is the one the prose talks about, and it had no
        pin of its own."""
        d = self._tree(tmp_path, {"t.yaml": "tenants:\n  shared:\n    m: 1\n"})
        (pathlib.Path(d) / "_defaults.yaml").write_text(
            "defaults:\n  mysql_connections: 70\ntenants:\n  shared:\n    m: 9\n",
            encoding="utf-8")
        r = vc.check_tenant_uniqueness(d)
        assert r["status"] == vc.PASS, r
