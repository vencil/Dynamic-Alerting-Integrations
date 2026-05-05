/**
 * Visual regression — TECH-DEBT-029 (#243)
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
 *   - To refresh a baseline: trigger the workflow_dispatch run of
 *     `nightly-race.yaml`-style updater (TODO: separate workflow), or
 *     run on a Linux dev container with: `npx playwright test visual.spec.ts --update-snapshots`
 *   - Commit the resulting PNGs in `tests/e2e/__snapshots__/visual.spec.ts/`
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
});
