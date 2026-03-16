"""Tests for fix_doc_links.py — MkDocs cross-reference link fixer."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import fix_doc_links as fdl  # noqa: E402


# ---------------------------------------------------------------------------
# get_subdir
# ---------------------------------------------------------------------------
class TestGetSubdir:
    """Tests for get_subdir() — relative subdirectory extraction."""

    def test_subdir_file(self):
        filepath = os.path.join(fdl.DOCS_DIR, "adr", "some-doc.md")
        assert fdl.get_subdir(filepath) == "adr"

    def test_root_file(self):
        filepath = os.path.join(fdl.DOCS_DIR, "architecture-and-design.md")
        assert fdl.get_subdir(filepath) == ""

    def test_nested_subdir(self):
        filepath = os.path.join(fdl.DOCS_DIR, "getting-started", "for-tenants.md")
        assert fdl.get_subdir(filepath) == "getting-started"


# ---------------------------------------------------------------------------
# fix_links_in_file
# ---------------------------------------------------------------------------
class TestFixLinksInFile:
    """Tests for fix_links_in_file() — link fixing patterns."""

    def test_removes_redundant_subdir_prefix(self, tmp_path, monkeypatch):
        """Pattern 1: adr/adr/X.md → adr/X.md when file is in adr/."""
        # Create directory structure mimicking docs/adr/
        adr_dir = tmp_path / "adr"
        adr_dir.mkdir()
        test_file = adr_dir / "test.md"
        test_file.write_text(
            "See [ADR-001](adr/ADR-001.md) for details.\n",
            encoding="utf-8",
        )
        # Override DOCS_DIR to use tmp_path
        monkeypatch.setattr(fdl, "DOCS_DIR", str(tmp_path))
        fixes = fdl.fix_links_in_file(str(test_file), dry_run=False)
        assert fixes > 0
        content = test_file.read_text(encoding="utf-8")
        assert "adr/ADR-001" not in content
        assert "ADR-001.md" in content

    def test_no_fix_needed(self, tmp_path, monkeypatch):
        """Files with correct links should not be modified."""
        test_file = tmp_path / "clean.md"
        test_file.write_text(
            "See [docs](../architecture-and-design.md) and [link](other.md).\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(fdl, "DOCS_DIR", str(tmp_path))
        fixes = fdl.fix_links_in_file(str(test_file), dry_run=False)
        assert fixes == 0

    def test_dry_run_no_modification(self, tmp_path, monkeypatch):
        """Dry run should not modify the file."""
        adr_dir = tmp_path / "adr"
        adr_dir.mkdir()
        test_file = adr_dir / "test.md"
        original = "See [ADR-001](adr/ADR-001.md) for details.\n"
        test_file.write_text(original, encoding="utf-8")
        monkeypatch.setattr(fdl, "DOCS_DIR", str(tmp_path))
        fixes = fdl.fix_links_in_file(str(test_file), dry_run=True)
        assert fixes > 0
        # File should be unchanged
        assert test_file.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate module constants."""

    def test_docs_dir_is_string(self):
        # DOCS_DIR should be a non-empty string path
        assert isinstance(fdl.DOCS_DIR, str)
        assert len(fdl.DOCS_DIR) > 0

    def test_github_blob_url(self):
        assert fdl.GITHUB_BLOB.startswith("https://github.com/")
        assert "blob/main" in fdl.GITHUB_BLOB
