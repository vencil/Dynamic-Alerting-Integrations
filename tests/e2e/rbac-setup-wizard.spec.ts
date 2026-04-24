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

  test('renders step navigation including permission step', async ({ page }) => {
    // Calibrated against rbac-setup-wizard.jsx — the wizard is step-based
    // (Define Groups → Assign Tenants → Set Permissions → Environment/Domain
    // Filters → Review & Export). The original fixme assumed role selection
    // on step one, but the real flow defines *groups* first and sets
    // permissions on step three. This test verifies the step nav is present
    // by looking for the "Set Permissions" step button, which is unique to
    // this wizard.
    await loadPortalTool(page, 'rbac-setup-wizard');

    const permStep = page.getByText('Set Permissions', { exact: true });
    await expect(permStep).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'rbac-setup-wizard');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('wizard navigation controls (Next + Reset) are present', async ({ page }) => {
    // Calibrated against rbac-setup-wizard.jsx — per-step nav renders
    // Next → / ← Back / 🔄 Reset buttons. We assert Next + Reset (always
    // visible); Back is intentionally not asserted because its visibility /
    // enabled state varies across steps (hidden / disabled on step 1). The
    // original "toggle/checkbox" hypothesis applied only to the permission
    // step, which requires navigation to reach — reframed to the always-on
    // wizard-chrome buttons.
    await loadPortalTool(page, 'rbac-setup-wizard');

    const nextBtn = page.getByRole('button', { name: /Next/i });
    const resetBtn = page.getByRole('button', { name: /Reset/i });
    await expect(nextBtn).toBeVisible({ timeout: 10000 });
    await expect(resetBtn).toBeVisible({ timeout: 10000 });
  });
});
