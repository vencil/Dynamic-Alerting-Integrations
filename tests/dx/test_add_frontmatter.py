"""Tests for add_frontmatter.py — YAML front matter injection for documentation.

Merged from previous _extra split (PR test-refactor sweep): metadata-extraction
helpers (detect_language / extract_version / extract_title / tag assignments)
sit alongside file-IO + orchestrator + main() coverage classes appended below.
"""
from __future__ import annotations

import os

import pytest

import add_frontmatter as af


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


# ---------------------------------------------------------------------------
# Orchestrator coverage (was test_add_frontmatter_extra.py)
#
# Audit flagged 33% coverage. The tests below fill the remaining surface:
#   - has_frontmatter / read_file_content / write_file_content (file IO)
#   - generate_frontmatter (template rendering)
#   - process_file (end-to-end + dry-run + already-has-frontmatter shortcut)
#   - _is_excluded (path filter)
#   - find_markdown_files (directory walk + symlink + scope filtering)
#   - main() (--check / --dry-run / --base-dir orchestration)
# ---------------------------------------------------------------------------


class TestHasFrontmatter:
    def test_file_with_frontmatter_returns_true(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("---\ntitle: x\n---\nbody\n", encoding="utf-8")
        assert af.has_frontmatter(str(f)) is True

    def test_file_without_frontmatter_returns_false(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# Heading\n\nbody\n", encoding="utf-8")
        assert af.has_frontmatter(str(f)) is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        assert af.has_frontmatter(str(tmp_path / "ghost.md")) is False

    def test_empty_file_returns_false(self, tmp_path):
        f = tmp_path / "empty.md"
        f.write_text("", encoding="utf-8")
        assert af.has_frontmatter(str(f)) is False


class TestReadWriteFileContent:
    def test_round_trip(self, tmp_path):
        f = tmp_path / "a.md"
        original = "Hello\n中文\n"
        af.write_file_content(str(f), original)
        assert af.read_file_content(str(f)) == original

    def test_overwrite_replaces_content(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("old", encoding="utf-8")
        af.write_file_content(str(f), "new")
        assert f.read_text(encoding="utf-8") == "new"


class TestGenerateFrontmatter:
    def test_basic_block(self):
        out = af.generate_frontmatter(
            "Title", ["alpha", "beta"], ["devs"], "v2.8.0", "en",
        )
        assert out.startswith("---\n")
        assert out.endswith("---\n")
        assert 'title: "Title"' in out
        assert "tags: [alpha, beta]" in out
        assert "audience: [devs]" in out
        assert "version: v2.8.0" in out
        assert "lang: en" in out

    def test_quotes_tags_with_spaces(self):
        out = af.generate_frontmatter(
            "T", ["multi word", "single"], ["adm in", "user"], "v1", "zh",
        )
        # Spaces → quoted; no-space → bare.
        assert '"multi word"' in out
        assert ", single" in out
        assert '"adm in"' in out
        assert ", user" in out


class TestProcessFile:
    def test_already_has_frontmatter_returns_unchanged(self, tmp_path):
        f = tmp_path / "a.md"
        original = "---\ntitle: x\n---\nbody"
        f.write_text(original, encoding="utf-8")
        was_modified, msg = af.process_file(str(f), str(tmp_path))
        assert was_modified is False
        assert "Already has front matter" in msg
        assert f.read_text(encoding="utf-8") == original

    def test_dry_run_does_not_modify_file(self, tmp_path):
        f = tmp_path / "a.md"
        original = "# Heading\n\nbody"
        f.write_text(original, encoding="utf-8")
        was_modified, msg = af.process_file(str(f), str(tmp_path), dry_run=True)
        assert was_modified is True
        assert "Would add" in msg
        # File on disk unchanged.
        assert f.read_text(encoding="utf-8") == original

    def test_real_run_prepends_frontmatter(self, tmp_path):
        # Use a docs/ subdirectory so detect_language / extract_title
        # have realistic input.
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "guide.md"
        f.write_text("# My Guide\n\nbody\n", encoding="utf-8")
        was_modified, msg = af.process_file(str(f), str(tmp_path))
        assert was_modified is True
        assert "Added front matter" in msg
        new = f.read_text(encoding="utf-8")
        assert new.startswith("---\n")
        assert "# My Guide\n\nbody\n" in new


class TestIsExcluded:
    def test_normal_path_not_excluded(self):
        assert af._is_excluded("docs/getting-started.md") is False

    def test_known_exclude_relative_path(self, monkeypatch):
        # Force a known entry into the EXCLUDE list to verify the lookup.
        monkeypatch.setattr(af, "EXCLUDE_RELATIVE_PATHS", {"docs/skip-me.md"})
        assert af._is_excluded("docs/skip-me.md") is True
        assert af._is_excluded("docs/keep-me.md") is False

    def test_prefix_excluded(self, monkeypatch):
        monkeypatch.setattr(af, "EXCLUDE_PATH_PREFIXES", ("docs/internal/",))
        assert af._is_excluded("docs/internal/secret.md") is True
        assert af._is_excluded("docs/public/ok.md") is False

    def test_glob_pattern_excluded(self, monkeypatch):
        monkeypatch.setattr(af, "EXCLUDE_PATH_GLOBS", ("**/draft.md",))
        assert af._is_excluded("docs/draft.md") is True
        assert af._is_excluded("docs/sub/draft.md") is True
        assert af._is_excluded("docs/final.md") is False

    def test_uses_posix_separator_normalisation(self, monkeypatch):
        # Backslash paths are normalised to forward slash before matching.
        monkeypatch.setattr(af, "EXCLUDE_PATH_PREFIXES", ("docs/skip/",))
        # Simulate a Windows path component.
        rel = "docs" + os.sep + "skip" + os.sep + "file.md"
        assert af._is_excluded(rel) is True


class TestFindMarkdownFiles:
    def test_finds_md_in_docs_subdir(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        assert any(f.endswith("a.md") for f in files)

    def test_finds_md_in_rule_packs_subdir(self, tmp_path):
        rp = tmp_path / "rule-packs"
        rp.mkdir()
        (rp / "rule-pack-database.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        assert any("rule-pack-database.md" in f for f in files)

    def test_finds_root_readme_and_changelog(self, tmp_path):
        (tmp_path / "README.md").write_text("x", encoding="utf-8")
        (tmp_path / "CHANGELOG.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        names = {os.path.basename(f) for f in files}
        assert "README.md" in names
        assert "CHANGELOG.md" in names

    def test_root_random_md_not_in_scope(self, tmp_path):
        # Random .md at root (not README/CHANGELOG/README.en.md) is excluded.
        (tmp_path / "random.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        assert all(not f.endswith("random.md") for f in files)

    def test_skips_hidden_dirs_and_node_modules(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / ".hidden").mkdir()
        (docs / ".hidden" / "skip.md").write_text("x", encoding="utf-8")
        (docs / "node_modules").mkdir()
        (docs / "node_modules" / "skip.md").write_text("x", encoding="utf-8")
        (docs / "real.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        names = [os.path.basename(f) for f in files]
        assert names.count("real.md") == 1
        assert "skip.md" not in names

    def test_skips_non_md_files(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "a.txt").write_text("x", encoding="utf-8")
        (docs / "b.md").write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        assert all(f.endswith(".md") for f in files)

    def test_returns_dedup_sorted(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        for n in ["c.md", "a.md", "b.md"]:
            (docs / n).write_text("x", encoding="utf-8")
        files = af.find_markdown_files(str(tmp_path))
        # Sorted asc.
        names = [os.path.basename(f) for f in files]
        assert names == sorted(names)
        # No duplicates.
        assert len(names) == len(set(names))


class TestMain:
    def test_missing_base_dir_returns_one(self, monkeypatch, tmp_path, caplog, cli_argv):
        ghost = tmp_path / "ghost"
        cli_argv("add_frontmatter.py", "--base-dir", str(ghost))
        assert af.main() == 1

    def test_no_md_files_returns_zero(self, monkeypatch, tmp_path, cli_argv):
        # Empty base dir → 0 md files → return 0 (warning logged).
        cli_argv("add_frontmatter.py", "--base-dir", str(tmp_path))
        assert af.main() == 0

    def test_dry_run_returns_zero_and_does_not_modify(self, monkeypatch, tmp_path, cli_argv):
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "a.md"
        original = "# A\n\nbody\n"
        f.write_text(original, encoding="utf-8")
        cli_argv("add_frontmatter.py",
            "--base-dir", str(tmp_path),
            "--dry-run")
        assert af.main() == 0
        # File unchanged.
        assert f.read_text(encoding="utf-8") == original

    def test_check_mode_with_missing_frontmatter_returns_one(
        self, monkeypatch, tmp_path, cli_argv,
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "missing.md").write_text("# No FM\n", encoding="utf-8")
        cli_argv("add_frontmatter.py",
            "--base-dir", str(tmp_path),
            "--check")
        assert af.main() == 1

    def test_check_mode_when_all_have_frontmatter_returns_zero(
        self, monkeypatch, tmp_path, cli_argv,
    ):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "ok.md").write_text(
            "---\ntitle: x\n---\nbody\n", encoding="utf-8",
        )
        cli_argv("add_frontmatter.py",
            "--base-dir", str(tmp_path),
            "--check")
        assert af.main() == 0

    def test_check_mode_does_not_modify_files(self, monkeypatch, tmp_path, cli_argv):
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "missing.md"
        original = "# No FM\n"
        f.write_text(original, encoding="utf-8")
        cli_argv("add_frontmatter.py",
            "--base-dir", str(tmp_path),
            "--check")
        af.main()
        # --check is read-only.
        assert f.read_text(encoding="utf-8") == original

    def test_real_run_writes_frontmatter(self, monkeypatch, tmp_path, cli_argv):
        docs = tmp_path / "docs"
        docs.mkdir()
        f = docs / "a.md"
        f.write_text("# Title\n\nbody\n", encoding="utf-8")
        cli_argv("add_frontmatter.py", "--base-dir", str(tmp_path))
        assert af.main() == 0
        assert f.read_text(encoding="utf-8").startswith("---\n")
