"""Tests for check_doc_links.py — documentation cross-reference checker."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_doc_links as cdl  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_checker(tmp_path, verbose=False):
    """Create a DocLinkChecker with a minimal repo layout."""
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    readme = tmp_path / "README.md"
    readme.write_text("# Readme\n", encoding="utf-8")
    return cdl.DocLinkChecker(str(tmp_path), verbose=verbose)


# ---------------------------------------------------------------------------
# _heading_to_anchor
# ---------------------------------------------------------------------------
class TestHeadingToAnchor:
    def test_basic(self):
        assert cdl.DocLinkChecker._heading_to_anchor("Hello World") == "hello-world"

    def test_strips_markdown(self):
        assert cdl.DocLinkChecker._heading_to_anchor("**Bold** Text") == "bold-text"

    def test_strips_code(self):
        assert cdl.DocLinkChecker._heading_to_anchor("`code` stuff") == "code-stuff"

    def test_strips_links(self):
        result = cdl.DocLinkChecker._heading_to_anchor("[Link](http://example.com)")
        assert result == "link"

    def test_cjk_preserved(self):
        result = cdl.DocLinkChecker._heading_to_anchor("專案概覽")
        assert "專案概覽" in result

    def test_special_chars_removed(self):
        result = cdl.DocLinkChecker._heading_to_anchor("Section (v2.1.0)")
        # Parentheses removed, dots and numbers kept
        assert "section" in result
        assert "(" not in result

    def test_emoji_shortcode_removed(self):
        result = cdl.DocLinkChecker._heading_to_anchor(":rocket: Launch")
        assert "rocket" not in result
        assert "launch" in result


# ---------------------------------------------------------------------------
# _is_external_url
# ---------------------------------------------------------------------------
class TestIsExternalUrl:
    def test_http(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("http://example.com") is True

    def test_https(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("https://example.com") is True

    def test_relative(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._is_external_url("../docs/guide.md") is False


# ---------------------------------------------------------------------------
# _resolve_link_path
# ---------------------------------------------------------------------------
class TestResolveLinkPath:
    def test_relative_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        source = tmp_path / "docs" / "guide.md"
        (tmp_path / "docs").mkdir(exist_ok=True)
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "other.md")
        assert valid is True
        assert target == (tmp_path / "docs" / "other.md").resolve()

    def test_anchor_only(self, tmp_path):
        checker = _make_checker(tmp_path)
        source = tmp_path / "docs" / "guide.md"
        (tmp_path / "docs").mkdir(exist_ok=True)
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "#section")
        assert valid is True
        assert target == source

    def test_parent_directory_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        subdir = tmp_path / "docs" / "sub"
        subdir.mkdir(parents=True)
        source = subdir / "nested.md"
        source.write_text("content", encoding="utf-8")
        target, valid = checker._resolve_link_path(source, "../guide.md")
        assert valid is True
        assert target == (tmp_path / "docs" / "guide.md").resolve()


# ---------------------------------------------------------------------------
# _load_ignore_file
# ---------------------------------------------------------------------------
class TestLoadIgnoreFile:
    def test_no_ignore_file(self, tmp_path):
        checker = _make_checker(tmp_path)
        assert checker._ignore_patterns == set()

    def test_with_patterns(self, tmp_path):
        (tmp_path / ".doclinkignore").write_text(
            "# comment\nbroken-link.md\ndocs/old.md:../legacy.md\n",
            encoding="utf-8",
        )
        checker = _make_checker(tmp_path)
        assert "broken-link.md" in checker._ignore_patterns
        assert "docs/old.md:../legacy.md" in checker._ignore_patterns

    def test_is_ignored(self, tmp_path):
        (tmp_path / ".doclinkignore").write_text(
            "legacy.md\ndocs/guide.md:../old.md\n", encoding="utf-8"
        )
        checker = _make_checker(tmp_path)
        # Generic match
        assert checker._is_ignored(
            tmp_path / "docs" / "any.md", "legacy.md") is True
        # File-specific match
        assert checker._is_ignored(
            tmp_path / "docs" / "guide.md", "../old.md") is True
        # No match
        assert checker._is_ignored(
            tmp_path / "docs" / "guide.md", "other.md") is False


# ---------------------------------------------------------------------------
# _get_headings
# ---------------------------------------------------------------------------
class TestGetHeadings:
    def test_extracts_headings(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "test.md"
        md.write_text(
            "# Title\n\n## Section A\n\n### Sub Section\n", encoding="utf-8"
        )
        headings = checker._get_headings(md)
        assert "title" in headings
        assert "section-a" in headings
        assert "sub-section" in headings

    def test_skips_code_blocks(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "test.md"
        md.write_text(
            "# Real\n\n```\n# Not a heading\n```\n\n## Also Real\n",
            encoding="utf-8",
        )
        headings = checker._get_headings(md)
        assert "real" in headings
        assert "also-real" in headings
        assert "not-a-heading" not in headings


# ---------------------------------------------------------------------------
# _fuzzy_best
# ---------------------------------------------------------------------------
class TestFuzzyBest:
    def test_exact_match(self):
        result = cdl.DocLinkChecker._fuzzy_best("hello", {"hello", "world"})
        assert result == "hello"

    def test_close_match(self):
        result = cdl.DocLinkChecker._fuzzy_best("helo", {"hello", "world"})
        assert result == "hello"

    def test_below_threshold(self):
        result = cdl.DocLinkChecker._fuzzy_best("xyz", {"hello", "world"})
        assert result == ""

    def test_empty_haystack(self):
        result = cdl.DocLinkChecker._fuzzy_best("hello", set())
        assert result == ""


# ---------------------------------------------------------------------------
# _build_code_block_set
# ---------------------------------------------------------------------------
class TestBuildCodeBlockSet:
    def test_marks_code_lines(self):
        lines = ["text\n", "```\n", "code\n", "```\n", "more text\n"]
        code_set = cdl.DocLinkChecker._build_code_block_set(lines)
        assert 0 not in code_set  # text
        assert 1 in code_set      # opening fence
        assert 2 in code_set      # code
        assert 3 in code_set      # closing fence
        assert 4 not in code_set  # more text

    def test_no_code_blocks(self):
        lines = ["line 1\n", "line 2\n"]
        code_set = cdl.DocLinkChecker._build_code_block_set(lines)
        assert len(code_set) == 0


# ---------------------------------------------------------------------------
# scan_file (integration-level)
# ---------------------------------------------------------------------------
class TestScanFile:
    def test_detects_broken_link(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [missing](nonexistent.md) for details.\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 1
        assert "nonexistent.md" in str(checker.broken_links[0]["link"])

    def test_valid_link_no_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# Other\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text("See [other](other.md) for details.\n", encoding="utf-8")
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_external_links_skipped(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [Google](https://google.com) for info.\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_code_block_links_skipped(self, tmp_path):
        checker = _make_checker(tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "```\n[fake](broken.md)\n```\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_links) == 0

    def test_broken_anchor_detected(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# Real Heading\n\nContent.\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [section](other.md#nonexistent-heading).\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_anchors) == 1

    def test_valid_anchor_no_error(self, tmp_path):
        checker = _make_checker(tmp_path)
        target = tmp_path / "docs" / "other.md"
        target.write_text("# My Section\n\nContent.\n", encoding="utf-8")
        md = tmp_path / "docs" / "guide.md"
        md.write_text(
            "See [section](other.md#my-section).\n", encoding="utf-8"
        )
        checker.scan_file(md)
        assert len(checker.broken_anchors) == 0


# ---------------------------------------------------------------------------
# check_cross_language_counterparts
# ---------------------------------------------------------------------------
class TestCrossLanguageCounterparts:
    def test_paired_files_ok(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "guide.md").write_text("zh", encoding="utf-8")
        (docs / "guide.en.md").write_text("en", encoding="utf-8")
        checker = _make_checker(tmp_path)
        checker.check_cross_language_counterparts()
        assert len(checker.missing_counterparts) == 0

    def test_missing_en_counterpart(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "guide.md").write_text("zh", encoding="utf-8")
        (docs / "other.en.md").write_text("en", encoding="utf-8")  # trigger en_dirs
        checker = _make_checker(tmp_path)
        checker.check_cross_language_counterparts()
        missing = [m for m in checker.missing_counterparts if m["direction"] == "zh→en"]
        assert any("guide" in m["missing"] for m in missing)
