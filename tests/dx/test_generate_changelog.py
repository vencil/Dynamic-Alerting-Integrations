"""Tests for generate_changelog.py — Conventional commit parsing and CHANGELOG generation."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import generate_changelog as gc  # noqa: E402


# ---------------------------------------------------------------------------
# parse_commit
# ---------------------------------------------------------------------------
class TestParseCommit:
    """Tests for parse_commit() — conventional commit parsing."""

    def test_simple_feat(self):
        result = gc.parse_commit("feat: add shadow monitoring support")
        assert result is not None
        assert result["type"] == "feat"
        assert result["scope"] == ""
        assert result["breaking"] is False
        assert result["desc"] == "add shadow monitoring support"

    def test_scoped_fix(self):
        result = gc.parse_commit("fix(exporter): resolve HA race condition")
        assert result is not None
        assert result["type"] == "fix"
        assert result["scope"] == "exporter"
        assert result["breaking"] is False

    def test_breaking_change(self):
        result = gc.parse_commit("feat!: remove deprecated v1 API")
        assert result is not None
        assert result["breaking"] is True
        assert result["type"] == "feat"

    def test_scoped_breaking(self):
        result = gc.parse_commit("refactor(config)!: rename _severity_dedup to inhibit-based")
        assert result is not None
        assert result["type"] == "refactor"
        assert result["scope"] == "config"
        assert result["breaking"] is True

    def test_docs_type(self):
        result = gc.parse_commit("docs: update architecture-and-design.md")
        assert result is not None
        assert result["type"] == "docs"

    def test_all_valid_types(self):
        for commit_type in gc.TYPE_SECTIONS:
            result = gc.parse_commit(f"{commit_type}: test message")
            assert result is not None, f"Failed to parse type: {commit_type}"
            assert result["type"] == commit_type

    def test_invalid_no_colon(self):
        result = gc.parse_commit("just a regular commit message")
        assert result is None

    def test_invalid_uppercase_type(self):
        result = gc.parse_commit("FEAT: uppercase type not conventional")
        assert result is None

    def test_empty_string(self):
        result = gc.parse_commit("")
        assert result is None


# ---------------------------------------------------------------------------
# COMMIT_RE regex
# ---------------------------------------------------------------------------
class TestCommitRegex:
    """Tests for the COMMIT_RE regex pattern."""

    def test_captures_all_groups(self):
        m = gc.COMMIT_RE.match("feat(scope)!: description")
        assert m is not None
        assert m.group("type") == "feat"
        assert m.group("scope") == "scope"
        assert m.group("breaking") == "!"
        assert m.group("desc") == "description"

    def test_scope_with_hyphen(self):
        m = gc.COMMIT_RE.match("fix(rule-pack): fix yaml parsing")
        assert m is not None
        assert m.group("scope") == "rule-pack"

    def test_scope_with_slash(self):
        m = gc.COMMIT_RE.match("feat(ops/diagnose): add profile lookup")
        assert m is not None
        assert m.group("scope") == "ops/diagnose"


# ---------------------------------------------------------------------------
# TYPE_SECTIONS / TYPE_EMOJI
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate constant dictionaries."""

    def test_type_sections_covers_all_standard_types(self):
        standard = {"feat", "fix", "perf", "refactor", "docs", "test", "build", "ci", "chore", "style", "revert"}
        assert standard.issubset(set(gc.TYPE_SECTIONS.keys()))

    def test_type_emoji_subset_of_sections(self):
        for key in gc.TYPE_EMOJI:
            assert key in gc.TYPE_SECTIONS, f"Emoji key '{key}' not in TYPE_SECTIONS"


# ---------------------------------------------------------------------------
# format_changelog
# ---------------------------------------------------------------------------
class TestFormatChangelog:
    """Tests for format_changelog() — takes grouped dict + breaking list."""

    def test_basic_formatting(self):
        grouped = {
            "feat": [{"scope": "", "desc": "add feature A"}],
            "fix": [{"scope": "exporter", "desc": "fix bug B"}],
        }
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=[])
        assert "v2.1.0" in result
        assert "Features" in result
        assert "add feature A" in result
        assert "fix bug B" in result

    def test_empty_commits(self):
        result = gc.format_changelog({}, version="v2.1.0", breaking=[])
        assert "v2.1.0" in result

    def test_breaking_changes_highlighted(self):
        grouped = {"feat": [{"scope": "", "desc": "remove old API"}]}
        breaking = [{"scope": "", "desc": "remove old API"}]
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=breaking)
        assert "Breaking" in result
        assert "remove old API" in result

    def test_scoped_commits_grouped(self):
        grouped = {
            "fix": [
                {"scope": "exporter", "desc": "fix race condition"},
                {"scope": "exporter", "desc": "fix reload"},
                {"scope": "", "desc": "fix typo"},
            ],
        }
        result = gc.format_changelog(grouped, version="v2.1.0", breaking=[])
        assert "exporter" in result
        assert "fix race condition" in result
