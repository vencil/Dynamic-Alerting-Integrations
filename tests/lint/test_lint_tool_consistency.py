"""Tests for lint_tool_consistency.py — interactive tool registry consistency."""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'lint')
sys.path.insert(0, _TOOLS_DIR)

import lint_tool_consistency as ltc  # noqa: E402


# ---------------------------------------------------------------------------
# parse_registry
# ---------------------------------------------------------------------------
class TestParseRegistry:
    def test_basic_registry(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
              - key: capacity-planner
                file: interactive/tools/capacity-planner.jsx
                audience: [platform, tenant]
                category: planning
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert len(tools) == 1
        assert tools[0]["key"] == "capacity-planner"
        assert tools[0]["file"] == "interactive/tools/capacity-planner.jsx"
        assert tools[0]["audience"] == ["platform", "tenant"]

    def test_multiple_tools(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
              - key: tool-a
                file: interactive/tools/a.jsx
                category: ops
              - key: tool-b
                file: interactive/tools/b.jsx
                category: dx
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert len(tools) == 2
        assert tools[0]["key"] == "tool-a"
        assert tools[1]["key"] == "tool-b"

    def test_inline_dict(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
              - key: my-tool
                file: interactive/tools/my.jsx
                title: { en: "My Tool", zh: "我的工具" }
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert tools[0]["title"]["en"] == "My Tool"
        assert tools[0]["title"]["zh"] == "我的工具"

    def test_block_list(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
              - key: my-tool
                file: interactive/tools/my.jsx
                appears_in:
                  - docs/getting-started/for-tenants.md
                  - docs/scenarios/scaling.md
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert tools[0]["appears_in"] == [
            "docs/getting-started/for-tenants.md",
            "docs/scenarios/scaling.md",
        ]

    def test_empty_file(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text("tools:\n", encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert tools == []

    def test_comments_skipped(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            # This is a comment
            tools:
              # Another comment
              - key: tool-a
                file: interactive/tools/a.jsx
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert len(tools) == 1


# ---------------------------------------------------------------------------
# check_hub_cards
# ---------------------------------------------------------------------------
class TestCheckHubCards:
    def test_card_found(self):
        tools = [{"key": "my-tool", "file": "interactive/tools/my-tool.jsx", "audience": ["platform"]}]
        hub = '<a class="card foo" data-audience="platform" href="tools/interactive/tools/my-tool.jsx">'
        errors, warnings = [], []
        ltc.check_hub_cards(tools, hub, errors, warnings)
        assert len(errors) == 0

    def test_card_missing(self):
        tools = [{"key": "my-tool", "file": "interactive/tools/my-tool.jsx", "audience": ["platform"]}]
        hub = '<div>no cards here</div>'
        errors, warnings = [], []
        ltc.check_hub_cards(tools, hub, errors, warnings)
        assert len(errors) == 1
        assert "no card" in errors[0]

    def test_audience_mismatch(self):
        tools = [{"key": "my-tool", "file": "interactive/tools/my-tool.jsx", "audience": ["platform", "tenant"]}]
        hub = '<a class="card" data-audience="platform" href="interactive/tools/my-tool.jsx">'
        errors, warnings = [], []
        ltc.check_hub_cards(tools, hub, errors, warnings)
        assert any("audience mismatch" in w for w in warnings)


# ---------------------------------------------------------------------------
# check_tool_meta
# ---------------------------------------------------------------------------
class TestCheckToolMeta:
    def test_key_present(self):
        tools = [{"key": "my-tool"}]
        loader = "const TOOL_META = { 'my-tool': { title: 'My Tool' } };"
        errors, warnings = [], []
        ltc.check_tool_meta(tools, loader, errors, warnings)
        assert len(errors) == 0

    def test_key_missing(self):
        tools = [{"key": "missing-tool"}]
        loader = "const TOOL_META = { 'other-tool': { title: 'Other' } };"
        errors, warnings = [], []
        ltc.check_tool_meta(tools, loader, errors, warnings)
        assert len(errors) == 1
        assert "missing-tool" in errors[0]


# ---------------------------------------------------------------------------
# check_jsx_frontmatter
# ---------------------------------------------------------------------------
class TestCheckJsxFrontmatter:
    def test_valid_related(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        (jsx_dir / "a.jsx").write_text(
            "---\nrelated: ['b']\n---\ncontent", encoding="utf-8"
        )
        (jsx_dir / "b.jsx").write_text(
            "---\nrelated: ['a']\n---\ncontent", encoding="utf-8"
        )
        tools = [
            {"key": "a", "file": "interactive/tools/a.jsx"},
            {"key": "b", "file": "interactive/tools/b.jsx"},
        ]
        errors, warnings = [], []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 0

    def test_invalid_related_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        (jsx_dir / "a.jsx").write_text(
            "---\nrelated: ['nonexistent']\n---\ncontent", encoding="utf-8"
        )
        tools = [{"key": "a", "file": "interactive/tools/a.jsx"}]
        errors, warnings = [], []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_missing_jsx_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        tools = [{"key": "ghost", "file": "interactive/tools/ghost.jsx"}]
        errors, warnings = [], []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "not found" in errors[0]


# ---------------------------------------------------------------------------
# check_appears_in
# ---------------------------------------------------------------------------
class TestCheckAppearsIn:
    def test_link_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        md_dir = tmp_path / "docs"
        md_dir.mkdir()
        (md_dir / "guide.md").write_text(
            "Use [Planner](interactive/tools/planner.jsx)", encoding="utf-8"
        )
        tools = [{"key": "planner", "file": "interactive/tools/planner.jsx",
                  "appears_in": ["docs/guide.md"]}]
        errors, warnings = [], []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 0

    def test_link_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        md_dir = tmp_path / "docs"
        md_dir.mkdir()
        (md_dir / "guide.md").write_text("No links here.", encoding="utf-8")
        tools = [{"key": "planner", "file": "interactive/tools/planner.jsx",
                  "appears_in": ["docs/guide.md"]}]
        errors, warnings = [], []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 1
        assert "no link found" in errors[0]

    def test_no_appears_in(self):
        tools = [{"key": "solo", "file": "interactive/tools/solo.jsx"}]
        errors, warnings = [], []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 0


# ---------------------------------------------------------------------------
# check_flow_components
# ---------------------------------------------------------------------------
class TestCheckFlowComponents:
    def test_valid_flow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        (assets / "step.jsx").write_text("content")
        import json
        (assets / "flows.json").write_text(json.dumps({
            "flows": {"onboard": {"steps": [
                {"tool": "wizard", "component": "step.jsx",
                 "title": "Step 1", "hint": "Do this"}
            ]}}
        }), encoding="utf-8")
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 0

    def test_unknown_tool_in_flow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        import json
        (assets / "flows.json").write_text(json.dumps({
            "flows": {"onboard": {"steps": [
                {"tool": "nonexistent", "title": "Step 1", "hint": "Do"}
            ]}}
        }), encoding="utf-8")
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert any("nonexistent" in e for e in errors)

    def test_missing_flows_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, 'PROJECT_ROOT', tmp_path)
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert any("not found" in w for w in warnings)
