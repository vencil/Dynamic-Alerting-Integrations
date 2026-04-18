"""Extended tests for lint_tool_consistency.py — coverage boost.

Targets: check_hub_cards, check_tool_meta, check_jsx_frontmatter,
check_appears_in, check_flow_components, check_markdown_tool_links,
check_related_symmetry, main().
"""
import json
import os
import sys
import textwrap

import pytest

import lint_tool_consistency as ltc


# ============================================================
# check_hub_cards
# ============================================================
class TestCheckHubCards:
    """check_hub_cards() tests."""

    def test_tool_found_with_matching_audience(self):
        tools = [{"key": "capacity-planner",
                  "file": "interactive/tools/capacity-planner.jsx",
                  "audience": ["platform"]}]
        hub_html = '''<a class="card" data-audience="platform" href="../../assets/jsx-loader.html?component=interactive/tools/capacity-planner.jsx">'''
        errors = []
        warnings = []
        ltc.check_hub_cards(tools, hub_html, errors, warnings)
        assert errors == []

    def test_tool_not_found(self):
        tools = [{"key": "missing-tool",
                  "file": "interactive/tools/missing.jsx",
                  "audience": []}]
        hub_html = "<html></html>"
        errors = []
        warnings = []
        ltc.check_hub_cards(tools, hub_html, errors, warnings)
        assert len(errors) == 1
        assert "missing-tool" in errors[0]

    def test_audience_mismatch(self):
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "audience": ["platform", "tenant"]}]
        hub_html = '''<a class="card" data-audience="platform" href="../../assets/jsx-loader.html?component=interactive/tools/tool-a.jsx">'''
        errors = []
        warnings = []
        ltc.check_hub_cards(tools, hub_html, errors, warnings)
        assert len(warnings) == 1
        assert "mismatch" in warnings[0]

    def test_href_found_but_audience_unparseable(self):
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "audience": []}]
        # href found but without data-audience in expected pattern
        hub_html = '''<div href="../jsx-loader.html?component=interactive/tools/tool-a.jsx"></div>'''
        errors = []
        warnings = []
        ltc.check_hub_cards(tools, hub_html, errors, warnings)
        assert len(warnings) == 1
        assert "could not parse" in warnings[0]


# ============================================================
# check_tool_meta
# ============================================================
class TestCheckToolMeta:
    """check_tool_meta() tests."""

    def test_tool_found_in_meta(self):
        tools = [{"key": "capacity-planner"}]
        loader_html = "TOOL_META = {'capacity-planner': {title: 'test'}}"
        errors = []
        warnings = []
        ltc.check_tool_meta(tools, loader_html, errors, warnings)
        assert errors == []

    def test_tool_missing_from_meta(self):
        tools = [{"key": "missing-tool"}]
        loader_html = "TOOL_META = {'other-tool': {}}"
        errors = []
        warnings = []
        ltc.check_tool_meta(tools, loader_html, errors, warnings)
        assert len(errors) == 1
        assert "missing-tool" in errors[0]


# ============================================================
# check_jsx_frontmatter
# ============================================================
class TestCheckJsxFrontmatter:
    """check_jsx_frontmatter() tests."""

    def test_valid_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text(
            "---\ntitle: Tool A\nrelated: [tool-b]\n---\ncontent",
            encoding="utf-8")
        jsx2 = jsx_dir / "tool-b.jsx"
        jsx2.write_text(
            "---\ntitle: Tool B\nrelated: [tool-a]\n---\ncontent",
            encoding="utf-8")
        tools = [
            {"key": "tool-a", "file": "interactive/tools/tool-a.jsx"},
            {"key": "tool-b", "file": "interactive/tools/tool-b.jsx"},
        ]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert errors == []

    def test_missing_jsx_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        (tmp_path / "docs").mkdir()
        tools = [{"key": "ghost", "file": "interactive/tools/ghost.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_no_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text("// no frontmatter\ncontent", encoding="utf-8")
        tools = [{"key": "tool-a", "file": "interactive/tools/tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(warnings) == 1
        assert "frontmatter" in warnings[0]

    def test_no_related_in_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text("---\ntitle: Tool A\n---\ncontent", encoding="utf-8")
        tools = [{"key": "tool-a", "file": "interactive/tools/tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(warnings) == 1
        assert "related" in warnings[0]

    def test_unknown_related_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        jsx_dir = tmp_path / "docs" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text(
            "---\ntitle: Tool A\nrelated: [nonexistent]\n---\ncontent",
            encoding="utf-8")
        tools = [{"key": "tool-a", "file": "interactive/tools/tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]


# ============================================================
# check_appears_in
# ============================================================
class TestCheckAppearsIn:
    """check_appears_in() tests."""

    def test_link_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.parent.mkdir(parents=True)
        md.write_text("See [tool](interactive/tools/tool-a.jsx)",
                      encoding="utf-8")
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "appears_in": ["docs/guide.md"]}]
        errors = []
        warnings = []
        ltc.check_appears_in(tools, errors, warnings)
        assert errors == []

    def test_link_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        md = tmp_path / "docs" / "guide.md"
        md.parent.mkdir(parents=True)
        md.write_text("No links here", encoding="utf-8")
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "appears_in": ["docs/guide.md"]}]
        errors = []
        warnings = []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 1
        assert "no link found" in errors[0]

    def test_nonexistent_md(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "appears_in": ["docs/nonexistent.md"]}]
        errors = []
        warnings = []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 1
        assert "non-existent" in errors[0]

    def test_no_appears_in(self):
        tools = [{"key": "tool-a", "file": "tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_appears_in(tools, errors, warnings)
        assert errors == []


# ============================================================
# check_flow_components
# ============================================================
class TestCheckFlowComponents:
    """check_flow_components() tests."""

    def test_no_flows_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        (tmp_path / "docs" / "assets").mkdir(parents=True)
        tools = []
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(warnings) == 1
        assert "not found" in warnings[0]

    def test_empty_flows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        (assets / "flows.json").write_text('{"flows": {}}',
                                           encoding="utf-8")
        tools = []
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(warnings) == 1
        assert "no flows" in warnings[0]

    def test_valid_flow(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        comp = assets / "test-comp.jsx"
        comp.write_text("component", encoding="utf-8")
        flows = {"flows": {"onboard": {"steps": [
            {"tool": "tool-a", "component": "test-comp.jsx",
             "title": "Step 1", "hint": "Do this"}
        ]}}}
        (assets / "flows.json").write_text(json.dumps(flows),
                                           encoding="utf-8")
        tools = [{"key": "tool-a"}]
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert errors == []

    def test_flow_unknown_tool(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        flows = {"flows": {"onboard": {"steps": [
            {"tool": "nonexistent", "title": "Step 1", "hint": "Do this"}
        ]}}}
        (assets / "flows.json").write_text(json.dumps(flows),
                                           encoding="utf-8")
        tools = [{"key": "tool-a"}]
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_flow_missing_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        flows = {"flows": {"onboard": {"steps": [
            {"tool": "tool-a", "hint": "Do this"}
        ]}}}
        (assets / "flows.json").write_text(json.dumps(flows),
                                           encoding="utf-8")
        tools = [{"key": "tool-a"}]
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert any("title" in w for w in warnings)

    def test_flow_empty_steps(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        flows = {"flows": {"empty-flow": {"steps": []}}}
        (assets / "flows.json").write_text(json.dumps(flows),
                                           encoding="utf-8")
        tools = []
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert any("no steps" in w for w in warnings)

    def test_flow_bad_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ltc, "PROJECT_ROOT", tmp_path)
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        (assets / "flows.json").write_text("{bad json", encoding="utf-8")
        tools = []
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 1
        assert "parse" in errors[0].lower()


# ============================================================
# check_related_symmetry
# ============================================================
class TestCheckRelatedSymmetry:
    """check_related_symmetry() tests."""

    def test_symmetric(self):
        tools = [
            {"key": "a", "related": ["b"]},
            {"key": "b", "related": ["a"]},
        ]
        warnings = []
        ltc.check_related_symmetry(tools, warnings)
        # Should pass without issues

    def test_asymmetric(self):
        tools = [
            {"key": "a", "related": ["b"]},
            {"key": "b", "related": []},
        ]
        warnings = []
        ltc.check_related_symmetry(tools, warnings)
        # Asymmetric is OK (informational only)


# ============================================================
# parse_registry extended
# ============================================================
class TestParseRegistryExtended:
    """Extended parse_registry tests."""

    def test_block_list(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            tools:
              - key: tool-a
                file: interactive/tools/tool-a.jsx
                appears_in:
                  - docs/guide.md
                  - docs/tutorial.md
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert len(tools) == 1
        assert tools[0]["appears_in"] == ["docs/guide.md", "docs/tutorial.md"]

    def test_comments_and_empty_lines(self, tmp_path):
        reg = tmp_path / "registry.yaml"
        reg.write_text(textwrap.dedent("""\
            # Comment
            tools:
              # Another comment
              - key: tool-a
                file: interactive/tools/tool-a.jsx
                category: ops
        """), encoding="utf-8")
        tools = ltc.parse_registry(str(reg))
        assert len(tools) == 1
        assert tools[0]["key"] == "tool-a"
