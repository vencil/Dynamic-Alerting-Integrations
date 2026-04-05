"""Snapshot tests for tool output stability.

Compares current tool output against baseline snapshots.
Run with --snapshot-update to regenerate baselines.

Snapshots are Python-version-specific (argparse formatting varies).
Files are named {name}_py{major}{minor}.snap (e.g., scaffold_help_py310.snap).
"""
import subprocess
import sys
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent / "scripts" / "tools"
SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
_PY_TAG = f"py{sys.version_info.major}{sys.version_info.minor}"


def ensure_snapshot_dir():
    """Ensure snapshots directory exists."""
    SNAPSHOT_DIR.mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def setup_snapshots():
    """Auto-setup snapshots directory before each test."""
    ensure_snapshot_dir()


class TestSnapshots:
    """Snapshot stability tests."""

    def _run_tool(self, args, timeout=15):
        """Execute a tool and return subprocess result."""
        result = subprocess.run(
            [sys.executable] + args,
            capture_output=True, timeout=timeout, text=True
        )
        return result

    def _check_snapshot(self, name, content, request):
        """Compare content against stored snapshot.

        If --snapshot-update is passed, regenerate the snapshot.
        If snapshot doesn't exist, create it and skip test (re-run to verify).
        Snapshot files are Python-version-specific to handle argparse changes.
        """
        snap_path = SNAPSHOT_DIR / f"{name}_{_PY_TAG}.snap"
        if request.config.getoption("--snapshot-update", default=False):
            snap_path.write_text(content, encoding="utf-8")
            return
        if not snap_path.exists():
            snap_path.write_text(content, encoding="utf-8")
            pytest.skip(f"Snapshot {name} created — re-run to verify")
        expected = snap_path.read_text(encoding="utf-8")
        assert content == expected, (
            f"Snapshot {name} changed.\n"
            f"Run with --snapshot-update to accept.\n"
            f"Diff: expected {len(expected)} chars, got {len(content)} chars"
        )

    def test_help_output_scaffold(self, request):
        """scaffold --help output should be stable."""
        result = self._run_tool([str(TOOLS_DIR / "ops" / "scaffold_tenant.py"), "--help"])
        assert result.returncode == 0, (
            f"scaffold --help failed: {result.stderr[:200]}"
        )
        self._check_snapshot("scaffold_help", result.stdout, request)

    def test_help_output_validate_config(self, request):
        """validate-config --help output should be stable."""
        result = self._run_tool([str(TOOLS_DIR / "ops" / "validate_config.py"), "--help"])
        assert result.returncode == 0, (
            f"validate-config --help failed: {result.stderr[:200]}"
        )
        self._check_snapshot("validate_config_help", result.stdout, request)

    def test_help_output_operator_generate(self, request):
        """operator-generate --help output should be stable."""
        result = self._run_tool([str(TOOLS_DIR / "ops" / "operator_generate.py"), "--help"])
        assert result.returncode == 0, (
            f"operator-generate --help failed: {result.stderr[:200]}"
        )
        self._check_snapshot("operator_generate_help", result.stdout, request)

    def test_help_output_config_diff(self, request):
        """config-diff --help output should be stable."""
        result = self._run_tool([str(TOOLS_DIR / "ops" / "config_diff.py"), "--help"])
        assert result.returncode == 0, (
            f"config-diff --help failed: {result.stderr[:200]}"
        )
        self._check_snapshot("config_diff_help", result.stdout, request)

    def test_help_output_alert_quality(self, request):
        """alert-quality --help output should be stable."""
        result = self._run_tool([str(TOOLS_DIR / "ops" / "alert_quality.py"), "--help"])
        assert result.returncode == 0, (
            f"alert-quality --help failed: {result.stderr[:200]}"
        )
        self._check_snapshot("alert_quality_help", result.stdout, request)
