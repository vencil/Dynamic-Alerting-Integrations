"""Tests for scripts/tools/lint/check_jsx_i18n.py.

Gap 4 (TRK-007 backlog) — third lint self-test (P1). Auto-hook lint at
301 LOC, previously zero unit-test coverage.

The lint exists specifically because of the v2.3.0 bug class:
  - jsx-loader.html language-toggle button returned the same string
    in both branches → user couldn't actually switch language

(The original second member of that bug class — TOOL_META ↔
CUSTOM_FLOW_MAP key sync — was retired in the TRK-242 residue
cleanup: TOOL_META left jsx-loader.html with renderJSX in TRK-230z.
Live guards today: auto hook `tool-consistency-check`
(lint_tool_consistency.py: registry key ⊆ loader + flow step → dist
bundle, error-level; absorbed the retired manual `flow-e2e-check`) +
sync_tool_registry.py as generator. Portal .jsx dup-param scanning
was removed in the same cleanup — zero `window.__t(` call sites in
tools/portal/src/ and tool-level findings were invisible warnings.)

A regex regression in this lint silently re-enables the bug. Three
parsers + one orchestrator (run_checks) covered:

  - parse_object_keys (CUSTOM_FLOW_MAP key extraction; generic parser)
  - find_duplicate_t_params (window.__t copy-paste detector)
  - check_language_toggle (ternary same-value detector)
  - run_checks (orchestrator + file-missing handling)
  - main CLI (--ci exit codes, --json shape, repo smoke)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_jsx_i18n.py"

_spec = importlib.util.spec_from_file_location("check_jsx_i18n", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_jsx_i18n"] = mod
_spec.loader.exec_module(mod)


# ============================================================
# parse_object_keys
# ============================================================


class TestParseObjectKeys:

    def test_var_declaration(self):
        # Property: `var TOOL_META = { 'a': ..., 'b': ... }` parses keys.
        content = (
            "var TOOL_META = {\n"
            "  'check-alert': { url: '/x' },\n"
            "  'diagnose': { url: '/y' },\n"
            "};\n"
        )
        keys, line = mod.parse_object_keys(content, "TOOL_META")
        assert keys == {"check-alert", "diagnose"}
        assert line == 1

    def test_const_declaration(self):
        content = (
            "const TOOL_META = {\n"
            "  'foo-bar': { },\n"
            "};\n"
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        assert keys == {"foo-bar"}

    def test_let_declaration(self):
        content = (
            "let TOOL_META = {\n"
            "  'x-y-z': { },\n"
            "};\n"
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        assert keys == {"x-y-z"}

    def test_double_quoted_keys(self):
        content = (
            'var TOOL_META = {\n'
            '  "alpha-beta": { },\n'
            '  "gamma": { },\n'
            '};\n'
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        # Note: 'gamma' has no '-' but the regex is `[a-z][a-z0-9-]+`
        # which requires at least 2 chars total — `gamma` (5 chars) matches.
        assert keys == {"alpha-beta", "gamma"}

    def test_missing_object_returns_empty(self):
        content = "var OTHER = { 'a': 1 };\n"
        keys, line = mod.parse_object_keys(content, "TOOL_META")
        assert keys == set()
        assert line == 0

    def test_object_assignment_form(self):
        # Property: `TOOL_META: { ... }` (object-property form, e.g. inside
        # a wrapping object) is also matched.
        content = (
            "{\n"
            "  TOOL_META: {\n"
            "    'a-b': 1,\n"
            "  },\n"
            "}\n"
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        assert keys == {"a-b"}

    def test_inline_keys_on_decl_line(self):
        # Property: keys on the same line as the opening brace are captured.
        content = "var TOOL_META = { 'a-b': 1, 'c-d': 2 };\n"
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        assert keys == {"a-b", "c-d"}

    def test_brace_depth_tracking(self):
        # Property: nested objects don't terminate the key scan early.
        content = (
            "var TOOL_META = {\n"
            "  'top-key': {\n"
            "    'nested': 1,\n"  # nested key NOT captured
            "  },\n"
            "  'second-key': 2,\n"
            "};\n"
            "var OTHER = {\n"
            "  'should-not-appear': 1,\n"
            "};\n"
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        # We expect top-level keys only.
        assert "top-key" in keys
        assert "second-key" in keys
        # Nested key would currently be captured (it matches the regex);
        # the brace_depth check exits the scan when depth returns to 0.
        # Don't assert on the nested-key behavior — just that we stopped
        # before reading OTHER.
        assert "should-not-appear" not in keys

    def test_uppercase_keys_not_matched(self):
        # Property: regex requires `[a-z][a-z0-9-]+`, so PascalCase keys
        # are NOT picked up as TOOL_META keys (they're treated as
        # constructors, not entry names).
        content = (
            "var TOOL_META = {\n"
            "  'PascalCase': 1,\n"
            "  'lower-kebab': 2,\n"
            "};\n"
        )
        keys, _ = mod.parse_object_keys(content, "TOOL_META")
        assert "PascalCase" not in keys
        assert "lower-kebab" in keys


# ============================================================
# find_duplicate_t_params
# ============================================================


class TestFindDuplicateTParams:

    def test_same_strings_flagged(self):
        # Property: window.__t("X", "X") is a copy-paste bug → flagged.
        content = "var x = window.__t('common text', 'common text');\n"
        issues = mod.find_duplicate_t_params(content)
        assert len(issues) == 1
        assert issues[0]["zh"] == "common text"
        assert issues[0]["en"] == "common text"
        assert issues[0]["line"] == 1

    def test_different_strings_not_flagged(self):
        # Positive: legitimate bilingual call passes.
        content = 'window.__t("中文", "English");\n'
        assert mod.find_duplicate_t_params(content) == []

    def test_double_and_single_quotes(self):
        # Property: both quote styles match.
        content = (
            'window.__t("a", "a");\n'
            "window.__t('b', 'b');\n"
        )
        issues = mod.find_duplicate_t_params(content)
        assert len(issues) == 2

    def test_multiple_calls_same_line(self):
        # Property: multiple bad calls on one line all surface.
        content = "x = window.__t('a','a'); y = window.__t('b','b');\n"
        issues = mod.find_duplicate_t_params(content)
        assert len(issues) == 2

    def test_no_calls_returns_empty(self):
        assert mod.find_duplicate_t_params("// just a comment\n") == []
        assert mod.find_duplicate_t_params("") == []

    def test_line_numbers_correct(self):
        content = (
            "// blank line\n"
            "var foo = 1;\n"
            "window.__t('z', 'z');\n"
            "// another\n"
            "window.__t('y', 'y');\n"
        )
        issues = mod.find_duplicate_t_params(content)
        assert {i["line"] for i in issues} == {3, 5}

    def test_context_truncated_to_100_chars(self):
        # Property: long lines don't blow up CI logs — context is sliced.
        long_prefix = "x" * 200
        content = f"{long_prefix} window.__t('z', 'z');\n"
        issues = mod.find_duplicate_t_params(content)
        assert len(issues) == 1
        assert len(issues[0]["context"]) <= 100


# ============================================================
# check_language_toggle
# ============================================================


class TestCheckLanguageToggle:

    def test_same_branch_values_flagged(self):
        # Property: ternary inside setLanguage where both branches return
        # identical strings → flagged.
        content = (
            "function setLanguage(lang) {\n"
            "  var label = lang === 'zh' ? '中文 / EN' : '中文 / EN';\n"
            "  doStuff(label);\n"
            "}\n"
        )
        issues = mod.check_language_toggle(content)
        assert len(issues) == 1
        assert "中文 / EN" in issues[0]["message"]
        assert issues[0]["line"] == 2

    def test_different_branch_values_not_flagged(self):
        content = (
            "function setLanguage(lang) {\n"
            "  var label = lang === 'zh' ? 'EN' : '中文';\n"
            "}\n"
        )
        assert mod.check_language_toggle(content) == []

    def test_outside_toggle_function_ignored(self):
        # Property: same-value ternary OUTSIDE a setLanguage / updateLbl /
        # toggleLang function is not flagged.
        content = (
            "function unrelated() {\n"
            "  var x = cond ? 'a' : 'a';\n"
            "}\n"
        )
        assert mod.check_language_toggle(content) == []

    def test_recognizes_updateLbl_function(self):
        content = (
            "function updateLbl(lang) {\n"
            "  return lang === 'zh' ? 'same' : 'same';\n"
            "}\n"
        )
        assert len(mod.check_language_toggle(content)) == 1

    def test_recognizes_toggleLang_function(self):
        content = (
            "function toggleLang(lang) {\n"
            "  document.title = lang === 'zh' ? 'X' : 'X';\n"
            "}\n"
        )
        assert len(mod.check_language_toggle(content)) == 1


# ============================================================
# run_checks — orchestrator
# ============================================================


class TestRunChecks:

    def test_missing_jsx_loader_yields_error(self, tmp_path, monkeypatch):
        # Property: missing jsx-loader.html → file-missing error and
        # empty stats (we abort early without scanning JSX_TOOLS_DIR).
        monkeypatch.setattr(mod, "JSX_LOADER", tmp_path / "nope.html")
        issues, stats = mod.run_checks()
        assert any(i["check"] == "file-missing" for i in issues)
        assert stats == {}

    def test_clean_loader_yields_no_issues(self, tmp_path, monkeypatch):
        # Positive: a balanced loader passes cleanly.
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = {\n"
            "  'a-b': { },\n"
            "  'c-d': { },\n"
            "};\n"
            "function setLanguage(lang) {\n"
            "  var l = lang === 'zh' ? 'EN' : '中文';\n"
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        issues, stats = mod.run_checks()
        assert issues == []
        assert stats["flow_map_count"] == 2

    def test_meta_flow_sync_retired(self, tmp_path, monkeypatch):
        # Property: the TOOL_META ↔ CUSTOM_FLOW_MAP sync check is RETIRED
        # (TRK-242 residue cleanup; TOOL_META left with renderJSX in
        # TRK-230z). Even a loader that still carries a mismatched
        # TOOL_META object must NOT produce meta-flow-sync issues —
        # that guard now lives in lint_tool_consistency.py
        # (check_tool_meta, exact key-set match). Other checks still run.
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var TOOL_META = {\n"
            "  'orphan-meta': { },\n"
            "};\n"
            "var CUSTOM_FLOW_MAP = {\n"
            "  'orphan-flow': { },\n"
            "};\n"
            "window.__t('same', 'same');\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        issues, stats = mod.run_checks()
        assert not any(i["check"] == "meta-flow-sync" for i in issues)
        # But the duplicate __t param check still fires.
        assert any(i["check"] == "t-duplicate-param" for i in issues)
        assert "tool_meta_count" not in stats

    def test_language_toggle_issue_propagates(self, tmp_path, monkeypatch):
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = { };\n"
            "function updateLbl(lang) {\n"
            "  return lang === 'zh' ? 'X' : 'X';\n"
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        issues, _ = mod.run_checks()
        assert any(i["check"] == "toggle-same-value" for i in issues)


# ============================================================
# main — CLI / exit codes
# ============================================================


class TestMainCLI:

    def test_clean_state_exits_zero(self, tmp_path, monkeypatch, capsys):
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = { 'a-b': 1 };\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        monkeypatch.setattr(sys, "argv", ["check_jsx_i18n", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0
        assert "通過" in capsys.readouterr().out

    def test_error_state_exits_one_with_ci(self, tmp_path, monkeypatch, capsys):
        # Negative: loader-level duplicate __t params (error severity)
        # → --ci exits 1.
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = { };\n"
            "window.__t('dup-text', 'dup-text');\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        monkeypatch.setattr(sys, "argv", ["check_jsx_i18n", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "dup-text" in out

    def test_error_state_without_ci_exits_zero(
        self, tmp_path, monkeypatch, capsys
    ):
        # Property: errors without --ci just print, exit 0.
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = { };\n"
            "window.__t('dup-text', 'dup-text');\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        monkeypatch.setattr(sys, "argv", ["check_jsx_i18n"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0  # report-only without --ci

    def test_json_output_shape(self, tmp_path, monkeypatch, capsys):
        loader = tmp_path / "jsx-loader.html"
        loader.write_text(
            "var CUSTOM_FLOW_MAP = { 'a-b': 1 };\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(mod, "JSX_LOADER", loader)
        monkeypatch.setattr(sys, "argv", ["check_jsx_i18n", "--json"])
        with pytest.raises(SystemExit):
            mod.main()
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["check"] == "jsx-i18n"
        assert "stats" in payload
        assert "issues" in payload
        assert "summary" in payload
        assert payload["summary"]["errors"] == 0


# ============================================================
# Repo-level smoke regression guard
# ============================================================


class TestRepoSmoke:

    def test_actual_repo_passes_or_warn_only(self, monkeypatch):
        """The shipped jsx-loader.html + JSX tools must pass the lint
        with --ci. Belt-and-suspenders alongside the pre-commit hook.
        """
        monkeypatch.setattr(sys, "argv", ["check_jsx_i18n", "--ci"])
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 0, (
            "repo's JSX i18n state fails its own lint"
        )
