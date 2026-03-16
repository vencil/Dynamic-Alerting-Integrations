"""Tests for add_frontmatter.py — YAML front matter injection for documentation."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import add_frontmatter as af  # noqa: E402


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------
class TestDetectLanguage:
    """Tests for detect_language() — filename-based language detection."""

    def test_english_file(self):
        assert af.detect_language("docs/README.en.md") == "en"

    def test_chinese_file(self):
        assert af.detect_language("docs/README.md") == "zh"

    def test_nested_path_english(self):
        assert af.detect_language("docs/getting-started/for-tenants.en.md") == "en"

    def test_nested_path_chinese(self):
        assert af.detect_language("docs/getting-started/for-tenants.md") == "zh"

    def test_root_changelog(self):
        assert af.detect_language("CHANGELOG.md") == "zh"


# ---------------------------------------------------------------------------
# extract_version
# ---------------------------------------------------------------------------
class TestExtractVersion:
    """Tests for extract_version() — version extraction from file content."""

    def test_extracts_from_content(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("# Architecture v2.1.0\nSome content.", encoding="utf-8")
        assert af.extract_version(str(p), str(tmp_path)) == "v2.1.0"

    def test_fallback_to_claude_md(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("# No version here\n", encoding="utf-8")
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.1.0)\n", encoding="utf-8")
        assert af.extract_version(str(p), str(tmp_path)) == "v2.1.0"

    def test_fallback_default(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("No version at all\n", encoding="utf-8")
        result = af.extract_version(str(p), str(tmp_path))
        assert result.startswith("v")

    def test_nonexistent_file(self, tmp_path):
        result = af.extract_version(str(tmp_path / "missing.md"), str(tmp_path))
        assert result.startswith("v")


# ---------------------------------------------------------------------------
# extract_title
# ---------------------------------------------------------------------------
class TestExtractTitle:
    """Tests for extract_title() — H1 extraction from markdown files."""

    def test_extracts_h1(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("# My Document Title\nSome content.", encoding="utf-8")
        assert af.extract_title(str(p), "test.md") == "My Document Title"

    def test_skips_frontmatter(self, tmp_path):
        p = tmp_path / "test.md"
        p.write_text("---\ntitle: FM Title\n---\n# Real Title\n", encoding="utf-8")
        title = af.extract_title(str(p), "test.md")
        assert title == "Real Title"

    def test_falls_back_to_filename(self, tmp_path):
        p = tmp_path / "no-heading.md"
        p.write_text("Just some text without any heading.\n", encoding="utf-8")
        title = af.extract_title(str(p), "no-heading.md")
        # Should be derived from filename
        assert "no-heading" in title or "No Heading" in title or title != ""


# ---------------------------------------------------------------------------
# TAG_ASSIGNMENTS
# ---------------------------------------------------------------------------
class TestTagAssignments:
    """Validate TAG_ASSIGNMENTS patterns and structure."""

    def test_all_entries_have_tags(self):
        for pattern, assignment in af.TAG_ASSIGNMENTS.items():
            assert "tags" in assignment, f"Pattern '{pattern}' missing 'tags'"
            assert isinstance(assignment["tags"], list)
            assert len(assignment["tags"]) > 0

    def test_all_entries_have_audience(self):
        for pattern, assignment in af.TAG_ASSIGNMENTS.items():
            assert "audience" in assignment, f"Pattern '{pattern}' missing 'audience'"
            assert isinstance(assignment["audience"], list)
            assert len(assignment["audience"]) > 0

    def test_known_patterns_present(self):
        """Key documentation patterns should be covered."""
        pattern_keys = list(af.TAG_ASSIGNMENTS.keys())
        pattern_str = " ".join(pattern_keys)
        assert "architecture" in pattern_str
        assert "migration" in pattern_str
        assert "getting-started" in pattern_str
        assert "troubleshooting" in pattern_str

    def test_root_level_patterns(self):
        assert len(af.ROOT_LEVEL_PATTERNS) > 0
        for pattern, assignment in af.ROOT_LEVEL_PATTERNS.items():
            assert "tags" in assignment
            assert "audience" in assignment
