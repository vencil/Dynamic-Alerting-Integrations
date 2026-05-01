"""Tests for scaffold_lint.py — codify-the-codifier (PR #171).

Pinned contracts
----------------
1. **Naming validation** — only snake_case ASCII; reject reserved names
   and invalid identifier shapes.
2. **Path derivation** — `check_<name>.py` + `test_check_<name>.py`
   + hook id `<name-with-hyphens>-check`.
3. **Per-kind ignore markers** — text uses HTML comment, yaml uses
   YAML comment, ast/meta/freshness use Python-style.
4. **Template rendering** — generated script is **syntactically valid
   Python** (compile() check) for all 5 kinds.
5. **Hook entry insertion** — idempotent on duplicate id; preserves
   existing entries; inserts at end of last hook block.
6. **End-to-end** — running main() with --dry-run reports correct
   actions; without dry-run actually writes; generated test file
   is also valid Python.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "dx"
)
sys.path.insert(0, _TOOLS_DIR)

import scaffold_lint as sl  # noqa: E402


# ---------------------------------------------------------------------------
# Naming validation
# ---------------------------------------------------------------------------
class TestNameValidation:
    @pytest.mark.parametrize(
        "name",
        ["foo", "foo_bar", "f1", "foo_bar_baz", "abc123_def"],
    )
    def test_valid_names(self, name):
        assert sl.is_valid_lint_name(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "Foo",  # uppercase
            "foo-bar",  # hyphen
            "1foo",  # digit start
            "foo bar",  # space
            "foo!",  # symbol
            "_foo",  # leading underscore
            "foo_",  # trailing underscore
            "check",  # reserved
            "lint",  # reserved
            "test",  # reserved
            "init",  # reserved
            "main",  # reserved
        ],
    )
    def test_invalid_names_rejected(self, name):
        assert sl.is_valid_lint_name(name) is False


# ---------------------------------------------------------------------------
# Path derivation
# ---------------------------------------------------------------------------
class TestDerivePaths:
    def test_paths_match_convention(self):
        paths = sl.derive_paths("foo_bar", "text")
        assert paths.script.name == "check_foo_bar.py"
        assert paths.test.name == "test_check_foo_bar.py"
        assert paths.hook_id == "foo-bar-check"

    def test_underscore_in_name_becomes_hyphen_in_hook(self):
        paths = sl.derive_paths("alpha_beta_gamma", "ast")
        assert paths.hook_id == "alpha-beta-gamma-check"

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="invalid lint name"):
            sl.derive_paths("FooBar", "text")

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="invalid kind"):
            sl.derive_paths("foo", "javascript")

    @pytest.mark.parametrize(
        "kind,expected_marker_prefix",
        [
            ("text", "<!--"),
            ("yaml", "#"),
            ("ast", "#"),
            ("meta", "#"),
            ("freshness", "#"),
        ],
    )
    def test_ignore_marker_per_kind(self, kind, expected_marker_prefix):
        paths = sl.derive_paths("foo", kind)
        assert paths.ignore_marker.startswith(expected_marker_prefix)

    def test_text_marker_uses_html_comment(self):
        paths = sl.derive_paths("foo", "text")
        assert paths.ignore_marker == "<!-- foo: ignore -->"


# ---------------------------------------------------------------------------
# Template rendering — generated code must be valid Python
# ---------------------------------------------------------------------------
class TestRenderScript:
    @pytest.mark.parametrize("kind", sl.VALID_KINDS)
    def test_generated_script_compiles(self, kind):
        """Compile the rendered script — fails if templates have syntax error."""
        paths = sl.derive_paths("foo_bar", kind)
        source = sl.render_script(paths, "Test description")
        # Should compile without SyntaxError.
        compile(source, "<scaffold_test>", "exec")

    @pytest.mark.parametrize("kind", sl.VALID_KINDS)
    def test_generated_script_has_expected_components(self, kind):
        paths = sl.derive_paths("foo_bar", kind)
        source = sl.render_script(paths, "Test description")
        # All 5 kinds produce these:
        assert "_compute_exit_code" in source
        assert "scan_source" in source
        assert "def main(" in source
        assert "_line_has_ignore" in source
        assert "FooBarFinding" in source  # CamelCase from snake_case

    def test_text_kind_includes_fenced_block_helper(self):
        paths = sl.derive_paths("foo", "text")
        source = sl.render_script(paths, "Test")
        assert "_build_fenced_block_set" in source
        assert "_is_within_code_span" in source

    def test_yaml_kind_imports_yaml(self):
        paths = sl.derive_paths("foo", "yaml")
        source = sl.render_script(paths, "Test")
        assert "import yaml" in source

    def test_ast_kind_imports_ast(self):
        paths = sl.derive_paths("foo", "ast")
        source = sl.render_script(paths, "Test")
        assert "import ast" in source

    def test_meta_kind_imports_re_and_yaml(self):
        paths = sl.derive_paths("foo", "meta")
        source = sl.render_script(paths, "Test")
        assert "import re" in source
        assert "import yaml" in source


class TestRenderTest:
    @pytest.mark.parametrize("kind", sl.VALID_KINDS)
    def test_generated_test_compiles(self, kind):
        paths = sl.derive_paths("foo_bar", kind)
        source = sl.render_test(paths, "Test description")
        compile(source, "<scaffold_test>", "exec")

    @pytest.mark.parametrize("kind", sl.VALID_KINDS)
    def test_generated_test_has_required_classes(self, kind):
        paths = sl.derive_paths("foo_bar", kind)
        source = sl.render_test(paths, "Test description")
        assert "class TestComputeExitCode" in source
        assert "class TestMain" in source
        assert "class TestLiveRepo" in source
        assert "import check_foo_bar as lint" in source


# ---------------------------------------------------------------------------
# Hook entry insertion — idempotency + correctness
# ---------------------------------------------------------------------------
class TestHookEntryInsertion:
    def test_inserts_at_end_of_last_hook(self):
        config = (
            "default_stages: [pre-commit]\n"
            "\n"
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: existing-one\n"
            "        name: First\n"
            "        entry: foo\n"
            "\n"
            "      - id: existing-two\n"
            "        name: Second\n"
            "        entry: bar\n"
            "\n"
        )
        new_entry = (
            "      - id: new-hook-check\n"
            "        name: New Hook\n"
            "        entry: baz\n"
            "\n"
        )
        new_text, changed = sl.insert_hook_entry(
            config, new_entry, "new-hook-check"
        )
        assert changed is True
        # Both old hooks preserved.
        assert "id: existing-one" in new_text
        assert "id: existing-two" in new_text
        # New hook present.
        assert "id: new-hook-check" in new_text
        # New hook appears after both existing.
        assert new_text.index("id: existing-two") < new_text.index(
            "id: new-hook-check"
        )

    def test_idempotent_on_duplicate_id(self):
        config = (
            "    hooks:\n"
            "      - id: foo-check\n"
            "        name: Foo\n"
            "\n"
        )
        new_entry = (
            "      - id: foo-check\n"
            "        name: Duplicate\n"
            "\n"
        )
        new_text, changed = sl.insert_hook_entry(
            config, new_entry, "foo-check"
        )
        assert changed is False
        assert new_text == config

    def test_works_when_no_existing_hooks(self):
        config = (
            "default_stages: [pre-commit]\n"
            "repos:\n"
            "  - repo: local\n"
            "    hooks: []\n"
        )
        new_entry = (
            "      - id: first-hook\n"
            "        name: First\n"
            "\n"
        )
        new_text, changed = sl.insert_hook_entry(
            config, new_entry, "first-hook"
        )
        assert changed is True
        assert "id: first-hook" in new_text


# ---------------------------------------------------------------------------
# End-to-end main() — dry-run + actual write
# ---------------------------------------------------------------------------
class TestMainE2E:
    @pytest.mark.timeout(15)
    def test_main_dry_run_reports_actions(self, capsys, monkeypatch):
        monkeypatch.setattr(
            sl, "PROJECT_ROOT", Path("/tmp/_nonexistent_root_for_dryrun")
        )
        monkeypatch.setattr(
            sl, "LINT_DIR", Path("/tmp/_nonexistent_root_for_dryrun/scripts/lint")
        )
        monkeypatch.setattr(
            sl, "TESTS_DIR", Path("/tmp/_nonexistent_root_for_dryrun/tests/lint")
        )
        monkeypatch.setattr(
            sl,
            "PRECOMMIT_CONFIG",
            Path("/tmp/_nonexistent_root_for_dryrun/.pre-commit-config.yaml"),
        )
        rc = sl.main([
            "--name", "dummy",
            "--kind", "text",
            "--description", "Test description",
            "--files", "^docs/.*\\.md$",
            "--dry-run",
        ])
        out = capsys.readouterr().out
        assert rc == 0
        assert "would write" in out
        assert "(dry-run — no files written)" in out

    @pytest.mark.timeout(15)
    def test_main_actual_write_creates_files(self, tmp_path, capsys, monkeypatch):
        # Set up a fake project root so we don't pollute the real repo.
        fake_root = tmp_path / "fake_project"
        fake_lint_dir = fake_root / "scripts" / "tools" / "lint"
        fake_test_dir = fake_root / "tests" / "lint"
        fake_config = fake_root / ".pre-commit-config.yaml"
        fake_lint_dir.mkdir(parents=True)
        fake_test_dir.mkdir(parents=True)
        fake_config.write_text(
            "default_stages: [pre-commit]\n"
            "repos:\n"
            "  - repo: local\n"
            "    hooks:\n"
            "      - id: existing-hook\n"
            "        name: Existing\n"
            "        entry: foo\n"
            "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sl, "PROJECT_ROOT", fake_root)
        monkeypatch.setattr(sl, "LINT_DIR", fake_lint_dir)
        monkeypatch.setattr(sl, "TESTS_DIR", fake_test_dir)
        monkeypatch.setattr(sl, "PRECOMMIT_CONFIG", fake_config)

        rc = sl.main([
            "--name", "dummy",
            "--kind", "text",
            "--description", "Test description",
            "--files", "^docs/.*\\.md$",
        ])
        assert rc == 0

        # Files actually written.
        script_path = fake_lint_dir / "check_dummy.py"
        test_path = fake_test_dir / "test_check_dummy.py"
        assert script_path.exists()
        assert test_path.exists()
        # Hook entry inserted.
        config = fake_config.read_text(encoding="utf-8")
        assert "id: dummy-check" in config
        assert "id: existing-hook" in config

        # Generated script + test must compile.
        compile(script_path.read_text(encoding="utf-8"), str(script_path), "exec")
        compile(test_path.read_text(encoding="utf-8"), str(test_path), "exec")

    @pytest.mark.timeout(15)
    def test_main_skip_existing_without_force(self, tmp_path, monkeypatch):
        fake_root = tmp_path / "fake"
        fake_lint = fake_root / "scripts" / "tools" / "lint"
        fake_test = fake_root / "tests" / "lint"
        fake_config = fake_root / ".pre-commit-config.yaml"
        fake_lint.mkdir(parents=True)
        fake_test.mkdir(parents=True)
        # Pre-create the script so we can check skip behavior.
        existing = fake_lint / "check_dummy.py"
        existing.write_text("# existing content\n", encoding="utf-8")
        fake_config.write_text(
            "repos:\n  - repo: local\n    hooks:\n      - id: x\n        name: X\n\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sl, "PROJECT_ROOT", fake_root)
        monkeypatch.setattr(sl, "LINT_DIR", fake_lint)
        monkeypatch.setattr(sl, "TESTS_DIR", fake_test)
        monkeypatch.setattr(sl, "PRECOMMIT_CONFIG", fake_config)

        rc = sl.main([
            "--name", "dummy",
            "--kind", "ast",
            "--description", "Test",
            "--files", "^x$",
        ])
        assert rc == 0
        # Existing file unchanged.
        assert existing.read_text(encoding="utf-8") == "# existing content\n"

    @pytest.mark.timeout(15)
    def test_main_force_overwrites(self, tmp_path, monkeypatch):
        fake_root = tmp_path / "fake"
        fake_lint = fake_root / "scripts" / "tools" / "lint"
        fake_test = fake_root / "tests" / "lint"
        fake_config = fake_root / ".pre-commit-config.yaml"
        fake_lint.mkdir(parents=True)
        fake_test.mkdir(parents=True)
        existing = fake_lint / "check_dummy.py"
        existing.write_text("# existing content\n", encoding="utf-8")
        fake_config.write_text(
            "repos:\n  - repo: local\n    hooks:\n      - id: x\n        name: X\n\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(sl, "PROJECT_ROOT", fake_root)
        monkeypatch.setattr(sl, "LINT_DIR", fake_lint)
        monkeypatch.setattr(sl, "TESTS_DIR", fake_test)
        monkeypatch.setattr(sl, "PRECOMMIT_CONFIG", fake_config)

        rc = sl.main([
            "--name", "dummy",
            "--kind", "ast",
            "--description", "Test",
            "--files", "^x$",
            "--force",
        ])
        assert rc == 0
        # Now overwritten — content has scaffold markers.
        new_content = existing.read_text(encoding="utf-8")
        assert "# existing content" not in new_content
        assert "_compute_exit_code" in new_content

    @pytest.mark.timeout(15)
    def test_main_no_hook_skips_config_modification(self, tmp_path, monkeypatch):
        fake_root = tmp_path / "fake"
        fake_lint = fake_root / "scripts" / "tools" / "lint"
        fake_test = fake_root / "tests" / "lint"
        fake_config = fake_root / ".pre-commit-config.yaml"
        fake_lint.mkdir(parents=True)
        fake_test.mkdir(parents=True)
        original_config = (
            "repos:\n  - repo: local\n    hooks:\n      - id: x\n        name: X\n\n"
        )
        fake_config.write_text(original_config, encoding="utf-8")
        monkeypatch.setattr(sl, "PROJECT_ROOT", fake_root)
        monkeypatch.setattr(sl, "LINT_DIR", fake_lint)
        monkeypatch.setattr(sl, "TESTS_DIR", fake_test)
        monkeypatch.setattr(sl, "PRECOMMIT_CONFIG", fake_config)

        rc = sl.main([
            "--name", "dummy",
            "--kind", "text",
            "--description", "Test",
            "--files", "^x$",
            "--no-hook",
        ])
        assert rc == 0
        # Config unchanged.
        assert fake_config.read_text(encoding="utf-8") == original_config

    @pytest.mark.timeout(15)
    def test_main_invalid_name_returns_2(self, capsys):
        rc = sl.main([
            "--name", "INVALID",
            "--kind", "text",
            "--description", "Test",
        ])
        assert rc == 2

    @pytest.mark.timeout(15)
    def test_main_unknown_kind_argparse_rejects(self):
        with pytest.raises(SystemExit):
            sl.main([
                "--name", "foo",
                "--kind", "javascript",
                "--description", "Test",
            ])
