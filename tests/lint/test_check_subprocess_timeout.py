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
