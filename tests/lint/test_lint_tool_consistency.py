"""Tests for lint_tool_consistency.py — interactive tool registry consistency.

Merged from previous _extended split (PR test-refactor sweep). Each existing
class has unique edge cases appended after its base methods.
TestCheckRelatedSymmetry was dropped along with check_related_symmetry
itself (loop body was `pass` — no-op dead code, both tests assertion-free).
Dropped from _extended:
test methods that were duplicates of base (test_card_found / test_card_missing /
test_audience_mismatch / test_key_present / test_key_missing / test_valid_related /
test_missing_jsx_file / test_unknown_related_key / test_link_found / test_link_missing /
test_no_appears_in / test_valid_flow / test_flow_unknown_tool / test_no_flows_file)
plus TestParseRegistryExtended entirely (both methods were dups of base
TestParseRegistry).
"""
from __future__ import annotations

import json
import textwrap

import pytest

import lint_tool_consistency as ltc


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

    # ── unique edge case (merged from _extended) ────────────────────────

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


# ---------------------------------------------------------------------------
# parse_flow_map
# ---------------------------------------------------------------------------
class TestParseFlowMap:
    def test_multiline_block(self):
        loader = textwrap.dedent("""\
            var CUSTOM_FLOW_MAP = {
              'wizard': '../getting-started/wizard.jsx',
              'playground': '../interactive/tools/playground.jsx'
            };
        """)
        fm = ltc.parse_flow_map(loader)
        assert fm == {
            "wizard": "../getting-started/wizard.jsx",
            "playground": "../interactive/tools/playground.jsx",
        }

    def test_inline_block(self):
        loader = "const CUSTOM_FLOW_MAP = { 'a': '../x/a.jsx' };"
        assert ltc.parse_flow_map(loader) == {"a": "../x/a.jsx"}

    def test_block_missing_returns_none(self):
        assert ltc.parse_flow_map("<html>no map here</html>") is None

    def test_pairs_outside_block_ignored(self):
        # The 'outside' pair sits at line start inside a LATER multi-line
        # object — only the brace-depth stop keeps it out (a line-anchored
        # regex alone would still collect it).
        loader = textwrap.dedent("""\
            var CUSTOM_FLOW_MAP = {
              'inside': '../x/inside.jsx'
            };
            var OTHER = {
              'outside': '../x/outside.jsx'
            };
        """)
        assert ltc.parse_flow_map(loader) == {"inside": "../x/inside.jsx"}


# ---------------------------------------------------------------------------
# check_tool_meta (CUSTOM_FLOW_MAP membership; TOOL_META removed in TRK-230z)
# ---------------------------------------------------------------------------
class TestCheckToolMeta:
    def test_key_present(self):
        tools = [{"key": "my-tool"}]
        loader = "var CUSTOM_FLOW_MAP = { 'my-tool': '../interactive/tools/my-tool.jsx' };"
        errors, warnings = [], []
        ltc.check_tool_meta(tools, loader, errors, warnings)
        assert len(errors) == 0

    def test_key_missing(self):
        tools = [{"key": "missing-tool"}]
        loader = "var CUSTOM_FLOW_MAP = { 'other-tool': '../interactive/tools/other-tool.jsx' };"
        errors, warnings = [], []
        ltc.check_tool_meta(tools, loader, errors, warnings)
        assert len(errors) == 1
        assert "missing-tool" in errors[0]

    def test_substring_elsewhere_does_not_pass(self):
        # Regression: the old probe was `'{key}' in loader_html` — any quoted
        # occurrence anywhere in the HTML passed, even outside the map.
        tools = [{"key": "my-tool"}]
        loader = (
            "var CUSTOM_FLOW_MAP = { 'other-tool': '../x/other-tool.jsx' };\n"
            "console.log('my-tool');"
        )
        errors, warnings = [], []
        ltc.check_tool_meta(tools, loader, errors, warnings)
        assert len(errors) == 1
        assert "my-tool" in errors[0]

    def test_map_block_absent_fails_loud(self):
        tools = [{"key": "my-tool"}]
        errors, warnings = [], []
        ltc.check_tool_meta(tools, "<html>no map</html>", errors, warnings)
        assert len(errors) == 1
        assert "CUSTOM_FLOW_MAP block not found" in errors[0]


# ---------------------------------------------------------------------------
# check_flow_map_dist
# ---------------------------------------------------------------------------
class TestCheckFlowMapDist:
    LOADER = textwrap.dedent("""\
        var CUSTOM_FLOW_MAP = {
          'my-tool': '../interactive/tools/my-tool.jsx'
        };
    """)

    def test_dist_present(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        dist = tmp_path / "docs" / "assets" / "dist"
        dist.mkdir(parents=True)
        (dist / "my-tool.js").write_text("export {};", encoding="utf-8")
        errors, warnings = [], []
        ltc.check_flow_map_dist(self.LOADER, errors, warnings)
        assert len(errors) == 0

    def test_dist_missing_is_error(self, patch_repo_root):
        patch_repo_root(ltc, "PROJECT_ROOT")
        errors, warnings = [], []
        ltc.check_flow_map_dist(self.LOADER, errors, warnings)
        assert len(errors) == 1
        assert "my-tool.js" in errors[0]
        assert "404" in errors[0]

    def test_no_map_is_silent(self, patch_repo_root):
        # Absence of the block is check_tool_meta's finding, not a dup here.
        patch_repo_root(ltc, "PROJECT_ROOT")
        errors, warnings = [], []
        ltc.check_flow_map_dist("<html>no map</html>", errors, warnings)
        assert errors == [] and warnings == []


# ---------------------------------------------------------------------------
# check_jsx_frontmatter
# ---------------------------------------------------------------------------
class TestCheckJsxFrontmatter:
    def test_valid_related(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        jsx_dir = tmp_path / "tools" / "portal" / "src" / "interactive" / "tools"
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

    def test_invalid_related_key(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        jsx_dir = tmp_path / "tools" / "portal" / "src" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        (jsx_dir / "a.jsx").write_text(
            "---\nrelated: ['nonexistent']\n---\ncontent", encoding="utf-8"
        )
        tools = [{"key": "a", "file": "interactive/tools/a.jsx"}]
        errors, warnings = [], []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_missing_jsx_file(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        tools = [{"key": "ghost", "file": "interactive/tools/ghost.jsx"}]
        errors, warnings = [], []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(errors) == 1
        assert "not found" in errors[0]

    # ── unique edge cases (merged from _extended) ───────────────────────

    def test_no_frontmatter(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        jsx_dir = tmp_path / "tools" / "portal" / "src" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text("// no frontmatter\ncontent", encoding="utf-8")
        tools = [{"key": "tool-a", "file": "interactive/tools/tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(warnings) == 1
        assert "frontmatter" in warnings[0]

    def test_no_related_in_frontmatter(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        jsx_dir = tmp_path / "tools" / "portal" / "src" / "interactive" / "tools"
        jsx_dir.mkdir(parents=True)
        jsx = jsx_dir / "tool-a.jsx"
        jsx.write_text("---\ntitle: Tool A\n---\ncontent", encoding="utf-8")
        tools = [{"key": "tool-a", "file": "interactive/tools/tool-a.jsx"}]
        errors = []
        warnings = []
        ltc.check_jsx_frontmatter(tools, errors, warnings)
        assert len(warnings) == 1
        assert "related" in warnings[0]


# ---------------------------------------------------------------------------
# check_appears_in
# ---------------------------------------------------------------------------
class TestCheckAppearsIn:
    def test_link_found(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    def test_link_missing(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    # ── unique edge case (merged from _extended) ────────────────────────

    def test_nonexistent_md(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        tools = [{"key": "tool-a",
                  "file": "interactive/tools/tool-a.jsx",
                  "appears_in": ["docs/nonexistent.md"]}]
        errors = []
        warnings = []
        ltc.check_appears_in(tools, errors, warnings)
        assert len(errors) == 1
        assert "non-existent" in errors[0]


# ---------------------------------------------------------------------------
# check_flow_components
# ---------------------------------------------------------------------------
# Valid flow-level bilingual metadata, reused across fixtures — flow title /
# desc became required (en+zh) when the flow-e2e-check smoke script retired
# into this lint.
FLOW_META = {
    "title": {"en": "Onboarding", "zh": "新手上路"},
    "desc": {"en": "Get started", "zh": "快速開始"},
}


class TestCheckFlowComponents:
    def test_valid_flow(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        # TRK-242: flow component paths now resolve against tools/portal/src/
        # (after stripping leading "./" / "../"); flows.json itself stays
        # under docs/assets/.
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        portal_src = tmp_path / "tools" / "portal" / "src"
        portal_src.mkdir(parents=True)
        (portal_src / "step.jsx").write_text("content")
        dist = assets / "dist"
        dist.mkdir()
        (dist / "step.js").write_text("export {};", encoding="utf-8")
        import json
        (assets / "flows.json").write_text(json.dumps({
            "flows": {"onboard": {**FLOW_META, "steps": [
                {"tool": "wizard", "component": "step.jsx",
                 "title": "Step 1", "hint": "Do this"}
            ]}}
        }), encoding="utf-8")
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 0

    def test_step_component_without_dist_bundle(self, patch_repo_root):
        # Source JSX exists but was never built — runtime loadDistBundle
        # would 404 on docs/assets/dist/step.js.
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        portal_src = tmp_path / "tools" / "portal" / "src"
        portal_src.mkdir(parents=True)
        (portal_src / "step.jsx").write_text("content")
        (assets / "flows.json").write_text(json.dumps({
            "flows": {"onboard": {**FLOW_META, "steps": [
                {"tool": "wizard", "component": "step.jsx",
                 "title": "Step 1", "hint": "Do this"}
            ]}}
        }), encoding="utf-8")
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 1
        assert "no dist bundle" in errors[0]
        assert "step.js" in errors[0]

    def test_unknown_tool_in_flow(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    def test_missing_flows_json(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        tools = [{"key": "wizard"}]
        errors, warnings = [], []
        ltc.check_flow_components(tools, errors, warnings)
        assert any("not found" in w for w in warnings)

    # ── unique edge cases (merged from _extended) ───────────────────────

    def test_empty_flows(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    def test_flow_missing_title(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    def test_flow_empty_steps(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
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

    def test_flow_bad_json(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        (assets / "flows.json").write_text("{bad json", encoding="utf-8")
        tools = []
        errors = []
        warnings = []
        ltc.check_flow_components(tools, errors, warnings)
        assert len(errors) == 1
        assert "parse" in errors[0].lower()

    # ── structural checks ported from the retired flow-e2e-check script ──

    @staticmethod
    def _write_flows(tmp_path, flow):
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "flows.json").write_text(
            json.dumps({"flows": {"onboard": flow}}), encoding="utf-8"
        )

    def test_flow_level_title_lang_hole_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {
            "title": {"en": "Onboarding"},  # zh missing
            "desc": FLOW_META["desc"],
            "steps": [{"tool": "wizard"}],
        })
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("title.zh missing" in e for e in errors)

    def test_flow_level_missing_desc_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {
            "title": FLOW_META["title"],
            "steps": [{"tool": "wizard"}],
        })
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("missing 'desc'" in e for e in errors)

    def test_step_missing_tool_and_component_are_errors(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [{}]})
        errors, warnings = [], []
        ltc.check_flow_components([], errors, warnings)
        assert any("missing 'tool'" in e for e in errors)
        assert any("missing 'component'" in e for e in errors)

    def test_step_title_lang_hole_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "title": {"en": "Step 1"},
             "hint": {"en": "Do", "zh": "做"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("title.zh missing" in e for e in errors)

    def test_step_hint_lang_hole_is_warning(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "title": {"en": "Step 1", "zh": "步驟一"},
             "hint": {"en": "Do"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("hint.zh missing" in w for w in warnings)
        assert not any("hint.zh" in e for e in errors)

    def test_condition_not_object_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "condition": "role=platform"}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("'condition' must be an object" in e for e in errors)

    def test_condition_value_not_array_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "condition": {"role": "platform"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("condition['role'] must be an array" in e for e in errors)

    def test_validation_required_state_not_array_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "validation": {"required_state": "role"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("required_state must be an array" in e for e in errors)

    def test_flow_not_object_is_error_not_crash(self, patch_repo_root):
        # Grammar guard: a flow that is a bare string must come back as a
        # structured error, not an AttributeError traceback.
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        assets = tmp_path / "docs" / "assets"
        assets.mkdir(parents=True)
        (assets / "flows.json").write_text(
            json.dumps({"flows": {"onboard": "not-an-object"}}),
            encoding="utf-8",
        )
        errors, warnings = [], []
        ltc.check_flow_components([], errors, warnings)
        assert any("must be an object" in e for e in errors)

    def test_steps_not_list_is_error_not_crash(self, patch_repo_root):
        # steps-as-dict used to crash with AttributeError ('str'.get) when
        # enumerate() yielded the dict KEYS.
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": {
            "step1": {"tool": "wizard"}
        }})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("'steps' must be an array" in e for e in errors)

    def test_step_not_object_is_error_not_crash(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": ["just-a-string"]})
        errors, warnings = [], []
        ltc.check_flow_components([], errors, warnings)
        assert any("step 0: must be an object" in e for e in errors)

    def test_step_title_plain_string_skips_bilingual(self, patch_repo_root):
        # Pin: a plain-string title is legal legacy shape — bilingual
        # checks only apply to dict titles (same semantics as the retired
        # flow-e2e-check script).
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "title": "Plain Step",
             "hint": {"en": "Do", "zh": "做"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert not any("title." in e for e in errors)

    def test_validation_warn_not_object_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard",
             "validation": {"required_state": ["role"],
                            "warn": "go back"}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("validation.warn must be an object" in e for e in errors)

    def test_validation_warn_lang_hole_is_error(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard",
             "validation": {"required_state": ["role"],
                            "warn": {"en": "Go back"}}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert any("validation.warn.zh missing" in e for e in errors)

    def test_valid_condition_and_validation_pass(self, patch_repo_root):
        tmp_path = patch_repo_root(ltc, "PROJECT_ROOT")
        portal_src = tmp_path / "tools" / "portal" / "src"
        portal_src.mkdir(parents=True)
        (portal_src / "step.jsx").write_text("content")
        dist = tmp_path / "docs" / "assets" / "dist"
        dist.mkdir(parents=True)
        (dist / "step.js").write_text("export {};", encoding="utf-8")
        self._write_flows(tmp_path, {**FLOW_META, "steps": [
            {"tool": "wizard", "component": "step.jsx",
             "title": {"en": "Step 1", "zh": "步驟一"},
             "hint": {"en": "Do", "zh": "做"},
             "condition": {"role": ["platform", "domain"]},
             "validation": {"required_state": ["role"],
                            "warn": {"en": "Go back", "zh": "請返回"}}}
        ]})
        errors, warnings = [], []
        ltc.check_flow_components([{"key": "wizard"}], errors, warnings)
        assert errors == []
        assert warnings == []


# ---------------------------------------------------------------------------
# check_hub_flow_section (ported from the retired flow-e2e-check script —
# the Hub side's only gate)
# ---------------------------------------------------------------------------
HUB_MARKERS = [
    "flow-cards",
    "flow-analytics",
    "custom-flow-builder",
    "__da_flow_progress_",
    "__da_flow_completed_",
    "flows.json",
]


class TestCheckHubFlowSection:
    HUB = (
        '<div id="flow-cards"></div>'
        '<div id="flow-analytics"></div>'
        '<div id="custom-flow-builder"></div>'
        "<script>localStorage.getItem('__da_flow_progress_' + name);"
        "localStorage.getItem('__da_flow_completed_' + name);"
        "fetch('../assets/flows.json');</script>"
    )

    def test_all_markers_present(self):
        errors = []
        ltc.check_hub_flow_section(self.HUB, errors)
        assert errors == []

    @pytest.mark.parametrize("marker", HUB_MARKERS)
    def test_each_missing_marker_is_error(self, marker):
        errors = []
        ltc.check_hub_flow_section(
            self.HUB.replace(marker, "renamed-away"), errors
        )
        assert len(errors) == 1
        assert marker in errors[0]


# ---------------------------------------------------------------------------
# check_loader_flow_infrastructure (ported from the retired flow-e2e-check
# script — persistence / gate / custom-flow markers have no E2E coverage;
# the render/load path additionally has Playwright ?flow=onboarding)
# ---------------------------------------------------------------------------
LOADER_MARKERS = [
    "__FLOW_STATE",
    "__flowSave",
    "__da_flow_progress_",
    "__da_flow_completed_",
    "filterSteps",
    "__checkFlowGate",
    "buildCustomFlow",
    "renderFlowUI",
    "flow-stepper",
    "flow-nav",
    "flow-hint",
]


class TestCheckLoaderFlowInfrastructure:
    LOADER = " ".join(LOADER_MARKERS)

    def test_all_markers_present(self):
        errors = []
        ltc.check_loader_flow_infrastructure(self.LOADER, errors)
        assert errors == []

    @pytest.mark.parametrize("marker", LOADER_MARKERS)
    def test_each_missing_marker_is_error(self, marker):
        # NB: markers are disjoint tokens (none is a substring of
        # another), so replacing one token removes exactly one marker.
        errors = []
        ltc.check_loader_flow_infrastructure(
            self.LOADER.replace(marker, "renamed-away"), errors
        )
        assert any(marker in e for e in errors)
