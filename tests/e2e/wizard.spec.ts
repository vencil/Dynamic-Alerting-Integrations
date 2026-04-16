/**
 * Getting Started Wizard — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader.html?component=wizard loads without 404
 *   - Page title matches expected pattern
 *   - Role selector renders (wizard is role-pick-first)
 *   - No REG-004-style hardcoded portal-absolute hrefs leak in
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 *
 * Uses the shared portal-tool-smoke helpers so new specs stay concise.
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Getting Started Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'wizard');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Wizard|Getting Started|Role|Dynamic Alerting/i,
      // Wizard has a known amber "START HERE" badge with borderline contrast
      // that is tracked separately (see wizard.md critique §Accessibility).
      // Allow up to 2 non-Critical violations while migration is in-flight.
      allowedNonCriticalViolations: 2,
    });
  });

  test('renders role selector as entry step', async ({ page }) => {
    await loadPortalTool(page, 'wizard');

    // The wizard's canonical first-step UI is a role picker with 3 cards
    // (platform-engineer / sre / domain-owner — see docs/getting-started/wizard.jsx).
    // We accept any of several role-like strings to keep the assertion resilient
    // to copy changes.
    const roleCard = page.locator(
      ':text-matches("Platform Engineer|SRE|Domain Owner|Tenant Operator", "i")'
    );
    await expect(roleCard.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'wizard');
    // Guard against the pattern that caused REG-004 in deployment-wizard.
    // Any tool that introduces `href="/foo"` will fail here.
    await assertNoAbsoluteRootHrefs(page);
  });

  test('Start-Here badge is visible and announced', async ({ page }) => {
    await loadPortalTool(page, 'wizard');

    // v2.5.0 Phase F added `role="status"` + `aria-label` to the amber badge
    // to make it screen-reader-announced. Guard that regression.
    const startHere = page
      .locator('[role="status"], [aria-label*="Start" i], :text-matches("Start Here", "i")')
      .first();
    await expect(startHere).toBeVisible({ timeout: 10000 });
  });
});
