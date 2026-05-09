"""Tests for check_includes_sync.py — bilingual include snippet sync checker.

Merged from previous _extended split (PR test-refactor sweep): pure-function
checks (count_*, compare_pair, _create_en_stub) live alongside the main()
CLI orchestrator tests at the bottom of the file.
"""
from __future__ import annotations

import textwrap

import pytest

import check_includes_sync as cis


# ---------------------------------------------------------------------------
# Structural metric functions
# ---------------------------------------------------------------------------
class TestCountCodeBlocks:
    def test_basic(self):
        md = "```python\nprint('hi')\n```\n\ntext\n\n```bash\nls\n```"
        assert cis.count_code_blocks(md) == 4  # opening + closing pairs

    def test_none(self):
        assert cis.count_code_blocks("no code blocks here") == 0


class TestCountTableRows:
    def test_basic(self):
        md = "| Col A | Col B |\n|---|---|\n| 1 | 2 |"
        assert cis.count_table_rows(md) == 3

    def test_none(self):
        assert cis.count_table_rows("no tables") == 0


class TestCountUrls:
    def test_basic(self):
        md = "See https://example.com and http://foo.bar/baz"
        urls = cis.count_urls(md)
        assert len(urls) == 2
        assert "https://example.com" in urls

    def test_none(self):
        assert cis.count_urls("no urls") == []


class TestExtractVersions:
    def test_semver(self):
        md = "Version v2.1.0 and 1.0.0"
        versions = cis.extract_versions(md)
        assert "v2.1.0" in versions
        assert "1.0.0" in versions

    def test_none(self):
        assert cis.extract_versions("no versions") == []


class TestCountListItems:
    def test_unordered(self):
        md = "- item 1\n- item 2\n- item 3"
        assert cis.count_list_items(md) == 3

    def test_ordered(self):
        md = "1. first\n2. second"
        assert cis.count_list_items(md) == 2

    def test_mixed(self):
        md = "- bullet\n1. numbered"
        assert cis.count_list_items(md) == 2

    def test_none(self):
        assert cis.count_list_items("no lists") == 0


# ---------------------------------------------------------------------------
# compare_pair
# ---------------------------------------------------------------------------
class TestComparePair:
    def test_in_sync(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        content = "# Title\n\n- item 1\n- item 2\n"
        zh.write_text(content, encoding="utf-8")
        en.write_text(content, encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert issues == []

    def test_missing_english(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("content", encoding="utf-8")
        # en doesn't exist
        issues = cis.compare_pair(zh, en)
        assert len(issues) == 1
        assert "missing" in issues[0].lower()

    def test_code_block_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("```yaml\nkey: val\n```\n", encoding="utf-8")
        en.write_text("no code blocks\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("code blocks" in i for i in issues)

    def test_table_row_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("| A | B |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
        en.write_text("| A | B |\n|---|---|\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("table rows" in i for i in issues)

    def test_version_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("Version v2.1.0\n", encoding="utf-8")
        en.write_text("Version v2.0.0\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("versions" in i for i in issues)

    def test_url_count_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("See https://a.com and https://b.com\n", encoding="utf-8")
        en.write_text("See https://a.com\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("URLs" in i for i in issues)

    def test_list_item_mismatch(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("- a\n- b\n- c\n", encoding="utf-8")
        en.write_text("- a\n- b\n", encoding="utf-8")
        issues = cis.compare_pair(zh, en)
        assert any("list items" in i for i in issues)


# ---------------------------------------------------------------------------
# _create_en_stub
# ---------------------------------------------------------------------------
class TestCreateEnStub:
    def test_creates_stub_with_translations(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("# 規則包 合計\n\n| 運營模式 | 自監控 |\n", encoding="utf-8")
        cis._create_en_stub(zh, en)
        assert en.exists()
        content = en.read_text(encoding="utf-8")
        assert "Rule Pack" in content
        assert "Total" in content
        assert "operational mode" in content
        assert "self-monitoring" in content

    def test_preserves_non_chinese(self, tmp_path):
        zh = tmp_path / "snippet.md"
        en = tmp_path / "snippet.en.md"
        zh.write_text("```yaml\nkey: value\n```\n", encoding="utf-8")
        cis._create_en_stub(zh, en)
        content = en.read_text(encoding="utf-8")
        assert "key: value" in content


# ---------------------------------------------------------------------------
# main() CLI — orchestrator coverage (was test_check_includes_sync_extended.py)
# ---------------------------------------------------------------------------
class TestMainCLI:
    """check_includes_sync main() tests."""

    def _setup_includes(self, tmp_path, zh_content="# Title\n\n- item 1\n",
                        en_content=None, create_en=True):
        """Set up an includes directory with zh/en pairs."""
        includes = tmp_path / "docs" / "includes"
        includes.mkdir(parents=True)

        zh = includes / "snippet.md"
        zh.write_text(zh_content, encoding="utf-8")

        if create_en and en_content is not None:
            en = includes / "snippet.en.md"
            en.write_text(en_content, encoding="utf-8")

        return includes

    def test_all_in_sync(self, tmp_path, monkeypatch, capsys, cli_argv):
        """All pairs in sync should return 0."""
        includes = self._setup_includes(tmp_path,
                                        "# Title\n\n- item 1\n",
                                        "# Title\n\n- item 1\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 0
        out = capsys.readouterr().out
        assert "in sync" in out

    def test_missing_english_check_mode(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Missing English file in --check mode should return 1."""
        includes = self._setup_includes(tmp_path, create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--check')
        result = cis.main()
        assert result == 1

    def test_missing_english_no_check(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Missing English file without --check returns 0."""
        includes = self._setup_includes(tmp_path, create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        # Without --check, returns 0 even with issues
        # Actually, the code returns 1 if total_issues > 0 or missing_en > 0
        # when --check is set, and 0 otherwise
        assert result == 0

    def test_verbose_in_sync(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--verbose shows OK for in-sync pairs."""
        includes = self._setup_includes(tmp_path,
                                        "# Title\n\n- item 1\n",
                                        "# Title\n\n- item 1\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--verbose')
        result = cis.main()
        assert result == 0
        out = capsys.readouterr().out
        assert "in sync" in out.lower()

    def test_fix_creates_stubs(self, tmp_path, monkeypatch, capsys, cli_argv):
        """--fix creates missing English stubs."""
        includes = self._setup_includes(tmp_path,
                                        "# 規則包\n",
                                        create_en=False)
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--fix')
        result = cis.main()
        assert result == 0
        en_file = includes / "snippet.en.md"
        assert en_file.exists()
        content = en_file.read_text(encoding="utf-8")
        assert "Rule Pack" in content

    def test_structural_mismatch(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Structural mismatch between zh and en."""
        includes = self._setup_includes(
            tmp_path,
            "```yaml\nkey: val\n```\n",
            "no code blocks\n")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync', '--check')
        result = cis.main()
        assert result == 1
        out = capsys.readouterr().out
        assert "code blocks" in out

    def test_no_includes_dir(self, tmp_path, monkeypatch, capsys, cli_argv):
        """Nonexistent includes dir returns 1."""
        monkeypatch.setattr(cis, "INCLUDES_DIR",
                            tmp_path / "nonexistent")
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 1

    def test_no_zh_files(self, tmp_path, monkeypatch, capsys, cli_argv):
        """No Chinese files returns 0."""
        includes = tmp_path / "docs" / "includes"
        includes.mkdir(parents=True)
        # Only en file, no zh
        (includes / "snippet.en.md").write_text("English only",
                                                 encoding="utf-8")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 0

    def test_abbreviations_md_ignored(self, tmp_path, monkeypatch, capsys, cli_argv):
        """abbreviations.md should be ignored."""
        includes = tmp_path / "docs" / "includes"
        includes.mkdir(parents=True)
        (includes / "abbreviations.md").write_text("content",
                                                    encoding="utf-8")
        monkeypatch.setattr(cis, "INCLUDES_DIR", includes)
        cli_argv('check_includes_sync')
        result = cis.main()
        assert result == 0
