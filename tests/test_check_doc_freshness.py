"""Tests for check_doc_freshness.py — documentation staleness scanner."""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import check_doc_freshness as cdf  # noqa: E402


# ---------------------------------------------------------------------------
# _load_ignore_patterns
# ---------------------------------------------------------------------------
class TestLoadIgnorePatterns:
    def test_missing_file(self, tmp_path):
        """Returns empty set when ignore file absent."""
        assert cdf._load_ignore_patterns(tmp_path) == set()

    def test_basic_patterns(self, tmp_path):
        ignore = tmp_path / cdf.IGNORE_FILE_NAME
        ignore.write_text("conf.d/\nrule-packs/old\n", encoding="utf-8")
        pats = cdf._load_ignore_patterns(tmp_path)
        assert pats == {"conf.d/", "rule-packs/old"}

    def test_comments_and_blanks(self, tmp_path):
        ignore = tmp_path / cdf.IGNORE_FILE_NAME
        ignore.write_text("# comment\n\nvalid-pattern\n  \n", encoding="utf-8")
        pats = cdf._load_ignore_patterns(tmp_path)
        assert pats == {"valid-pattern"}

    def test_type_specific_pattern(self, tmp_path):
        ignore = tmp_path / cdf.IGNORE_FILE_NAME
        ignore.write_text("missing_file:conf.d/\n", encoding="utf-8")
        pats = cdf._load_ignore_patterns(tmp_path)
        assert "missing_file:conf.d/" in pats


# ---------------------------------------------------------------------------
# _is_ignored
# ---------------------------------------------------------------------------
class TestIsIgnored:
    def test_generic_prefix_match(self):
        issue = {"reference": "conf.d/old.yaml", "type": "missing_file"}
        assert cdf._is_ignored(issue, {"conf.d/"}) is True

    def test_generic_no_match(self):
        issue = {"reference": "docs/README.md", "type": "missing_file"}
        assert cdf._is_ignored(issue, {"conf.d/"}) is False

    def test_type_specific_match(self):
        issue = {"reference": "conf.d/stale.yaml", "type": "missing_file"}
        assert cdf._is_ignored(issue, {"missing_file:conf.d/"}) is True

    def test_type_specific_wrong_type(self):
        issue = {"reference": "conf.d/stale.yaml", "type": "version_mismatch"}
        assert cdf._is_ignored(issue, {"missing_file:conf.d/"}) is False

    def test_empty_patterns(self):
        issue = {"reference": "anything", "type": "missing_file"}
        assert cdf._is_ignored(issue, set()) is False


# ---------------------------------------------------------------------------
# extract_version_from_claude_md
# ---------------------------------------------------------------------------
class TestExtractVersion:
    def test_valid_claude_md(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("## 專案概覽 (v2.1.0)\nsome text", encoding="utf-8")
        assert cdf.extract_version_from_claude_md(tmp_path) == "v2.1.0"

    def test_missing_claude_md(self, tmp_path):
        assert cdf.extract_version_from_claude_md(tmp_path) is None

    def test_no_version_pattern(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Some other heading\n", encoding="utf-8")
        assert cdf.extract_version_from_claude_md(tmp_path) is None


# ---------------------------------------------------------------------------
# extract_paths_from_markdown
# ---------------------------------------------------------------------------
class TestExtractPaths:
    def test_code_block_paths(self):
        md = textwrap.dedent("""\
            ```yaml
            path: conf.d/my-tenant.yaml
            ```
        """)
        paths = cdf.extract_paths_from_markdown(md)
        assert "conf.d/my-tenant.yaml" in paths

    def test_inline_code_paths(self):
        md = "See `scripts/tools/validate_all.py` for details."
        paths = cdf.extract_paths_from_markdown(md)
        assert "scripts/tools/validate_all.py" in paths

    def test_no_false_positives_for_random_text(self):
        md = "This is plain text without any file paths."
        paths = cdf.extract_paths_from_markdown(md)
        assert len(paths) == 0


# ---------------------------------------------------------------------------
# extract_da_tools_commands
# ---------------------------------------------------------------------------
class TestExtractDaToolsCommands:
    def test_code_block_command(self):
        md = textwrap.dedent("""\
            ```bash
            da-tools scaffold my-tenant
            ```
        """)
        cmds = cdf.extract_da_tools_commands(md)
        assert "scaffold" in cmds

    def test_inline_code_command(self):
        md = "Run `da-tools validate-config --all` to check."
        cmds = cdf.extract_da_tools_commands(md)
        assert "validate-config" in cmds

    def test_filters_non_command_words(self):
        md = "The `da-tools command` is useful."
        cmds = cdf.extract_da_tools_commands(md)
        assert "command" not in cmds

    def test_prose_not_matched(self):
        """da-tools in prose (not code) should not be extracted."""
        md = "The da-tools image is built with Docker."
        cmds = cdf.extract_da_tools_commands(md)
        # 'image' is in _NON_COMMAND_WORDS, and prose isn't scanned
        assert len(cmds) == 0


# ---------------------------------------------------------------------------
# extract_docker_images
# ---------------------------------------------------------------------------
class TestExtractDockerImages:
    def test_threshold_exporter(self):
        md = "Image: ghcr.io/vencil/threshold-exporter:v1.0.0"
        images = cdf.extract_docker_images(md)
        assert "threshold-exporter:v1.0.0" in images
        assert images["threshold-exporter:v1.0.0"] == ("threshold-exporter", "v1.0.0")

    def test_da_tools(self):
        md = "Pull ghcr.io/vencil/da-tools:v2.1.0 image."
        images = cdf.extract_docker_images(md)
        assert "da-tools:v2.1.0" in images

    def test_no_match(self):
        md = "No docker images here."
        images = cdf.extract_docker_images(md)
        assert len(images) == 0


# ---------------------------------------------------------------------------
# file_exists
# ---------------------------------------------------------------------------
class TestFileExists:
    def test_existing_file(self, tmp_path):
        (tmp_path / "hello.txt").write_text("hi")
        assert cdf.file_exists(tmp_path, "hello.txt") is True

    def test_missing_file(self, tmp_path):
        assert cdf.file_exists(tmp_path, "nope.txt") is False


# ---------------------------------------------------------------------------
# collect_existing_tools
# ---------------------------------------------------------------------------
class TestCollectExistingTools:
    def test_from_cli_reference(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        cli_ref = docs / "cli-reference.md"
        cli_ref.write_text(
            "# CLI Reference\n\n#### scaffold\n\nScaffold a tenant.\n\n"
            "#### validate-config\n\nValidate config.\n",
            encoding="utf-8",
        )
        tools = cdf.collect_existing_tools(tmp_path)
        assert "scaffold" in tools
        assert "validate-config" in tools

    def test_fallback_to_scripts(self, tmp_path):
        tools_dir = tmp_path / "scripts" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "deprecate_rule.py").write_text("pass")
        (tools_dir / "_lib_python.py").write_text("pass")  # should be skipped
        tools = cdf.collect_existing_tools(tmp_path)
        assert "deprecate-rule" in tools
        assert "deprecate_rule" in tools
        # _lib_python should not be in tools (starts with _lib)
        assert "_lib-python" not in tools


# ---------------------------------------------------------------------------
# check_doc_file (integration-level)
# ---------------------------------------------------------------------------
class TestCheckDocFile:
    def test_detects_missing_file_reference(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        md = docs / "guide.md"
        md.write_text(
            "See `conf.d/nonexistent.yaml` for config.\n", encoding="utf-8"
        )
        issues = []
        cdf.check_doc_file(md, tmp_path, "v2.1.0", set(), issues)
        types = [i["type"] for i in issues]
        assert "missing_file" in types

    def test_detects_missing_command(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        md = docs / "guide.md"
        md.write_text(
            "Run `da-tools nonexistent-cmd --flag`.\n", encoding="utf-8"
        )
        issues = []
        cdf.check_doc_file(md, tmp_path, "v2.1.0", {"scaffold"}, issues)
        missing_cmds = [i for i in issues if i["type"] == "missing_command"]
        assert len(missing_cmds) == 1
        assert "nonexistent-cmd" in missing_cmds[0]["reference"]

    def test_no_issues_for_clean_doc(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        md = docs / "guide.md"
        md.write_text("# Guide\n\nNo code references here.\n", encoding="utf-8")
        issues = []
        cdf.check_doc_file(md, tmp_path, "v2.1.0", set(), issues)
        assert len(issues) == 0

    def test_detects_docker_version_mismatch(self, tmp_path):
        # Setup Chart.yaml for threshold-exporter version lookup
        chart_dir = tmp_path / "components" / "threshold-exporter"
        chart_dir.mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text(
            "version: 1.5.0\n", encoding="utf-8"
        )
        docs = tmp_path / "docs"
        docs.mkdir()
        md = docs / "guide.md"
        md.write_text(
            "Pull `ghcr.io/vencil/threshold-exporter:v9.9.9` image.\n",
            encoding="utf-8",
        )
        issues = []
        cdf.check_doc_file(md, tmp_path, "v2.1.0", set(), issues)
        version_issues = [i for i in issues if i["type"] == "version_mismatch"]
        assert len(version_issues) == 1


# ---------------------------------------------------------------------------
# extract_chart_version
# ---------------------------------------------------------------------------
class TestExtractChartVersion:
    def test_valid_chart(self, tmp_path):
        chart_dir = tmp_path / "components" / "threshold-exporter"
        chart_dir.mkdir(parents=True)
        (chart_dir / "Chart.yaml").write_text("version: 1.5.0\nname: te\n")
        assert cdf.extract_chart_version(tmp_path, "threshold-exporter") == "1.5.0"

    def test_missing_chart(self, tmp_path):
        assert cdf.extract_chart_version(tmp_path, "nonexistent") is None
