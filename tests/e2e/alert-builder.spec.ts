/**
 * Alert Builder Wizard — E2E smoke test (S#91 / C-3 PR-2)
 *
 * Validates:
 *   - jsx-loader loads alert-builder without 404 / JS error
 *   - Page title matches expected pattern
 *   - 4 step indicators visible (identity / expression / severity / review)
 *   - Step navigation: fill Step 0 (identity) → click Next → Step 1 visible
 *   - Step 0 Next button gated on valid input (disabled when blank)
 *   - Final review step renders YAML output containing the alert name
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Alert Builder Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Alert Builder|告警/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders 4 step indicators', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');

    await expect(
      page.getByTestId('alert-builder-step-identity')
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('alert-builder-step-expression')).toBeVisible();
    await expect(page.getByTestId('alert-builder-step-severity')).toBeVisible();
    await expect(page.getByTestId('alert-builder-step-review')).toBeVisible();
  });

  test('Next gate: disabled when step 0 is blank, enabled after valid input', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');

    const nextBtn = page.getByTestId('alert-builder-next');
    await expect(nextBtn).toBeVisible({ timeout: 10000 });
    await expect(nextBtn).toBeDisabled();

    // Fill required step 0 fields (alertName + summary; groupName has
    // a default). With those populated, Next should enable.
    await page.getByTestId('alert-builder-name').fill('HighCPUUsage');
    // Summary input — locate by placeholder since there's no testid.
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('CPU usage above 80%');
    await expect(nextBtn).toBeEnabled();
  });

  test('Step 0 → Step 1 navigation reveals expression form', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');

    await page.getByTestId('alert-builder-name').fill('HighCPUUsage');
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('CPU usage above 80%');
    await page.getByTestId('alert-builder-next').click();

    // Step 1 expression textarea now visible
    await expect(
      page.getByTestId('alert-builder-expr')
    ).toBeVisible({ timeout: 5000 });
  });

  test('Final review step renders YAML containing the alert name', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');

    // Walk all 4 steps end-to-end with valid input.
    await page.getByTestId('alert-builder-name').fill('HighCPUUsage');
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('CPU usage above 80%');
    await page.getByTestId('alert-builder-next').click();

    await page
      .getByTestId('alert-builder-expr')
      .fill('rate(node_cpu_seconds_total[5m])');
    await page.getByTestId('alert-builder-threshold').fill('0.8');
    await page.getByTestId('alert-builder-next').click();

    // Step 2 severity selection
    await page.getByTestId('alert-builder-severity-warning').click();
    await page.getByTestId('alert-builder-next').click();

    // Step 3 review — YAML pre block contains alert name
    const yaml = page.getByTestId('alert-builder-yaml');
    await expect(yaml).toBeVisible({ timeout: 5000 });
    await expect(yaml).toContainText('HighCPUUsage');
    await expect(yaml).toContainText('rate(node_cpu_seconds_total[5m])');
    await expect(yaml).toContainText('severity: warning');
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'alert-builder');
    await assertNoAbsoluteRootHrefs(page);
  });
});
