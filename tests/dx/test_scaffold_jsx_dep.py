"""Unit tests for scaffold_jsx_dep.py.

Covers the five extraction patterns codified by PR-2d (#153) Phase 1+2:
  fixture / util / hook / component / view

Plus the orchestrator-update logic (idempotent insertion into the
front-matter `dependencies: [...]` array and the `const X = window.__X;`
import block).
"""
from __future__ import annotations

import os
import sys

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx"
)
sys.path.insert(0, _TOOLS_DIR)

import scaffold_jsx_dep as sj  # noqa: E402


# ---------------------------------------------------------------------------
# Naming-convention validation in derive_paths
# ---------------------------------------------------------------------------
class TestDerivePathsValidation:
    def test_hook_must_start_with_use(self):
        with pytest.raises(ValueError, match="must start with 'use'"):
            sj.derive_paths("hook", "FetchData", "tenant-manager")

    def test_hook_with_use_prefix_ok(self):
        p = sj.derive_paths("hook", "useFetchData", "tenant-manager")
        assert p.dep_relpath == "tenant-manager/hooks/useFetchData.js"

    def test_component_must_be_pascal_case(self):
        with pytest.raises(ValueError, match="must be PascalCase"):
            sj.derive_paths("component", "tenantCard", "tenant-manager")

    def test_view_must_be_pascal_case(self):
        with pytest.raises(ValueError, match="must be PascalCase"):
            sj.derive_paths("view", "loadingView", "tenant-manager")

    def test_component_pascal_ok(self):
        p = sj.derive_paths("component", "TenantCard", "tenant-manager")
        assert p.dep_relpath == "tenant-manager/components/TenantCard.jsx"

    def test_fixture_kebab_ok(self):
        # Fixtures (data files) often use kebab-case filenames.
        p = sj.derive_paths("fixture", "demo-tenants", "tenant-manager")
        assert p.dep_relpath == "tenant-manager/fixtures/demo-tenants.js"

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValueError, match="Unknown kind"):
            sj.derive_paths("widget", "Foo", "tenant-manager")


# ---------------------------------------------------------------------------
# Path derivation — extension + subdir per kind
# ---------------------------------------------------------------------------
class TestDerivePathsLayout:
    @pytest.mark.parametrize("kind, name, subdir, ext", [
        ("fixture",   "demo-x",       "fixtures",   ".js"),
        ("util",      "yaml-helpers", "utils",      ".js"),
        ("hook",      "useFoo",       "hooks",      ".js"),
        ("component", "TenantCard",   "components", ".jsx"),
        ("view",      "LoadingView",  "views",      ".jsx"),
    ])
    def test_kind_to_path_mapping(self, kind, name, subdir, ext):
        p = sj.derive_paths(kind, name, "tenant-manager")
        assert p.dep_relpath == f"tenant-manager/{subdir}/{name}{ext}"
        assert str(p.dep_file).endswith(f"{name}{ext}")
        assert subdir in str(p.dep_file)


# ---------------------------------------------------------------------------
# Template rendering — content sanity checks
# ---------------------------------------------------------------------------
class TestRenderTemplate:
    def test_fixture_default_kebab_auto_converts_to_screaming_snake(self):
        # Pre-merge self-review on PR #160 caught: pre-fix this generated
        # `const demo-foo = ...` which is INVALID JavaScript (parser splits
        # `demo` and `-foo`). Now kebab fixture names auto-convert to
        # SCREAMING_SNAKE — matching the established convention
        # (demo-tenants.js → DEMO_TENANTS in PR #156).
        out = sj.render_template("fixture", "demo-foo", "tenant-manager")
        # Auto-converted symbol used:
        assert "const DEMO_FOO = {" in out
        assert "window.__DEMO_FOO = DEMO_FOO;" in out
        # Original kebab name does NOT appear as an identifier (it's still
        # in the front-matter title but never as a JS const name).
        assert "const demo-foo" not in out
        assert "= demo-foo;" not in out  # not used as a JS reference

    def test_fixture_default_valid_identifier_unchanged(self):
        # When name is already a valid JS identifier, no conversion.
        out = sj.render_template("fixture", "DemoFoo", "tenant-manager")
        assert "const DemoFoo = {" in out
        assert "window.__DemoFoo = DemoFoo;" in out

    def test_fixture_multi_symbol(self):
        out = sj.render_template(
            "fixture", "demo-bars", "tenant-manager",
            symbols=["DEMO_BARS", "DEMO_BAR_GROUPS"],
        )
        # Two const declarations
        assert "const DEMO_BARS" in out
        assert "const DEMO_BAR_GROUPS" in out
        # Two window registrations
        assert "window.__DEMO_BARS = DEMO_BARS;" in out
        assert "window.__DEMO_BAR_GROUPS = DEMO_BAR_GROUPS;" in out

    def test_util_kebab_default_rejected(self):
        # util filenames rarely map 1:1 to a single symbol (yaml-generators.js
        # exports BOTH generateMaintenanceYaml AND generateSilentModeYaml),
        # so kebab-case util names without --symbols MUST error out rather
        # than auto-convert to a single misleading symbol.
        with pytest.raises(ValueError, match="Pass --symbols explicitly"):
            sj.render_template("util", "yaml-helpers", "tenant-manager")

    def test_util_with_symbols_ok(self):
        out = sj.render_template(
            "util", "yaml-helpers", "tenant-manager",
            symbols=["generateXYaml", "generateZYaml"],
        )
        assert "function generateXYaml" in out
        assert "function generateZYaml" in out
        assert "window.__generateXYaml = generateXYaml;" in out
        assert "window.__generateZYaml = generateZYaml;" in out

    def test_util_camel_default_ok(self):
        # If util filename is already a valid identifier (no kebab),
        # use it directly as the symbol.
        out = sj.render_template("util", "yamlHelpers", "tenant-manager")
        assert "function yamlHelpers" in out
        assert "window.__yamlHelpers = yamlHelpers;" in out

    def test_hook_template_includes_react_destructure(self):
        out = sj.render_template("hook", "useFoo", "tenant-manager")
        assert "const { useState, useEffect } = React;" in out
        assert "function useFoo()" in out
        assert "window.__useFoo = useFoo;" in out
        # Mentions the rationale doc / S#70 in the comment block
        assert "S#70" in out

    def test_component_template_returns_null_default(self):
        out = sj.render_template("component", "FooBar", "tenant-manager")
        assert "function FooBar(props)" in out
        assert "return null;" in out
        assert "window.__FooBar = FooBar;" in out

    def test_view_template_distinct_from_component_metadata(self):
        out = sj.render_template("view", "LoadingView", "tenant-manager")
        # View shares component template body but kind label differs in front-matter.
        assert "function LoadingView(props)" in out
        # The kind label "view" should appear in the purpose block.
        assert "this view" in out

    def test_all_templates_have_window_registration(self):
        for kind, name in [
            ("fixture", "x"), ("util", "x"),
            ("hook", "useX"), ("component", "X"), ("view", "X"),
        ]:
            out = sj.render_template(kind, name, "tenant-manager")
            assert "window.__" in out, f"{kind} template missing window registration"

    def test_all_templates_have_front_matter(self):
        for kind, name in [
            ("fixture", "x"), ("util", "x"),
            ("hook", "useX"), ("component", "X"), ("view", "X"),
        ]:
            out = sj.render_template(kind, name, "tenant-manager")
            # YAML front-matter delimiter at start
            assert out.startswith("---\n"), f"{kind} template missing front-matter"

    def test_parent_title_humanized_in_front_matter(self):
        # 'tenant-manager' → 'Tenant Manager' in title
        out = sj.render_template("component", "X", "tenant-manager")
        assert 'title: "Tenant Manager — X"' in out

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="Unknown kind"):
            sj.render_template("invalid", "X", "tenant-manager")


# ---------------------------------------------------------------------------
# Symbol derivation helpers — added in PR #160 self-review (Pass 1)
# ---------------------------------------------------------------------------
class TestSymbolDerivation:
    @pytest.mark.parametrize("ident, ok", [
        ("foo", True),
        ("Foo", True),
        ("FOO_BAR", True),
        ("useFoo", True),
        ("$foo", True),
        ("_foo", True),
        ("foo123", True),
        ("foo-bar", False),    # kebab not allowed in JS identifier
        ("123foo", False),     # can't start with digit
        ("foo.bar", False),    # dots not allowed
        ("", False),
        ("foo bar", False),    # space not allowed
    ])
    def test_is_valid_js_identifier(self, ident, ok):
        assert sj._is_valid_js_identifier(ident) is ok

    def test_kebab_to_screaming_snake(self):
        assert sj._kebab_to_screaming_snake("demo-foo") == "DEMO_FOO"
        assert sj._kebab_to_screaming_snake("a-b-c-d") == "A_B_C_D"
        assert sj._kebab_to_screaming_snake("alreadyOK") == "ALREADYOK"
        assert sj._kebab_to_screaming_snake("a") == "A"

    def test_derive_default_symbols_fixture_kebab(self):
        assert sj.derive_default_symbols("fixture", "demo-foo") == ["DEMO_FOO"]

    def test_derive_default_symbols_fixture_already_valid(self):
        assert sj.derive_default_symbols("fixture", "DemoFoo") == ["DemoFoo"]

    def test_derive_default_symbols_util_kebab_rejected(self):
        with pytest.raises(ValueError, match="Pass --symbols explicitly"):
            sj.derive_default_symbols("util", "yaml-helpers")

    def test_derive_default_symbols_util_camel_ok(self):
        assert sj.derive_default_symbols("util", "yamlHelpers") == ["yamlHelpers"]

    def test_derive_default_symbols_hook(self):
        # Hooks pass through (already validated by derive_paths).
        assert sj.derive_default_symbols("hook", "useFoo") == ["useFoo"]

    def test_derive_default_symbols_component(self):
        assert sj.derive_default_symbols("component", "FooBar") == ["FooBar"]

    def test_main_rejects_invalid_symbols_arg(self, tmp_path, monkeypatch):
        # Bogus --symbols entries (non-identifier) should error out.
        tools = tmp_path / "docs" / "interactive" / "tools"
        tools.mkdir(parents=True)
        (tools / "foo.jsx").write_text(
            _ORCH_FIXTURE_MIN.replace("tenant-manager/", "foo/").replace(
                'title: "Tenant Manager"', 'title: "Foo"'
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(sj, "PROJECT_ROOT", tmp_path)
        rc = sj.main([
            "--kind", "fixture", "--name", "demo-x", "--parent", "foo",
            "--symbols", "VALID,bad-name-with-hyphens",
        ])
        assert rc == 2


# ---------------------------------------------------------------------------
# update_orchestrator_deps — multi-line array + idempotency
# ---------------------------------------------------------------------------
_ORCH_FIXTURE_MIN = """\
---
title: "Tenant Manager"
dependencies: [
  "tenant-manager/fixtures/demo-tenants.js",
  "tenant-manager/styles.js"
]
---

import React from 'react';

const styles = window.__styles;
const DEMO_TENANTS = window.__DEMO_TENANTS;

export default function TenantManager() {
  return null;
}
"""


class TestUpdateOrchestratorDeps:
    def test_appends_new_path(self):
        new, changed = sj.update_orchestrator_deps(
            _ORCH_FIXTURE_MIN, "tenant-manager/hooks/useFoo.js"
        )
        assert changed is True
        assert "tenant-manager/hooks/useFoo.js" in new
        # Original deps still there
        assert "tenant-manager/fixtures/demo-tenants.js" in new
        assert "tenant-manager/styles.js" in new

    def test_idempotent(self):
        new, changed = sj.update_orchestrator_deps(
            _ORCH_FIXTURE_MIN, "tenant-manager/styles.js"
        )
        assert changed is False
        assert new == _ORCH_FIXTURE_MIN

    def test_preserves_indentation(self):
        new, _ = sj.update_orchestrator_deps(
            _ORCH_FIXTURE_MIN, "tenant-manager/hooks/useFoo.js"
        )
        # Existing entries used 2-space indent; new entry should too.
        # We look for the new entry on its own line with 2-space indent.
        assert '  "tenant-manager/hooks/useFoo.js"' in new

    def test_preserves_closing_bracket(self):
        new, _ = sj.update_orchestrator_deps(
            _ORCH_FIXTURE_MIN, "tenant-manager/hooks/useFoo.js"
        )
        # Front-matter dependencies array still closes with `]`
        # AND nothing trails inside the array beyond the new entry.
        assert "]" in new
        assert "dependencies: [" in new

    def test_no_dependencies_block_raises(self):
        bad = '---\ntitle: "X"\n---\n\nbody'
        with pytest.raises(RuntimeError, match="no `dependencies:"):
            sj.update_orchestrator_deps(bad, "x/y.js")

    def test_inserts_comma_after_unterminated_last_entry(self):
        # If the last existing entry has no trailing comma, the script
        # must add one before appending the new entry.
        new, changed = sj.update_orchestrator_deps(
            _ORCH_FIXTURE_MIN, "tenant-manager/utils/foo.js"
        )
        assert changed is True
        # Make sure the previous last entry now has a comma.
        # The body should contain "styles.js"," with a comma.
        assert '"tenant-manager/styles.js",' in new


# ---------------------------------------------------------------------------
# update_orchestrator_imports — idempotency + insertion point
# ---------------------------------------------------------------------------
class TestUpdateOrchestratorImports:
    def test_appends_new_import(self):
        new, changed = sj.update_orchestrator_imports(_ORCH_FIXTURE_MIN, "useFoo")
        assert changed is True
        assert "const useFoo = window.__useFoo;" in new
        # Existing imports preserved
        assert "const styles = window.__styles;" in new
        assert "const DEMO_TENANTS = window.__DEMO_TENANTS;" in new

    def test_idempotent(self):
        new, changed = sj.update_orchestrator_imports(_ORCH_FIXTURE_MIN, "styles")
        assert changed is False
        assert new == _ORCH_FIXTURE_MIN

    def test_appends_after_last_existing_import(self):
        # The new line should come after `const DEMO_TENANTS = ...;` (the
        # last existing const-window import). Verify by index.
        new, _ = sj.update_orchestrator_imports(_ORCH_FIXTURE_MIN, "useFoo")
        i_demo = new.index("const DEMO_TENANTS = window.__DEMO_TENANTS;")
        i_new = new.index("const useFoo = window.__useFoo;")
        assert i_new > i_demo, "new import should come AFTER the last existing one"

    def test_no_import_block_raises(self):
        bad = '---\ntitle: "X"\n---\n\nimport React from "react";\n\nexport default function X() {}\n'
        with pytest.raises(RuntimeError, match="no `const X = window.__X;` import block"):
            sj.update_orchestrator_imports(bad, "useFoo")


# ---------------------------------------------------------------------------
# End-to-end via main() — uses tmp_path to avoid touching repo files
# ---------------------------------------------------------------------------
class TestMainEndToEnd:
    def _setup_fake_tree(self, tmp_path, monkeypatch):
        """Build: tmp/docs/interactive/tools/foo.jsx with the fixture
        orchestrator content, then point PROJECT_ROOT to tmp."""
        tools = tmp_path / "docs" / "interactive" / "tools"
        tools.mkdir(parents=True)
        (tools / "foo.jsx").write_text(
            _ORCH_FIXTURE_MIN.replace("tenant-manager/", "foo/").replace(
                'title: "Tenant Manager"', 'title: "Foo"'
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(sj, "PROJECT_ROOT", tmp_path)
        return tools

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        tools = self._setup_fake_tree(tmp_path, monkeypatch)
        rc = sj.main(
            ["--kind", "hook", "--name", "useBar", "--parent", "foo", "--dry-run"]
        )
        assert rc == 0
        # No new file created
        assert not (tools / "foo" / "hooks" / "useBar.js").exists()
        # Orchestrator NOT modified
        orch_after = (tools / "foo.jsx").read_text(encoding="utf-8")
        assert "useBar" not in orch_after

    def test_creates_file_and_updates_orchestrator(self, tmp_path, monkeypatch):
        tools = self._setup_fake_tree(tmp_path, monkeypatch)
        rc = sj.main(
            ["--kind", "hook", "--name", "useBar", "--parent", "foo"]
        )
        assert rc == 0
        # New file exists
        new_file = tools / "foo" / "hooks" / "useBar.js"
        assert new_file.exists()
        body = new_file.read_text(encoding="utf-8")
        assert "function useBar()" in body
        assert "window.__useBar = useBar;" in body
        # Orchestrator updated
        orch_after = (tools / "foo.jsx").read_text(encoding="utf-8")
        assert "foo/hooks/useBar.js" in orch_after
        assert "const useBar = window.__useBar;" in orch_after

    def test_refuses_overwrite_without_force(self, tmp_path, monkeypatch):
        tools = self._setup_fake_tree(tmp_path, monkeypatch)
        # Create the dep file first
        sj.main(["--kind", "hook", "--name", "useBar", "--parent", "foo"])
        # Second run without --force should fail
        rc = sj.main(
            ["--kind", "hook", "--name", "useBar", "--parent", "foo"]
        )
        assert rc == 2

    def test_orchestrator_not_found(self, tmp_path, monkeypatch):
        # No foo.jsx in tmp — main() should fail with code 2.
        tools = tmp_path / "docs" / "interactive" / "tools"
        tools.mkdir(parents=True)
        monkeypatch.setattr(sj, "PROJECT_ROOT", tmp_path)
        rc = sj.main(
            ["--kind", "hook", "--name", "useBar", "--parent", "foo"]
        )
        assert rc == 2

    def test_idempotent_re_run_orchestrator_updates(self, tmp_path, monkeypatch):
        # Running scaffold twice (with --force) should NOT duplicate
        # the orchestrator entries.
        tools = self._setup_fake_tree(tmp_path, monkeypatch)
        sj.main(["--kind", "hook", "--name", "useBar", "--parent", "foo"])
        orch_after_1 = (tools / "foo.jsx").read_text(encoding="utf-8")
        sj.main(["--kind", "hook", "--name", "useBar", "--parent", "foo", "--force"])
        orch_after_2 = (tools / "foo.jsx").read_text(encoding="utf-8")
        # Count occurrences of the new line — should be exactly 1.
        assert orch_after_1 == orch_after_2
        assert orch_after_2.count("const useBar = window.__useBar;") == 1
        assert orch_after_2.count("foo/hooks/useBar.js") == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
