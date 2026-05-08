"""Extended tests for check_includes_sync.py — coverage boost.

Targets: main() function with --check, --verbose, --fix flags.
"""
import os
import sys
from pathlib import Path

import pytest

import check_includes_sync as cis


# ============================================================
# main() CLI
# ============================================================
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
