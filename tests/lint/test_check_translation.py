"""Tests for check_translation.py — Translation quality checking."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_translation as ct  # noqa: E402


# ---------------------------------------------------------------------------
# count_headings
# ---------------------------------------------------------------------------
class TestCountHeadings:
    """Tests for count_headings() — markdown heading counter."""

    def test_counts_all_levels(self):
        content = "# H1\n## H2\n### H3\ntext\n## H2 again\n"
        result = ct.count_headings(content)
        assert result == {1: 1, 2: 2, 3: 1}

    def test_empty_content(self):
        result = ct.count_headings("")
        assert result == {1: 0, 2: 0, 3: 0}

    def test_no_headings(self):
        result = ct.count_headings("Just plain text.\nNo headings here.")
        assert result == {1: 0, 2: 0, 3: 0}

    def test_ignores_h4_and_beyond(self):
        content = "#### H4\n##### H5\n"
        result = ct.count_headings(content)
        assert result == {1: 0, 2: 0, 3: 0}

    def test_requires_space_after_hash(self):
        content = "#NotAHeading\n##AlsoNot\n"
        result = ct.count_headings(content)
        assert result == {1: 0, 2: 0, 3: 0}


# ---------------------------------------------------------------------------
# count_code_blocks
# ---------------------------------------------------------------------------
class TestCountCodeBlocks:
    """Tests for count_code_blocks()."""

    def test_single_block(self):
        content = "text\n```python\ncode\n```\nmore text"
        assert ct.count_code_blocks(content) == 1

    def test_multiple_blocks(self):
        content = "```\ncode1\n```\n\n```yaml\ncode2\n```\n"
        assert ct.count_code_blocks(content) == 2

    def test_no_blocks(self):
        assert ct.count_code_blocks("plain text only") == 0

    def test_odd_backticks_rounds_down(self):
        content = "```\ncode\n```\nextra ```"
        assert ct.count_code_blocks(content) == 1


# ---------------------------------------------------------------------------
# count_mermaid_diagrams
# ---------------------------------------------------------------------------
class TestCountMermaidDiagrams:
    """Tests for count_mermaid_diagrams()."""

    def test_single_diagram(self):
        content = "text\n```mermaid\ngraph TD\nA-->B\n```\n"
        assert ct.count_mermaid_diagrams(content) == 1

    def test_no_diagrams(self):
        assert ct.count_mermaid_diagrams("```python\ncode\n```") == 0


# ---------------------------------------------------------------------------
# count_tables
# ---------------------------------------------------------------------------
class TestCountTables:
    """Tests for count_tables()."""

    def test_single_table(self):
        content = "| Col1 | Col2 |\n| --- | --- |\n| a | b |\n"
        assert ct.count_tables(content) == 1

    def test_no_tables(self):
        assert ct.count_tables("no tables here") == 0

    def test_two_tables(self):
        content = "| A | B |\n| - | - |\n| 1 | 2 |\n\ntext\n\n| C | D |\n| - | - |\n| 3 | 4 |\n"
        assert ct.count_tables(content) == 2


# ---------------------------------------------------------------------------
# count_links
# ---------------------------------------------------------------------------
class TestCountLinks:
    """Tests for count_links()."""

    def test_counts_markdown_links(self):
        content = "See [docs](url1) and [more](url2)."
        assert ct.count_links(content) == 2

    def test_no_links(self):
        assert ct.count_links("no links here") == 0


# ---------------------------------------------------------------------------
# extract_front_matter
# ---------------------------------------------------------------------------
class TestExtractFrontMatter:
    """Tests for extract_front_matter()."""

    def test_extracts_fields(self):
        content = "---\ntitle: Test Doc\ntags: [a, b]\nversion: v2.1.0\nlang: zh\n---\n# Content"
        fm = ct.extract_front_matter(content)
        assert fm["title"] == "Test Doc"
        assert "v2.1.0" in fm["version"]

    def test_no_front_matter(self):
        fm = ct.extract_front_matter("# Just a heading\nContent.")
        assert fm == {}


# ---------------------------------------------------------------------------
# compare_front_matter
# ---------------------------------------------------------------------------
class TestCompareFrontMatter:
    """Tests for compare_front_matter()."""

    def test_matching_fields(self):
        zh = {"tags": "[a]", "audience": "[sre]", "version": "v2.1.0", "lang": "zh"}
        en = {"tags": "[a]", "audience": "[sre]", "version": "v2.1.0", "lang": "en"}
        issues = ct.compare_front_matter(zh, en)
        assert len(issues) == 0

    def test_mismatched_tags(self):
        zh = {"tags": "[a, b]", "audience": "[sre]", "version": "v2.1.0", "lang": "zh"}
        en = {"tags": "[a]", "audience": "[sre]", "version": "v2.1.0", "lang": "en"}
        issues = ct.compare_front_matter(zh, en)
        assert any("tags" in i for i in issues)

    def test_same_lang_warns(self):
        zh = {"lang": "zh"}
        en = {"lang": "zh"}
        issues = ct.compare_front_matter(zh, en)
        assert any("lang" in i for i in issues)

    def test_empty_front_matter(self):
        issues = ct.compare_front_matter({}, {})
        assert len(issues) == 0
