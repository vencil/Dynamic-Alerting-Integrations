"""Tests for suggest_related.py — Tool similarity scoring and recommendation."""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import suggest_related as sr  # noqa: E402


# ---------------------------------------------------------------------------
# compute_similarity
# ---------------------------------------------------------------------------
class TestComputeSimilarity:
    """Tests for compute_similarity() — Jaccard-based tool similarity scoring."""

    def test_identical_tools_max_score(self):
        tool = {
            "key": "tool-a",
            "audience": ["platform-engineer", "sre"],
            "tags": ["config", "yaml"],
            "icon": "🔧",
            "hub_section": "operations",
        }
        score = sr.compute_similarity(tool, tool)
        # audience=0.4 + tags=0.35 + icon=0.05 + same_section=0.05 = 0.85
        assert score == pytest.approx(0.85, abs=0.01)

    def test_no_overlap_low_score(self):
        tool_a = {
            "key": "tool-a",
            "audience": ["platform-engineer"],
            "tags": ["config"],
            "icon": "🔧",
            "hub_section": "operations",
        }
        tool_b = {
            "key": "tool-b",
            "audience": ["tenant"],
            "tags": ["visualization"],
            "icon": "📊",
            "hub_section": "explore",
        }
        score = sr.compute_similarity(tool_a, tool_b)
        # audience=0, tags=0, icon=0, cross_section=0.15
        assert score == pytest.approx(0.15, abs=0.01)

    def test_partial_audience_overlap(self):
        tool_a = {"key": "a", "audience": ["platform-engineer", "sre"], "tags": []}
        tool_b = {"key": "b", "audience": ["sre", "tenant"], "tags": []}
        score = sr.compute_similarity(tool_a, tool_b)
        # Jaccard: 1/3 = 0.333, * 0.4 = 0.133
        assert 0.1 < score < 0.2

    def test_cross_section_bonus(self):
        # Note: None==None icon match adds 0.05
        tool_a = {"key": "a", "audience": [], "tags": [], "icon": "🔧", "hub_section": "operations"}
        tool_b = {"key": "b", "audience": [], "tags": [], "icon": "📊", "hub_section": "explore"}
        score = sr.compute_similarity(tool_a, tool_b)
        assert score == pytest.approx(0.15, abs=0.01)

    def test_same_section_lower_bonus(self):
        tool_a = {"key": "a", "audience": [], "tags": [], "icon": "🔧", "hub_section": "operations"}
        tool_b = {"key": "b", "audience": [], "tags": [], "icon": "📊", "hub_section": "operations"}
        score = sr.compute_similarity(tool_a, tool_b)
        assert score == pytest.approx(0.05, abs=0.01)

    def test_empty_tools_no_icon(self):
        # None == None for icon counts as a match (+0.05)
        score = sr.compute_similarity({"key": "a"}, {"key": "b"})
        assert score == pytest.approx(0.05, abs=0.01)

    def test_icon_match_bonus(self):
        tool_a = {"key": "a", "audience": [], "tags": [], "icon": "🔧"}
        tool_b = {"key": "b", "audience": [], "tags": [], "icon": "🔧"}
        score = sr.compute_similarity(tool_a, tool_b)
        assert score == pytest.approx(0.05, abs=0.01)

    def test_different_icon_no_bonus(self):
        tool_a = {"key": "a", "audience": [], "tags": [], "icon": "🔧"}
        tool_b = {"key": "b", "audience": [], "tags": [], "icon": "📊"}
        score = sr.compute_similarity(tool_a, tool_b)
        assert score == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------------------
# suggest
# ---------------------------------------------------------------------------
class TestSuggest:
    """Tests for suggest() — top-N recommendation engine."""

    def _tools(self):
        return [
            {"key": "tool-a", "audience": ["sre"], "tags": ["config"], "hub_section": "ops"},
            {"key": "tool-b", "audience": ["sre"], "tags": ["config", "yaml"], "hub_section": "ops"},
            {"key": "tool-c", "audience": ["tenant"], "tags": ["viz"], "hub_section": "explore"},
            {"key": "tool-d", "audience": ["sre", "tenant"], "tags": ["config"], "hub_section": "ops"},
        ]

    def test_returns_all_tools(self):
        tools = self._tools()
        result = sr.suggest(tools, top_n=3)
        assert set(result.keys()) == {"tool-a", "tool-b", "tool-c", "tool-d"}

    def test_respects_top_n(self):
        tools = self._tools()
        result = sr.suggest(tools, top_n=2)
        for key, suggestions in result.items():
            assert len(suggestions) <= 2

    def test_does_not_recommend_self(self):
        tools = self._tools()
        result = sr.suggest(tools, top_n=3)
        for key, suggestions in result.items():
            suggested_keys = [s[0] if isinstance(s, (list, tuple)) else s for s in suggestions]
            assert key not in suggested_keys

    def test_similar_tools_ranked_higher(self):
        tools = self._tools()
        result = sr.suggest(tools, top_n=3)
        # tool-a (sre, config, ops) should recommend tool-b (sre, config+yaml, ops) highly
        a_suggestions = result["tool-a"]
        top_key = a_suggestions[0][0] if isinstance(a_suggestions[0], (list, tuple)) else a_suggestions[0]
        # tool-b or tool-d should be top (both share sre + config + ops)
        assert top_key in ("tool-b", "tool-d")

    def test_single_tool(self):
        tools = [{"key": "only", "audience": ["sre"], "tags": ["config"]}]
        result = sr.suggest(tools, top_n=3)
        assert result["only"] == []


# ---------------------------------------------------------------------------
# parse_registry (basic structure test)
# ---------------------------------------------------------------------------
class TestParseRegistry:
    """Tests for parse_registry() — YAML-like registry parsing without PyYAML."""

    def test_parses_basic_structure(self, tmp_path):
        content = """tools:
  - key: playground
    title: Config Playground
    tags: [config, yaml]
    audience: [tenant, platform-engineer]
    hub_section: explore
  - key: config-diff
    title: Config Version Diff
    tags: [diff, compare]
    audience: [platform-engineer]
    hub_section: tools
"""
        p = tmp_path / "registry.yaml"
        p.write_text(content, encoding="utf-8")
        tools = sr.parse_registry(str(p))
        assert len(tools) == 2
        assert tools[0]["key"] == "playground"
        assert tools[1]["key"] == "config-diff"
        assert "config" in tools[0].get("tags", [])

    def test_handles_empty_file(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("tools:\n", encoding="utf-8")
        tools = sr.parse_registry(str(p))
        assert tools == []
