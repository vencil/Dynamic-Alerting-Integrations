"""Tests for check_subprocess_timeout.py — S#74 Code-driven Layer A lint.

Pinned contracts
----------------
1. **Detection rules**:
   - ``subprocess.run/call/check_call/check_output(...)`` without
     ``timeout=`` → flagged (rule "subprocess.<fn>-no-timeout")
   - ``<expr>.communicate(...)`` without ``timeout=`` → flagged
     (rule "communicate-no-timeout")
   - calls WITH ``timeout=...`` → NOT flagged
   - calls with ``# subprocess-timeout: ignore`` on the line OR the
     line above → NOT flagged

2. **Severity matrix** (4-state truth table over --ci × --strict):
   - !ci, * → exit 0 (audit mode never fails)
   - ci, !strict → exit 0 (warn-only during cleanup track)
   - ci, strict, 0 violations → exit 0
   - ci, strict, >0 violations → exit 1

3. **Robustness**:
   - syntax errors in source → empty result (don't crash)
   - subprocess.Popen(...) constructor → NOT flagged (Popen has no
     timeout kwarg in its constructor; timeout belongs on later
     .communicate() / .wait())
   - bare ``run(...)`` from ``from subprocess import run`` → NOT
     flagged (deliberate: bare-import is uncommon in this codebase
     and would require import-tracking)

The headline regression-detection (#1 and #2) maps the codify-via-lint
pattern from PR #162's ``lint_jsx_babel.py`` granular --strict split.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_subprocess_timeout as cst  # noqa: E402


def _scan(source: str, fake_path: str = "fake.py"):
    """Parse + scan a snippet, return list of violations."""
    return cst.scan_source(Path(fake_path), source)


# ---------------------------------------------------------------------------
# Detection — subprocess.<fn>(...)
# ---------------------------------------------------------------------------
class TestSubprocessFnDetection:
    @pytest.mark.parametrize(
        "fn",
        ["run", "call", "check_call", "check_output"],
    )
    def test_subprocess_fn_without_timeout_flagged(self, fn):
        src = f"import subprocess\nsubprocess.{fn}(['ls'])\n"
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == f"subprocess.{fn}-no-timeout"
        assert violations[0].line == 2

    @pytest.mark.parametrize(
        "fn",
        ["run", "call", "check_call", "check_output"],
    )
    def test_subprocess_fn_with_timeout_not_flagged(self, fn):
        src = f"import subprocess\nsubprocess.{fn}(['ls'], timeout=30)\n"
        assert _scan(src) == []

    def test_subprocess_run_with_timeout_among_other_kwargs(self):
        src = (
            "import subprocess\n"
            "subprocess.run(['ls'], capture_output=True, text=True, timeout=10)\n"
        )
        assert _scan(src) == []

    def test_multiline_call_flagged(self):
        src = (
            "import subprocess\n"
            "result = subprocess.run(\n"
            "    ['ls', '-la'],\n"
            "    capture_output=True,\n"
            ")\n"
        )
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == "subprocess.run-no-timeout"

    def test_subprocess_popen_constructor_not_flagged(self):
        """Popen(...) constructor has no timeout kwarg; timeout belongs
        on later .communicate() / .wait() calls."""
        src = (
            "import subprocess\n"
            "proc = subprocess.Popen(['ls'], stdout=subprocess.PIPE)\n"
        )
        assert _scan(src) == []

    def test_bare_imported_run_not_flagged(self):
        """Bare ``run(...)`` from ``from subprocess import run`` is
        not flagged (would require import tracking; tradeoff for
        zero false-positives on common patterns like ``run()`` in
        unrelated modules)."""
        src = "from subprocess import run\nrun(['ls'])\n"
        assert _scan(src) == []


# ---------------------------------------------------------------------------
# Detection — <expr>.communicate(...)
# ---------------------------------------------------------------------------
class TestCommunicateDetection:
    def test_communicate_without_timeout_flagged(self):
        src = (
            "import subprocess\n"
            "proc = subprocess.Popen(['ls'])\n"
            "proc.communicate()\n"
        )
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == "communicate-no-timeout"
        assert violations[0].line == 3

    def test_communicate_with_timeout_not_flagged(self):
        src = (
            "import subprocess\n"
            "proc = subprocess.Popen(['ls'])\n"
            "proc.communicate(timeout=60)\n"
        )
        assert _scan(src) == []

    def test_communicate_with_input_and_timeout(self):
        src = (
            "import subprocess\n"
            "proc = subprocess.Popen(['ls'], stdin=subprocess.PIPE)\n"
            "proc.communicate(input=b'data', timeout=60)\n"
        )
        assert _scan(src) == []

    def test_communicate_chained_call(self):
        """``subprocess.Popen(...).communicate(...)`` chained — both
        the Popen (constructor, no flag) and the bare communicate
        (heuristic flagged) appear; only communicate flagged."""
        src = (
            "import subprocess\n"
            "subprocess.Popen(['ls']).communicate()\n"
        )
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == "communicate-no-timeout"


# ---------------------------------------------------------------------------
# Per-line ignore comment
# ---------------------------------------------------------------------------
class TestIgnoreComment:
    def test_ignore_on_call_line_suppresses(self):
        src = (
            "import subprocess\n"
            "subprocess.run(['git', 'rev-parse', 'HEAD'])  "
            "# subprocess-timeout: ignore\n"
        )
        assert _scan(src) == []

    def test_ignore_on_line_above_suppresses(self):
        """For multi-line calls, ignore on the line above is allowed
        because the AST node points to the call line; users may write
        the comment above for readability."""
        src = (
            "import subprocess\n"
            "# subprocess-timeout: ignore\n"
            "subprocess.run(\n"
            "    ['git', 'rev-parse', 'HEAD']\n"
            ")\n"
        )
        assert _scan(src) == []

    def test_ignore_within_3_line_lookback_suppresses(self):
        """3-line lookback supports multi-line comment blocks above the
        call (e.g. rationale + marker structure)."""
        src = (
            "import subprocess\n"
            "# rationale line 1\n"
            "# rationale line 2\n"
            "# subprocess-timeout: ignore\n"
            "subprocess.run(['ls'])\n"
        )
        assert _scan(src) == []

    def test_ignore_outside_3_line_lookback_does_not_suppress(self):
        """Marker more than 3 lines above the call does NOT suppress —
        keeps the ignore radius tight to prevent dangling markers
        across refactors."""
        src = (
            "import subprocess\n"
            "# subprocess-timeout: ignore\n"
            "x = 1\n"
            "y = 2\n"
            "z = 3\n"
            "w = 4\n"
            "subprocess.run(['ls'])\n"
        )
        violations = _scan(src)
        assert len(violations) == 1


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------
class TestRobustness:
    def test_syntax_error_returns_empty(self):
        """A file with syntax errors must not crash the lint."""
        src = "def broken(\n    x = subprocess.run('ls')\n"
        assert _scan(src) == []

    def test_string_containing_subprocess_run_not_flagged(self):
        """AST-based, not string-matching: subprocess.run() inside a
        string literal is NOT a real call."""
        src = (
            'import subprocess\n'
            's = "subprocess.run([\'ls\'])"\n'
            'subprocess.run(["echo", s], timeout=5)\n'
        )
        assert _scan(src) == []

    def test_comment_containing_subprocess_run_not_flagged(self):
        src = (
            "import subprocess\n"
            "# subprocess.run(['ls']) — example call in comment\n"
            "subprocess.run(['echo'], timeout=5)\n"
        )
        assert _scan(src) == []


# ---------------------------------------------------------------------------
# Severity matrix — _compute_exit_code truth table
# ---------------------------------------------------------------------------
class TestComputeExitCode:
    """4-state truth table: (--ci) × (--strict-subprocess-timeout) ×
    (n_violations 0/N) → exit code.

    Mirrors lint_jsx_babel.py's _compute_exit_code matrix from PR #154
    + PR #162's granular --strict split.
    """

    @pytest.mark.parametrize("strict", [False, True])
    @pytest.mark.parametrize("n_violations", [0, 5])
    def test_no_ci_always_exit_0(self, strict, n_violations):
        rc = cst._compute_exit_code(
            ci=False,
            strict_subprocess_timeout=strict,
            n_violations=n_violations,
        )
        assert rc == 0

    @pytest.mark.parametrize("n_violations", [0, 5])
    def test_ci_no_strict_always_exit_0(self, n_violations):
        """--ci alone is warn-only — surfaces the count without blocking."""
        rc = cst._compute_exit_code(
            ci=True,
            strict_subprocess_timeout=False,
            n_violations=n_violations,
        )
        assert rc == 0

    def test_ci_strict_zero_violations_exit_0(self):
        rc = cst._compute_exit_code(
            ci=True,
            strict_subprocess_timeout=True,
            n_violations=0,
        )
        assert rc == 0

    @pytest.mark.parametrize("n_violations", [1, 5, 100])
    def test_ci_strict_with_violations_exit_1(self, n_violations):
        rc = cst._compute_exit_code(
            ci=True,
            strict_subprocess_timeout=True,
            n_violations=n_violations,
        )
        assert rc == 1


# ---------------------------------------------------------------------------
# Edge cases — timeout=None / timeout=0 semantics
# ---------------------------------------------------------------------------
class TestTimeoutValueSemantics:
    """``timeout=`` presence alone is not enough — ``timeout=None`` and
    ``timeout=0`` are functionally identical to no timeout. They MUST
    be flagged. ``_has_meaningful_timeout`` is the gate.
    """

    def test_timeout_none_flagged(self):
        src = "import subprocess\nsubprocess.run(['ls'], timeout=None)\n"
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == "subprocess.run-no-timeout"

    def test_timeout_zero_int_flagged(self):
        src = "import subprocess\nsubprocess.run(['ls'], timeout=0)\n"
        violations = _scan(src)
        assert len(violations) == 1

    def test_timeout_zero_float_flagged(self):
        src = "import subprocess\nsubprocess.run(['ls'], timeout=0.0)\n"
        violations = _scan(src)
        assert len(violations) == 1

    def test_timeout_positive_int_accepted(self):
        src = "import subprocess\nsubprocess.run(['ls'], timeout=30)\n"
        assert _scan(src) == []

    def test_timeout_positive_float_accepted(self):
        src = "import subprocess\nsubprocess.run(['ls'], timeout=2.5)\n"
        assert _scan(src) == []

    def test_timeout_variable_accepted(self):
        """Variables can't be statically evaluated; structural check
        only — the author wrote ``timeout=X``, that's the lint's bar."""
        src = (
            "import subprocess\n"
            "MY_TIMEOUT = 60\n"
            "subprocess.run(['ls'], timeout=MY_TIMEOUT)\n"
        )
        assert _scan(src) == []

    def test_timeout_expression_accepted(self):
        src = (
            "import subprocess\n"
            "subprocess.run(['ls'], timeout=int(os.environ.get('T', '60')))\n"
        )
        assert _scan(src) == []

    def test_communicate_timeout_none_flagged(self):
        src = (
            "import subprocess\n"
            "p = subprocess.Popen(['ls'])\n"
            "p.communicate(timeout=None)\n"
        )
        violations = _scan(src)
        assert len(violations) == 1
        assert violations[0].rule == "communicate-no-timeout"


# ---------------------------------------------------------------------------
# File discovery — _iter_python_files
# ---------------------------------------------------------------------------
class TestIterPythonFiles:
    def test_single_py_file_yielded(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        assert list(cst._iter_python_files([f])) == [f]

    def test_non_py_file_not_yielded(self, tmp_path):
        f = tmp_path / "a.md"
        f.write_text("# md\n")
        assert list(cst._iter_python_files([f])) == []

    def test_directory_recursive(self, tmp_path):
        (tmp_path / "a.py").write_text("")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "b.py").write_text("")
        (sub / "c.txt").write_text("")
        results = list(cst._iter_python_files([tmp_path]))
        names = {p.name for p in results}
        assert names == {"a.py", "b.py"}

    def test_deterministic_sort_order(self, tmp_path):
        (tmp_path / "z.py").write_text("")
        (tmp_path / "a.py").write_text("")
        (tmp_path / "m.py").write_text("")
        results = list(cst._iter_python_files([tmp_path]))
        names = [p.name for p in results]
        assert names == sorted(names)

    def test_nonexistent_root_silently_skipped(self, tmp_path):
        bogus = tmp_path / "does-not-exist"
        # Not is_file and not is_dir → falls through; no crash.
        assert list(cst._iter_python_files([bogus])) == []


# ---------------------------------------------------------------------------
# Path resolution — _resolve_scan_paths
# ---------------------------------------------------------------------------
class TestResolveScanPaths:
    def test_explicit_absolute_path_used_as_is(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("")
        # Mimic argparse Namespace
        import argparse
        args = argparse.Namespace(paths=[str(f)])
        resolved = cst._resolve_scan_paths(args)
        assert resolved == [f]

    def test_explicit_relative_path_anchored_to_project_root(self):
        import argparse
        args = argparse.Namespace(paths=["scripts/tools/lint/check_subprocess_timeout.py"])
        resolved = cst._resolve_scan_paths(args)
        assert len(resolved) == 1
        assert resolved[0].is_absolute()
        # Anchored to PROJECT_ROOT
        assert str(resolved[0]).startswith(str(cst.PROJECT_ROOT))

    def test_no_paths_falls_back_to_default_roots(self):
        import argparse
        args = argparse.Namespace(paths=[])
        resolved = cst._resolve_scan_paths(args)
        # Defaults are scripts/, components/da-tools/, tests/
        # — at least scripts/ should exist at PROJECT_ROOT
        assert any(p.name == "scripts" for p in resolved)


# ---------------------------------------------------------------------------
# main() integration — argparse + print + exit code wiring
# ---------------------------------------------------------------------------
class TestMain:
    """End-to-end via monkeypatch sys.argv + capsys, no subprocess.

    Mirrors lint_jsx_babel.py's main()-test pattern from PR #154.
    The truth-table tests above cover ``_compute_exit_code`` in
    isolation; these tests pin the wiring (argparse → scan → print →
    exit code) is correct.
    """

    @pytest.mark.timeout(30)
    def test_main_clean_codebase_exits_0(self, tmp_path, capsys, monkeypatch):
        """No violations + --ci → exit 0 + clean message."""
        clean = tmp_path / "clean.py"
        clean.write_text(
            "import subprocess\n"
            "subprocess.run(['ls'], timeout=30)\n"
        )
        monkeypatch.setattr(
            sys, "argv", ["check_subprocess_timeout.py", "--ci", str(clean)]
        )
        rc = cst.main()
        out = capsys.readouterr().out
        assert rc == 0
        assert "no subprocess calls without timeout=" in out

    @pytest.mark.timeout(30)
    def test_main_violations_under_ci_only_warns(self, tmp_path, capsys, monkeypatch):
        """Violations + --ci (no --strict-...) → exit 0, but reports."""
        dirty = tmp_path / "dirty.py"
        dirty.write_text(
            "import subprocess\n"
            "subprocess.run(['ls'])\n"
        )
        monkeypatch.setattr(
            sys, "argv", ["check_subprocess_timeout.py", "--ci", str(dirty)]
        )
        rc = cst.main()
        out = capsys.readouterr().out
        assert rc == 0  # warn-only
        assert "WARN" in out
        assert "subprocess.run-no-timeout" in out

    @pytest.mark.timeout(30)
    def test_main_violations_under_strict_exits_1(self, tmp_path, capsys, monkeypatch):
        """Violations + --ci + --strict-subprocess-timeout → exit 1."""
        dirty = tmp_path / "dirty.py"
        dirty.write_text(
            "import subprocess\n"
            "subprocess.run(['ls'])\n"
        )
        monkeypatch.setattr(
            sys, "argv",
            [
                "check_subprocess_timeout.py",
                "--ci",
                "--strict-subprocess-timeout",
                str(dirty),
            ],
        )
        rc = cst.main()
        out = capsys.readouterr().out
        assert rc == 1
        assert "ERROR" in out

    @pytest.mark.timeout(30)
    def test_main_no_ci_always_exits_0(self, tmp_path, capsys, monkeypatch):
        """Violations + no --ci → exit 0 (audit mode never fails)."""
        dirty = tmp_path / "dirty.py"
        dirty.write_text(
            "import subprocess\n"
            "subprocess.run(['ls'])\n"
        )
        monkeypatch.setattr(
            sys, "argv", ["check_subprocess_timeout.py", str(dirty)]
        )
        rc = cst.main()
        assert rc == 0  # audit mode never fails


# ---------------------------------------------------------------------------
# Multi-violation report shape
# ---------------------------------------------------------------------------
class TestReportShape:
    def test_multiple_violations_in_one_file(self):
        src = (
            "import subprocess\n"
            "subprocess.run(['a'])\n"
            "subprocess.run(['b'], timeout=10)\n"
            "proc = subprocess.Popen(['c'])\n"
            "proc.communicate()\n"
        )
        violations = _scan(src)
        # Expect 2: line 2 (run no-timeout), line 5 (communicate no-timeout)
        assert len(violations) == 2
        rules = sorted(v.rule for v in violations)
        assert rules == ["communicate-no-timeout", "subprocess.run-no-timeout"]

    def test_render_produces_path_line_col_rule_format(self):
        v = cst.TimeoutViolation(
            path=Path("a/b.py"),
            line=42,
            col=8,
            rule="subprocess.run-no-timeout",
            snippet="result = subprocess.run(...)",
        )
        rendered = v.render()
        assert "a" in rendered and "b.py" in rendered
        assert ":42:8" in rendered
        assert "[subprocess.run-no-timeout]" in rendered
