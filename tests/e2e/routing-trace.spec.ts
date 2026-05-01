/**
 * Routing Trace Wizard — E2E smoke test (S#92 / C-3 PR-3)
 *
 * Validates:
 *   - jsx-loader loads routing-trace without 404 / JS error
 *   - Page title matches expected pattern
 *   - 4 step indicators visible (alert / default / children / trace)
 *   - Default seeded state (HighCPUUsage + critical + team=platform)
 *     reaches step 3 trace and matches child route #1
 *   - Trace result renders matched receiver (team-platform default
 *     seeded child route)
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Routing Trace Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Routing|Trace|路由/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders 4 step indicators', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');

    await expect(
      page.getByTestId('routing-trace-step-alert')
    ).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('routing-trace-step-default')).toBeVisible();
    await expect(page.getByTestId('routing-trace-step-children')).toBeVisible();
    await expect(page.getByTestId('routing-trace-step-trace')).toBeVisible();
  });

  test('Step 0 alertname seeded as HighCPUUsage; Next enabled', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');

    const name = page.getByTestId('routing-trace-alertname');
    await expect(name).toHaveValue('HighCPUUsage', { timeout: 10000 });
    await expect(page.getByTestId('routing-trace-next')).toBeEnabled();
  });

  test('Default seed walks all 4 steps and matches first child route', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');

    // Step 0 (alert) — accept seed and advance
    await expect(
      page.getByTestId('routing-trace-step-alert')
    ).toBeVisible({ timeout: 10000 });
    await page.getByTestId('routing-trace-next').click();

    // Step 1 (default route) — seed receiver = default-pager
    await expect(
      page.getByTestId('routing-trace-default-receiver')
    ).toBeVisible({ timeout: 5000 });
    await page.getByTestId('routing-trace-next').click();

    // Step 2 (child routes) — 2 seeded routes (#1 severity=critical →
    // team-platform; #2 team=database → team-database). Default alert
    // is severity=critical so #1 matches.
    await expect(
      page.getByTestId('routing-trace-child-route-0')
    ).toBeVisible({ timeout: 5000 });
    await page.getByTestId('routing-trace-next').click();

    // Step 3 (trace result) — final receiver = team-platform
    const receiver = page.getByTestId('routing-trace-receiver');
    await expect(receiver).toBeVisible({ timeout: 5000 });
    await expect(receiver).toHaveText('team-platform');
  });

  test('Add Child Route button creates a new route block', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');

    // Walk to step 2
    await page.getByTestId('routing-trace-next').click();
    await page.getByTestId('routing-trace-next').click();

    // 2 child routes seeded by default
    await expect(page.getByTestId('routing-trace-child-route-0')).toBeVisible({
      timeout: 5000,
    });
    await expect(page.getByTestId('routing-trace-child-route-1')).toBeVisible();

    // Add a 3rd via the Add Child Route button
    await page.getByTestId('routing-trace-add-child-route').click();
    await expect(page.getByTestId('routing-trace-child-route-2')).toBeVisible();
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'routing-trace');
    await assertNoAbsoluteRootHrefs(page);
  });
});
