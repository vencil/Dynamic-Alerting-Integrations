/**
 * Getting Started Wizard — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * SCOPE DISCLAIMER (Day 5 retrospective review, 2026-04-16):
 *   This is SMOKE coverage, not comprehensive E2E. It proves:
 *     - the tool loads without 404 / JS error
 *     - the first visible contract (role cards, Start-Here badge) renders
 *     - REG-004-style absolute hrefs don't leak
 *     - 0 Critical axe violations (with a budget of 2 non-Critical for the
 *       known amber Start-Here badge borderline contrast — see wizard.md §A11y)
 *   It does NOT cover:
 *     - full multi-step traversal (role → scenario → config generation)
 *     - the 19 state-specific color waivers from ADR-017 (DEC-A)
 *     - token-layer contrast issues on wizard (Day 5 retrospective runtime
 *       axe showed wizard itself has 0 violations — ADR-017 Option A
 *       validated in this spec's axe coverage)
 *   For broader Phase .a0 a11y picture across all migrated tools, see
 *   `_axe-audit-day1to3.spec.ts` and `_axe-audit-day4.spec.ts`.
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

    // v2.7.0 calibration (§8.11.4): wizard.jsx:80-94 defines 3 roles:
    //   Platform Engineer / Domain Expert (DBA) / Tenant Team
    // (Historical names "SRE / Domain Owner / Tenant Operator" were renamed
    // in v2.6.x — regex updated accordingly.)
    const roleCard = page.locator(
      ':text-matches("Platform Engineer|Domain Expert|Tenant Team", "i")'
    );
    await expect(roleCard.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'wizard');
    // Guard against the pattern that caused REG-004 in deployment-wizard.
    // Any tool that introduces `href="/foo"` will fail here.
    await assertNoAbsoluteRootHrefs(page);
  });

  test('Start-Here badge is visible after role + goal selection', async ({ page }) => {
    await loadPortalTool(page, 'wizard');

    // v2.7.0 calibration (§8.11.4): wizard.jsx:364 renders the badge as plain
    // text inside `<span className="... bg-amber-200">START HERE</span>` —
    // no role="status" / aria-label is applied. The badge only becomes
    // visible at step 2 (after role + goal selection).
    //
    // Adding `role="status"` is out of scope for this regression guard — it
    // would be a behaviour change. We match on the canonical uppercase text
    // "START HERE" emitted by the JSX. A11y enhancement is tracked in the
    // frontend-quality backlog (wizard.md §A11y).

    // Step 1: click a role card (Platform Engineer is the canonical first role).
    await page.getByRole('button', { name: /Platform Engineer/i }).click();

    // Step 2: click the first goal ("Initial Setup" for Platform role).
    await page.getByRole('button', { name: /Initial Setup/i }).click();

    // Step 3: START HERE badge on the first recommended doc must be visible.
    const startHere = page.getByText('START HERE', { exact: true }).first();
    await expect(startHere).toBeVisible({ timeout: 10000 });
  });
});
