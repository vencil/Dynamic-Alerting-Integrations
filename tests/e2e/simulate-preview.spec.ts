/**
 * Simulate Preview Widget — E2E (S#95 / C-4 PR-2)
 *
 * Validates the C-7b `/api/v1/tenants/simulate` preview widget:
 *
 *   1. Smoke load via jsx-loader (no 404 / JS error)
 *   2. Default-seeded inputs auto-fire a simulate POST after debounce
 *      and render the success state (4 testid sections)
 *   3. URL `?tenant_id=<id>` pre-fills the Tenant ID input (S#94
 *      deep-link convention reuse)
 *   4. 4xx error response surfaces structured error banner
 *   5. Network failure (route-aborted) surfaces the unreachable-API
 *      error message
 *
 * Selector discipline (S#94 lesson): every interactive element is
 * pinned via `data-testid`. We avoid `page.getByDisplayValue` (an RTL
 * API that does NOT exist on Playwright `Page`). For input-value
 * assertions we use the `readAllInputValues` helper pattern from
 * `tenant-manager-deeplink.spec.ts` — `page.evaluate(...)` over the
 * DOM is the robust path for React-controlled inputs.
 */
import { test, expect, Page } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
} from './fixtures/portal-tool-smoke';
// S#98: side-effect import registers `toBeVisibleWithDiagnostics`
// matcher used below for state-* assertions where the default
// "element(s) not found" failure mode wastes a CI round-trip.
import './fixtures/diagnostic-matchers';

async function readAllInputValues(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('input')).map(
      (el) => (el as HTMLInputElement).value
    )
  );
}

const HAPPY_RESPONSE = {
  tenant_id: 'example-tenant',
  source_hash: 'aaaaaaaaaaaaaaaa',
  merged_hash: 'bbbbbbbbbbbbbbbb',
  defaults_chain: ['_defaults.yaml'],
  effective_config: {
    cpu_threshold: 70,
    mem_threshold: 80,
    routing_channel: 'slack:#tenant-alerts',
  },
};

test.describe('Simulate Preview Widget @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    // Stub the API up-front so the auto-simulate effect that fires on
    // mount never tries to hit a real backend during the a11y scan.
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(HAPPY_RESPONSE),
      });
    });

    await loadPortalTool(page, 'simulate-preview');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Simulate|Preview|預覽/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('auto-simulates on mount and renders success state', async ({ page }) => {
    let callCount = 0;
    let lastBody: any = null;
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      callCount += 1;
      try {
        lastBody = JSON.parse(route.request().postData() || '{}');
      } catch (_) {
        lastBody = null;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(HAPPY_RESPONSE),
      });
    });

    await loadPortalTool(page, 'simulate-preview');

    // Wait for the inputs to mount, then for the debounced auto-simulate
    // to fire. Default debounce is 500ms — give it 2s of headroom for
    // CI variance.
    await expect(page.getByTestId('simulate-preview-tenant-id')).toBeVisible({
      timeout: 10000,
    });
    // S#98 demonstration: state-* testid uses the diagnostic matcher
    // so a regression to `state-empty` / `state-loading` produces a CI
    // error message that lists what testids ARE visible — no need to
    // re-run locally to discover actual state.
    await expect(
      page.getByTestId('simulate-preview-state-ready')
    ).toBeVisibleWithDiagnostics({ timeout: 5000 });

    // Pin success-state sections by testid.
    await expect(page.getByTestId('simulate-preview-source-hash')).toContainText(
      HAPPY_RESPONSE.source_hash
    );
    await expect(page.getByTestId('simulate-preview-merged-hash')).toContainText(
      HAPPY_RESPONSE.merged_hash
    );
    await expect(page.getByTestId('simulate-preview-defaults-chain')).toContainText(
      '_defaults.yaml'
    );
    await expect(
      page.getByTestId('simulate-preview-effective-config')
    ).toContainText('cpu_threshold');

    // Wire-level: the request body must be JSON with base64-encoded
    // YAML strings. We don't decode — just assert structural shape so
    // a future regression that drops base64 (or sends raw YAML) trips
    // here rather than silently breaking the backend hash parity.
    expect(callCount).toBeGreaterThanOrEqual(1);
    expect(lastBody).not.toBeNull();
    expect(typeof lastBody.tenant_id).toBe('string');
    expect(typeof lastBody.tenant_yaml).toBe('string');
    // Base64 alphabet: [A-Za-z0-9+/=] — non-empty + no whitespace.
    expect(lastBody.tenant_yaml).toMatch(/^[A-Za-z0-9+/=]+$/);
  });

  test('reads ?tenant_id= from URL and pre-fills the Tenant ID input', async ({
    page,
  }) => {
    const TENANT_ID = 'pr95-deeplink-tenant';
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...HAPPY_RESPONSE, tenant_id: TENANT_ID }),
      });
    });

    // Navigate via the same jsx-loader URL convention `loadPortalTool`
    // uses, but with the deep-link query param appended.
    await page.goto(`../assets/jsx-loader.html?component=simulate-preview&tenant_id=${TENANT_ID}`);
    await page
      .waitForFunction(
        () => document.title.length > 0 && document.title !== 'Interactive Component',
        { timeout: 15000 }
      )
      .catch(() => {});

    await expect(page.getByTestId('simulate-preview-tenant-id')).toBeVisible({
      timeout: 10000,
    });
    const values = await readAllInputValues(page);
    expect(values).toContain(TENANT_ID);
  });

  test('surfaces structured error on 4xx response', async ({ page }) => {
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'tenant id not present in tenant_yaml' }),
      });
    });

    await loadPortalTool(page, 'simulate-preview');

    const errBanner = page.getByTestId('simulate-preview-state-error');
    // S#98: diagnostic matcher — if the widget surfaces a different
    // state (e.g. ready / empty), the failure message lists the
    // visible testids so we can see the actual transition.
    await expect(errBanner).toBeVisibleWithDiagnostics({ timeout: 10000 });
    await expect(errBanner).toContainText('400');
    await expect(errBanner).toContainText(/tenant id not present/);
  });

  test('surfaces unreachable-API error when fetch fails', async ({ page }) => {
    // Abort every simulate request — Playwright surfaces this to the
    // browser as a network error, which our widget's catch path turns
    // into the "Could not reach backend API" banner.
    await page.route('**/api/v1/tenants/simulate', async (route) => {
      await route.abort('failed');
    });

    await loadPortalTool(page, 'simulate-preview');

    const errBanner = page.getByTestId('simulate-preview-state-error');
    // S#98: diagnostic matcher (same rationale as 4xx scenario above).
    await expect(errBanner).toBeVisibleWithDiagnostics({ timeout: 10000 });
    await expect(errBanner).toContainText(/Could not reach|無法連線/);
  });
});
