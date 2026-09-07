"""Tests for generate_tool_map.py — Tool navigation auto-generation."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)
# generate_tool_map imports `_lib_toolcount` from scripts/tools/, so that
# directory has to be importable for the module under test to load at all.
sys.path.insert(0, os.path.join(_TOOLS_DIR, '..'))

import generate_tool_map as gtm  # noqa: E402

# Captured before any test patches `gtm.gather_tools`, for the one row that
# needs the real walker inside the sandboxed `env` fixture.
_REAL_GATHER_TOOLS = gtm.gather_tools


@pytest.fixture(autouse=True)
def _fresh_unreadable_ledger(monkeypatch):
    """`_UNREADABLE` is process-global (it dedups the stderr warning and
    makes `--check` refuse to go green); every test starts with it empty."""
    monkeypatch.setattr(gtm, "_UNREADABLE", {})


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

    # -- #1542: files that cannot be READ (as opposed to parsed) ----------
    # Before the fix these raised out of `extract_tool_description`, and
    # because `--check --lang all` is the `tool-map-check` pre-commit hook,
    # the traceback was read as tool-map drift. The contract is: placeholder
    # description, ONE warning line on stderr naming the file and the
    # exception class, nothing on stdout (in no-flag mode stdout IS the
    # generated document).

    def test_cp950_file_yields_placeholder_and_warns_on_stderr(
            self, tmp_path, capsys):
        """A cp950-encoded tool file is unreadable as UTF-8: placeholder + stderr."""
        p = tmp_path / "legacy.py"
        p.write_bytes('"""工具 — 說明"""\n'.encode("cp950"))
        desc = gtm.extract_tool_description(p)
        out, err = capsys.readouterr()
        assert desc == gtm.UNREADABLE_DESCRIPTION
        assert "legacy.py" in err
        assert "UnicodeDecodeError" in err
        assert out == ""

    def test_directory_named_py_yields_placeholder_and_warns(
            self, tmp_path, capsys):
        """A directory named `pkg.py` cannot be read as a file: placeholder + stderr."""
        d = tmp_path / "pkg.py"
        d.mkdir()
        desc = gtm.extract_tool_description(d)
        out, err = capsys.readouterr()
        assert desc == gtm.UNREADABLE_DESCRIPTION
        assert "pkg.py" in err
        assert "IsADirectoryError" in err
        assert out == ""

    def test_permission_error_yields_placeholder_and_warns(
            self, tmp_path, capsys, monkeypatch):
        """An unreadable-by-permission file: placeholder + stderr.

        The suite runs as root in CI and in the dev container, where chmod
        0o000 does not deny anything, so the denial is injected at
        `Path.read_text` for this one path only.
        """
        p = tmp_path / "locked.py"
        p.write_text('"""locked.py — hidden."""\n', encoding="utf-8")
        real_read_text = Path.read_text

        def _deny(self, *args, **kwargs):
            if self == p:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _deny)
        desc = gtm.extract_tool_description(p)
        out, err = capsys.readouterr()
        assert desc == gtm.UNREADABLE_DESCRIPTION
        assert "locked.py" in err
        assert "PermissionError" in err
        assert out == ""

    def test_syntax_error_file_is_still_silent_and_empty(
            self, tmp_path, capsys):
        """Control for #1542: the SyntaxError branch is unchanged.

        A file that reads fine but does not parse still yields "" and
        writes nothing to either stream — the unreadable branch must not
        have widened into "anything that fails".
        """
        p = tmp_path / "broken.py"
        p.write_text("def broken(:\n  pass\n", encoding="utf-8")
        desc = gtm.extract_tool_description(p)
        out, err = capsys.readouterr()
        assert desc == ""
        assert err == ""
        assert out == ""


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


# ---------------------------------------------------------------------------
# gather_tools with an unreadable member (#1542)
# ---------------------------------------------------------------------------
class TestGatherToolsUnreadableMember:
    """#1542 at the level the hook actually runs: one bad file must not
    take the whole inventory down, and must not be dropped from it either
    (filtering at the scan layer would silently move the published count).
    """

    def test_unreadable_member_is_listed_with_placeholder(
            self, tmp_path, monkeypatch, capsys):
        """ops/good.py (UTF-8) + ops/legacy.py (cp950): both listed, no raise."""
        root = tmp_path / "tools"
        ops = root / "ops"
        ops.mkdir(parents=True)
        (ops / "good.py").write_text(
            '"""good.py — A readable tool."""\n', encoding="utf-8")
        (ops / "legacy.py").write_bytes(
            '"""legacy.py — 舊工具"""\n'.encode("cp950"))
        monkeypatch.setattr(gtm, "TOOLS_ROOT", root)

        categorized = gtm.gather_tools()  # must not raise
        out, err = capsys.readouterr()

        listed = dict(categorized["ops"])
        assert listed == {
            "good.py": "A readable tool.",
            "legacy.py": gtm.UNREADABLE_DESCRIPTION,
        }, categorized
        assert err.count("legacy.py") == 1, err
        assert "UnicodeDecodeError" in err
        assert "good.py" not in err
        assert out == ""

    def test_shared_library_footer_warns_once_across_languages(
            self, tmp_path, monkeypatch, capsys):
        """The `_lib*` footer re-reads each shared module once PER LANGUAGE
        (`generate_tool_map` is called for zh and for en), so without the
        per-process ledger a cp950 `_lib_x.py` warned twice per run —
        measured in blind review. Exactly one line per file per process."""
        root = tmp_path / "tools"
        root.mkdir()
        (root / "_lib_bad.py").write_bytes(
            '"""_lib_bad.py — 舊"""\n'.encode("cp950"))
        monkeypatch.setattr(gtm, "TOOLS_ROOT", root)
        monkeypatch.setattr(gtm, "require_platform_version",
                            lambda *a, **k: "v0.0.0-test")
        empty = {c: [] for c in gtm.CATEGORY_ORDER}

        zh = gtm.generate_tool_map(empty, "zh")
        en = gtm.generate_tool_map(empty, "en")
        _out, err = capsys.readouterr()

        assert gtm.UNREADABLE_DESCRIPTION in zh and gtm.UNREADABLE_DESCRIPTION in en
        assert err.count("_lib_bad.py") == 1, err


# ---------------------------------------------------------------------------
# --check fix hints (#1696)
# ---------------------------------------------------------------------------
class TestFixHintLeadsToGreen:
    """#1696: the hint `--check` prints must, when executed verbatim,
    regenerate exactly what `--check` checked, so the next `--check` is
    green. The old hints said `Run with --generate` while `--generate`
    defaulted to zh only, so a red `tool-map.en.md` sent the reader to a
    command that changed nothing.

    Two assertions per row, each guarding a different half: the string
    assertion pins that the hint ECHOES the `--lang` that was checked (an
    over-regenerating `--lang all` hint would still reach green, so
    execution alone cannot tell "echo" from "over-regenerate"); the
    execution pins that following it actually reaches green.
    """

    _HINT = re.compile(r"Run with (--generate(?: --lang \w+)?) ")

    @pytest.fixture
    def env(self, tmp_path, monkeypatch, capsys):
        """A sandboxed generator: targets, tools root and version are all fake."""
        docs = tmp_path / "docs" / "internal"
        docs.mkdir(parents=True)
        tools = tmp_path / "scripts" / "tools"
        tools.mkdir(parents=True)
        # `main` renders paths relative to REPO_ROOT, so the sandbox has
        # to be the root or `relative_to` raises before any hint prints.
        monkeypatch.setattr(gtm, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(gtm, "TOOLS_ROOT", tools)
        monkeypatch.setattr(gtm, "TOOL_MAP", docs / "tool-map.md")
        monkeypatch.setattr(gtm, "TOOL_MAP_EN", docs / "tool-map.en.md")
        fixed = {c: [] for c in gtm.CATEGORY_ORDER}
        fixed["ops"].append(("fixed_tool.py", "A fixed tool."))
        monkeypatch.setattr(gtm, "gather_tools", lambda: fixed)
        # Imported by name (`from _lib_versions import require_platform_version`),
        # so the module-level binding is what `generate_tool_map` looks up.
        monkeypatch.setattr(gtm, "require_platform_version",
                            lambda *a, **k: "v0.0.0-test")

        def run(*argv):
            """Drive `gtm.main()` with argv; return (rc, stdout)."""
            monkeypatch.setattr(sys, "argv", ["generate_tool_map.py", *argv])
            try:
                gtm.main()
            except SystemExit as exc:
                rc = exc.code if exc.code is not None else 0
            else:
                rc = 0
            out, _err = capsys.readouterr()
            return rc, out

        return run

    @staticmethod
    def _targets(lang):
        return {"zh": [gtm.TOOL_MAP], "en": [gtm.TOOL_MAP_EN],
                "all": [gtm.TOOL_MAP, gtm.TOOL_MAP_EN]}[lang]

    @pytest.mark.parametrize("lang", ["zh", "en", "all"])
    @pytest.mark.parametrize("state", ["missing", "stale"])
    def test_executing_the_hint_reaches_green(self, env, lang, state):
        """`--check --lang L` red → run the printed hint → `--check --lang L` green."""
        if state == "stale":
            for target in self._targets(lang):
                target.write_text("junk\n", encoding="utf-8")

        rc, out = env("--check", "--lang", lang)
        assert rc == 1, out
        assert f"Run with --generate --lang {lang}" in out, out

        m = self._HINT.search(out)
        assert m, out
        hint_argv = m.group(1).split()
        rc, out = env(*hint_argv)
        assert rc == 0, out

        rc, out = env("--check", "--lang", lang)
        assert rc == 0, out
        assert "up to date" in out, out
        assert "❌" not in out, out

    def test_check_refuses_green_while_a_tool_file_is_unreadable(
            self, env, tmp_path, monkeypatch):
        """#1542, the half the diff cannot show: after `--generate` writes the
        placeholder the documents MATCH, so a plain drift check would be
        green while a shipped doc says `⚠️ description unreadable`.
        `--check` must stay red and name the file on STDOUT — that is the
        line `validate_all` and `make pr-preflight` quote — and the message
        must not send the reader to `--generate` (it cannot clear this).
        """
        tools = gtm.TOOLS_ROOT
        (tools / "ops").mkdir()
        (tools / "ops" / "legacy.py").write_bytes(
            '"""legacy.py — 舊"""\n'.encode("cp950"))
        # The sandbox stubs `gather_tools`; this row needs the real walker.
        monkeypatch.setattr(gtm, "gather_tools", _REAL_GATHER_TOOLS)

        rc, out = env("--generate", "--lang", "all")
        assert rc == 0, out
        assert "unreadable: scripts/tools/ops/legacy.py (UnicodeDecodeError)" in out, out
        assert gtm.UNREADABLE_DESCRIPTION in gtm.TOOL_MAP.read_text(encoding="utf-8")

        rc, out = env("--check")
        assert rc == 1, out
        last = [ln for ln in out.strip().splitlines() if ln.strip()][-1]
        assert "unreadable" in last and "scripts/tools/ops/legacy.py" in last, out
        assert "UnicodeDecodeError" in last, out
        assert "--generate" not in last, out

    @pytest.mark.parametrize("en_state", ["missing", "stale"])
    def test_bare_check_covers_the_en_file(self, env, en_state):
        """Default pin: zh fresh, en not → bare `--check` is red and names en.

        This is the ticket's "same tree, two opposite answers" row: with
        the old `--lang zh` default a bare `--check` said green while the
        machine callers' `--check --lang all` said red on the same tree.
        """
        rc, _out = env("--generate", "--lang", "zh")
        assert rc == 0
        assert gtm.TOOL_MAP.exists()
        if en_state == "stale":
            gtm.TOOL_MAP_EN.write_text("junk\n", encoding="utf-8")
        else:
            assert not gtm.TOOL_MAP_EN.exists()

        rc, out = env("--check")
        assert rc == 1, out
        assert "tool-map.en.md" in out, out
        assert "Run with --generate --lang all" in out, out

        # And the hint it printed is itself sufficient.
        rc, out = env("--generate", "--lang", "all")
        assert rc == 0, out
        rc, out = env("--check")
        assert rc == 0, out
