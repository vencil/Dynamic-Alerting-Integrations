/**
 * Tenant Manager × Wizard Deep-Link — E2E (S#94 / C-4 PR-1)
 *
 * Validates the deep-link bridge from tenant cards into the
 * alert-builder and routing-trace wizards:
 *
 *   1. Each tenant card surfaces a 🛠️ Alert + 🧭 Route footer link
 *      with stable `data-testid` and `?component=...&tenant_id=<id>`
 *      hrefs (kept in tenant-manager open via target="_blank").
 *   2. Navigating directly to `?component=alert-builder&tenant_id=<id>`
 *      pre-fills a `tenant=<id>` row in the labels editor (step 2).
 *   3. Navigating directly to `?component=routing-trace&tenant_id=<id>`
 *      pre-fills a `tenant=<id>` row in the alert-input labels editor
 *      (step 0).
 *
 * Wire pattern: tenant-manager.spec.ts API-mode tests use `page.route()`
 * to stub `/api/v1/tenants/search` and `loadTenantManagerDirect()` to
 * bypass the index page and load jsx-loader.html directly. We reuse the
 * same convention here so the deep-link tests do not depend on demo
 * fixtures matching specific tenant IDs.
 */
import { test, expect, Page } from '@playwright/test';

// Pin a single tenant ID across all tests so failures are easy to grep.
const TENANT_ID = 'pr94-deeplink-tenant';

async function loadTenantManagerWithMockedApi(page: Page) {
  await page.route('**/api/v1/tenants/search**', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        items: [
          {
            id: TENANT_ID,
            environment: 'production',
            tier: 'tier-1',
            domain: 'finance',
            db_type: 'mariadb',
            owner: 'team-deeplink',
            tags: [],
            groups: [],
          },
        ],
        total_matched: 1,
        page_size: 500,
        next_offset: null,
      }),
    });
  });
  // Same convention as tenant-manager.spec.ts loadTenantManagerDirect():
  // baseURL = .../interactive/, so ../assets/jsx-loader.html resolves
  // to /assets/jsx-loader.html, then the JSX path is resolved relative
  // to where jsx-loader.html lives.
  await page.goto(
    '../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx'
  );
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(2000);
}

test.describe('Tenant Manager × Wizard Deep-Link @critical', () => {
  test('tenant card surfaces Alert + Route deep-link footer with correct hrefs', async ({
    page,
  }) => {
    await loadTenantManagerWithMockedApi(page);

    // Both deep-link anchors should be present on the rendered card
    // for our mocked tenant. Pin via data-testid (PR #182 lesson: prefer
    // testid over text/role to dodge strict-mode collisions when other
    // emoji-laden links appear elsewhere on the page).
    const alertLink = page.getByTestId(`tenant-card-${TENANT_ID}-build-alert`);
    const traceLink = page.getByTestId(`tenant-card-${TENANT_ID}-trace-routing`);

    await expect(alertLink).toBeVisible({ timeout: 10000 });
    await expect(traceLink).toBeVisible();

    // href contract — the orchestrator query-string convention is
    // `?component=<toolKey>&tenant_id=<id>`. We assert exact-match on
    // both params so a future refactor that drops `tenant_id` (or
    // renames the key) trips this test rather than silently breaking
    // pre-fill in production.
    const alertHref = await alertLink.getAttribute('href');
    expect(alertHref).toContain('component=alert-builder');
    expect(alertHref).toContain(`tenant_id=${TENANT_ID}`);

    const traceHref = await traceLink.getAttribute('href');
    expect(traceHref).toContain('component=routing-trace');
    expect(traceHref).toContain(`tenant_id=${TENANT_ID}`);

    // target="_blank" so the tenant-manager stays open for context
    // — central UX intent of this PR.
    expect(await alertLink.getAttribute('target')).toBe('_blank');
    expect(await traceLink.getAttribute('target')).toBe('_blank');
  });

  test('alert-builder pre-fills tenant label from ?tenant_id= URL param', async ({
    page,
  }) => {
    await page.goto(
      `../assets/jsx-loader.html?component=alert-builder&tenant_id=${TENANT_ID}`
    );
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});

    // Walk steps 0 → 1 → 2 with valid input so the labels editor
    // (which lives on step 2 / severity) is visible. Mirrors the
    // alert-builder.spec.ts traversal.
    await page.getByTestId('alert-builder-name').fill('HighCPUUsage');
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('CPU usage above 80%');
    await page.getByTestId('alert-builder-next').click();

    // Step 1: expression — fill the textarea so Next enables.
    const exprInput = page.getByTestId('alert-builder-expr');
    await expect(exprInput).toBeVisible({ timeout: 5000 });
    await exprInput.fill('rate(cpu_usage[5m])');
    // Threshold field also gates Next on step 1.
    await page.getByTestId('alert-builder-threshold').fill('80');
    await page.getByTestId('alert-builder-next').click();

    // Step 2: severity step now visible. The labels editor renders
    // each (key, value) pair as two adjacent inputs. Both inputs
    // for the seeded `tenant=<id>` row must be present.
    await expect(page.getByDisplayValue('tenant')).toBeVisible({ timeout: 5000 });
    await expect(page.getByDisplayValue(TENANT_ID)).toBeVisible();
  });

  test('routing-trace pre-fills tenant label from ?tenant_id= URL param', async ({
    page,
  }) => {
    await page.goto(
      `../assets/jsx-loader.html?component=routing-trace&tenant_id=${TENANT_ID}`
    );
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});

    // routing-trace step 0 (Alert) is the default view — labels editor
    // is visible without any navigation. Both inputs of the seeded
    // `tenant=<id>` row should be on screen immediately.
    await expect(page.getByDisplayValue('tenant')).toBeVisible({ timeout: 10000 });
    await expect(page.getByDisplayValue(TENANT_ID)).toBeVisible();
  });

  test('alert-builder without ?tenant_id= does NOT inject a tenant label', async ({
    page,
  }) => {
    // Negative case: graceful no-op when query param absent. Otherwise a
    // future regression that hard-codes `tenant: ''` (or worse, an
    // empty-string tenant value) would silently pollute every alert.
    await page.goto('../assets/jsx-loader.html?component=alert-builder');
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});

    // Walk to step 2 (severity).
    await page.getByTestId('alert-builder-name').fill('HighCPUUsage');
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('CPU usage above 80%');
    await page.getByTestId('alert-builder-next').click();
    const exprInput = page.getByTestId('alert-builder-expr');
    await expect(exprInput).toBeVisible({ timeout: 5000 });
    await exprInput.fill('rate(cpu_usage[5m])');
    await page.getByTestId('alert-builder-threshold').fill('80');
    await page.getByTestId('alert-builder-next').click();

    // No `tenant` key should be seeded. Default labels just have `team`.
    await expect(page.getByDisplayValue('team')).toBeVisible({ timeout: 5000 });
    await expect(page.getByDisplayValue('tenant')).toHaveCount(0);
  });
});
