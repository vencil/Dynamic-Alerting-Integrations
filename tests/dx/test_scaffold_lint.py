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
7. **Generated ast lint fails closed** (#2609) — decodes like the
   interpreter (BOM, PEP 263 cookie), reports true line numbers, and an
   unreadable / unparseable file exits 2 instead of passing as clean; an
   unreadable file exits 2 for every kind.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _platform_fs import symlink_or_skip  # noqa: E402

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
            "foo\n",  # trailing newline: `$` + .match let it through (#1779)
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

    @pytest.mark.parametrize("kind", sl.VALID_KINDS)
    def test_generated_test_stubs_use_pytest_skip_not_silent_pass(self, kind):
        """TODO stubs MUST be loud (pytest.skip) not silent (pass).

        Self-review pass 2 finding: ``pass`` in a stub method makes
        unfilled tests silently green; ``pytest.skip("TODO...")`` makes
        them xfail-style visible in pytest output.
        """
        paths = sl.derive_paths("foo_bar", kind)
        source = sl.render_test(paths, "Test description")
        # Three TestSuppression methods + one TestMain dirty-stub = 4
        # stubs all should call pytest.skip, none should be bare `pass`.
        assert source.count('pytest.skip("TODO') >= 4, (
            "Generated test scaffold must use pytest.skip(\"TODO...\") in "
            "stub method bodies, not bare `pass`. Bare pass = silently "
            "green = author may forget to fill in. See PR #171 self-review "
            "Fix 2."
        )
        # Specifically, the TestSuppression class body should NOT
        # contain a bare `pass` statement (would be a silent stub).
        # Check by looking for `def test_` followed by `pass` (a method
        # that ONLY has `pass` is the silent-stub anti-pattern we forbid).
        suppression_class_idx = source.find("class TestSuppression")
        assert suppression_class_idx >= 0
        # Find next class boundary or EOF
        next_class_idx = source.find("\nclass ", suppression_class_idx + 1)
        end = next_class_idx if next_class_idx > 0 else len(source)
        suppression_block = source[suppression_class_idx:end]
        # Bare `pass` as a function body would appear as `\n        pass\n`
        # at indent level 8 (inside method inside class).
        assert "\n        pass\n" not in suppression_block, (
            "TestSuppression methods contain bare `pass` (silent stub). "
            "Use pytest.skip(\"TODO...\") instead."
        )


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


# ---------------------------------------------------------------------------
# Generated ast lint fails closed and reads source like the interpreter (#2609)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_REAL_LINT_DIR = _REPO_ROOT / "scripts" / "tools" / "lint"
_REAL_TOOLS_DIR = _REPO_ROOT / "scripts" / "tools"

# Swapped in for the template's stub loop body: flag any bare ``eval(...)``.
_STUB_BODY = "        del node  # placeholder\n"
_EVAL_RULE = (
    "        if not (isinstance(node, ast.Call)\n"
    "                and isinstance(node.func, ast.Name)\n"
    "                and node.func.id == \"eval\"):\n"
    "            continue\n"
    "        if _line_has_ignore(source_lines, node.lineno):\n"
    "            continue\n"
    "        findings.append(EvalProbeFinding(\n"
    "            path=path,\n"
    "            line=node.lineno,\n"
    "            col=char_col_offset(source_lines[node.lineno - 1],\n"
    "                                node.col_offset) + 1,\n"
    "            snippet=source_lines[node.lineno - 1].strip(),\n"
    "        ))\n"
)


@pytest.fixture
def eval_lint(tmp_path, monkeypatch):
    """Scaffold an ast lint into a fake root and give it a real rule.

    The generated file imports ``_lint_helpers`` / ``_lib_exitcodes`` from
    its own directory and its parent; in the fake root those are absent,
    so the real directories go on ``PYTHONPATH`` (CLI) and ``sys.path``.
    """
    fake_root = tmp_path / "fake"
    fake_lint = fake_root / "scripts" / "tools" / "lint"
    fake_test = fake_root / "tests" / "lint"
    fake_lint.mkdir(parents=True)
    fake_test.mkdir(parents=True)
    monkeypatch.setattr(sl, "PROJECT_ROOT", fake_root)
    monkeypatch.setattr(sl, "LINT_DIR", fake_lint)
    monkeypatch.setattr(sl, "TESTS_DIR", fake_test)
    monkeypatch.setattr(sl, "PRECOMMIT_CONFIG", fake_root / ".pre-commit-config.yaml")
    rc = sl.main([
        "--name", "eval_probe",
        "--kind", "ast",
        "--description", "Flag eval calls",
        "--no-hook",
    ])
    assert rc == 0
    script = fake_lint / "check_eval_probe.py"
    source = script.read_text(encoding="utf-8")
    assert source.count(_STUB_BODY) == 1, "template stub moved; update _STUB_BODY"
    script.write_text(
        source.replace(_STUB_BODY, _EVAL_RULE), encoding="utf-8", newline="\n",
    )
    return script


def _run_cli(script: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REAL_LINT_DIR), str(_REAL_TOOLS_DIR), env.get("PYTHONPATH", "")]
    )
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )


def _load(script: Path, monkeypatch):
    monkeypatch.syspath_prepend(str(_REAL_TOOLS_DIR))
    monkeypatch.syspath_prepend(str(_REAL_LINT_DIR))
    spec = importlib.util.spec_from_file_location("check_eval_probe", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "check_eval_probe", module)
    spec.loader.exec_module(module)
    return module


class TestGeneratedAstLintFailsClosed:
    """The scaffolded ast lint must not report an unscanned file as clean."""

    @pytest.mark.timeout(60)
    def test_bom_file_violation_is_reported_with_true_line(self, eval_lint, tmp_path):
        target = tmp_path / "bom.py"
        # The form feed is one line to the tokenizer but two to
        # str.splitlines(): a splitlines()-based snippet would be off by one.
        target.write_text(
            "x = 1\n\x0c\ny = eval('1')\n", encoding="utf-8-sig", newline="\n",
        )
        assert target.read_bytes().startswith(b"\xef\xbb\xbf")
        res = _run_cli(eval_lint, "--ci", str(target))
        assert res.returncode == 1, res.stderr
        assert f"{target}:3:5 y = eval('1')" in res.stderr

    @pytest.mark.timeout(60)
    def test_latin1_cookie_file_violation_is_reported(self, eval_lint, tmp_path):
        target = tmp_path / "cookie.py"
        # The é is byte 0xE9: invalid UTF-8, so a reader that ignores the
        # cookie cannot even parse this identifier.
        target.write_text(
            "# -*- coding: latin-1 -*-\ncafé = eval('2')\n",
            encoding="latin-1",
            newline="\n",
        )
        res = _run_cli(eval_lint, "--ci", str(target))
        assert res.returncode == 1, res.stderr
        # ast's col_offset counts the UTF-8 of the decoded text ("é" is 2
        # bytes, byte column 9); the template's char_col_offset reports the
        # character column, 8 (#2646).
        assert f"{target}:2:8 café = eval('2')" in res.stderr
        assert "café = eval('2')" in res.stderr

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("ci", [True, False])
    def test_unparseable_file_exits_2_naming_it(self, eval_lint, tmp_path, ci):
        target = tmp_path / "broken.py"
        target.write_text("def broken(:\n    eval('3')\n", encoding="utf-8", newline="\n")
        res = _run_cli(eval_lint, *(["--ci"] if ci else []), str(target))
        assert res.returncode == 2, (res.stdout, res.stderr)
        assert f"{target}: cannot parse as Python" in res.stderr

    @pytest.mark.timeout(60)
    def test_unreadable_file_exits_2_naming_it(self, eval_lint, tmp_path):
        target = tmp_path / "dangling.py"
        symlink_or_skip(tmp_path / "does-not-exist.py", target)
        res = _run_cli(eval_lint, "--ci", str(target))
        assert res.returncode == 2, (res.stdout, res.stderr)
        assert f"{target}: cannot read" in res.stderr

    @pytest.mark.timeout(60)
    def test_unscanned_file_does_not_hide_others(self, eval_lint, tmp_path, monkeypatch, capsys):
        good = tmp_path / "dirty.py"
        good.write_text("z = eval('4')\n", encoding="utf-8", newline="\n")
        bad = tmp_path / "broken.py"
        bad.write_text("def broken(:\n", encoding="utf-8", newline="\n")
        lint = _load(eval_lint, monkeypatch)
        rc = lint.main(["--ci", str(good), str(bad)])
        err = capsys.readouterr().err
        assert rc == 2
        assert f"{good}:1:5" in err
        assert str(bad) in err

    @pytest.mark.timeout(60)
    def test_scan_source_raises_instead_of_returning_empty(self, eval_lint, monkeypatch):
        lint = _load(eval_lint, monkeypatch)
        with pytest.raises(lint.PythonSourceError, match="cannot parse"):
            lint.scan_source(Path("x.py"), "def broken(:\n")
        hits = lint.scan_source(Path("x.py"), b"\xef\xbb\xbfq = eval('5')\n")
        assert [(f.line, f.snippet) for f in hits] == [(1, "q = eval('5')")]


class TestGeneratedTextKindsFailClosed:
    """Non-ast kinds share ``main``: an unreadable file exits 2 for them too."""

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("kind", [k for k in sl.VALID_KINDS if k != "ast"])
    def test_unreadable_file_exits_2(self, kind, tmp_path, monkeypatch, capsys):
        script = tmp_path / f"check_probe_{kind}.py"
        script.write_text(
            sl.render_script(sl.derive_paths("probe", kind), "probe"),
            encoding="utf-8",
            newline="\n",
        )
        monkeypatch.syspath_prepend(str(_REAL_TOOLS_DIR))
        monkeypatch.syspath_prepend(str(_REAL_LINT_DIR))
        spec = importlib.util.spec_from_file_location(f"check_probe_{kind}", script)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, f"check_probe_{kind}", module)
        spec.loader.exec_module(module)
        target = tmp_path / "dangling.md"
        symlink_or_skip(tmp_path / "nowhere.md", target)
        rc = module.main([str(target)])
        assert rc == 2
        assert f"{target}: cannot read" in capsys.readouterr().err


class TestGeneratedFreshnessLintFailsClosed:
    """Front-matter the freshness lint cannot parse exits 2, not "fresh" (#2645).

    ⛔ The template used to ``return []`` on ``yaml.YAMLError``: a file whose
    dates were never looked at reported as clean, rc 0 even under ``--ci``.
    """

    @staticmethod
    def _script(tmp_path: Path) -> Path:
        script = tmp_path / "check_probe_freshness.py"
        script.write_text(
            sl.render_script(sl.derive_paths("probe", "freshness"), "probe"),
            encoding="utf-8",
            newline="\n",
        )
        return script

    @pytest.mark.timeout(60)
    @pytest.mark.parametrize("ci", [True, False])
    def test_broken_front_matter_exits_2_naming_it(self, tmp_path, ci):
        script = self._script(tmp_path)
        target = tmp_path / "broken.md"
        target.write_text(
            "---\ndate: [2026-01-01\n---\nbody\n", encoding="utf-8", newline="\n",
        )
        res = _run_cli(script, *(["--ci"] if ci else []), str(target))
        assert res.returncode == 2, (res.stdout, res.stderr)
        assert f"{target}: front-matter is not valid YAML" in res.stderr

    @pytest.mark.timeout(60)
    def test_valid_front_matter_is_still_clean(self, tmp_path):
        """Control: the raise must be confined to the parse failure."""
        script = self._script(tmp_path)
        target = tmp_path / "ok.md"
        target.write_text(
            "---\ndate: 2026-01-01\n---\nbody\n", encoding="utf-8", newline="\n",
        )
        res = _run_cli(script, "--ci", str(target))
        assert res.returncode == 0, (res.stdout, res.stderr)
