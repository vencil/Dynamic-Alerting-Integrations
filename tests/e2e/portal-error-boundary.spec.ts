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
    // Inject the throw BEFORE the loader script runs. The poisoned
    // createElement only throws for elements with the sentinel name
    // we ship below; everything else (the boundary's own fallback
    // tree, the loader DOM) renders normally.
    await page.addInitScript(() => {
      const POISON = '__BOUNDARY_TEST_POISON__';
      // Hook React.createElement once React loads. Use a polling
      // microtask because React is loaded by a vendor <script> tag
      // after this init script runs.
      const hook = () => {
        if (!window.React) return setTimeout(hook, 10);
        const orig = window.React.createElement;
        window.React.createElement = function (type: unknown, ...rest: unknown[]) {
          if (
            typeof type === 'function' &&
            (type as { name?: string }).name === POISON
          ) {
            throw new Error('intentional boundary test failure');
          }
          return orig.call(this, type, ...rest);
        };
      };
      hook();
    });

    // Use a real tool; we'll patch the rendered component name into the
    // sentinel via a follow-up evaluate so the createElement hook fires
    // when the loader's render-call evaluates.
    await page.goto('../assets/jsx-loader.html?component=playground');

    // Force the user-component name lookup to return the poisoned name.
    // jsx-loader stores the export-default name in window.__currentComponentName
    // and uses it inside the `React.createElement(<name>, ...)` line.
    // Simpler: replace the actual component identifier on the global
    // before render by re-defining `Playground` (the playground export).
    await page.evaluate(() => {
      const w = window as unknown as Record<string, unknown>;
      // Defer until Babel has emitted the user-component as a global.
      const wait = () =>
        new Promise<void>((resolve) => {
          const tick = () => {
            if (w.Playground) return resolve();
            setTimeout(tick, 25);
          };
          tick();
        });
      return wait().then(() => {
        // Reassign to a poisoned function with the sentinel name so the
        // createElement hook trips on the next render attempt.
        Object.defineProperty(w, 'Playground', {
          configurable: true,
          value: function __BOUNDARY_TEST_POISON__() {
            return null;
          },
        });
        // Force a re-render by toggling the language preference, which
        // triggers the loader's re-render path.
        const rerender = w.__rerenderCurrent as (() => void) | undefined;
        if (typeof rerender === 'function') rerender();
      });
    });

    // Either the original mount caught the throw, or the forced
    // re-render triggers it. Boundary fallback should now be visible.
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
    // Intercept the loader's fetch for any _common/ dep and serve a 404
    // so loadDependencies hits the catch path; the wrapped error from
    // PR-portal-2 should then arrive at showError.
    await page.route('**/_common/hooks/useDebouncedValue.js', (route) =>
      route.fulfill({ status: 404, body: 'not found' }),
    );

    // tenant-manager depends on useDebouncedValue — so the 404 trips
    // its dep load.
    await page.goto('../assets/jsx-loader.html?component=tenant-manager');

    // showError replaces the loader's #loading with #error; the
    // dep-load message should be self-explanatory.
    const errorEl = page.locator('#error');
    await expect(errorEl).toBeVisible({ timeout: 15000 });
    await expect(errorEl).toContainText(/Could not load dependency/);
    await expect(errorEl).toContainText(/useDebouncedValue\.js/);
  });
});
