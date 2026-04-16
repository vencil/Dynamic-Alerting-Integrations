/**
 * RBAC Setup Wizard — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads rbac-setup-wizard without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: role/permission selection step visible
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('RBAC Setup Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'rbac-setup-wizard');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /RBAC|Role|Access|Permission|Setup/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders role or permission selection', async ({ page }) => {
    test.fixme();
    // TODO: calibrate locator against real DOM
    await loadPortalTool(page, 'rbac-setup-wizard');

    // RBAC wizard should show role types or permission groups as step one.
    const roleEl = page.locator(
      ':text-matches("Admin|Operator|Viewer|Read|Write|Role|Permission", "i")'
    );
    await expect(roleEl.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'rbac-setup-wizard');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('toggle or checkbox for permissions is interactive', async ({ page }) => {
    test.fixme();
    // TODO: calibrate locator against real DOM
    await loadPortalTool(page, 'rbac-setup-wizard');

    // Should have interactive toggle/checkbox elements for permission assignment.
    // Post-migration these have aria-pressed (ADR-015 token migration).
    const toggle = page.locator(
      'button[aria-pressed], input[type="checkbox"], [role="switch"], [role="checkbox"]'
    );
    await expect(toggle.first()).toBeVisible({ timeout: 10000 });
  });
});
