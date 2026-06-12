import { test, expect } from '@playwright/test';

/**
 * Portal Error Boundary — E2E (PR-portal-2 → TRK-230z)
 *
 * Validates that the ESM dist-bundle path mounts tools without showing
 * an error-boundary fallback for known-good tools.
 *
 * **TRK-230z scope reduction**: scenarios 2 (render-time throw) and 3
 * (dep-load 404) tested the LEGACY jsx-loader fetch + Babel transform
 * path. That path is gone — every tool loads via `<script type="module"
 * src="../assets/dist/<name>.js">`. Render-error coverage now lives
 * inside each tool's dist bundle (the entry script wraps the orchestrator
 * in ErrorBoundary); tested implicitly by every other E2E spec rendering
 * its target tool successfully. The dep-404 scenario doesn't exist as a
 * concept anymore — bundles include all deps; HTTP failures only happen
 * on dist-file 404 (which `script.onerror` surfaces via `showError`).
 */

test.describe('Portal ErrorBoundary', () => {
  test('happy path — playground renders without fallback', async ({ page }) => {
    await page.goto('../assets/jsx-loader.html?component=playground');
    // Loader hides #loading once the component mounts; boundary fallback
    // would have data-testid="error-boundary-fallback" if it tripped.
    await expect(page.locator('#loading')).toBeHidden({ timeout: 15000 });
    await expect(
      page.getByTestId('error-boundary-fallback'),
    ).toHaveCount(0);
  });

  test('missing dist bundle — surfaces script load error', async ({ page }) => {
    // TRK-230z: dist-bundle-only world. If a `?component=<name>` doesn't
    // resolve to an existing dist file, jsx-loader's `loadDistBundle`
    // appends a `<script type="module">` whose load fails → script.onerror
    // calls showError → user-readable message replaces the loading spinner.
    await page.goto(
      '../assets/jsx-loader.html?component=__nonexistent_tool_for_test__',
    );

    const errorEl = page.locator('#error');
    await expect(errorEl).toBeVisible({ timeout: 15000 });
    await expect(errorEl).toContainText(/Failed to load ESM bundle/);
  });
});

test.describe('jsx-loader reflected-XSS guard', () => {
  // Regression guard: ?component= / ?flow= used to flow unsanitized into
  // showError's innerHTML — `?component=<img onerror=…>` executed script
  // on the public GitHub Pages tool page. showError now renders messages
  // via textContent and the component param is allowlist-validated; a
  // markup payload must surface as inert banner text, never as elements.
  const PAYLOAD = '<img src=x onerror="window.__xss_executed=1">';

  test('markup in ?component= is rejected and rendered inert', async ({ page }) => {
    await page.goto(
      '../assets/jsx-loader.html?component=' + encodeURIComponent(PAYLOAD),
    );

    const errorEl = page.locator('#error');
    await expect(errorEl).toBeVisible({ timeout: 15000 });
    await expect(errorEl).toContainText('Invalid component name');
    await expect(errorEl.locator('img')).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as { __xss_executed?: number }).__xss_executed),
    ).toBeUndefined();
  });

  test('markup in ?flow= is rendered inert in the unknown-flow banner', async ({ page }) => {
    await page.goto(
      '../assets/jsx-loader.html?flow=' + encodeURIComponent(PAYLOAD),
    );

    const errorEl = page.locator('#error');
    await expect(errorEl).toBeVisible({ timeout: 15000 });
    await expect(errorEl).toContainText('Unknown flow');
    await expect(errorEl.locator('img')).toHaveCount(0);
    expect(
      await page.evaluate(() => (window as { __xss_executed?: number }).__xss_executed),
    ).toBeUndefined();
  });

  test('attribute-breakout ?lang= on a valid flow cannot inject markup', async ({ page }) => {
    // renderFlowUI builds stepper/nav href strings with `&lang=` + the raw
    // value and assigns via innerHTML. A valid ?flow= reaches that path, so
    // ?lang="><img onerror=…> used to break out of the href attribute even
    // though the flow name itself was trusted. __DA_LANG is now normalized
    // to 'zh'/'en', so the payload can never reach the markup.
    const attrPayload = '"><img src=x onerror="window.__xss_executed=1">';
    await page.goto(
      '../assets/jsx-loader.html?flow=onboarding&step=0&lang=' +
        encodeURIComponent(attrPayload),
    );

    // The flow stepper renders (valid flow); assert no injected <img> and no
    // script execution anywhere in the document.
    await expect(page.locator('.flow-stepper')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('img[onerror]')).toHaveCount(0);
    expect(await page.evaluate(() => document.body.querySelectorAll('img').length)).toBe(0);
    expect(
      await page.evaluate(() => (window as { __xss_executed?: number }).__xss_executed),
    ).toBeUndefined();
    // Normalized to a safe language value.
    expect(
      await page.evaluate(() => (window as { __DA_LANG?: string }).__DA_LANG),
    ).toBe('en');
  });
});
