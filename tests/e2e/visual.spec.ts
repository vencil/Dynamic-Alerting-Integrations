/**
 * Visual regression — TRK-229 (#243)
 *
 * Playwright `toHaveScreenshot()` baselines for portal pages, captured
 * to `tests/e2e/__snapshots__/` and compared on every CI run.
 *
 * Scope (first batch):
 *   1. Tenant Manager landing — populated state with 2 mock tenants
 *   2. Saved Views panel populated
 *   3. Alert Builder step 1 (entry form)
 *
 * Tolerance / determinism:
 *   - `maxDiffPixelRatio: 0.02` absorbs font hinting / sub-pixel drift
 *   - `animations: 'disabled'` + media query reduce-motion eliminate timing
 *   - Fixed viewport 1280×720 — set on each spec so changing the global
 *     default doesn't quietly break baselines
 *
 * Baseline-generation contract (per project memory):
 *   - Baselines MUST be generated on the same Ubuntu image CI uses.
 *     Don't run `--update-snapshots` on a Windows host — font rendering
 *     differs and you'll commit baselines that fail in CI.
 *   - To refresh a baseline:
 *       gh workflow run visual-baseline.yaml \
 *           --ref <branch> \
 *           -f reason="<why>" \
 *           -f spec="visual.spec.ts"
 *     Then download the resulting `visual-baselines-<run_id>` artifact
 *     and unzip into `tests/e2e/` (preserves the `<spec>-snapshots/`
 *     subdir layout Playwright writes by default).
 *   - Or run on a Linux dev container with:
 *       npx playwright test visual.spec.ts --update-snapshots
 *   - Commit the resulting PNGs in `tests/e2e/visual.spec.ts-snapshots/`.
 */
import { test, expect, Page } from '@playwright/test';

const VIEWPORT = { width: 1280, height: 720 };

const TWO_TENANT_FIXTURE = {
  items: [
    { id: 'prod-mariadb-01', environment: 'production', tier: 'tier-1', domain: 'finance', db_type: 'mariadb', owner: 'team-a', tags: [], groups: [] },
    { id: 'staging-pg-01',   environment: 'staging',    tier: 'tier-2', domain: 'analytics', db_type: 'postgresql', owner: 'team-b', tags: [], groups: [] },
  ],
  total_matched: 2,
  page_size: 500,
  next_offset: null,
};

const SAVED_VIEWS_FIXTURE = {
  views: {
    'prod-finance':    { label: 'Production Finance',    created_by: 'admin@example.com',
                         filters: { environment: 'production', domain: 'finance' } },
    'critical-silent': { label: 'Critical + Silent',     created_by: 'sre@example.com',
                         filters: { tier: 'tier-1', operational_mode: 'silent' } },
  },
};

async function mockMe(page: Page) {
  // LD-6 P7 (#962): an authed /me now triggers a first-visit callout on
  // the tenant-manager surface. Pre-seed its dismissal flag so the frozen
  // PNG baselines stay callout-free (the callout has its own functional
  // coverage in auth-flow.spec.ts).
  await page.addInitScript(() => {
    try { localStorage.setItem('da_tm_scope_callout_v1', '1'); } catch { /* ignore */ }
  });
  await page.route('**/api/v1/me', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        email: 'tester@example.com',
        groups: [],
        accessible_tenants: [],
        permissions: { global: ['read', 'write'] },
      }),
    }),
  );
}

async function mockTenants(page: Page) {
  await page.route('**/api/v1/tenants/search**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(TWO_TENANT_FIXTURE),
    }),
  );
}

test.describe('Visual regression baselines @visual', () => {
  test.use({
    viewport: VIEWPORT,
  });

  test('tenant-manager: landing populated', async ({ page }) => {
    await mockMe(page);
    await mockTenants(page);
    // No views endpoint mocked → panel hides; baseline captures the
    // canonical "tenant cards visible, no views panel" landing.
    await page.route('**/api/v1/views', (r) => r.fulfill({ status: 404, body: 'not found' }));

    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    // Wait for tenant cards to render before snapshotting.
    await page.locator('text=prod-mariadb-01').waitFor({ timeout: 10000 });

    await expect(page).toHaveScreenshot('tenant-manager-landing.png', {
      fullPage: true,
      maxDiffPixelRatio: 0.02,
      animations: 'disabled',
    });
  });

  test('tenant-manager: saved views panel populated', async ({ page }) => {
    await mockMe(page);
    await mockTenants(page);
    await page.route('**/api/v1/views', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(SAVED_VIEWS_FIXTURE),
      }),
    );

    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.locator('[data-testid="saved-views-panel"]').waitFor({ timeout: 10000 });

    await expect(page.locator('[data-testid="saved-views-panel"]')).toHaveScreenshot(
      'saved-views-panel-populated.png',
      {
        maxDiffPixelRatio: 0.02,
        animations: 'disabled',
      },
    );
  });

  test('alert-builder: step 1 entry form', async ({ page }) => {
    // Alert builder is purely client-side — no /api/v1 deps. Just load.
    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/alert-builder.jsx');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    // Wait for any form input to confirm the page mounted.
    await page.locator('input, select').first().waitFor({ timeout: 10000 });

    await expect(page).toHaveScreenshot('alert-builder-step1.png', {
      fullPage: true,
      maxDiffPixelRatio: 0.02,
      animations: 'disabled',
    });
  });

  /*
   * ───────────────────────────────────────────────────────────────────
   * TRK-238 expansion (Plan A) — 5 staged baselines across categories.
   *
   * Status: PARKED as block comment.
   *
   * Why: A-13 ESLint rule (eslint.config.mjs) forbids `test.fixme()`.
   * We can't ship "stub specs that need baselines later" because:
   *   1. Adding active toHaveScreenshot tests without baselines fails CI
   *      (Playwright treats missing baselines as failures, not auto-creates
   *      them, when --update-snapshots is not passed)
   *   2. fixme guard prevents the usual "skip until baseline lands" pattern
   *
   * Activation flow when ready:
   *   1. Un-comment a block here (delete the surrounding `/* ... *\/`),
   *      commit on a new branch, push.
   *   2. Run the workflow against that branch:
   *        gh workflow run visual-baseline.yaml --ref <branch> \
   *            -f reason="<why>" -f spec="visual.spec.ts"
   *      It uploads the regenerated `tests/e2e/*-snapshots/` dir as
   *      `visual-baselines-<run_id>` artifact.
   *   3. Download + unzip into `tests/e2e/` on the same branch.
   *      `git add tests/e2e/visual.spec.ts-snapshots/<new>.png`, commit,
   *      push, open PR.
   *   4. CI on the PR runs the un-commented test against the new baseline;
   *      should be green.
   *
   * Selection rationale: one tool per visual category, maximize catch-rate
   * per baseline. Skipping rich-data tools that change frequently
   * (release-notes-generator, schema-explorer) — they'd produce noise PRs.
   *
   * ─── Onboarding ─────────────────────────────────────────────────────
   * test('master-onboarding: dual-entry choice screen', async ({ page }) => {
   *   await page.goto('../assets/jsx-loader.html?component=master-onboarding');
   *   await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
   *   await page.locator('text=/Onboarding|入門/i').first().waitFor({ timeout: 10000 });
   *   await expect(page).toHaveScreenshot('master-onboarding-landing.png', {
   *     fullPage: true, maxDiffPixelRatio: 0.02, animations: 'disabled',
   *   });
   * });
   *
   * ─── Reference ──────────────────────────────────────────────────────
   * test('glossary: categorized list', async ({ page }) => {
   *   await page.goto('../assets/jsx-loader.html?component=glossary');
   *   await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
   *   await page.locator('text=/Rule Pack|Tenant/').first().waitFor({ timeout: 10000 });
   *   await expect(page).toHaveScreenshot('glossary-list.png', {
   *     fullPage: true, maxDiffPixelRatio: 0.02, animations: 'disabled',
   *   });
   * });
   *
   * ─── Calculator ─────────────────────────────────────────────────────
   * test('threshold-calculator: percentile sliders', async ({ page }) => {
   *   await page.goto('../assets/jsx-loader.html?component=threshold-calculator');
   *   await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
   *   await page.locator('text=/Threshold|閾值/i').first().waitFor({ timeout: 10000 });
   *   await expect(page).toHaveScreenshot('threshold-calculator-sliders.png', {
   *     fullPage: true, maxDiffPixelRatio: 0.02, animations: 'disabled',
   *   });
   * });
   *
   * ─── Wizard ─────────────────────────────────────────────────────────
   * test('routing-trace: wizard step 1', async ({ page }) => {
   *   await page.goto('../assets/jsx-loader.html?component=routing-trace');
   *   await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
   *   await page.locator('text=/Routing|路由/i').first().waitFor({ timeout: 10000 });
   *   await expect(page).toHaveScreenshot('routing-trace-step1.png', {
   *     fullPage: true, maxDiffPixelRatio: 0.02, animations: 'disabled',
   *   });
   * });
   *
   * ─── Educational ────────────────────────────────────────────────────
   * test('architecture-quiz: question screen', async ({ page }) => {
   *   await page.goto('../assets/jsx-loader.html?component=architecture-quiz');
   *   await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
   *   await page.locator('text=/Architecture|架構/i').first().waitFor({ timeout: 10000 });
   *   await expect(page).toHaveScreenshot('architecture-quiz-q1.png', {
   *     fullPage: true, maxDiffPixelRatio: 0.02, animations: 'disabled',
   *   });
   * });
   * ───────────────────────────────────────────────────────────────────
   */
});
