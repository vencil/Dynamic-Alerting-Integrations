"""Tests for check_structure.py — project directory structure enforcement."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_structure as cs  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fake_tracked(*paths: str) -> list[str]:
    """Build a list of tracked file paths for testing."""
    return list(paths)


# ---------------------------------------------------------------------------
# check_tools_root
# ---------------------------------------------------------------------------
class TestCheckToolsRoot:
    """Validate scripts/tools/ root cleanliness enforcement."""

    def test_allowed_files_pass(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/tools/_lib_python.py",
            "scripts/tools/metric-dictionary.yaml",
            "scripts/tools/validate_all.py",
            "scripts/tools/vendor_download.sh",
        )
        assert cs.check_tools_root(tmp_path, tracked) == []

    def test_subdir_files_pass(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/tools/ops/scaffold_tenant.py",
            "scripts/tools/dx/bump_docs.py",
            "scripts/tools/lint/check_structure.py",
        )
        assert cs.check_tools_root(tmp_path, tracked) == []

    def test_stray_file_detected(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/tools/_lib_python.py",
            "scripts/tools/stray_script.py",
        )
        violations = cs.check_tools_root(tmp_path, tracked)
        assert len(violations) == 1
        assert "STRAY" in violations[0]
        assert "stray_script.py" in violations[0]

    def test_non_tools_files_ignored(self, tmp_path):
        tracked = _fake_tracked(
            "src/main.py",
            "README.md",
        )
        assert cs.check_tools_root(tmp_path, tracked) == []

    def test_multiple_stray_files(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/tools/rogue_a.py",
            "scripts/tools/rogue_b.sh",
        )
        violations = cs.check_tools_root(tmp_path, tracked)
        assert len(violations) == 2


# ---------------------------------------------------------------------------
# check_jsx_placement
# ---------------------------------------------------------------------------
class TestCheckJsxPlacement:
    """Validate .jsx file placement rules."""

    def test_allowed_locations_pass(self, tmp_path):
        tracked = _fake_tracked(
            "docs/interactive/tools/capacity-planner.jsx",
            "docs/getting-started/wizard.jsx",
        )
        assert cs.check_jsx_placement(tmp_path, tracked) == []

    def test_docs_root_jsx_flagged(self, tmp_path):
        tracked = _fake_tracked("docs/stray-tool.jsx")
        violations = cs.check_jsx_placement(tmp_path, tracked)
        assert len(violations) == 1
        assert "MISPLACED" in violations[0]

    def test_non_docs_jsx_ok(self, tmp_path):
        """JSX files outside docs/ are not flagged."""
        tracked = _fake_tracked("src/components/App.jsx")
        assert cs.check_jsx_placement(tmp_path, tracked) == []

    def test_non_jsx_files_ignored(self, tmp_path):
        tracked = _fake_tracked("docs/guide.md", "docs/index.html")
        assert cs.check_jsx_placement(tmp_path, tracked) == []


# ---------------------------------------------------------------------------
# check_test_placement
# ---------------------------------------------------------------------------
class TestCheckTestPlacement:
    """Validate test file placement rules."""

    def test_tests_dir_ok(self, tmp_path):
        tracked = _fake_tracked(
            "tests/test_scaffold_tenant.py",
            "tests/test_integration.py",
        )
        assert cs.check_test_placement(tmp_path, tracked) == []

    def test_misplaced_test_detected(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/test_something.py",
            "tests/test_ok.py",
        )
        violations = cs.check_test_placement(tmp_path, tracked)
        assert len(violations) == 1
        assert "test_something.py" in violations[0]

    def test_non_test_files_ignored(self, tmp_path):
        tracked = _fake_tracked(
            "scripts/tools/ops/scaffold_tenant.py",
            "conftest.py",
        )
        assert cs.check_test_placement(tmp_path, tracked) == []

    def test_test_in_root_detected(self, tmp_path):
        tracked = _fake_tracked("test_root_level.py")
        violations = cs.check_test_placement(tmp_path, tracked)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# check_banned_dirs
# ---------------------------------------------------------------------------
class TestCheckBannedDirs:
    """Validate banned tracked directory detection."""

    def test_clean_repo(self, tmp_path):
        tracked = _fake_tracked(
            "tests/test_scaffold_tenant.py",
            "scripts/tools/ops/scaffold_tenant.py",
        )
        assert cs.check_banned_dirs(tmp_path, tracked) == []

    def test_test_output_tracked(self, tmp_path):
        tracked = _fake_tracked(
            "tests/_test_output/result.json",
            "tests/_test_output/log.txt",
        )
        violations = cs.check_banned_dirs(tmp_path, tracked)
        assert len(violations) == 2
        assert all("TRACKED" in v for v in violations)

    def test_multidb_output_tracked(self, tmp_path):
        tracked = _fake_tracked("tests/_test_multidb_output/data.json")
        violations = cs.check_banned_dirs(tmp_path, tracked)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# main() integration
# ---------------------------------------------------------------------------
class TestMain:
    """Integration tests for main() entry point."""

    def test_clean_repo_returns_zero(self, tmp_path):
        with patch.object(cs, '_git_tracked', return_value=[
            "scripts/tools/_lib_python.py",
            "tests/test_ok.py",
            "docs/interactive/tools/tool.jsx",
        ]):
            with patch('check_structure.Path') as mock_path_cls:
                # Make Path(__file__).resolve().parent.parent.parent.parent
                # return tmp_path
                mock_resolve = mock_path_cls.return_value.resolve.return_value
                mock_resolve.parent.parent.parent.parent = tmp_path

                with patch('sys.argv', ['check_structure.py']):
                    result = cs.main()
        assert result == 0

    def test_violations_ci_returns_one(self, tmp_path):
        with patch.object(cs, '_git_tracked', return_value=[
            "scripts/tools/stray.py",  # violation
        ]):
            with patch('check_structure.Path') as mock_path_cls:
                mock_resolve = mock_path_cls.return_value.resolve.return_value
                mock_resolve.parent.parent.parent.parent = tmp_path

                with patch('sys.argv', ['check_structure.py', '--ci']):
                    result = cs.main()
        assert result == 1
