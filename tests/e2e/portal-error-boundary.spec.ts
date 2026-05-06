import { test, expect } from '@playwright/test';

/**
 * Portal Error Boundary — E2E (PR-portal-2 → TD-030z)
 *
 * Validates that the ESM dist-bundle path mounts tools without showing
 * an error-boundary fallback for known-good tools.
 *
 * **TD-030z scope reduction**: scenarios 2 (render-time throw) and 3
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
    // TD-030z: dist-bundle-only world. If a `?component=<name>` doesn't
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
