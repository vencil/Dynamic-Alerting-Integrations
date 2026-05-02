/**
 * Saved Views (Smart Views) — E2E (S#100 / C-6)
 *
 * Validates the frontend integration of the v2.5.0-shipped backend
 * `/api/v1/views` CRUD into the Tenant Manager UI:
 *
 *   1. Empty state — when GET /api/v1/views returns no views, panel
 *      shows the "No saved views yet" hint and no select dropdown.
 *   2. List + apply — given mocked views, the select dropdown lists
 *      them and picking one pushes filters into orchestrator state.
 *   3. Save current — opens modal, validates id charset, sends a
 *      well-shaped PUT body, refreshes list on success.
 *   4. Delete — confirm modal blocks accidental deletion; confirm
 *      sends DELETE and refreshes list.
 *   5. RBAC hide — when canWrite=false (no /api/v1/me write perm),
 *      Save / Delete controls are hidden but list + apply still work.
 *   6. Unreachable backend (404) — entire panel hides itself; demo
 *      mode UX is clean.
 *
 * Selector discipline (S#94 / S#92): every interactive element pinned
 * via `data-testid`. State assertions use S#98 `toBeVisibleWithDiagnostics`
 * matcher so a regression to a different state produces a self-explanatory
 * CI error listing every visible testid.
 *
 * Spec follows §LL §11 cold-start contract: each test() block establishes
 * its input via `page.route()` mocks BEFORE asserting state.
 */
import { test, expect, Page } from '@playwright/test';
// S#98: side-effect import registers `toBeVisibleWithDiagnostics` matcher.
import './fixtures/diagnostic-matchers';

const VIEWS_FIXTURE = {
  views: {
    'prod-finance': {
      label: 'Production Finance',
      description: 'All production tenants in finance domain',
      created_by: 'admin@example.com',
      filters: {
        environment: 'production',
        domain: 'finance',
      },
    },
    'critical-silent': {
      label: 'Critical + Silent',
      created_by: 'sre@example.com',
      filters: {
        tier: 'tier-1',
        operational_mode: 'silent',
      },
    },
  },
};

/**
 * Standard mocks: backend reachable, mock /api/v1/me as write-permitted,
 * mock /api/v1/tenants/search with a couple of tenants so the page
 * renders past the loading state. Each test composes additional
 * route handlers on top.
 */
async function mountTenantManagerWithBackend(page: Page) {
  await page.route('**/api/v1/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        email: 'tester@example.com',
        permissions: { 'global': ['read', 'write'] },
      }),
    });
  });
  await page.route('**/api/v1/tenants/search**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          { id: 'prod-mariadb-01', environment: 'production', tier: 'tier-1', domain: 'finance', db_type: 'mariadb', owner: 'team-a', tags: [], groups: [] },
          { id: 'staging-pg-01', environment: 'staging', tier: 'tier-2', domain: 'analytics', db_type: 'postgresql', owner: 'team-b', tags: [], groups: [] },
        ],
        total_matched: 2,
        page_size: 500,
        next_offset: null,
      }),
    });
  });
}

async function loadTenantManager(page: Page) {
  await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(1500);
}

test.describe('Saved Views (Smart Views) @critical', () => {
  test('panel hides when /api/v1/views is unreachable (404)', async ({ page }) => {
    await mountTenantManagerWithBackend(page);
    // Backend has no views endpoint deployed — UI must not show the panel.
    await page.route('**/api/v1/views', async (route) => {
      await route.fulfill({ status: 404, body: 'not found' });
    });

    await loadTenantManager(page);

    // Panel should NOT be visible — this is the "demo mode" contract.
    await expect(page.getByTestId('saved-views-panel')).toHaveCount(0);
  });

  test('shows empty-state hint when backend has no saved views', async ({ page }) => {
    await mountTenantManagerWithBackend(page);
    await page.route('**/api/v1/views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ views: {} }),
      });
    });

    await loadTenantManager(page);

    await expect(
      page.getByTestId('saved-views-panel')
    ).toBeVisibleWithDiagnostics({ timeout: 10000 });
    await expect(page.getByTestId('saved-views-empty')).toBeVisible();
    // No select dropdown when empty.
    await expect(page.getByTestId('saved-views-select')).toHaveCount(0);
  });

  test('lists saved views and applies filters when one is selected', async ({ page }) => {
    await mountTenantManagerWithBackend(page);
    await page.route('**/api/v1/views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(VIEWS_FIXTURE),
      });
    });

    await loadTenantManager(page);

    const selector = page.getByTestId('saved-views-select');
    await expect(selector).toBeVisibleWithDiagnostics({ timeout: 10000 });

    // The select must contain both fixture views as options. Use
    // attribute query rather than text match — labels are translated
    // and the data-test contract is the option `value` (= view id).
    await expect(
      selector.locator('option[value="prod-finance"]')
    ).toHaveCount(1);
    await expect(
      selector.locator('option[value="critical-silent"]')
    ).toHaveCount(1);

    // Apply the prod-finance view → orchestrator filters should
    // update. The Environment <select> is the easiest observable.
    await selector.selectOption('prod-finance');
    await expect(page.locator('#filter-env')).toHaveValue('production');
    await expect(page.locator('#filter-domain')).toHaveValue('finance');
  });

  test('save flow: opens modal, sends PUT with correct body, refreshes list', async ({ page }) => {
    await mountTenantManagerWithBackend(page);

    // Backend starts empty; after save we mock the next reload to
    // include the new view (so "refresh list on success" is testable).
    let putBody: any = null;
    let listCallCount = 0;
    await page.route('**/api/v1/views', async (route) => {
      listCallCount += 1;
      const body = listCallCount === 1
        ? { views: {} }
        : { views: { 'my-new-view': {
            label: 'My New View',
            filters: { environment: 'staging' },
          } } };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(body),
      });
    });
    await page.route('**/api/v1/views/my-new-view', async (route) => {
      try {
        putBody = JSON.parse(route.request().postData() || '{}');
      } catch (_) {
        putBody = null;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true }),
      });
    });

    await loadTenantManager(page);

    // Set a filter so "Save current" has something to save.
    await page.locator('#filter-env').selectOption('staging');

    // Open save modal.
    await page.getByTestId('saved-views-save-btn').click();
    await expect(
      page.getByTestId('saved-views-save-modal')
    ).toBeVisibleWithDiagnostics({ timeout: 5000 });

    // Fill in id + label, then confirm.
    await page.getByTestId('saved-views-new-id').fill('my-new-view');
    await page.getByTestId('saved-views-new-label').fill('My New View');
    await page.getByTestId('saved-views-save-confirm').click();

    // Modal should close once save resolves.
    await expect(page.getByTestId('saved-views-save-modal')).toHaveCount(0, {
      timeout: 5000,
    });

    // PUT body must contain shape backend expects: { label, filters: {...} }
    expect(putBody).not.toBeNull();
    expect(putBody.label).toBe('My New View');
    expect(putBody.filters).toEqual({ environment: 'staging' });
    // Reload was called: initial load + post-save refresh = 2.
    expect(listCallCount).toBeGreaterThanOrEqual(2);
  });

  test('delete flow: confirm modal blocks accidental delete, confirm sends DELETE', async ({
    page,
  }) => {
    await mountTenantManagerWithBackend(page);
    let deleteCalled = false;
    await page.route('**/api/v1/views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(VIEWS_FIXTURE),
      });
    });
    await page.route('**/api/v1/views/prod-finance', async (route) => {
      if (route.request().method() === 'DELETE') {
        deleteCalled = true;
        await route.fulfill({ status: 204, body: '' });
        return;
      }
      await route.fallback();
    });

    await loadTenantManager(page);

    // Open delete dropdown, pick the prod-finance view.
    const delSelect = page.getByTestId('saved-views-delete-select');
    await expect(delSelect).toBeVisibleWithDiagnostics({ timeout: 10000 });
    await delSelect.selectOption('prod-finance');

    // Confirm modal should appear; cancel first to verify it blocks.
    await expect(
      page.getByTestId('saved-views-delete-modal')
    ).toBeVisibleWithDiagnostics({ timeout: 5000 });
    await page.getByTestId('saved-views-delete-cancel').click();
    await expect(page.getByTestId('saved-views-delete-modal')).toHaveCount(0);
    expect(deleteCalled).toBe(false);

    // Now actually confirm.
    await delSelect.selectOption('prod-finance');
    await expect(page.getByTestId('saved-views-delete-modal')).toBeVisible();
    await page.getByTestId('saved-views-delete-confirm').click();
    await expect(page.getByTestId('saved-views-delete-modal')).toHaveCount(0);
    expect(deleteCalled).toBe(true);
  });

  test('RBAC: hide Save / Delete controls when /api/v1/me has no write perm', async ({
    page,
  }) => {
    // Override the standard /api/v1/me mock to remove write permissions.
    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          email: 'readonly@example.com',
          permissions: { 'global': ['read'] },
        }),
      });
    });
    await page.route('**/api/v1/tenants/search**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{ id: 'prod-mariadb-01', environment: 'production', tier: 'tier-1', domain: 'finance', db_type: 'mariadb', owner: 'team-a', tags: [], groups: [] }],
          total_matched: 1, page_size: 500, next_offset: null,
        }),
      });
    });
    await page.route('**/api/v1/views', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(VIEWS_FIXTURE),
      });
    });

    await loadTenantManager(page);

    // Panel still renders; list / apply still work.
    await expect(
      page.getByTestId('saved-views-panel')
    ).toBeVisibleWithDiagnostics({ timeout: 10000 });
    await expect(page.getByTestId('saved-views-select')).toBeVisible();

    // Save / Delete are RBAC-hidden.
    await expect(page.getByTestId('saved-views-save-btn')).toHaveCount(0);
    await expect(page.getByTestId('saved-views-delete-select')).toHaveCount(0);
  });
});
