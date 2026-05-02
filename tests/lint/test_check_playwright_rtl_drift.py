"""Tests for check_playwright_rtl_drift.py (S#96) — RTL API name detection in Playwright specs.

Pinned contracts
----------------
1. **Detection** — three RTL-only method names flagged when called as
   ``<receiver>.<method>(``:
   - ``getByDisplayValue``
   - ``getByLabelText``
   - ``getByPlaceholderText``
   ``getByAltText`` is intentionally NOT flagged (Playwright supports it
   under the same name since 1.27).

2. **Suppression** — three layers:
   - Per-line `// playwright-rtl-drift: ignore` marker (3-line lookback)
   - TS line-comments (`// ...`) and JSDoc body (` * ...`) skipped
     wholesale
   - Inline backtick code-spans skipped (matches inside `` `page.foo()` ``
     are documentation, not calls)

3. **Severity matrix** (``_compute_exit_code``):
   - !ci, * → exit 0
   - ci, 0 findings → exit 0
   - ci, >0 findings → exit 1

4. **Live dogfood** (``TestLiveRepo``) — real `tests/e2e/**/*.spec.ts`
   produces 0 findings (PR #185 first-CI-fail already fixed; ship
   strict-from-day-1).

5. **Pre-merge intentional-break dogfood** in ``TestRegressionDogfood``
   — re-inject the historical PR #184 root cause into a synthetic
   source string and verify the lint catches it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "tools", "lint"
)
sys.path.insert(0, _TOOLS_DIR)

import check_playwright_rtl_drift as lint  # noqa: E402


def _scan(source: str, fake_path: str = "fake.spec.ts"):
    return lint.scan_source(Path(fake_path), source)


# ---------------------------------------------------------------------------
# Detection — RTL-only methods get flagged
# ---------------------------------------------------------------------------
class TestRTLMethodDetection:
    @pytest.mark.parametrize(
        "src,expected_method",
        [
            ("await page.getByDisplayValue('foo').click();\n", "getByDisplayValue"),
            ("expect(page.getByLabelText(/Name/)).toBeVisible();\n", "getByLabelText"),
            ("page.getByPlaceholderText('search').fill('x');\n", "getByPlaceholderText"),
            ("locator.getByDisplayValue('y').focus();\n", "getByDisplayValue"),
            ("frameLocator.getByLabelText('Submit').click();\n", "getByLabelText"),
            ("component.getByPlaceholderText(/q/).fill('z');\n", "getByPlaceholderText"),
        ],
    )
    def test_rtl_method_flagged(self, src, expected_method):
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].method == expected_method

    def test_multiple_methods_on_separate_lines(self):
        src = (
            "page.getByDisplayValue('a');\n"
            "page.getByLabelText('b');\n"
            "page.getByPlaceholderText('c');\n"
        )
        findings = _scan(src)
        methods = sorted(f.method for f in findings)
        assert methods == [
            "getByDisplayValue",
            "getByLabelText",
            "getByPlaceholderText",
        ]

    def test_two_methods_same_line(self):
        # Cursed but legal — pin the contract that both fire.
        src = "page.getByDisplayValue('a'); page.getByLabelText('b');\n"
        findings = _scan(src)
        assert len(findings) == 2

    def test_column_points_at_method_name(self):
        # `page.getByDisplayValue` — column should land on `g` of getBy.
        src = "page.getByDisplayValue('foo');\n"
        findings = _scan(src)
        assert len(findings) == 1
        # 1-based: 'p'=1, 'a'=2, 'g'=3, 'e'=4, '.'=5, 'g'=6
        assert findings[0].col == 6


# ---------------------------------------------------------------------------
# Negative — Playwright-native methods do NOT fire
# ---------------------------------------------------------------------------
class TestPlaywrightNativePass:
    @pytest.mark.parametrize(
        "src",
        [
            # Playwright native — should NOT fire.
            "page.getByLabel('Name').fill('x');\n",
            "page.getByPlaceholder('search').click();\n",
            "page.getByAltText('logo').click();\n",  # both libraries have this
            "page.getByText('hello').isVisible();\n",
            "page.getByRole('button').click();\n",
            "page.getByTitle('tooltip').hover();\n",
            "page.getByTestId('btn').click();\n",
            # Substring of banned name — must not fire (word boundary).
            "page.myGetByDisplayValueWrapper('x');\n",
            # No method call (just a reference, no parens).
            "// see page.getByDisplayValue\n",
            # Standalone identifier without dot — not a call on a receiver.
            "const fn = getByDisplayValue;\n",
        ],
    )
    def test_playwright_native_not_flagged(self, src):
        assert _scan(src) == []


# ---------------------------------------------------------------------------
# Suppression — comments + ignore markers + backticks
# ---------------------------------------------------------------------------
class TestSuppression:
    def test_inline_line_comment_skipped(self):
        # `//` inline comment alone is treated as a comment line.
        src = "// page.getByDisplayValue('x') is RTL-only, never call this\n"
        assert _scan(src) == []

    def test_jsdoc_body_skipped(self):
        # ` * ...` JSDoc body lines are skipped.
        src = (
            "/**\n"
            " * Don't use page.getByDisplayValue() — it's RTL-only.\n"
            " * Prefer page.evaluate(...) for input value reads.\n"
            " */\n"
        )
        assert _scan(src) == []

    def test_inline_trailing_comment_does_not_mask_real_call(self):
        # Real call followed by a comment discussing the alternative —
        # we still flag the call, but only once.
        src = "page.getByDisplayValue('x'); // see notes about RTL\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].method == "getByDisplayValue"

    def test_backtick_code_span_skipped(self):
        # Markdown-style inline code in a comment — but inside actual TS
        # code, the only place backticks appear is template literals.
        # We're conservative: any odd-backtick prefix on the line is
        # treated as inside-span. Test a JSDoc reference scenario.
        src = (
            "// Why not `page.getByDisplayValue('x')`? Because RTL.\n"
            "// `page.getByLabelText('y')` also doesn't exist.\n"
        )
        assert _scan(src) == []

    def test_backtick_template_literal_real_call_flagged(self):
        # Backticks inside a template-literal arg shouldn't suppress
        # the receiver call itself — verify boundary.
        src = "page.getByDisplayValue(`name-${id}`);\n"
        findings = _scan(src)
        # The method call is BEFORE the first backtick, so col is
        # outside any span — should fire.
        assert len(findings) == 1

    def test_ignore_marker_same_line(self):
        src = "page.getByDisplayValue('x'); // playwright-rtl-drift: ignore — pinned for compat test\n"
        assert _scan(src) == []

    def test_ignore_marker_lookback_one_line(self):
        src = (
            "// playwright-rtl-drift: ignore — see notes below\n"
            "page.getByDisplayValue('x');\n"
        )
        assert _scan(src) == []

    def test_ignore_marker_lookback_three_lines(self):
        src = (
            "// playwright-rtl-drift: ignore\n"
            "// rationale line 1\n"
            "// rationale line 2\n"
            "page.getByDisplayValue('x');\n"
        )
        assert _scan(src) == []

    def test_ignore_marker_lookback_too_far(self):
        # 4 lines above — outside lookback, should fire.
        src = (
            "// playwright-rtl-drift: ignore\n"
            "// pad 1\n"
            "// pad 2\n"
            "// pad 3\n"
            "page.getByDisplayValue('x');\n"
        )
        assert len(_scan(src)) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_source(self):
        assert _scan("") == []

    def test_whitespace_only_source(self):
        assert _scan("   \n\n  \t\n") == []

    def test_method_at_end_of_file_no_trailing_newline(self):
        # No final newline — historically this trips up line-based
        # parsers. Pin the contract.
        src = "page.getByDisplayValue('x')"
        assert len(_scan(src)) == 1

    def test_chained_method_call(self):
        # `page.getByDisplayValue('x').click()` should fire on the
        # banned method, not on the chained `.click()`.
        src = "page.getByDisplayValue('x').click();\n"
        findings = _scan(src)
        assert len(findings) == 1
        assert findings[0].method == "getByDisplayValue"

    def test_unicode_safe(self):
        # Non-ASCII content in surrounding code shouldn't trip the regex.
        src = "// 中文註解\npage.getByDisplayValue('x');\n"
        findings = _scan(src)
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Severity / exit-code matrix
# ---------------------------------------------------------------------------
class TestExitCode:
    def test_warn_only_with_findings(self):
        assert lint._compute_exit_code(ci=False, n_findings=5) == 0

    def test_warn_only_zero_findings(self):
        assert lint._compute_exit_code(ci=False, n_findings=0) == 0

    def test_ci_zero_findings(self):
        assert lint._compute_exit_code(ci=True, n_findings=0) == 0

    def test_ci_with_findings(self):
        assert lint._compute_exit_code(ci=True, n_findings=1) == 1

    def test_ci_many_findings(self):
        assert lint._compute_exit_code(ci=True, n_findings=99) == 1


# ---------------------------------------------------------------------------
# main() integration via subprocess (real exit codes)
# ---------------------------------------------------------------------------
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "tools" / "lint" / "check_playwright_rtl_drift.py"
)


class TestMainIntegration:
    def test_clean_file_warn_exit_zero(self, tmp_path):
        spec = tmp_path / "clean.spec.ts"
        spec.write_text("page.getByLabel('x').click();\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), str(spec)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0

    def test_dirty_file_ci_exit_one(self, tmp_path):
        spec = tmp_path / "dirty.spec.ts"
        spec.write_text(
            "page.getByDisplayValue('x').click();\n", encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), "--ci", str(spec)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert "getByDisplayValue" in result.stderr

    def test_dirty_file_warn_only_exit_zero(self, tmp_path):
        spec = tmp_path / "dirty.spec.ts"
        spec.write_text(
            "page.getByDisplayValue('x').click();\n", encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), str(spec)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Warn-only mode (no --ci) — even with findings, exit 0.
        assert result.returncode == 0
        assert "getByDisplayValue" in result.stderr


# ---------------------------------------------------------------------------
# Live dogfood — real repo audit
# ---------------------------------------------------------------------------
class TestLiveRepo:
    def test_zero_findings_in_repo(self):
        """Real repo must be clean — PR #185 fixed the historical drift."""
        repo_specs = sorted(
            (lint.PROJECT_ROOT / "tests" / "e2e").rglob("*.spec.ts")
        )
        assert repo_specs, "No Playwright specs found — directory layout changed?"
        all_findings = []
        for spec in repo_specs:
            source = spec.read_text(encoding="utf-8", errors="replace")
            all_findings.extend(lint.scan_source(spec, source))
        assert all_findings == [], (
            f"Live repo has RTL drift: {[f.render() for f in all_findings]}"
        )


# ---------------------------------------------------------------------------
# Pre-merge intentional-break dogfood
# ---------------------------------------------------------------------------
class TestRegressionDogfood:
    """Re-inject the historical PR #184 root cause to verify regression catch."""

    def test_pr_184_historical_pattern_caught(self):
        # PR #184 first-CI-fail spec contained these literal calls. After
        # commit 912cf2b they were replaced by the readAllInputValues
        # helper. This test inlines the pattern to confirm the lint
        # would have caught it at commit time.
        historical_pattern = (
            "// reproduced from PR #184 first CI run\n"
            "await expect(page.getByDisplayValue('tenant')).toBeVisible();\n"
            "await expect(page.getByDisplayValue(TENANT_ID)).toBeVisible();\n"
        )
        findings = _scan(historical_pattern, fake_path="historical.spec.ts")
        # Two flagged calls (lines 2 and 3 of the snippet).
        assert len(findings) == 2
        assert all(f.method == "getByDisplayValue" for f in findings)
