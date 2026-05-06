import { test, expect } from '@playwright/test';

/**
 * Portal Error Boundary — E2E (PR-portal-2)
 *
 * Validates that the inline ErrorBoundary installed in jsx-loader.html
 * around the root React render catches per-tool render errors AND that
 * dep-load failures surface a user-readable message rather than
 * silently failing.
 *
 * Coverage:
 *
 *   1. Happy path — a known-good tool (playground) loads without
 *      hitting the error fallback (silent-regression guard for the
 *      common case).
 *   2. Render-time throw — the boundary catches a forced render error
 *      and shows the "此工具暫時無法載入 / Tool failed to load" panel
 *      with the error message + Reload button.
 *   3. Dep-load 404 — pointing a non-existent dep at the loader
 *      surfaces the wrapped error message via `showError` rather than
 *      a bare HTTP error. Verifies the loadDependencies wrapper
 *      annotation introduced in PR-portal-2.
 *
 * Why the throw-injection works (scenario 2)
 * ------------------------------------------
 * jsx-loader.html runs user JSX inside `<script type="text/babel">`
 * which Babel transforms client-side. Patching `window.React.createElement`
 * to throw on a sentinel component name lets us trigger a render-time
 * error without modifying any tool source — the boundary's
 * `getDerivedStateFromError` then catches it.
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

  test('render error — boundary catches throw and shows fallback', async ({ page }) => {
    // Mock the JSX fetch to return a deliberately-broken component.
    // jsx-loader fetches → strips front-matter → Babel-transforms →
    // mounts. The component throws synchronously on render → React's
    // error path → window.__ErrorBoundary catches → fallback renders.
    //
    // This is more robust than patching React.createElement on the
    // page because jsx-loader doesn't expose user components on
    // window — they live inside the Babel script-tag's lexical scope.
    const BROKEN_JSX = `---
title: "Boundary test fixture"
---
import React from 'react';
export default function Boom() {
  throw new Error('intentional boundary test failure');
}
`;
    // TD-030f: use a fictitious tool name not in ESM_TOOLS so jsx-loader
    // takes the legacy fetch path. Real tools are now mostly ESM-bundled —
    // mocking their .jsx URL no longer fires (dist bundle pre-empts).
    // jsx-loader's bare-key resolution falls through to
    // `../interactive/tools/<name>.jsx` for unknown names.
    await page.route('**/__test_render_error__.jsx', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/plain; charset=utf-8',
        body: BROKEN_JSX,
      }),
    );

    await page.goto('../assets/jsx-loader.html?component=__test_render_error__');

    await expect(page.getByTestId('error-boundary-fallback')).toBeVisible({
      timeout: 15000,
    });
    await expect(page.getByTestId('error-boundary-message')).toContainText(
      /intentional boundary test failure/,
    );
    // Reload button is part of the fallback UI.
    await expect(
      page.getByRole('button', { name: /Reload tool|重新載入/ }),
    ).toBeVisible();
  });

  test('dep 404 — loadDependencies surfaces wrapped error', async ({ page }) => {
    // TD-030f: all real tools now ESM-bundled, so we mock BOTH the tool
    // entry (with a frontmatter dependency on _common/hooks/...) AND
    // the dep itself as 404. jsx-loader takes legacy fetch path for the
    // unknown tool name, parses deps, tries to load the dep, hits 404,
    // → showError fires. After TD-030z (jsx-loader retired), this test
    // becomes obsolete and gets removed.
    const TOOL_WITH_DEP = `---
title: "Boundary test fixture — dep 404"
dependencies: [
  "_common/hooks/useDebouncedValue.js"
]
---
import React from 'react';
export default function NoOp() { return null; }
`;
    await page.route('**/__test_dep_404__.jsx', (route) =>
      route.fulfill({
        status: 200,
        contentType: 'text/plain; charset=utf-8',
        body: TOOL_WITH_DEP,
      }),
    );
    await page.route('**/_common/hooks/useDebouncedValue.js', (route) =>
      route.fulfill({ status: 404, body: 'not found' }),
    );

    await page.goto('../assets/jsx-loader.html?component=__test_dep_404__');

    // showError replaces the loader's #loading with #error; the
    // dep-load message should be self-explanatory.
    const errorEl = page.locator('#error');
    await expect(errorEl).toBeVisible({ timeout: 15000 });
    await expect(errorEl).toContainText(/Could not load dependency/);
    await expect(errorEl).toContainText(/useDebouncedValue\.js/);
  });
});
