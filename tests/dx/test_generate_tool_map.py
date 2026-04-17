"""Tests for generate_tool_map.py — Tool navigation auto-generation."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import generate_tool_map as gtm  # noqa: E402


# ---------------------------------------------------------------------------
# extract_tool_description
# ---------------------------------------------------------------------------
class TestExtractToolDescription:
    """Tests for extract_tool_description() — docstring extraction from Python files."""

    def test_module_docstring_with_prefix(self, tmp_path):
        """Extracts description from 'scriptname.py — description' format."""
        p = tmp_path / "diagnose.py"
        p.write_text('"""diagnose.py — Quick health check for tenants."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "Quick health check for tenants."

    def test_module_docstring_with_dash(self, tmp_path):
        """Supports em-dash, en-dash, and regular dash separators."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py - Simple description."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "Simple description."

    def test_module_docstring_no_prefix(self, tmp_path):
        """Falls back to full first line when no prefix pattern matches."""
        p = tmp_path / "util.py"
        p.write_text('"""A utility for processing YAML files."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "A utility for processing YAML files."

    def test_multiline_docstring(self, tmp_path):
        """Only extracts the first line of multi-line docstrings."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py — First line.\n\nDetailed description.\nMore lines.\n"""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "First line."

    def test_no_docstring(self, tmp_path):
        """Returns empty string when no docstring found."""
        p = tmp_path / "nodoc.py"
        p.write_text("import os\nprint('hello')\n", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_syntax_error_file(self, tmp_path):
        """Handles files with syntax errors gracefully."""
        p = tmp_path / "broken.py"
        p.write_text("def broken(:\n  pass\n", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_empty_file(self, tmp_path):
        """Handles empty files gracefully."""
        p = tmp_path / "empty.py"
        p.write_text("", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_chinese_description(self, tmp_path):
        """Supports Chinese descriptions in docstrings."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py — 工具導覽自動生成"""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "工具導覽自動生成"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate module constants and category configuration."""

    def test_category_order_matches_subdir_category(self):
        for cat in gtm.CATEGORY_ORDER:
            assert cat in gtm.SUBDIR_CATEGORY.values()

    def test_category_headers_bilingual(self):
        for lang in ("zh", "en"):
            assert lang in gtm.CATEGORY_HEADERS
            for cat in gtm.CATEGORY_ORDER:
                assert cat in gtm.CATEGORY_HEADERS[lang]

    def test_skip_prefixes_excludes_lib(self):
        assert any("_lib" in p for p in gtm.SKIP_PREFIXES)
        assert any("__init__" in p for p in gtm.SKIP_PREFIXES)
