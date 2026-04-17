"""Tests for check_frontmatter_versions.py — Frontmatter version global scan."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"))
import check_frontmatter_versions as cfv


# ============================================================
# Unit Tests — extract_frontmatter
# ============================================================

class TestExtractFrontmatter:
    """Tests for frontmatter parsing from markdown files."""

    def test_basic_frontmatter(self, tmp_path):
        """有 frontmatter 且含 version 欄位的標準情況"""
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\nversion: v2.0.0\n---\n# Hello\n",
                      encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.has_frontmatter is True
        assert info.version == "2.0.0"
        assert info.version_line > 0

    def test_no_frontmatter(self, tmp_path):
        """沒有 frontmatter 的 markdown"""
        f = tmp_path / "test.md"
        f.write_text("# Hello\nSome content\n", encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.has_frontmatter is False
        assert info.version is None

    def test_frontmatter_without_version(self, tmp_path):
        """有 frontmatter 但無 version 欄位"""
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\ntags: [a, b]\n---\n# Hello\n",
                      encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.has_frontmatter is True
        assert info.version is None

    def test_version_with_v_prefix(self, tmp_path):
        """version 有 v 前綴 — 應自動去除"""
        f = tmp_path / "test.md"
        f.write_text('---\nversion: v1.5.0\n---\n', encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.version == "1.5.0"

    def test_version_without_prefix(self, tmp_path):
        """version 無前綴的情況"""
        f = tmp_path / "test.md"
        f.write_text('---\nversion: 2.0.0\n---\n', encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.version == "2.0.0"

    def test_version_quoted(self, tmp_path):
        """version 值被引號包覆"""
        f = tmp_path / "test.md"
        f.write_text('---\nversion: "v2.0.0"\n---\n', encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.version == "2.0.0"

    def test_empty_file(self, tmp_path):
        """空檔案"""
        f = tmp_path / "test.md"
        f.write_text("", encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.has_frontmatter is False

    def test_only_delimiters(self, tmp_path):
        """只有 frontmatter 分隔符沒有內容"""
        f = tmp_path / "test.md"
        f.write_text("---\n---\n", encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.has_frontmatter is True
        assert info.version is None

    def test_version_line_number(self, tmp_path):
        """確認 version_line 指向正確行"""
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: Test\nlang: zh\nversion: v2.0.0\n---\n",
                      encoding="utf-8")
        info = cfv.extract_frontmatter(f)
        assert info.version_line == 4


# ============================================================
# Unit Tests — detect_drift
# ============================================================

class TestDetectDrift:
    """Tests for version drift detection."""

    def test_no_drift(self, tmp_path):
        """所有版號一致 — 無漂移"""
        infos = [
            cfv.FrontmatterInfo(
                file_path=tmp_path / "a.md",
                relative_path="docs/a.md",
                version="2.0.0",
                version_line=3,
                has_frontmatter=True,
            ),
        ]
        items = cfv.detect_drift(infos, "2.0.0")
        assert len(items) == 0

    def test_version_mismatch(self, tmp_path):
        """版號不匹配 — 回報 error"""
        infos = [
            cfv.FrontmatterInfo(
                file_path=tmp_path / "a.md",
                relative_path="docs/a.md",
                version="1.9.0",
                version_line=3,
                has_frontmatter=True,
            ),
        ]
        items = cfv.detect_drift(infos, "2.0.0")
        assert len(items) == 1
        assert items[0].severity == "error"
        assert items[0].current_version == "1.9.0"

    def test_missing_version_field(self, tmp_path):
        """有 frontmatter 但無 version — 回報 warning"""
        infos = [
            cfv.FrontmatterInfo(
                file_path=tmp_path / "a.md",
                relative_path="docs/a.md",
                version=None,
                has_frontmatter=True,
            ),
        ]
        items = cfv.detect_drift(infos, "2.0.0")
        assert len(items) == 1
        assert items[0].severity == "warn"

    def test_no_frontmatter_skipped(self, tmp_path):
        """無 frontmatter 的文件應被跳過"""
        infos = [
            cfv.FrontmatterInfo(
                file_path=tmp_path / "a.md",
                relative_path="docs/a.md",
                has_frontmatter=False,
            ),
        ]
        items = cfv.detect_drift(infos, "2.0.0")
        assert len(items) == 0

    def test_mixed_results(self, tmp_path):
        """混合情況"""
        infos = [
            cfv.FrontmatterInfo(file_path=tmp_path / "ok.md",
                                relative_path="docs/ok.md",
                                version="2.0.0", version_line=3,
                                has_frontmatter=True),
            cfv.FrontmatterInfo(file_path=tmp_path / "old.md",
                                relative_path="docs/old.md",
                                version="1.0.0", version_line=3,
                                has_frontmatter=True),
            cfv.FrontmatterInfo(file_path=tmp_path / "missing.md",
                                relative_path="docs/missing.md",
                                version=None,
                                has_frontmatter=True),
            cfv.FrontmatterInfo(file_path=tmp_path / "nofm.md",
                                relative_path="docs/nofm.md",
                                has_frontmatter=False),
        ]
        items = cfv.detect_drift(infos, "2.0.0")
        assert len(items) == 2  # old + missing


# ============================================================
# Unit Tests — scan_docs
# ============================================================

class TestScanDocs:
    """Tests for directory scanning."""

    def test_scan_finds_md_files(self, tmp_path):
        """掃描目錄下的 .md 文件"""
        (tmp_path / "a.md").write_text("---\nversion: v1.0.0\n---\n",
                                        encoding="utf-8")
        (tmp_path / "b.md").write_text("# No frontmatter\n",
                                        encoding="utf-8")
        results = cfv.scan_docs(tmp_path)
        assert len(results) == 2

    def test_scan_recursive(self, tmp_path):
        """遞迴掃描子目錄"""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.md").write_text("---\nversion: v2.0.0\n---\n",
                                      encoding="utf-8")
        results = cfv.scan_docs(tmp_path)
        assert len(results) == 1

    def test_scan_skips_hidden(self, tmp_path):
        """跳過隱藏目錄"""
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "secret.md").write_text("---\nversion: v1.0.0\n---\n",
                                           encoding="utf-8")
        results = cfv.scan_docs(tmp_path)
        assert len(results) == 0

    def test_scan_empty_dir(self, tmp_path):
        """空目錄返回空列表"""
        results = cfv.scan_docs(tmp_path)
        assert len(results) == 0

    def test_scan_nonexistent_dir(self, tmp_path):
        """不存在的目錄返回空列表"""
        results = cfv.scan_docs(tmp_path / "nonexistent")
        assert len(results) == 0


# ============================================================
# Unit Tests — fix_drift
# ============================================================

class TestFixDrift:
    """Tests for in-place version fix."""

    def test_fix_updates_version(self, tmp_path, monkeypatch):
        """修復版號漂移 — 原地更新"""
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        doc = tmp_path / "docs" / "test.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("---\ntitle: Test\nversion: v1.0.0\n---\n# Hello\n",
                        encoding="utf-8")

        items = [cfv.DriftItem(
            file="docs/test.md", line=3,
            current_version="1.0.0", expected_version="2.0.0",
            severity="error",
        )]
        fixed = cfv.fix_drift(items, "2.0.0")
        assert fixed == 1
        content = doc.read_text(encoding="utf-8")
        assert "version: v2.0.0" in content

    def test_fix_skips_warnings(self, tmp_path, monkeypatch):
        """只修復 error，跳過 warning（missing version）"""
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        items = [cfv.DriftItem(
            file="docs/test.md", line=1,
            current_version="(missing)", expected_version="2.0.0",
            severity="warn",
        )]
        fixed = cfv.fix_drift(items, "2.0.0")
        assert fixed == 0


# ============================================================
# Unit Tests — read_platform_version
# ============================================================


class TestReadPlatformVersion:
    """Tests for read_platform_version supporting both CLAUDE.md header formats."""

    def test_inline_format(self, tmp_path, monkeypatch):
        """Inline format: ``## 專案概覽 (v2.0.0)`` — the early format."""
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.0.0)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)
        assert cfv.read_platform_version() == "2.0.0"

    def test_body_format(self, tmp_path, monkeypatch):
        """Body format: ``## 專案概覽`` + bold tagline with version on next line.

        This mirrors the current CLAUDE.md (v2.6.0+) layout where the
        heading is separated from the version-bearing tagline by a blank
        line, e.g.::

            ## 專案概覽

            **Multi-Tenant Dynamic Alerting 平台 (v2.6.0)** — ...
        """
        claude = tmp_path / "CLAUDE.md"
        claude.write_text(
            "## 專案概覽\n\n"
            "**Multi-Tenant Dynamic Alerting 平台 (v2.6.0)** — Config-driven.\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)
        assert cfv.read_platform_version() == "2.6.0"

    def test_missing_file(self, tmp_path, monkeypatch):
        """Missing CLAUDE.md returns None (caller decides how to error)."""
        monkeypatch.setattr(cfv, "CLAUDE_MD", tmp_path / "nonexistent.md")
        assert cfv.read_platform_version() is None

    def test_no_anchor(self, tmp_path, monkeypatch):
        """File exists but has no 專案概覽 anchor → None."""
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("# Some other content\n(v9.9.9)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)
        # Must not match the stray (v9.9.9) — anchor-less.
        assert cfv.read_platform_version() is None

    def test_version_beyond_window(self, tmp_path, monkeypatch):
        """Version too far from the 專案概覽 anchor is ignored.

        Prevents accidentally picking up an unrelated ``(vX.Y.Z)`` that
        appears much later in the file (e.g. in a changelog snippet).
        """
        claude = tmp_path / "CLAUDE.md"
        claude.write_text(
            "## 專案概覽\n"
            + ("\n" * 10)
            + "(v9.9.9)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)
        assert cfv.read_platform_version() is None


# ============================================================
# Unit Tests — format_text_report / format_json_report
# ============================================================

class TestReportFormatting:
    """Tests for report formatting."""

    def test_text_report_no_drift(self):
        """無漂移的文字報告"""
        text = cfv.format_text_report([], "2.0.0", 10, 8)
        assert "✅" in text
        assert "8 frontmatter versions match" in text

    def test_text_report_with_drift(self):
        """有漂移的文字報告"""
        items = [cfv.DriftItem(
            file="docs/old.md", line=3,
            current_version="1.0.0", expected_version="2.0.0",
            severity="error",
        )]
        text = cfv.format_text_report(items, "2.0.0", 10, 8)
        assert "❌" in text
        assert "1 error" in text

    def test_json_report_structure(self):
        """JSON 報告結構"""
        items = [cfv.DriftItem(
            file="docs/old.md", line=3,
            current_version="1.0.0", expected_version="2.0.0",
        )]
        text = cfv.format_json_report(items, "2.0.0", 10, 8)
        data = json.loads(text)
        assert data["expected_version"] == "2.0.0"
        assert data["total_scanned"] == 10
        assert len(data["items"]) == 1
        assert data["summary"]["errors"] == 1


# ============================================================
# CLI Tests
# ============================================================

class TestCLI:
    """Tests for CLI entry point."""

    def test_cli_json_output(self, tmp_path, monkeypatch):
        """--json 輸出 JSON 格式"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "test.md").write_text(
            "---\ntitle: Test\nversion: v2.0.0\n---\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "DOCS_DIR", docs)
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        # Create CLAUDE.md for platform version
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.0.0)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)

        cfv.main(["--json"])  # Should not exit

    def test_cli_ci_exit_on_error(self, tmp_path, monkeypatch):
        """--ci 有 error 時 exit 1"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "old.md").write_text(
            "---\nversion: v1.0.0\n---\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "DOCS_DIR", docs)
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.0.0)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)

        with pytest.raises(SystemExit) as exc_info:
            cfv.main(["--ci"])
        assert exc_info.value.code == 1

    def test_cli_ci_pass(self, tmp_path, monkeypatch):
        """--ci 全部通過時正常結束"""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "ok.md").write_text(
            "---\nversion: v2.0.0\n---\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "DOCS_DIR", docs)
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.0.0)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)

        cfv.main(["--ci"])  # Should not raise

    def test_cli_missing_claude_md(self, tmp_path, monkeypatch):
        """CLAUDE.md 不存在時 exit 1"""
        monkeypatch.setattr(cfv, "CLAUDE_MD", tmp_path / "nonexistent.md")
        with pytest.raises(SystemExit) as exc_info:
            cfv.main([])
        assert exc_info.value.code == 1

    def test_cli_fix(self, tmp_path, monkeypatch):
        """--fix 修復漂移"""
        docs = tmp_path / "docs"
        docs.mkdir()
        doc = docs / "old.md"
        doc.write_text("---\ntitle: Test\nversion: v1.0.0\n---\n",
                        encoding="utf-8")
        monkeypatch.setattr(cfv, "DOCS_DIR", docs)
        monkeypatch.setattr(cfv, "REPO_ROOT", tmp_path)
        claude = tmp_path / "CLAUDE.md"
        claude.write_text("## 專案概覽 (v2.0.0)\n", encoding="utf-8")
        monkeypatch.setattr(cfv, "CLAUDE_MD", claude)

        cfv.main(["--fix"])
        content = doc.read_text(encoding="utf-8")
        assert "version: v2.0.0" in content
