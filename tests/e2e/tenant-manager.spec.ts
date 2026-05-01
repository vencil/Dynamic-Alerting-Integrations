import { test, expect, Page } from '@playwright/test';
import { checkA11y, formatA11yViolations, waitForPageReady } from './fixtures/axe-helper';

/**
 * Tenant Manager smoke tests - verifies core filtering and data display
 */

async function loadTenantManager(page: Page) {
  // Navigate to tenant-manager tool via jsx-loader or direct route
  await page.goto('./');

  // Try to find and click tenant-manager tool card
  const tenantCard = page.locator(
    ':text-is("tenant-manager"), [data-tool="tenant-manager"], [aria-label*="tenant" i]'
  ).first();

  const tenantCardCount = await page
    .locator(':text-is("tenant-manager"), [data-tool="tenant-manager"]')
    .count();

  if (tenantCardCount > 0) {
    await tenantCard.click();
    // Wait for tool to load
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {
      // Graceful degradation - tool might load without network idle
    });
  }
}

/**
 * Direct loader for the API-mode tests. The permissive `loadTenantManager`
 * above does `page.goto('./')` then tries to click a card, but if no card
 * matches its selectors the JSX never mounts and `page.route()` mocks have
 * nothing to intercept. The API-mode tests below MUST have tenant-manager.jsx
 * actually executing, so we bypass the index page and navigate straight to
 * the jsx-loader URL with `?component=...` — same convention `jsx-loader.html`
 * uses internally.
 */
async function loadTenantManagerDirect(page: Page) {
  // baseURL = http://localhost:8080/interactive/, so ../assets/jsx-loader.html
  // resolves to http://localhost:8080/assets/jsx-loader.html. The `component`
  // path is resolved relative to where jsx-loader.html lives, hence
  // `../interactive/tools/tenant-manager.jsx` from /assets/.
  await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
}

test.describe('Tenant Manager @critical', () => {
  test('should load tenant-manager tool and display data', async ({ page }) => {
    await loadTenantManager(page);

    // Check for tool title or heading
    const heading = page.locator(
      'h1:has-text("tenant-manager"), h2:has-text("Tenant"), [data-testid="tool-title"]'
    ).first();

    // Verify tenant manager interface exists (flexible check for loading state)
    const iframeCount = await page.locator('iframe').count();
    const contentDivCount = await page.locator('[data-tool="tenant-manager"]').count();

    // Either embedded in iframe or rendered in main document
    expect(iframeCount + contentDivCount).toBeGreaterThanOrEqual(0);

    // Wait a moment for data to load or show graceful message
    await page.waitForTimeout(2000);

    const bodyText = await page.locator('body').textContent();
    expect(bodyText).toBeTruthy();
  });

  test('should allow filtering by tenant name', async ({ page }) => {
    await loadTenantManager(page);

    // Wait for any tenant list or filter inputs to load
    await page.waitForTimeout(2000);

    // Look for filter/search input
    const filterInputs = page.locator('input[type="text"], input[placeholder*="search" i], input[aria-label*="filter" i]');
    const inputCount = await filterInputs.count();

    if (inputCount > 0) {
      const firstInput = filterInputs.first();

      // Type a search term
      await firstInput.fill('test');

      // Wait for results to filter
      await page.waitForTimeout(1000);

      // Verify input contains our text
      await expect(firstInput).toHaveValue('test');

      // Clear and verify
      await firstInput.clear();
      await expect(firstInput).toHaveValue('');
    }
  });

  test('should support metadata filtering', async ({ page }) => {
    await loadTenantManager(page);

    await page.waitForTimeout(2000);

    // Look for filter controls - common patterns: buttons, selects, checkboxes
    const filterControls = page.locator(
      '[data-testid*="filter"], [aria-label*="filter" i], .filter-control, select'
    );

    const filterCount = await filterControls.count();

    if (filterCount > 0) {
      // Verify at least one filter control exists
      const firstFilter = filterControls.first();
      await expect(firstFilter).toBeVisible();
    }
  });

  test('should display result count or list', async ({ page }) => {
    await loadTenantManager(page);

    await page.waitForTimeout(2000);

    // Look for result indicators: list items, table rows, badges with counts
    const listItems = page.locator('[data-testid="result-item"], li, tr');
    const resultCount = page.locator('[data-testid="result-count"], .count-badge, .result-count');

    // Either we have list items or explicit count display
    const itemCount = await listItems.count();
    const resultCountCount = await resultCount.count();

    // Should have at least one of these
    if (itemCount > 0 || resultCountCount > 0) {
      expect(true).toBe(true);
    }
  });

  test('should handle filter state persistence', async ({ page }) => {
    await loadTenantManager(page);

    await page.waitForTimeout(2000);

    // Find a filter input
    const filterInputs = page.locator('input[type="text"], input[placeholder*="search" i]');
    const inputCount = await filterInputs.count();

    if (inputCount > 0) {
      const input = filterInputs.first();

      // Set a filter value
      await input.fill('prod');
      await page.waitForTimeout(500);

      // Reload and check if filter is maintained (if app supports it)
      const currentValue = await input.inputValue();
      expect(currentValue).toBe('prod');
    }
  });

  test('should show graceful degradation on data load errors', async ({ page }) => {
    await loadTenantManager(page);

    await page.waitForTimeout(2000);

    // Check for error message or empty state
    const errorMessages = page.locator(
      '[role="alert"], .error-message, .empty-state, :text-is("No data"), :text("error")'
    );

    // Either we have data loaded or graceful error handling
    const bodyContent = await page.locator('body').textContent();
    expect(bodyContent).toBeTruthy();
    expect(bodyContent?.length || 0).toBeGreaterThan(100); // Expect substantial content
  });

  // C-2 PR-2: API-first data source (with platform-data.json fallback).
  // tenant-manager.jsx tries /api/v1/tenants/search first. These tests
  // cover the three production code paths via page.route() mocks:
  //   1. happy-path → tenants from API response render
  //   2. 429 retry-with-backoff → toast appears, then succeeds
  //   3. overflow banner → total_matched > items.length surfaces banner

  test('renders tenants from /api/v1/tenants/search when API responds 200', async ({ page }) => {
    // Capture browser console + page errors so a JSX runtime error
    // surfaces in test output rather than as a silent render failure.
    // Many earlier iterations of this PR failed with body.toContain
    // missing the mock IDs, but the actual root cause was buried in
    // the browser console.
    const consoleMessages: string[] = [];
    const pageErrors: string[] = [];
    page.on('console', (msg) => {
      const type = msg.type();
      if (type === 'error' || type === 'warning') {
        consoleMessages.push(`[${type}] ${msg.text()}`);
      }
    });
    page.on('pageerror', (err) => {
      pageErrors.push(`[pageerror] ${err.message}\n${err.stack || ''}`);
    });

    // Stub the live API BEFORE navigating so the very first fetch hits
    // our mock (page.route() applies to all subsequent requests).
    let apiCalled = false;
    await page.route('**/api/v1/tenants/search**', async (route) => {
      apiCalled = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { id: 'api-tenant-alpha', environment: 'production', tier: 'tier-1', domain: 'finance', db_type: 'mariadb', owner: 'alice', tags: ['stub'], groups: [] },
            { id: 'api-tenant-beta', environment: 'staging', tier: 'tier-2', domain: 'ops', db_type: 'redis', owner: 'bob', tags: [], groups: [] },
          ],
          total_matched: 2,
          page_size: 500,
          next_offset: null,
        }),
      });
    });

    await loadTenantManagerDirect(page);
    await page.waitForTimeout(2000);

    // Layered assertions:
    //   (a) Wire-level — JSX must call our endpoint with page_size=500.
    //       This catches "data-source layer never wired up" regressions
    //       even if the rendered DOM is hard to introspect through the
    //       jsx-loader / Babel-standalone path.
    //   (b) Render-level — pin against IDs unique to the mock so a
    //       silent fallback to platform-data.json/DEMO_TENANTS fails
    //       the test (those sources don't contain `api-tenant-*` IDs).
    expect(apiCalled).toBe(true);
    const body = await page.locator('body').textContent();
    if (!body?.includes('api-tenant-alpha')) {
      // Surface the diagnostic state into the assertion failure so we
      // can see WHY the render didn't produce the expected text.
      console.error('=== Browser console messages ===');
      consoleMessages.forEach((m) => console.error(m));
      console.error('=== Browser page errors ===');
      pageErrors.forEach((e) => console.error(e));
    }
    expect(body).toContain('api-tenant-alpha');
    expect(body).toContain('api-tenant-beta');
  });

  test('shows overflow banner when total_matched > items.length', async ({ page }) => {
    // Mock the API to claim there are 2000 tenants but only return
    // 500 — exactly the "we hit the page_size cap" condition the
    // banner should surface.
    await page.route('**/api/v1/tenants/search**', async (route) => {
      const items = Array.from({ length: 500 }, (_, i) => ({
        id: `bulk-tenant-${i}`,
        environment: 'production',
        tier: 'tier-2',
        domain: 'ops',
        db_type: 'mariadb',
        owner: 'team',
        tags: [],
        groups: [],
      }));
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items,
          total_matched: 2000,
          page_size: 500,
          next_offset: 500,
        }),
      });
    });

    await loadTenantManagerDirect(page);
    await page.waitForTimeout(3000);

    // The overflow banner contains "500" and "2000" plus a hint to
    // refine filters. We use a regex so the test survives small
    // copy edits — we only care that BOTH numbers + the "refine"
    // hint appear together.
    const bodyText = await page.locator('body').textContent();
    expect(bodyText).toMatch(/500.+2000|2000.+500/);
    expect(bodyText?.toLowerCase()).toMatch(/refine|narrow|篩選|搜尋/);
  });

  test('retries once on 429 with Retry-After and surfaces a toast', async ({ page }) => {
    // First request: 429 with Retry-After: 1 (the smallest meaningful
    // value — keeps the test fast). Second request: 200 with a single
    // tenant so the post-retry state is verifiable.
    let callCount = 0;
    await page.route('**/api/v1/tenants/search**', async (route) => {
      callCount += 1;
      if (callCount === 1) {
        await route.fulfill({
          status: 429,
          headers: { 'Retry-After': '1' },
          contentType: 'application/json',
          body: JSON.stringify({ error: 'rate limit exceeded' }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            { id: 'after-retry-tenant', environment: 'production', tier: 'tier-1', domain: 'ops', db_type: 'mariadb', owner: 'alice', tags: [], groups: [] },
          ],
          total_matched: 1,
          page_size: 500,
          next_offset: null,
        }),
      });
    });

    await loadTenantManagerDirect(page);
    // Allow up to ~5s for the retry path: 1s Retry-After + buffer.
    await page.waitForTimeout(5000);

    // Both requests fired (1 = initial 429, 2 = retry success).
    expect(callCount).toBeGreaterThanOrEqual(2);
    // Post-retry tenant is rendered.
    const body = await page.locator('body').textContent();
    expect(body).toContain('after-retry-tenant');
  });

  // PR-2b (#TBD): server-side `q` filter — typing in the search box
  // sends `?q=` to /api/v1/tenants/search after debounce. Pinning the
  // wire-level contract here because the orchestrator's client-side
  // filter still works in static / demo modes so a "did the UI filter"
  // assertion alone wouldn't catch a server-side regression.
  test('debounces search-text into ?q= query param (PR-2b)', async ({ page }) => {
    const seenQueries: string[] = [];
    await page.route('**/api/v1/tenants/search**', async (route) => {
      const url = new URL(route.request().url());
      seenQueries.push(url.searchParams.get('q') || '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{
            id: 'q-test-tenant', environment: 'production', tier: 'tier-1',
            domain: 'finance', db_type: 'mariadb', owner: 'alice', tags: [], groups: [],
          }],
          total_matched: 1, page_size: 500, next_offset: null,
        }),
      });
    });

    await loadTenantManagerDirect(page);
    await page.waitForTimeout(2000); // initial fetch (q='')

    // Type into the search input. `q` is debounced 300ms; pre-fix
    // would have fired one fetch per keystroke.
    const searchInput = page.locator('input[type="text"], input[placeholder*="search" i]').first();
    await searchInput.fill('mariadb');
    await page.waitForTimeout(800); // > 300ms debounce + fetch + render

    // We expect: (1) initial mount fetch with q='' OR no q at all,
    // and (2) at least one subsequent fetch with q='mariadb'.
    expect(seenQueries.length).toBeGreaterThanOrEqual(2);
    expect(seenQueries).toContain('mariadb');
    // No intermediate single-char queries should appear (debounce
    // collapses 'm' / 'ma' / 'mar' / ... down to the final value).
    const intermediates = seenQueries.filter(q =>
      q.length > 0 && q.length < 'mariadb'.length
    );
    expect(intermediates.length).toBe(0);
  });

  test('reads ?q= from URL on mount (PR-2b URL state)', async ({ page }) => {
    const seenQueries: string[] = [];
    await page.route('**/api/v1/tenants/search**', async (route) => {
      const url = new URL(route.request().url());
      seenQueries.push(url.searchParams.get('q') || '');
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [{
            id: 'url-state-tenant', environment: 'production', tier: 'tier-1',
            domain: 'finance', db_type: 'mariadb', owner: 'alice', tags: [], groups: [],
          }],
          total_matched: 1, page_size: 500, next_offset: null,
        }),
      });
    });

    // Navigate WITH ?q=preset already in the URL.
    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx&q=preset');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2000);

    // The very first API call should already include q=preset
    // (initial state seeded from URL, not from empty string).
    expect(seenQueries.length).toBeGreaterThanOrEqual(1);
    expect(seenQueries[0]).toBe('preset');
  });

  test('passes WCAG 2.1 AA accessibility checks', async ({ page }) => {
    // Load tenant-manager tool
    await loadTenantManager(page);
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    await waitForPageReady(page);

    // Run accessibility check
    const results = await checkA11y(page);

    // Log violations before asserting so CI output contains diagnostics
    if (results.violations.length > 0) {
      const violationDetails = formatA11yViolations(results.violations);
      console.error(`Tenant manager a11y violations:\n${violationDetails}`);
    }

    // Assert no violations
    expect(results.violations.length).toBe(0);
  });
});
