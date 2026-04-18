/**
 * Cost Estimator — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads cost-estimator without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: tenant count or resource input, cost output area
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Cost Estimator @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'cost-estimator');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Cost|Estimat|Pric|Resource/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders input controls for estimation parameters', async ({ page }) => {
    await loadPortalTool(page, 'cost-estimator');

    // v2.7.0 calibration (§8.11.4): cost-estimator.jsx:462-470 renders
    // multiple <input type="range" role="slider" aria-label=...> controls
    // for tenant count, packs-per-tenant, scrape interval, retention, replicas.
    const slider = page.getByRole('slider').first();
    await expect(slider).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'cost-estimator');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('Recommendation region is present', async ({ page }) => {
    await loadPortalTool(page, 'cost-estimator');

    // v2.7.0 calibration (§8.11.4): cost-estimator.jsx:778 emits
    // <div role="region" aria-live="polite" aria-label="Recommendation">
    // which holds the computed monthly-cost summary. The region is
    // always rendered (reactive), so it is visible on initial load.
    const region = page.getByRole('region', { name: /Recommendation/i });
    await expect(region).toBeVisible({ timeout: 10000 });
  });
});
