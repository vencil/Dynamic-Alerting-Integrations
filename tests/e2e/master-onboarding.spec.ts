/**
 * Master Onboarding — E2E smoke test (S#90 / C-3 PR-1 MVP)
 *
 * Validates:
 *   - jsx-loader loads master-onboarding without 404 / JS error
 *   - Page title matches expected pattern
 *   - Step 0 dual-entry choice: both Import + Wizard journey cards visible
 *   - Click Import card → 5-step Import path renders (parser / profile /
 *     batch-pr / guard CTAs)
 *   - Click Wizard card → 5-step Wizard path renders (cicd / deploy CTAs;
 *     alert-builder + routing-trace marked "Planned")
 *   - Back button returns to choice screen
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Master Onboarding @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Onboarding|Dual Entry|入門/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('step 0 shows both journey cards', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');

    const importCard = page.getByTestId('onboarding-card-import');
    const wizardCard = page.getByTestId('onboarding-card-wizard');
    await expect(importCard).toBeVisible({ timeout: 10000 });
    await expect(wizardCard).toBeVisible();
  });

  test('Import journey lists 5 steps with key CTAs', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');

    await page.getByTestId('onboarding-card-import').click();

    // 5 steps numbered 1/5..5/5
    await expect(page.getByText('1 / 5')).toBeVisible();
    await expect(page.getByText('5 / 5')).toBeVisible();

    // Key CTAs (one per step) — assert presence by visible link text
    await expect(page.getByText(/Migration Toolkit|安裝指南/)).toBeVisible();
    await expect(page.getByText(/CLI Reference/i)).toBeVisible();
    await expect(page.getByText(/ADR-019/)).toBeVisible();
  });

  test('Wizard journey marks alert-builder + routing-trace as Planned', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');

    await page.getByTestId('onboarding-card-wizard').click();

    // Two steps explicitly marked Planned (deferred to v2.8.x C-3 PR-2)
    const planned = page.getByText(/Planned|規劃中/);
    await expect(planned.first()).toBeVisible({ timeout: 10000 });
    await expect(await planned.count()).toBeGreaterThanOrEqual(2);
  });

  test('Back button returns to journey choice', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');

    await page.getByTestId('onboarding-card-import').click();
    await expect(page.getByTestId('onboarding-back')).toBeVisible();
    await page.getByTestId('onboarding-back').click();

    // Back at choice — both cards visible again
    await expect(page.getByTestId('onboarding-card-import')).toBeVisible();
    await expect(page.getByTestId('onboarding-card-wizard')).toBeVisible();
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'master-onboarding');
    await assertNoAbsoluteRootHrefs(page);
  });
});
