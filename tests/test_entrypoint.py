#!/usr/bin/env python3
"""Tests for da-tools entrypoint.py — CLI dispatcher logic.

Covers:
- COMMAND_MAP completeness vs build.sh / release-tools.yaml
- inject_prometheus_env() env-var fallback
- print_usage() / --version output
- run_tool() error handling
- main() routing
"""

import os
import re
import sys
import unittest

# Make entrypoint importable
DA_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), os.pardir,
    "components", "da-tools", "app",
)
sys.path.insert(0, os.path.abspath(DA_TOOLS_DIR))

import entrypoint  # noqa: E402


# ── Command Map Consistency ────────────────────────────────────────


class TestCommandMapConsistency(unittest.TestCase):
    """COMMAND_MAP must cover every tool listed in build.sh / CI workflow."""

    # Tools that build.sh copies (excluding metric-dictionary.yaml)
    BUILD_SH_TOOLS = {
        "check_alert.py",
        "baseline_discovery.py",
        "validate_migration.py",
        "migrate_rule.py",
        "scaffold_tenant.py",
        "offboard_tenant.py",
        "deprecate_rule.py",
        "lint_custom_rules.py",
    }

    def test_command_map_covers_build_tools(self):
        """Every .py in build.sh TOOL_FILES has a COMMAND_MAP entry."""
        mapped_scripts = set(entrypoint.COMMAND_MAP.values())
        missing = self.BUILD_SH_TOOLS - mapped_scripts
        self.assertEqual(missing, set(),
                         f"Scripts in build.sh but not in COMMAND_MAP: {missing}")

    def test_command_map_values_are_py_files(self):
        """Every COMMAND_MAP value ends with .py."""
        for cmd, script in entrypoint.COMMAND_MAP.items():
            self.assertTrue(script.endswith(".py"),
                            f"Command '{cmd}' maps to non-.py: {script}")

    def test_prometheus_commands_subset_of_map(self):
        """PROMETHEUS_COMMANDS only references valid commands."""
        invalid = entrypoint.PROMETHEUS_COMMANDS - set(entrypoint.COMMAND_MAP.keys())
        self.assertEqual(invalid, set(),
                         f"PROMETHEUS_COMMANDS references unknown commands: {invalid}")


# ── inject_prometheus_env ──────────────────────────────────────────


class TestInjectPrometheusEnv(unittest.TestCase):
    """inject_prometheus_env() inserts PROMETHEUS_URL when --prometheus absent."""

    def test_injects_when_env_set_and_no_flag(self):
        """When PROMETHEUS_URL is set and --prometheus not in args, inject it."""
        os.environ["PROMETHEUS_URL"] = "http://test:9090"
        try:
            args = ["--tenant", "db-a"]
            result = entrypoint.inject_prometheus_env(args)
            self.assertIn("--prometheus", result)
            self.assertIn("http://test:9090", result)
        finally:
            del os.environ["PROMETHEUS_URL"]

    def test_no_inject_when_flag_present(self):
        """When --prometheus already in args, don't inject."""
        os.environ["PROMETHEUS_URL"] = "http://test:9090"
        try:
            args = ["--prometheus", "http://custom:9090", "--tenant", "db-a"]
            result = entrypoint.inject_prometheus_env(args)
            # Should NOT have duplicated --prometheus
            count = result.count("--prometheus")
            self.assertEqual(count, 1)
        finally:
            del os.environ["PROMETHEUS_URL"]

    def test_no_inject_when_env_unset(self):
        """When PROMETHEUS_URL not set, leave args unchanged."""
        os.environ.pop("PROMETHEUS_URL", None)
        args = ["--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        self.assertNotIn("--prometheus", result)

    def test_returns_same_list_reference(self):
        """inject_prometheus_env modifies and returns the same list."""
        os.environ.pop("PROMETHEUS_URL", None)
        args = ["--tenant", "db-a"]
        result = entrypoint.inject_prometheus_env(args)
        self.assertIs(result, args)


# ── Version Display ────────────────────────────────────────────────


class TestVersionDisplay(unittest.TestCase):
    """--version reads from VERSION file."""

    def test_version_file_exists(self):
        """VERSION file must exist in da-tools app directory."""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        self.assertTrue(os.path.isfile(version_path),
                        f"VERSION file not found at {version_path}")

    def test_version_is_semver(self):
        """VERSION content must be a valid semver string."""
        version_path = os.path.join(DA_TOOLS_DIR, "VERSION")
        with open(version_path, encoding="utf-8") as f:
            ver = f.read().strip()
        self.assertRegex(ver, r"^[0-9]+\.[0-9]+\.[0-9]+$",
                         f"VERSION '{ver}' is not valid semver")


# ── run_tool error handling ────────────────────────────────────────


class TestRunToolErrors(unittest.TestCase):
    """run_tool() exits on missing script."""

    def test_missing_script_exits(self):
        """run_tool with nonexistent script should sys.exit(1)."""
        with self.assertRaises(SystemExit) as ctx:
            entrypoint.run_tool("nonexistent_tool_xyz.py", [])
        self.assertEqual(ctx.exception.code, 1)


# ── print_usage ────────────────────────────────────────────────────


class TestPrintUsage(unittest.TestCase):
    """print_usage() exits with code 0."""

    def test_usage_exits_zero(self):
        """print_usage should sys.exit(0)."""
        with self.assertRaises(SystemExit) as ctx:
            entrypoint.print_usage()
        self.assertEqual(ctx.exception.code, 0)


# ── CI Workflow Sync ───────────────────────────────────────────────


class TestCIWorkflowSync(unittest.TestCase):
    """release-tools.yaml TOOLS array must match build.sh TOOL_FILES."""

    def _parse_tools_from_file(self, filepath, start_marker, end_marker):
        """Extract tool filenames from a script between markers."""
        tools = []
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
        in_block = False
        for line in content.splitlines():
            stripped = line.strip()
            if start_marker in stripped:
                in_block = True
                continue
            if in_block and end_marker in stripped:
                break
            if in_block and stripped and not stripped.startswith("#"):
                # Remove trailing quotes, parens, commas
                name = stripped.strip("\"'(),")
                if name.endswith(".py") or name.endswith(".yaml"):
                    tools.append(name)
        return set(tools)

    def test_ci_matches_build_sh(self):
        """release-tools.yaml TOOLS must be superset of build.sh TOOL_FILES."""
        repo_root = os.path.join(os.path.dirname(__file__), os.pardir)
        build_sh = os.path.join(repo_root, "components", "da-tools", "app", "build.sh")
        ci_yaml = os.path.join(repo_root, ".github", "workflows", "release-tools.yaml")

        if not os.path.isfile(build_sh) or not os.path.isfile(ci_yaml):
            self.skipTest("build.sh or release-tools.yaml not found")

        build_tools = self._parse_tools_from_file(build_sh, "TOOL_FILES=(", ")")
        ci_tools = self._parse_tools_from_file(ci_yaml, "TOOLS=(", ")")

        missing_in_ci = build_tools - ci_tools
        self.assertEqual(missing_in_ci, set(),
                         f"Tools in build.sh but missing from CI workflow: {missing_in_ci}")


# ── bump_docs tools rule coverage ──────────────────────────────────


class TestBumpDocsToolsRuleCoverage(unittest.TestCase):
    """bump_docs.py tools_rules must cover da-tools README header version."""

    def test_readme_header_rule_exists(self):
        """bump_docs tools_rules must include da-tools README version header."""
        tools_dir = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "tools")
        sys.path.insert(0, os.path.abspath(tools_dir))
        try:
            import bump_docs
            rules = bump_docs._build_rules()
            tools_descs = [r["desc"] for r in rules["tools"]]
            header_rules = [d for d in tools_descs if "version header" in d.lower()]
            self.assertGreaterEqual(len(header_rules), 1,
                                   "No bump_docs rule covers da-tools README version header")
        finally:
            sys.path.pop(0)


# ── main() routing ─────────────────────────────────────────────────


class TestMainRouting(unittest.TestCase):
    """main() routes commands or exits on error."""

    def test_unknown_command_exits(self):
        """Unknown command should sys.exit(1)."""
        orig_argv = sys.argv
        try:
            sys.argv = ["da-tools", "nonexistent-xyz"]
            with self.assertRaises(SystemExit) as ctx:
                entrypoint.main()
            self.assertEqual(ctx.exception.code, 1)
        finally:
            sys.argv = orig_argv

    def test_help_exits_zero(self):
        """--help should sys.exit(0)."""
        orig_argv = sys.argv
        try:
            sys.argv = ["da-tools", "--help"]
            with self.assertRaises(SystemExit) as ctx:
                entrypoint.main()
            self.assertEqual(ctx.exception.code, 0)
        finally:
            sys.argv = orig_argv


if __name__ == "__main__":
    unittest.main()
