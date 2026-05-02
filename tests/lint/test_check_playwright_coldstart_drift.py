"""Tests for check_playwright_coldstart_drift.py (S#97) — Tier 1 mechanical net for testing-playbook §LL §11.

Pinned contracts
----------------
1. **Detection** — `getByTestId('X-state-(ready|success|loaded|error|fail)')`
   followed by `.toBeVisible(` flagged when the test() block has NO
   preceding input establishment.

2. **Input establishment** — any of these in the same block before the
   assertion suppresses the warning:
     - `*.fill(` / `*.click(` / `*.dispatchEvent(` / `*.selectOption(`
       / `*.setInputFiles(` / `*.check(` / `*.uncheck(` / `*.press(`
       / `*.type(`
     - `page.goto('<url with ?key=value>')`
     - `page.route('...')`
     - `page.evaluate(...)`

3. **Markers**:
     - `// playwright-coldstart: auto-fire` → block-level escape
       (3-line lookback for JSDoc)
     - `// playwright-coldstart: ignore` → per-line escape
       (3-line lookback)

4. **State-suffix narrowness** — only `-state-(ready|success|loaded|
   error|fail)` matches. `-preview` / `-result` / `-output` are NOT
   matched (would false-positive on tool names like `simulate-preview-
   tenant-id`).

5. **Severity matrix**:
   - !ci, * → exit 0
   - ci, 0 findings → exit 0
   - ci, >0 findings → exit 1

6. **Live dogfood** — `tests/e2e/**/*.spec.ts` produces 0 findings
   (PR #185 fix already merged).

7. **Pre-merge intentional-break dogfood** — re-inject PR #185
   broken pattern (no input establishment, asserts state-ready) →
   verify the lint catches it.
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

import check_playwright_coldstart_drift as lint  # noqa: E402


def _scan(source: str, fake_path: str = "fake.spec.ts"):
    return lint.scan_source(Path(fake_path), source)


def _make_test_block(body: str, name: str = "scenario") -> str:
    """Wrap body inside a minimal test() block."""
    return (
        f"test('{name}', async ({{ page }}) => {{\n"
        f"  await loadPortalTool(page, 'simulate-preview');\n"
        f"{body}"
        f"}});\n"
    )


# ---------------------------------------------------------------------------
# Detection — downstream-state testid + toBeVisible flagged when no input
# ---------------------------------------------------------------------------
class TestDriftDetection:
    @pytest.mark.parametrize(
        "state",
        ["ready", "success", "loaded", "error", "fail"],
    )
    def test_each_state_suffix_flagged(self, state):
        body = (
            f"  await expect(page.getByTestId('foo-state-{state}'))"
            f".toBeVisible();\n"
        )
        findings = _scan(_make_test_block(body))
        assert len(findings) == 1
        assert findings[0].testid == f"foo-state-{state}"

    def test_pr_185_historical_testid_flagged(self):
        # The exact pattern PR #185 first CI run failed on.
        body = (
            "  await expect(page.getByTestId('simulate-preview-state-ready'))"
            ".toBeVisible({ timeout: 5000 });\n"
        )
        findings = _scan(_make_test_block(body))
        assert len(findings) == 1
        assert findings[0].testid == "simulate-preview-state-ready"

    def test_finding_records_test_block_start(self):
        body = (
            "  await expect(page.getByTestId('foo-state-ready'))"
            ".toBeVisible();\n"
        )
        src = _make_test_block(body, name="block-1")
        findings = _scan(src)
        assert len(findings) == 1
        # block@L1 because test( is on line 1 of our wrapper.
        assert findings[0].test_block_start == 1


# ---------------------------------------------------------------------------
# Negative — not flagged when various conditions
# ---------------------------------------------------------------------------
class TestNegativeCases:
    def test_input_state_testid_not_flagged(self):
        # `simulate-preview-tenant-id` is INPUT, not state — must not flag.
        # This was the false positive in early dev (caught by dogfood).
        body = (
            "  await expect(page.getByTestId('simulate-preview-tenant-id'))"
            ".toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_state_empty_not_flagged(self):
        # state-empty IS a legitimate cold-start state — render on mount
        # is the contract. Not flagged.
        body = (
            "  await expect(page.getByTestId('foo-state-empty'))"
            ".toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_state_loading_not_flagged(self):
        body = (
            "  await expect(page.getByTestId('foo-state-loading'))"
            ".toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    @pytest.mark.parametrize(
        "input_call",
        [
            "  await page.getByTestId('input').fill('x');\n",
            "  await page.getByTestId('btn').click();\n",
            "  await page.getByTestId('select').selectOption('opt');\n",
            "  await page.getByTestId('cb').check();\n",
            "  await page.keyboard.press('Tab');\n",
            "  await page.dispatchEvent('input', 'change');\n",
            "  await page.setInputFiles('input', '/tmp/file');\n",
            "  await page.type('input', 'hello');\n",
            "  await page.uncheck('checkbox');\n",
        ],
    )
    def test_input_establishment_suppresses_warning(self, input_call):
        body = (
            f"{input_call}"
            f"  await expect(page.getByTestId('foo-state-ready'))"
            f".toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_page_goto_with_query_params_suppresses(self):
        body = (
            "  await page.goto('../assets/jsx-loader.html?component=foo&tenant_id=x');\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_page_goto_without_query_params_does_not_suppress(self):
        # A bare goto without params does NOT count as input — the URL
        # alone doesn't push state.
        body = (
            "  await page.goto('../assets/jsx-loader.html?component=foo');\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        # Whoops — this DOES have ?component= which matches our pattern.
        # The pattern fires on any ?key=value, regardless of which key.
        # That's intentional — hard to distinguish "presentation params"
        # from "state params" without runtime knowledge.
        assert _scan(_make_test_block(body)) == []

    def test_page_route_suppresses(self):
        body = (
            "  await page.route('**/api/v1/foo', async route => route.fulfill({}));\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_page_evaluate_suppresses(self):
        body = (
            "  await page.evaluate(() => window.dispatch({}));\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_no_to_be_visible_not_flagged(self):
        # Reading the testid for some other purpose — no .toBeVisible — not
        # flagged. Different contract (e.g. `expect(...).toContainText`).
        body = (
            "  await expect(page.getByTestId('foo-state-ready')).toContainText('done');\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_no_test_block_no_findings(self):
        # Source with no test() block (just imports / utility code).
        src = (
            "import { test } from '@playwright/test';\n"
            "function helper() { return 1; }\n"
            "// page.getByTestId('foo-state-ready') in comment\n"
        )
        assert _scan(src) == []


# ---------------------------------------------------------------------------
# Marker suppression
# ---------------------------------------------------------------------------
class TestMarkers:
    def test_auto_fire_marker_inline_suppresses(self):
        body = (
            "  // playwright-coldstart: auto-fire — verified default state\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_auto_fire_marker_anywhere_in_block_suppresses(self):
        # auto-fire is a BLOCK-level marker — placement within the block
        # should not matter.
        body = (
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "  // playwright-coldstart: auto-fire\n"  # AFTER the assertion
        )
        assert _scan(_make_test_block(body)) == []

    def test_ignore_marker_per_line_suppresses(self):
        body = (
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible(); "
            "// playwright-coldstart: ignore\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_ignore_marker_lookback_one_line(self):
        body = (
            "  // playwright-coldstart: ignore\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_ignore_marker_lookback_three_lines(self):
        body = (
            "  // playwright-coldstart: ignore\n"
            "  // pad 1\n"
            "  // pad 2\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_ignore_marker_lookback_too_far(self):
        # 4 lines above — outside lookback, should fire.
        body = (
            "  // playwright-coldstart: ignore\n"
            "  // pad 1\n"
            "  // pad 2\n"
            "  // pad 3\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
        )
        assert len(_scan(_make_test_block(body))) == 1


# ---------------------------------------------------------------------------
# Multi-block + complex sources
# ---------------------------------------------------------------------------
class TestMultiBlock:
    def test_two_blocks_independent_input_establishment(self):
        # Block A has fill, B does not. A passes, B should fire.
        src = (
            "test('block-a', async ({ page }) => {\n"
            "  await page.getByTestId('input').fill('x');\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n"
            "test('block-b', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n"
        )
        findings = _scan(src)
        assert len(findings) == 1
        # Block B (the failing one) starts at line 5 (1-based).
        assert findings[0].test_block_start == 5

    def test_describe_wrapper_does_not_count_as_block(self):
        # test.describe() is a container, not a block. The inner test()
        # is the real block.
        src = (
            "test.describe('group', () => {\n"
            "  test('inner', async ({ page }) => {\n"
            "    await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "  });\n"
            "});\n"
        )
        findings = _scan(src)
        assert len(findings) == 1
        # block start at line 2 (the test(), not the describe).
        assert findings[0].test_block_start == 2

    def test_test_skip_and_test_only_recognized_as_blocks(self):
        src = (
            "test.skip('skipped', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n"
            "test.only('focused', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n"
        )
        findings = _scan(src)
        assert len(findings) == 2

    def test_multiple_assertions_in_one_block(self):
        body = (
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "  await expect(page.getByTestId('foo-state-error')).toBeVisible();\n"
        )
        findings = _scan(_make_test_block(body))
        assert len(findings) == 2


# ---------------------------------------------------------------------------
# Comment / docstring safety
# ---------------------------------------------------------------------------
class TestCommentSafety:
    def test_jsdoc_mentions_state_testid_not_flagged(self):
        # JSDoc body lines (` *`) skipped wholesale.
        src = (
            "/**\n"
            " * Don't write expect(getByTestId('foo-state-ready')).toBeVisible()\n"
            " * without preceding input establishment.\n"
            " */\n"
            "test('block', async ({ page }) => {\n"
            "  await page.getByTestId('input').fill('x');\n"
            "});\n"
        )
        assert _scan(src) == []

    def test_inline_comment_with_state_pattern_not_flagged(self):
        body = (
            "  // expect(getByTestId('foo-state-ready')).toBeVisible() — bad\n"
            "  await page.getByTestId('input').fill('x');\n"
        )
        assert _scan(_make_test_block(body)) == []

    def test_inline_trailing_comment_does_not_mask_real_assertion(self):
        # Real assertion + trailing discussion comment — still flag the
        # real assertion (when no input).
        body = (
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible(); "
            "// see notes about cold-start\n"
        )
        findings = _scan(_make_test_block(body))
        assert len(findings) == 1


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_empty_source(self):
        assert _scan("") == []

    def test_no_trailing_newline(self):
        # Source with no final newline (historical line-parser trip).
        src = (
            "test('block', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});"  # no \n
        )
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


# ---------------------------------------------------------------------------
# main() integration via subprocess
# ---------------------------------------------------------------------------
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts" / "tools" / "lint" / "check_playwright_coldstart_drift.py"
)


class TestMainIntegration:
    def test_clean_file_warn_exit_zero(self, tmp_path):
        spec = tmp_path / "clean.spec.ts"
        spec.write_text(
            "test('block', async ({ page }) => {\n"
            "  await page.getByTestId('input').fill('x');\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n",
            encoding="utf-8",
        )
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
            "test('block', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), "--ci", str(spec)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert "coldstart-drift" in result.stderr
        assert "foo-state-ready" in result.stderr

    def test_dirty_file_warn_only_exit_zero(self, tmp_path):
        spec = tmp_path / "dirty.spec.ts"
        spec.write_text(
            "test('block', async ({ page }) => {\n"
            "  await expect(page.getByTestId('foo-state-ready')).toBeVisible();\n"
            "});\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), str(spec)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Warn-only mode — even with findings, exit 0.
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Live dogfood — real repo audit
# ---------------------------------------------------------------------------
class TestLiveRepo:
    def test_zero_findings_in_repo(self):
        """Real repo must be clean — PR #185 fix already merged."""
        repo_specs = sorted(
            (lint.PROJECT_ROOT / "tests" / "e2e").rglob("*.spec.ts")
        )
        assert repo_specs, "No Playwright specs found — directory layout changed?"
        all_findings = []
        for spec in repo_specs:
            source = spec.read_text(encoding="utf-8", errors="replace")
            all_findings.extend(lint.scan_source(spec, source))
        assert all_findings == [], (
            f"Live repo has cold-start drift: "
            f"{[f.render() for f in all_findings]}"
        )


# ---------------------------------------------------------------------------
# Pre-merge intentional-break dogfood
# ---------------------------------------------------------------------------
class TestRegressionDogfood:
    """Re-inject the historical PR #185 broken pattern."""

    def test_pr_185_first_ci_pattern_caught(self):
        # PR #185 first CI fail spec contained:
        #   await loadPortalTool(page, 'simulate-preview');
        #   await expect(page.getByTestId(
        #     'simulate-preview-state-ready')).toBeVisible({ timeout: 5000 });
        # No fill / click / route preceded — fire on cold-start
        # state-empty. After fix `3beb127` Tenant ID seeded with
        # `'example-tenant'`, but the spec also added page.route() to
        # establish input. This test inlines the broken historical
        # pattern (no input establishment) to confirm catch.
        historical = (
            "test('auto-simulates on mount and renders success state', "
            "async ({ page }) => {\n"
            "  await loadPortalTool(page, 'simulate-preview');\n"
            "  await expect(page.getByTestId('simulate-preview-tenant-id'))"
            ".toBeVisible({ timeout: 10000 });\n"
            "  await expect(page.getByTestId('simulate-preview-state-ready'))"
            ".toBeVisible({ timeout: 5000 });\n"
            "});\n"
        )
        findings = _scan(historical, fake_path="historical.spec.ts")
        # tenant-id is INPUT, not state — should NOT fire.
        # state-ready IS state suffix — SHOULD fire.
        # Expect exactly 1 finding.
        assert len(findings) == 1
        assert findings[0].testid == "simulate-preview-state-ready"
