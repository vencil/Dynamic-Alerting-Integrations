"""Tests for generate_tool_map.py — Tool navigation auto-generation."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)
# generate_tool_map imports `_lib_toolcount` from scripts/tools/, so that
# directory has to be importable for the module under test to load at all.
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import generate_tool_map as gtm  # noqa: E402


# ---------------------------------------------------------------------------
# extract_tool_description
# ---------------------------------------------------------------------------
class TestExtractToolDescription:
    """Tests for extract_tool_description() — docstring extraction from Python files."""

    def test_module_docstring_with_prefix(self, tmp_path):
        """Extracts description from 'scriptname.py — description' format."""
        p = tmp_path / "diagnose.py"
        p.write_text('"""diagnose.py — Quick health check for tenants."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "Quick health check for tenants."

    def test_module_docstring_with_dash(self, tmp_path):
        """Supports em-dash, en-dash, and regular dash separators."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py - Simple description."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "Simple description."

    def test_module_docstring_no_prefix(self, tmp_path):
        """Falls back to full first line when no prefix pattern matches."""
        p = tmp_path / "util.py"
        p.write_text('"""A utility for processing YAML files."""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "A utility for processing YAML files."

    def test_multiline_docstring(self, tmp_path):
        """Only extracts the first line of multi-line docstrings."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py — First line.\n\nDetailed description.\nMore lines.\n"""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "First line."

    def test_no_docstring(self, tmp_path):
        """Returns empty string when no docstring found."""
        p = tmp_path / "nodoc.py"
        p.write_text("import os\nprint('hello')\n", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_syntax_error_file(self, tmp_path):
        """Handles files with syntax errors gracefully."""
        p = tmp_path / "broken.py"
        p.write_text("def broken(:\n  pass\n", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_empty_file(self, tmp_path):
        """Handles empty files gracefully."""
        p = tmp_path / "empty.py"
        p.write_text("", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == ""

    def test_chinese_description(self, tmp_path):
        """Supports Chinese descriptions in docstrings."""
        p = tmp_path / "tool.py"
        p.write_text('"""tool.py — 工具導覽自動生成"""\n', encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        assert desc == "工具導覽自動生成"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
class TestConstants:
    """Validate module constants and category configuration."""

    def test_category_order_matches_subdir_category(self):
        for cat in gtm.CATEGORY_ORDER:
            assert cat in gtm.SUBDIR_CATEGORY.values()

    def test_category_headers_bilingual(self):
        for lang in ("zh", "en"):
            assert lang in gtm.CATEGORY_HEADERS
            for cat in gtm.CATEGORY_ORDER:
                assert cat in gtm.CATEGORY_HEADERS[lang]

    # ⛔ #1511: these two were briefly DELETED on the grounds that neither
    # could see the case its own failure message named. That was wrong,
    # and the correction is worth keeping because it is the mistake this
    # whole cohort is about — one measured bypass was generalised into
    # "blind". Re-measured at both commits, each catches its named case
    # and each has exactly ONE bypass:
    #
    #   guard                              catches                bypass
    #   test_gather_tools_calls_...        a RENAMED private      a private
    #                                      scanner (`_local_scan`) `def tool_map_scope`
    #   test_the_shared_library_footer_... the footer glob        a second
    #                                      re-spelled `_lib*`     module-level literal
    #
    # ⚠️ And the claim that the deletion leaned on — "including a version
    # that dropped the repo root, 24 passed, generator rc=0" — was never
    # run by its author. Measured: that version is KILLED at both commits
    # by `test_the_generator_keeps_the_root_and_only_the_root`.
    #
    # So both bypasses are disclosed below rather than used as a reason to
    # delete. A guard with one known hole still refuses the other shapes.

    def test_gather_tools_calls_the_shared_scanner(self):
        """#1511: this module used to own a second `SKIP_PREFIXES` tuple.

        ⛔ An earlier version asserted `gtm.tool_map_scope is
        _lib_toolcount.tool_map_scope`, which pins the IMPORT SPELLING.
        Measured, wrong in both directions: rewriting `from X import f`
        to `import X` + `X.f(...)` (identical behaviour, generator rc=0)
        turned it RED, while a private `_local_scan()` inside
        `gather_tools` with the import line left in place kept it GREEN.

        `co_names` holds the global names the compiled body looks up, so
        a private scanner under a DIFFERENT name stops satisfying it
        whichever way the import is spelled. Measured at `d5133cc8`: a
        renamed behaviour-identical `_local_scan` → this test fails with
        the globals it does reach printed.

        ⚠️ Known bypass, measured, not papered over: a private
        `def tool_map_scope(root)` in this module satisfies it, because
        `co_names` records names and not provenance. What still refuses
        that shape is behaviour — `test_the_generator_keeps_the_root_and
        _only_the_root` kills any private copy whose output differs.
        """
        reached = set(gtm.gather_tools.__code__.co_names)
        assert "tool_map_scope" in reached or "_lib_toolcount" in reached, (
            "gather_tools no longer reaches for _lib_toolcount.tool_map_scope; "
            "a private scanner here is how the #1511 divergence started. "
            "Globals it does reach: %s" % sorted(reached))
        assert not hasattr(gtm, "SKIP_PREFIXES"), (
            "a module-level SKIP_PREFIXES is back; the prefixes live in "
            "_lib_toolcount.TOOL_SKIP_PREFIXES so both halves of the "
            "partition come from one string")

    def test_the_shared_library_footer_follows_the_shared_prefix(
            self, tmp_path, monkeypatch):
        """⛔ Control for the other half of the same partition.

        The tool tables skip `_lib*` and this document's shared-library
        section lists them, so the two must read one string. Measured
        before this control existed: reverting the footer to its own
        literal `"_lib*.py"` left every test green — the collapse had no
        guard at all, which is how a second spelling comes back. With
        this control, that same edit fails, naming it.

        ⚠️ Known bypass, measured: writing a second module-level
        `SHARED_LIB_PREFIX = "_lib"` here satisfies it, because the
        monkeypatch below proves only that the footer reads a module
        global — not where that global came from.
        """
        tools = tmp_path / "tools"
        tools.mkdir()
        for name in ("_lib_old.py", "_zzlib_new.py", "realtool.py"):
            (tools / name).write_text(
                '"""%s — stub."""\n' % name, encoding="utf-8", newline="\n")
        monkeypatch.setattr(gtm, "TOOLS_ROOT", tools)
        monkeypatch.setattr(gtm, "SHARED_LIB_PREFIX", "_zzlib")

        rendered = gtm.generate_tool_map({c: [] for c in gtm.CATEGORY_ORDER})

        assert "_zzlib_new.py" in rendered, (
            "the shared-library footer ignored SHARED_LIB_PREFIX, so this "
            "module spells the prefix a second time — the partition is "
            "back to two definitions")
        assert "_lib_old.py" not in rendered, (
            "the footer still lists the old prefix after it moved")
