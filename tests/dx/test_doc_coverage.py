"""Tests for doc_coverage.py — documentation coverage Dashboard analyzer.

Closes the audit gap (P1-5 / 596 LOC tool was 0% covered). Targets the spine:
  - DocCoverageAnalyzer init + scan_dirs / root_md_files
  - _is_excluded / _is_bilingual_excluded — path filters (per rule)
  - _extract_frontmatter — parses ---/--- block, dict + list values, missing closer
  - _is_frontmatter_complete — required-field gate
  - _has_bilingual_pair — .md ↔ .en.md sibling check
  - _is_external_url — http(s) prefix
  - _is_in_code_block — fence-count parity
  - _resolve_link_path — relative + anchor + out-of-repo
  - analyze + get_statistics — end-to-end on tmp_path repo
  - get_json_report + get_badge_json — output shapes + color thresholds
  - main() CLI — text / --json / --badge / --ci paths
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import doc_coverage as dc
from _lib_exitcodes import EXIT_CALLER_ERROR


# ---------------------------------------------------------------------------
# Helper: build a minimal "repo" under tmp_path
# ---------------------------------------------------------------------------


def _build_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a tmp repo with given relative-path → content mapping."""
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# DocCoverageAnalyzer.__init__
# ---------------------------------------------------------------------------
class TestInit:
    def test_resolves_repo_root_to_absolute(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a.repo_root == tmp_path.resolve()

    def test_default_scan_dirs(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert "docs" in a.scan_dirs
        assert "rule-packs" in a.scan_dirs

    def test_root_md_files_set(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert "README.md" in a.root_md_files
        assert "CHANGELOG.md" in a.root_md_files
        assert "CLAUDE.md" in a.root_md_files

    def test_initial_counters_zero(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a.bilingual_pairs == 0
        assert a.files_with_frontmatter == 0
        assert a.files_with_complete_frontmatter == 0
        assert a.total_links_checked == 0
        assert a.broken_links == []


# ---------------------------------------------------------------------------
# _is_excluded
# ---------------------------------------------------------------------------
class TestIsExcluded:
    def test_normal_file_not_excluded(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        f = tmp_path / "docs" / "guide.md"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        assert a._is_excluded(f) is False

    def test_known_relative_path_excluded(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        # docs/CHANGELOG.md is in the EXCLUDE_RELATIVE_PATHS set.
        f = tmp_path / "docs" / "CHANGELOG.md"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        assert a._is_excluded(f) is True

    def test_prefix_excluded(self, tmp_path, monkeypatch):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        monkeypatch.setattr(a, "EXCLUDE_PATH_PREFIXES", ("docs/internal/",))
        f = tmp_path / "docs" / "internal" / "secret.md"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        assert a._is_excluded(f) is True
        # Sibling under different prefix passes.
        g = tmp_path / "docs" / "public" / "ok.md"
        g.parent.mkdir(parents=True)
        g.write_text("x", encoding="utf-8")
        assert a._is_excluded(g) is False

    def test_glob_excluded(self, tmp_path, monkeypatch):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        monkeypatch.setattr(a, "EXCLUDE_PATH_GLOBS", ("docs/internal/_resume-*.md",))
        f = tmp_path / "docs" / "internal" / "_resume-2026-05-09.md"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        assert a._is_excluded(f) is True

    def test_outside_repo_returns_false(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        # File outside repo — relative_to raises ValueError; method returns False.
        outside = Path("/no/such/path/outside.md")
        assert a._is_excluded(outside) is False


# ---------------------------------------------------------------------------
# _is_bilingual_excluded
# ---------------------------------------------------------------------------
class TestIsBilingualExcluded:
    def test_normal_file_not_excluded(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        f = tmp_path / "docs" / "guide.md"
        f.parent.mkdir()
        f.write_text("x", encoding="utf-8")
        assert a._is_bilingual_excluded(f) is False

    def test_prefix_path_excluded(self, tmp_path, monkeypatch):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        monkeypatch.setattr(a, "BILINGUAL_EXCLUDE_PATH_PREFIXES", ("docs/internal/",))
        f = tmp_path / "docs" / "internal" / "x.md"
        f.parent.mkdir(parents=True)
        f.write_text("x", encoding="utf-8")
        assert a._is_bilingual_excluded(f) is True

    def test_outside_repo_returns_false(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a._is_bilingual_excluded(Path("/no/such/file.md")) is False


# ---------------------------------------------------------------------------
# _extract_frontmatter
# ---------------------------------------------------------------------------
class TestExtractFrontmatter:
    def test_simple_frontmatter(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text(
            "---\ntitle: Example\nlang: en\n---\nbody\n",
            encoding="utf-8",
        )
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        has, fields = a._extract_frontmatter(f)
        assert has is True
        assert fields["title"] == "Example"
        assert fields["lang"] == "en"

    def test_quoted_value_unquoted(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text('---\ntitle: "My Doc"\n---\nbody\n', encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        _, fields = a._extract_frontmatter(f)
        assert fields["title"] == "My Doc"

    def test_list_value_parsed(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text(
            "---\ntags: [alpha, beta, gamma]\n---\nbody",
            encoding="utf-8",
        )
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        _, fields = a._extract_frontmatter(f)
        assert fields["tags"] == ["alpha", "beta", "gamma"]

    def test_no_leading_dashes_returns_empty(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# heading\nbody\n", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        has, fields = a._extract_frontmatter(f)
        assert has is False
        assert fields == {}

    def test_unclosed_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("---\ntitle: x\nbody (no closer)\n", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        has, fields = a._extract_frontmatter(f)
        assert has is False
        assert fields == {}

    def test_too_short_file_returns_empty(self, tmp_path):
        f = tmp_path / "a.md"
        # Less than 3 lines after split → can't have valid frontmatter.
        f.write_text("---\n", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        has, fields = a._extract_frontmatter(f)
        assert has is False
        assert fields == {}

    def test_lines_without_colon_skipped(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text(
            "---\ntitle: x\nthis-line-has-no-colon\nlang: zh\n---\n",
            encoding="utf-8",
        )
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        _, fields = a._extract_frontmatter(f)
        assert "title" in fields
        assert "lang" in fields
        assert "this-line-has-no-colon" not in fields

    def test_oserror_returns_empty(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        ghost = tmp_path / "ghost.md"
        has, fields = a._extract_frontmatter(ghost)
        assert has is False
        assert fields == {}


# ---------------------------------------------------------------------------
# _is_frontmatter_complete
# ---------------------------------------------------------------------------
class TestIsFrontmatterComplete:
    def test_all_required_present(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        fields = {"title": "x", "tags": ["a"], "audience": ["devs"],
                  "version": "v1", "lang": "en"}
        assert a._is_frontmatter_complete(fields) is True

    def test_missing_field_fails(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        fields = {"title": "x", "tags": ["a"], "audience": ["devs"], "lang": "en"}
        # version missing
        assert a._is_frontmatter_complete(fields) is False

    def test_empty_value_fails(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        fields = {"title": "x", "tags": [], "audience": ["devs"],
                  "version": "v1", "lang": "en"}
        # Empty list is falsy.
        assert a._is_frontmatter_complete(fields) is False


# ---------------------------------------------------------------------------
# _has_bilingual_pair
# ---------------------------------------------------------------------------
class TestHasBilingualPair:
    def test_pair_exists(self, tmp_path):
        zh = tmp_path / "guide.md"
        en = tmp_path / "guide.en.md"
        zh.write_text("x", encoding="utf-8")
        en.write_text("y", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a._has_bilingual_pair(zh) is True

    def test_no_en_sibling(self, tmp_path):
        zh = tmp_path / "guide.md"
        zh.write_text("x", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a._has_bilingual_pair(zh) is False

    def test_en_md_returns_false(self, tmp_path):
        en = tmp_path / "guide.en.md"
        en.write_text("y", encoding="utf-8")
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        # An .en.md file is not asked "do you have a pair" — it returns False
        # so it's not double-counted.
        assert a._has_bilingual_pair(en) is False


# ---------------------------------------------------------------------------
# _is_external_url + _is_in_code_block
# ---------------------------------------------------------------------------
class TestIsExternalUrl:
    @pytest.mark.parametrize("url,expected", [
        ("https://example.com", True),
        ("http://example.com", True),
        ("relative/path.md", False),
        ("./local.md", False),
        ("/absolute.md", False),
        ("#anchor", False),
    ])
    def test_classification(self, tmp_path, url, expected):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        assert a._is_external_url(url) is expected


class TestIsInCodeBlock:
    def test_inside_fenced_block_returns_true(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        lines = ["text\n", "```\n", "code\n", "```\n", "more\n"]
        # Line 2 (0-indexed) is "code", inside the fence.
        assert a._is_in_code_block(lines, 2) is True

    def test_outside_fence_returns_false(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        lines = ["text\n", "```\n", "code\n", "```\n", "more\n"]
        # Line 4 (0-indexed) is "more", after both fences (count=2, even).
        assert a._is_in_code_block(lines, 4) is False

    def test_no_fences_returns_false(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        lines = ["one\n", "two\n", "three\n"]
        assert a._is_in_code_block(lines, 1) is False

    def test_unclosed_fence_inside(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        lines = ["```\n", "code\n", "more code\n"]
        # After the opening fence, count is 1 (odd) — inside block.
        assert a._is_in_code_block(lines, 2) is True


# ---------------------------------------------------------------------------
# _resolve_link_path
# ---------------------------------------------------------------------------
class TestResolveLinkPath:
    def test_pure_anchor_returns_source(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        src = tmp_path / "docs" / "a.md"
        src.parent.mkdir()
        src.write_text("x", encoding="utf-8")
        target, valid = a._resolve_link_path(src, "#section-1")
        assert target == src
        assert valid is True

    def test_relative_link_resolved(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        src = tmp_path / "docs" / "a.md"
        src.parent.mkdir()
        src.write_text("x", encoding="utf-8")
        target, valid = a._resolve_link_path(src, "b.md")
        assert target == (tmp_path / "docs" / "b.md").resolve()
        assert valid is True

    def test_link_with_anchor_strips_anchor(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        src = tmp_path / "docs" / "a.md"
        src.parent.mkdir()
        src.write_text("x", encoding="utf-8")
        target, valid = a._resolve_link_path(src, "b.md#sec")
        assert target.name == "b.md"
        assert valid is True

    def test_outside_repo_returns_invalid(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        src = tmp_path / "docs" / "a.md"
        src.parent.mkdir()
        src.write_text("x", encoding="utf-8")
        # ../../../etc/hosts escapes the repo root.
        target, valid = a._resolve_link_path(src, "../../../escape.md")
        assert valid is False


# ---------------------------------------------------------------------------
# analyze + get_statistics — end-to-end on tmp repo
# ---------------------------------------------------------------------------
class TestAnalyzeEndToEnd:
    def test_empty_repo(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        stats = a.get_statistics()
        assert stats["total_files"] == 0
        # Empty → coverage falls back to defaults.
        assert stats["link_health_percent"] == 100

    def test_single_complete_doc(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/guide.md": (
                '---\ntitle: "G"\ntags: [a]\naudience: [devs]\n'
                "version: v1\nlang: zh\n---\nbody\n"
            ),
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        stats = a.get_statistics()
        assert stats["total_files"] == 1
        assert stats["files_with_complete_frontmatter"] == 1
        assert stats["frontmatter_coverage_percent"] == 100.0

    def test_bilingual_pair_counted(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/guide.md": "---\ntitle: G\n---\nbody",
            "docs/guide.en.md": "---\ntitle: G\n---\nbody",
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        stats = a.get_statistics()
        # 1 base file with bilingual pair (guide.md). guide.en.md doesn't
        # count for bilingual denominator.
        assert stats["bilingual_pairs"] == 1
        assert stats["bilingual_coverage_percent"] == 100.0

    def test_broken_link_detected(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/guide.md": (
                "# G\n\n"
                "See [other](nonexistent.md) for details.\n"
            ),
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        stats = a.get_statistics()
        assert stats["total_links_checked"] >= 1
        assert stats["broken_links"] >= 1
        assert stats["link_health_percent"] < 100

    def test_external_links_skipped(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/guide.md": "[external](https://example.com)\n",
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        # External URLs counted in total but not checked for existence.
        assert a.total_links_checked == 1
        assert a.broken_links == []

    def test_links_in_code_block_skipped(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/guide.md": (
                "valid text\n"
                "```\n"
                "[fake](missing.md)\n"  # inside fence — must be skipped
                "```\n"
            ),
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        # The link inside the code block is not checked.
        assert a.total_links_checked == 0


# ---------------------------------------------------------------------------
# get_json_report + get_badge_json
# ---------------------------------------------------------------------------
class TestReportShapes:
    def test_json_report_has_required_keys(self, tmp_path):
        _build_repo(tmp_path, {
            "docs/g.md": (
                '---\ntitle: G\ntags: [a]\naudience: [d]\nversion: v1\nlang: zh\n---\n'
            ),
        })
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        rep = a.get_json_report()
        assert set(rep.keys()) >= {"timestamp", "statistics", "files", "broken_links"}
        assert isinstance(rep["files"], list)

    def test_badge_color_green_at_high_coverage(self, tmp_path):
        # Empty repo → all coverages default 100% / link_health 100%.
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        badge = a.get_badge_json()
        assert badge["schemaVersion"] == 1
        assert badge["label"] == "docs coverage"
        # All defaults add to (0+0+100)/3 ≈ 33% for empty (frontmatter+bilingual=0).
        # Color at 33% is "red".
        assert badge["color"] in {"red", "orange", "yellow", "green"}

    def test_badge_message_format(self, tmp_path):
        a = dc.DocCoverageAnalyzer(str(tmp_path))
        a.analyze()
        badge = a.get_badge_json()
        assert badge["message"].endswith("%")


# ---------------------------------------------------------------------------
# main() CLI
# ---------------------------------------------------------------------------
class TestMainCLI:
    def test_text_mode_prints_dashboard(self, tmp_path, capsys, cli_argv):
        cli_argv("doc_coverage.py", "--repo-root", str(tmp_path))
        rc = dc.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "DOCUMENTATION COVERAGE DASHBOARD" in out
        assert "FRONT MATTER" in out

    def test_json_mode_emits_json(self, tmp_path, capsys, cli_argv):
        cli_argv("doc_coverage.py", "--json", "--repo-root", str(tmp_path))
        rc = dc.main()
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert "statistics" in parsed
        assert "files" in parsed

    def test_badge_mode_emits_shield_json(self, tmp_path, capsys, cli_argv):
        cli_argv("doc_coverage.py", "--badge", "--repo-root", str(tmp_path))
        rc = dc.main()
        assert rc == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["schemaVersion"] == 1
        assert parsed["label"] == "docs coverage"

    def test_nonexistent_repo_returns_caller_error(self, tmp_path, capsys, cli_argv):
        cli_argv("doc_coverage.py", "--repo-root", str(tmp_path / "ghost"))
        rc = dc.main()
        assert rc == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "not found" in err.lower()

    def test_ci_mode_below_threshold_returns_one(self, tmp_path, capsys, cli_argv):
        # Empty repo → frontmatter / bilingual coverage are 0%, well below 80.
        cli_argv("doc_coverage.py", "--ci", "--threshold", "80",
                 "--repo-root", str(tmp_path))
        rc = dc.main()
        assert rc == 1
        err = capsys.readouterr().err
        assert "below threshold" in err.lower() or "below" in err.lower()

    def test_ci_mode_threshold_zero_passes(self, tmp_path, cli_argv):
        # With threshold=0, even an empty repo passes CI.
        cli_argv("doc_coverage.py", "--ci", "--threshold", "0",
                 "--repo-root", str(tmp_path))
        rc = dc.main()
        assert rc == 0
