/**
 * Threshold Heatmap — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads threshold-heatmap without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: heatmap grid/table renders
 *   - Colorblind accessibility: Unicode severity symbols present (ADR-012 hotfix)
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Threshold Heatmap @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'threshold-heatmap');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Heatmap|Threshold|Overview|Matrix/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders threshold distribution table', async ({ page }) => {
    // Calibrated against threshold-heatmap.jsx — renders <table role="table"
    // aria-label="Threshold distribution table">. getByRole lets us avoid the
    // generic `table` matcher that would also grab unrelated tables.
    await loadPortalTool(page, 'threshold-heatmap');

    const grid = page.getByRole('table', { name: /threshold distribution/i });
    await expect(grid).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'threshold-heatmap');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('colorblind-safe severity symbols are rendered (ADR-012)', async ({ page }) => {
    // Calibrated against ADR-012 colorblind hotfix — the heatmap emits
    // `<span aria-hidden="true">✓</span>` (plus ⚠ / ❌) alongside the color
    // cell, so the calibrated probe saw 106 occurrences on a loaded heatmap.
    // We assert at least one ✓ (success) symbol is present, which is the most
    // common state when seed data renders. If the heatmap is empty (no seed
    // fixtures), fall back to asserting some severity symbol exists rather
    // than requiring ✓ specifically.
    await loadPortalTool(page, 'threshold-heatmap');

    const successSymbol = page.getByText('✓', { exact: true });
    const anySeverity = page.locator('span[aria-hidden="true"]').filter({ hasText: /[✓⚠❌]/ });

    const successCount = await successSymbol.count();
    if (successCount > 0) {
      await expect(successSymbol.first()).toBeVisible({ timeout: 5000 });
    } else {
      // Heatmap loaded but without a ✓ — verify some other severity symbol.
      expect(await anySeverity.count()).toBeGreaterThan(0);
    }
  });
});
