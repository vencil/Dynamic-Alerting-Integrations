/**
 * Tenant Manager × Wizard Deep-Link — E2E (S#94 / C-4 PR-1; extended in S#99 / §C-4 (v) closure)
 *
 * Validates the deep-link bridge from tenant cards into the
 * alert-builder, routing-trace, and simulate-preview wizards.
 *
 * Coverage (after S#99 extension):
 *
 *   1. Each tenant card surfaces 🛠️ Alert + 🧭 Route + 🔍 Preview
 *      footer links with stable `data-testid` and
 *      `?component=...&tenant_id=<id>` hrefs (kept in tenant-manager
 *      open via target="_blank").
 *   2. Navigating directly to `?component=alert-builder&tenant_id=<id>`
 *      pre-fills a `tenant=<id>` row in the labels editor (step 2).
 *   3. Navigating directly to `?component=routing-trace&tenant_id=<id>`
 *      pre-fills a `tenant=<id>` row in the alert-input labels editor
 *      (step 0).
 *   4. Negative — alert-builder without `?tenant_id=` does NOT inject
 *      an empty `tenant` key (silent-regression guard).
 *   5. Navigating directly to `?component=simulate-preview&tenant_id=<id>`
 *      pre-fills the Tenant ID input AND auto-simulates to the ready
 *      state. Uses `toBeVisibleWithDiagnostics` (S#98) so a regression
 *      to `state-empty` produces a self-explanatory CI error.
 *   6. URL-encoding round-trip — tenant ids containing dashes and dots
 *      survive the deep-link → wizard handoff intact (no double-encode,
 *      no decode surprise).
 *
 * The 5-scenario integration coverage closes planning §C-4 sub-task (v).
 * Sub-tasks (i) tab container and (iii) cross-tool state are deferred-
 * not-pursuing (post-S#94 separate-tab UX is the established pattern;
 * see CHANGELOG / planning §12.2 C-4 row for the rationale).
 *
 * Wire pattern: tenant-manager.spec.ts API-mode tests use `page.route()`
 * to stub `/api/v1/tenants/search` and `loadTenantManagerDirect()` to
 * bypass the index page and load jsx-loader.html directly. We reuse the
 * same convention here so the deep-link tests do not depend on demo
 * fixtures matching specific tenant IDs.
 */
import { test, expect, Page } from '@playwright/test';
// S#98: side-effect import registers `toBeVisibleWithDiagnostics`
// matcher, used below for state-* assertions where the default
// "element(s) not found" failure mode wastes a CI round-trip.
import './fixtures/diagnostic-matchers';
import { checkA11y, formatA11yViolations, waitForPageReady } from './fixtures/axe-helper';

// Pin a single tenant ID across all tests so failures are easy to grep.
const TENANT_ID = 'pr94-deeplink-tenant';

/**
 * Read every `<input>` value currently on the page.
 *
 * Why this helper exists (lesson from CI failure on PR #184 first run):
 * Playwright does NOT have `page.getByDisplayValue()` — that API belongs
 * to React Testing Library. We initially used it and CI failed with
 * `TypeError: page.getByDisplayValue is not a function`. CSS attribute
 * selectors (`input[value="x"]`) also don't reliably work for React-
 * controlled inputs because React sets the DOM property, not always the
 * attribute. The robust path is to evaluate `el.value` (the property)
 * via `page.evaluate` and assert against the returned array.
 */
async function readAllInputValues(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('input')).map(
      (el) => (el as HTMLInputElement).value
    )
  );
}

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
  test('tenant card surfaces Alert + Route + Preview deep-link footer with correct hrefs', async ({
    page,
  }) => {
    await loadTenantManagerWithMockedApi(page);

    // All three deep-link anchors should be present on the rendered
    // card for our mocked tenant. Pin via data-testid (PR #182 lesson:
    // prefer testid over text/role to dodge strict-mode collisions
    // when other emoji-laden links appear elsewhere on the page).
    // S#94 shipped the first 2 (alert-builder + routing-trace);
    // S#95 / C-4 PR-2 added the 3rd (simulate-preview).
    const alertLink = page.getByTestId(`tenant-card-${TENANT_ID}-build-alert`);
    const traceLink = page.getByTestId(`tenant-card-${TENANT_ID}-trace-routing`);
    const previewLink = page.getByTestId(
      `tenant-card-${TENANT_ID}-simulate-preview`
    );

    await expect(alertLink).toBeVisible({ timeout: 10000 });
    await expect(traceLink).toBeVisible();
    await expect(previewLink).toBeVisible();

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

    const previewHref = await previewLink.getAttribute('href');
    expect(previewHref).toContain('component=simulate-preview');
    expect(previewHref).toContain(`tenant_id=${TENANT_ID}`);

    // target="_blank" so the tenant-manager stays open for context
    // — central UX intent of this PR.
    expect(await alertLink.getAttribute('target')).toBe('_blank');
    expect(await traceLink.getAttribute('target')).toBe('_blank');
    expect(await previewLink.getAttribute('target')).toBe('_blank');
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
    // for the seeded `tenant=<id>` row must be present. Wait for
    // the step transition to settle (Next click → re-render).
    await expect(page.getByPlaceholder(/可包含|Supports/i)).toBeVisible({
      timeout: 5000,
    });
    const values = await readAllInputValues(page);
    expect(values).toContain('tenant');
    expect(values).toContain(TENANT_ID);
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
    // `tenant=<id>` row should be on screen immediately. Wait for the
    // alertname testid so the JSX has actually mounted, then read.
    await expect(page.getByTestId('routing-trace-alertname')).toBeVisible({
      timeout: 10000,
    });
    const values = await readAllInputValues(page);
    expect(values).toContain('tenant');
    expect(values).toContain(TENANT_ID);
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

    // Wait for step 2's labels editor to render before reading values.
    await expect(page.getByPlaceholder(/可包含|Supports/i)).toBeVisible({
      timeout: 5000,
    });
    // No `tenant` key should be seeded. Default labels just have `team`.
    const values = await readAllInputValues(page);
    expect(values).toContain('team');
    expect(values).not.toContain('tenant');
  });

  // -------------------------------------------------------------------
  // S#99 — §C-4 sub-task (v) integration coverage extension.
  //
  // The 4 scenarios above exercise the deep-link bridge into
  // alert-builder + routing-trace. The 2 below extend coverage to
  // simulate-preview + URL-encoding round-trip — which together with
  // the existing scenarios close the 5-scenario integration target.
  // -------------------------------------------------------------------

  test('simulate-preview pre-fills Tenant ID + reaches ready state from ?tenant_id=', async ({
    page,
  }) => {
    // Mock the simulate API so the auto-simulate effect on cold-start
    // can resolve and the widget transitions to STATUS.READY (post-PR
    // #185 fix the default tenantId is `'example-tenant'`, but with a
    // URL param the override should win — verify both).
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          tenant_id: TENANT_ID,
          source_hash: 'cccccccccccccccc',
          merged_hash: 'dddddddddddddddd',
          defaults_chain: ['_defaults.yaml'],
          effective_config: { cpu_threshold: 70 },
        }),
      });
    });

    await page.goto(
      `../assets/jsx-loader.html?component=simulate-preview&tenant_id=${TENANT_ID}`
    );
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});

    // Tenant ID input must be pre-filled with our URL-param value
    // (override of the default `'example-tenant'` cold-start seed).
    await expect(page.getByTestId('simulate-preview-tenant-id')).toBeVisible({
      timeout: 10000,
    });
    const inputValues = await readAllInputValues(page);
    expect(inputValues).toContain(TENANT_ID);

    // S#98 diagnostic matcher: if a regression makes the widget render
    // `state-empty` / `state-error` instead of `state-ready`, the CI
    // failure message lists every visible testid so we don't need to
    // re-run locally to find what state the widget is in.
    await expect(
      page.getByTestId('simulate-preview-state-ready')
    ).toBeVisibleWithDiagnostics({ timeout: 5000 });
  });

  test('URL-encoding round-trip — tenant ids with dashes/dots survive the handoff', async ({
    page,
  }) => {
    // Pin a tenant id with characters that often misbehave in URL
    // encoding round-trips: dashes (treated by encodeURIComponent as
    // safe), dots (also safe but historically tripped on some servers),
    // and a digit prefix (no semantic load but exercises the regex).
    // We don't include `:` or `#` because those have URL semantic
    // meaning that the deep-link contract rightly does not promise to
    // round-trip.
    const SPECIAL_TENANT = 'team-platform-2026.q2';
    await page.route('**/api/v1/tenants/search**', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          items: [
            {
              id: SPECIAL_TENANT,
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
    await page.goto(
      '../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx'
    );
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
    await page.waitForTimeout(2000);

    // All 3 footer buttons must contain the EXACT tenant id (URL-
    // encoded by URLSearchParams). encodeURIComponent leaves dashes
    // and dots unchanged, so the literal id appears in the href.
    const alertHref = await page
      .getByTestId(`tenant-card-${SPECIAL_TENANT}-build-alert`)
      .getAttribute('href');
    const traceHref = await page
      .getByTestId(`tenant-card-${SPECIAL_TENANT}-trace-routing`)
      .getAttribute('href');
    const previewHref = await page
      .getByTestId(`tenant-card-${SPECIAL_TENANT}-simulate-preview`)
      .getAttribute('href');

    expect(alertHref).toContain(`tenant_id=${SPECIAL_TENANT}`);
    expect(traceHref).toContain(`tenant_id=${SPECIAL_TENANT}`);
    expect(previewHref).toContain(`tenant_id=${SPECIAL_TENANT}`);

    // Round-trip check: navigate via the alert-builder href and verify
    // the wizard receives the literal id (no decode mishap).
    await page.goto(`../assets/jsx-loader.html${alertHref}`);
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});
    await page.getByTestId('alert-builder-name').fill('SpecialCharsAlert');
    await page
      .getByPlaceholder(/CPU 使用率超過 80%|CPU usage above 80%/)
      .fill('Round-trip check');
    await page.getByTestId('alert-builder-next').click();
    const expr = page.getByTestId('alert-builder-expr');
    await expect(expr).toBeVisibleWithDiagnostics({ timeout: 5000 });
    await expr.fill('rate(cpu_usage[5m])');
    await page.getByTestId('alert-builder-threshold').fill('80');
    await page.getByTestId('alert-builder-next').click();
    await expect(page.getByPlaceholder(/可包含|Supports/i)).toBeVisible({
      timeout: 5000,
    });

    const labels = await readAllInputValues(page);
    expect(labels).toContain('tenant');
    expect(labels).toContain(SPECIAL_TENANT);
  });

  // TECH-DEBT-020 (#225): scan the tenant-manager landing view (the
  // host page from which the deep-link footer renders) for WCAG 2.1 AA
  // violations. If new violations surface, register them in
  // `docs/internal/frontend-quality-backlog.md` rather than relaxing
  // this assertion silently.
  test('passes WCAG 2.1 AA accessibility checks (tenant-manager landing view)', async ({ page }) => {
    await loadTenantManagerWithMockedApi(page);
    await waitForPageReady(page);

    const results = await checkA11y(page);
    if (results.violations.length > 0) {
      console.error(`tenant-manager-deeplink a11y violations:\n${formatA11yViolations(results.violations)}`);
    }
    expect(results.violations.length).toBe(0);
  });
});
