"""Tests for scripts/ops/ci_flake_retry.py — surgical Go-test retry wrapper.

Covers:
  - flaky-tests.yaml parsing (load_registry)
  - Go test output parsing (parse_failing_tests)
  - flake-vs-regression classification (classify_failures)
  - argv splitting (split_args) — both `--` separator and bare flags
  - end-to-end retry decision (main) via mocked subprocess
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Load ci_flake_retry as a module (lives under scripts/ops/, not on sys.path
# for tests/ by default; conftest already inserts scripts/tools/ but not
# scripts/ops/).
REPO_ROOT = Path(__file__).resolve().parents[2]
_RETRY_SCRIPT = REPO_ROOT / "scripts" / "ops" / "ci_flake_retry.py"

_spec = importlib.util.spec_from_file_location("ci_flake_retry", _RETRY_SCRIPT)
ci_retry = importlib.util.module_from_spec(_spec)
sys.modules["ci_flake_retry"] = ci_retry
_spec.loader.exec_module(ci_retry)


# ============================================================
# load_registry
# ============================================================

class TestLoadRegistry:

    def test_missing_file_returns_empty(self, tmp_path):
        """Missing registry file → empty list (degrades to pass-through)."""
        assert ci_retry.load_registry(tmp_path / "nope.yaml") == []

    def test_parses_well_formed_entry(self, tmp_path):
        """Single entry with all fields populates FlakeEntry correctly."""
        f = tmp_path / "flakes.yaml"
        f.write_text(
            "known_flakes:\n"
            "  - test: TestX\n"
            "    pattern: ^TestX$\n"
            "    max_retries: 3\n"
            "    owner: '@team'\n"
            "    tracked_by: 'HA-N'\n"
            "    expire_at: v2.9.0\n",
            encoding="utf-8",
        )
        entries = ci_retry.load_registry(f)
        assert len(entries) == 1
        e = entries[0]
        assert e.test == "TestX"
        assert e.pattern == "^TestX$"
        assert e.max_retries == 3
        assert e.owner == "@team"

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty YAML → empty list."""
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        assert ci_retry.load_registry(f) == []

    def test_no_known_flakes_key_returns_empty(self, tmp_path):
        """YAML without `known_flakes:` key → empty list."""
        f = tmp_path / "noflakes.yaml"
        f.write_text("other_key: value\n", encoding="utf-8")
        assert ci_retry.load_registry(f) == []

    def test_skips_non_dict_entries(self, tmp_path):
        """Entries that aren't dicts are silently skipped."""
        f = tmp_path / "bad.yaml"
        f.write_text(
            "known_flakes:\n"
            "  - 'string-not-dict'\n"
            "  - test: Real\n"
            "    pattern: ^Real$\n"
            "    max_retries: 1\n"
            "    owner: o\n"
            "    tracked_by: t\n"
            "    expire_at: v2.9.0\n",
            encoding="utf-8",
        )
        entries = ci_retry.load_registry(f)
        assert len(entries) == 1
        assert entries[0].test == "Real"

    def test_pyyaml_missing_degrades_to_empty(self, tmp_path, capsys):
        """PyYAML ImportError → empty registry + stderr advisory.

        Defends the CI activation contract: missing pyyaml shouldn't crash
        the wrapper; the wrapper degrades to pure pass-through and the
        env-setup error is reported to stderr where CI logs catch it.
        """
        f = tmp_path / "flakes.yaml"
        f.write_text(
            "known_flakes:\n  - test: T\n    pattern: ^T$\n"
            "    max_retries: 1\n    owner: o\n    tracked_by: t\n"
            "    expire_at: v2.9.0\n",
            encoding="utf-8",
        )
        # Force ImportError on the local `import yaml` inside load_registry.
        # `sys.modules['yaml'] = None` makes `import yaml` raise ImportError
        # without unloading the real module from other contexts.
        with patch.dict(sys.modules, {"yaml": None}):
            entries = ci_retry.load_registry(f)
        assert entries == []
        captured = capsys.readouterr()
        assert "pyyaml not installed" in captured.err


# ============================================================
# parse_failing_tests
# ============================================================

class TestParseFailingTests:

    def test_extracts_top_level_fail(self):
        out = "--- FAIL: TestAlpha (0.05s)\n"
        assert ci_retry.parse_failing_tests(out) == ["TestAlpha"]

    def test_extracts_subtest_fail(self):
        out = "    --- FAIL: TestAlpha/subA (0.02s)\n"
        assert ci_retry.parse_failing_tests(out) == ["TestAlpha/subA"]

    def test_mixed_pass_and_fail(self):
        out = (
            "--- PASS: TestPasses (0.01s)\n"
            "--- FAIL: TestFails1 (0.10s)\n"
            "--- PASS: TestPasses2 (0.01s)\n"
            "--- FAIL: TestFails2 (0.20s)\n"
        )
        assert ci_retry.parse_failing_tests(out) == ["TestFails1", "TestFails2"]

    def test_empty_output(self):
        assert ci_retry.parse_failing_tests("") == []

    def test_no_failures(self):
        out = "ok  github.com/x/y  0.123s\n"
        assert ci_retry.parse_failing_tests(out) == []


# ============================================================
# classify_failures
# ============================================================

@pytest.fixture
def alpha_entry():
    return ci_retry.FlakeEntry(
        test="TestAlpha", pattern="^TestAlpha$",
        max_retries=2, owner="@team", tracked_by="HA-N",
        expire_at="v2.9.0",
    )


class TestClassifyFailures:

    def test_known_flake_matched(self, alpha_entry):
        matched, unmatched = ci_retry.classify_failures(
            ["TestAlpha"], [alpha_entry])
        assert [n for n, _ in matched] == ["TestAlpha"]
        assert unmatched == []

    def test_unknown_test_unmatched(self, alpha_entry):
        matched, unmatched = ci_retry.classify_failures(
            ["TestNew"], [alpha_entry])
        assert matched == []
        assert unmatched == ["TestNew"]

    def test_subtest_matches_via_parent(self, alpha_entry):
        """`TestAlpha/case1` should match registry entry for `TestAlpha`."""
        matched, unmatched = ci_retry.classify_failures(
            ["TestAlpha/case1"], [alpha_entry])
        assert [n for n, _ in matched] == ["TestAlpha/case1"]
        assert unmatched == []

    def test_partial_match_classification(self, alpha_entry):
        """One known + one unknown → split correctly."""
        matched, unmatched = ci_retry.classify_failures(
            ["TestAlpha", "TestNew"], [alpha_entry])
        assert [n for n, _ in matched] == ["TestAlpha"]
        assert unmatched == ["TestNew"]

    def test_anchored_pattern_rejects_prefix(self, alpha_entry):
        """`^TestAlpha$` should NOT match `TestAlphaBeta`."""
        matched, unmatched = ci_retry.classify_failures(
            ["TestAlphaBeta"], [alpha_entry])
        assert matched == []
        assert unmatched == ["TestAlphaBeta"]


# ============================================================
# split_args
# ============================================================

class TestSplitArgs:

    def test_explicit_separator(self):
        s, c = ci_retry.split_args(["--verbose", "--", "go", "test"])
        assert s == ["--verbose"]
        assert c == ["go", "test"]

    def test_bare_self_test_flag(self):
        """`--self-test` alone should be recognized as a script flag."""
        s, c = ci_retry.split_args(["--self-test"])
        assert s == ["--self-test"]
        assert c == []

    def test_registry_with_value(self):
        s, c = ci_retry.split_args(["--registry", "foo.yaml", "go", "test"])
        assert s == ["--registry", "foo.yaml"]
        assert c == ["go", "test"]

    def test_registry_equals_value(self):
        s, c = ci_retry.split_args(["--registry=foo.yaml", "go", "test"])
        assert s == ["--registry=foo.yaml"]
        assert c == ["go", "test"]

    def test_no_script_flags_all_command(self):
        s, c = ci_retry.split_args(["go", "test", "./..."])
        assert s == []
        assert c == ["go", "test", "./..."]


# ============================================================
# main — end-to-end retry decisions via mocked subprocess
# ============================================================

class TestMainEndToEnd:

    def test_pass_returns_zero(self, tmp_path):
        """Command succeeds first time → exit 0, no retries attempted."""
        registry = tmp_path / "flakes.yaml"
        registry.write_text("known_flakes: []\n", encoding="utf-8")
        with patch.object(ci_retry, "run_command", return_value=(0, "ok\n", "")):
            rc = ci_retry.main([
                "--registry", str(registry), "--", "go", "test", "./...",
            ])
        assert rc == 0

    def test_unmatched_failure_passes_through(self, tmp_path):
        """Failure in test not in registry → exit with original rc, no retry."""
        registry = tmp_path / "flakes.yaml"
        registry.write_text("known_flakes: []\n", encoding="utf-8")
        out = "--- FAIL: TestNew (0.01s)\n"
        with patch.object(ci_retry, "run_command", return_value=(1, out, "")):
            rc = ci_retry.main([
                "--registry", str(registry), "--", "go", "test",
            ])
        assert rc == 1

    def test_matched_flake_recovers(self, tmp_path):
        """Flake fails first run, passes on retry → exit 0."""
        registry = tmp_path / "flakes.yaml"
        registry.write_text(
            "known_flakes:\n"
            "  - test: TestFlaky\n"
            "    pattern: ^TestFlaky$\n"
            "    max_retries: 2\n"
            "    owner: o\n"
            "    tracked_by: t\n"
            "    expire_at: v2.9.0\n",
            encoding="utf-8",
        )
        first_run = "--- FAIL: TestFlaky (0.01s)\n"
        retry_run = "--- PASS: TestFlaky (0.01s)\n"
        # Sequence: first run fails, retry succeeds
        with patch.object(
            ci_retry, "run_command",
            side_effect=[(1, first_run, ""), (0, retry_run, "")],
        ):
            rc = ci_retry.main([
                "--registry", str(registry), "--", "go", "test",
            ])
        assert rc == 0

    def test_matched_flake_persistent_fail(self, tmp_path):
        """Flake fails AND retry budget exhausted → exit 1."""
        registry = tmp_path / "flakes.yaml"
        registry.write_text(
            "known_flakes:\n"
            "  - test: TestFlaky\n"
            "    pattern: ^TestFlaky$\n"
            "    max_retries: 2\n"
            "    owner: o\n"
            "    tracked_by: t\n"
            "    expire_at: v2.9.0\n",
            encoding="utf-8",
        )
        fail = "--- FAIL: TestFlaky (0.01s)\n"
        # First run + 2 retries all fail
        with patch.object(
            ci_retry, "run_command",
            side_effect=[(1, fail, ""), (1, fail, ""), (1, fail, "")],
        ):
            rc = ci_retry.main([
                "--registry", str(registry), "--", "go", "test",
            ])
        assert rc == 1

    def test_no_command_returns_two(self):
        rc = ci_retry.main([])
        assert rc == 2

    def test_self_test_runs(self):
        """--self-test invokes inline doctest harness; returns 0 on pass."""
        rc = ci_retry.main(["--self-test"])
        assert rc == 0

    def test_build_failure_no_fail_lines_passes_through(self, tmp_path):
        """Non-zero exit but no `--- FAIL:` lines (e.g. build error) → pass through."""
        registry = tmp_path / "flakes.yaml"
        registry.write_text("known_flakes: []\n", encoding="utf-8")
        build_err = "package x: cannot find module providing X\n"
        with patch.object(ci_retry, "run_command", return_value=(1, build_err, "")):
            rc = ci_retry.main([
                "--registry", str(registry), "--", "go", "build", "./...",
            ])
        # Original exit code preserved; no false "all flakes recovered" return
        assert rc == 1
