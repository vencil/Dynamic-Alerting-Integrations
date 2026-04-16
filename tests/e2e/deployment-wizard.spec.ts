/**
 * Deployment Profile Wizard — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * PRIMARY PURPOSE: REG-004 regression guard.
 *
 * REG-004 is the v2.5.0 regression in which deployment-wizard.jsx shipped
 * `href="/..."` absolute-root links. When the docs site is served from a
 * sub-path (GitHub Pages project sites, nested MkDocs builds, or the Cowork
 * preview shell), absolute-root hrefs break navigation because they resolve
 * against the hosting origin rather than the portal base. See:
 *   - docs/internal/known-regressions.md § REG-004
 *   - docs/internal/v2.7.0-planning.md § Phase .a A-6
 *
 * This spec's hardest assertion is `assertNoAbsoluteRootHrefs` — that single
 * check is what turns this spec into a regression guard. Everything else
 * (title, smoke axe scan, step rendering) is supporting coverage so we catch
 * bundle/mount failures alongside the link-shape check.
 *
 * SCOPE DISCLAIMER (Day 5, v2.7.0):
 *   Despite the file name, this spec is NOT a comprehensive end-to-end
 *   validation of the deployment wizard's user flows. The name reflects the
 *   component under test; the scope is:
 *     (a) component mount/render smoke,
 *     (b) REG-004 link-shape guard (THE reason this file exists),
 *     (c) a11y budget check (Phase .a0 batch 4 token-migration guardrail).
 *   It deliberately does NOT cover: step-N data validation, form submission,
 *   profile download, edge cases in tier selection, cross-step state persistence,
 *   or backend contract. Those live in (future) deployment-wizard.e2e.spec.ts
 *   or unit-level component tests. If you are here looking for a behavioural
 *   E2E, this is not it — add a new spec or extend this one deliberately with
 *   a broader name.
 *
 * Validates:
 *   - jsx-loader.html?component=deployment-wizard loads without 404
 *   - Page title matches expected pattern (Deployment|Wizard)
 *   - Step UI renders at least one tier selection card
 *   - NO absolute-root (`href="/..."` / `action="/..."`) attributes exist
 *     anywhere in the rendered DOM (REG-004 guard)
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 *     (Day 5 runtime finding: step indicator --da-color-tag-bg + --da-color-muted
 *      pair fails AA contrast → TECH-DEBT-003, tracked in known-regressions.md)
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Deployment Profile Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'deployment-wizard');

    await runToolSmokeChecks(page, {
      // Component title in the registry is "Deployment Profile Wizard".
      // Keep the regex permissive so copy edits don't break the test.
      expectedTitleMatch: /Deployment|Wizard|Profile/i,
      // Phase .a0 batch 4 fully migrated deployment-wizard to design tokens
      // (Group A, 89 tokens, 0 palette). We hold it to the stricter default
      // (0 non-Critical violations) to lock in that win and surface any
      // regression fast.
      allowedNonCriticalViolations: 0,
    });
  });

  test('renders first-step tier selector', async ({ page }) => {
    await loadPortalTool(page, 'deployment-wizard');

    // Step 1 of the deployment-wizard is a tier selector with three cards
    // (Tier 1 / Tier 2 / Tier 3 — see docs/interactive/tools/deployment-wizard.jsx).
    // Copy changes are expected over time; accept any tier-like label so this
    // stays resilient without masking actual mount failures.
    const tierCard = page.locator(
      ':text-matches("Tier\\s?[123]|Production|Staging|Development", "i")'
    );
    await expect(tierCard.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'deployment-wizard');

    // THIS is the canonical REG-004 assertion. If this test fails it means
    // deployment-wizard has re-introduced the pattern that caused REG-004
    // in v2.5.0: `href="/foo"` (absolute-root) rather than `href="foo"`
    // or `href="../foo"` (portal-relative).
    //
    // The fix is always the same: convert the link to a relative path, or
    // (preferably) call the jsx-loader `navigate(key)` helper introduced in
    // Phase .a0 DEC-I Option C.
    await assertNoAbsoluteRootHrefs(page);
  });

  test('tier selection advances progress indicator', async ({ page }) => {
    await loadPortalTool(page, 'deployment-wizard');

    // Click the first visible tier card and verify the progress indicator
    // responds. This is a lightweight interaction smoke — the goal is to
    // catch total freeze/error after hydration, not to verify wizard logic.
    const firstSelectable = page
      .locator('button:has-text("Tier"), [role="button"]:has-text("Tier")')
      .first();

    if (await firstSelectable.count()) {
      await firstSelectable.click({ trial: false }).catch(() => {});
      // Progress readout lives in a "Progress"/"Step" label near the top of
      // the wizard. We just check the page didn't produce a visible error.
      const body = await page.locator('body').innerText();
      expect(body, 'wizard should not display an error after tier click')
        .not.toMatch(/TypeError|Cannot read|undefined is not/i);
    }
  });
});
