/**
 * Portal Tool Smoke Helpers
 *
 * Shared utilities for JSX tool smoke tests (Phase .a A-6, v2.7.0).
 * Extracted from patterns used across operator-setup-wizard.spec.ts and
 * tenant-manager.spec.ts so that new specs don't re-invent the navigation
 * + axe-core + screenshot plumbing.
 *
 * Usage:
 *   import { loadPortalTool, runToolSmokeChecks } from './fixtures/portal-tool-smoke';
 *
 *   test.describe('wizard @critical', () => {
 *     test('loads + basic a11y', async ({ page }) => {
 *       await loadPortalTool(page, 'wizard');
 *       await runToolSmokeChecks(page, { expectedTitleMatch: /Wizard|Role|Selector/i });
 *     });
 *   });
 */
import { Page, expect } from '@playwright/test';
import { checkA11y, waitForPageReady, formatA11yViolations } from './axe-helper';

/**
 * Uncaught exceptions collected per page since loadPortalTool().
 * ESM module-evaluation throws fire neither `script.onerror` nor any
 * ErrorBoundary — the loader's onload still runs, hides the spinner,
 * and leaves #root blank. Before this collector, such a tool passed
 * every smoke check while being completely broken in prod.
 */
const pageErrors = new WeakMap<Page, Error[]>();

export interface ToolSmokeOptions {
  /** Regex the page title should match after the tool loads. */
  expectedTitleMatch?: RegExp;
  /** Additional selector(s) to exclude from axe (e.g. third-party widgets, known-safe visuals). */
  axeExclude?: string[];
  /** Max number of non-Critical axe violations tolerated. Defaults to 0 (strict). */
  allowedNonCriticalViolations?: number;
  /** Skip axe run entirely (useful for tools with known in-flight a11y work). Defaults to false. */
  skipA11y?: boolean;
}

/**
 * Navigate to a JSX tool via jsx-loader.
 * Assumes Playwright `baseURL` is set to `<root>/interactive/`, so the loader
 * lives at `../assets/jsx-loader.html?component=<key>` (matches the portal's
 * relative-path convention — do NOT use absolute root paths, see TRK-104).
 *
 * @param page     Playwright Page
 * @param toolKey  Tool registry key (e.g. "wizard", "deployment-wizard")
 */
export async function loadPortalTool(page: Page, toolKey: string): Promise<void> {
  let errors = pageErrors.get(page);
  if (!errors) {
    errors = [];
    pageErrors.set(page, errors);
    page.on('pageerror', (err) => errors!.push(err));
  }
  // Reset per load so a second loadPortalTool() on the same page only
  // reports errors from its own navigation.
  errors.length = 0;
  await page.goto(`../assets/jsx-loader.html?component=${toolKey}`);
  // jsx-loader sets document.title once the component mounts. Wait for that
  // signal with a fallback to networkidle so we don't hang on slow CI.
  await page
    .waitForFunction(
      () => document.title.length > 0 && document.title !== 'Interactive Component',
      { timeout: 15000 }
    )
    .catch(() => {});
  await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
}

/**
 * Run the standard smoke-level assertion bundle against a loaded tool:
 *   1. No uncaught page errors since loadPortalTool() (module-eval throws)
 *   2. #root contains rendered content (blank #root = bundle never mounted)
 *   3. Title matches expected regex (if provided)
 *   4. No "Failed to load" / "404" text anywhere on page
 *   5. axe-core WCAG 2.1 AA scan — 0 Critical violations
 */
export async function runToolSmokeChecks(
  page: Page,
  options: ToolSmokeOptions = {}
): Promise<void> {
  const {
    expectedTitleMatch,
    axeExclude = [],
    allowedNonCriticalViolations = 0,
    skipA11y = false,
  } = options;

  await waitForPageReady(page);

  // Uncaught exceptions — catches module-evaluation throws that render
  // nothing (no error banner, no ErrorBoundary fallback, blank #root).
  // Third-party CDN scripts (lucide-react / tailwind off unpkg+cdnjs)
  // have a known document.write ordering race that throws before React
  // is defined; tools tolerate it via icon fallbacks, so only errors
  // with at least one same-origin stack frame (our dist bundles / the
  // loader page) fail the smoke gate.
  const uncaught = (pageErrors.get(page) ?? []).filter((e) => {
    const urls = (e.stack ?? '').match(/https?:\/\/[^\s):]+/g) ?? [];
    return urls.length === 0 || urls.some((u) => u.includes('localhost') || u.includes('127.0.0.1'));
  });
  expect(
    uncaught.map((e) => e.message),
    'no uncaught page errors during tool load'
  ).toEqual([]);

  // A mounted tool (or its ErrorBoundary fallback) always renders into
  // #root; an empty #root means the bundle never mounted.
  await expect(
    page.locator('#root > *').first(),
    '#root should contain rendered content (empty #root = bundle never mounted)'
  ).toBeAttached();

  if (expectedTitleMatch) {
    const title = await page.title();
    expect(title, `page title should match ${expectedTitleMatch}`).toMatch(
      expectedTitleMatch
    );
  }

  // Page-level error sentinel — catches loader failures that render a red banner
  // rather than the expected component (common failure mode in CI).
  const bodyText = await page.locator('body').innerText().catch(() => '');
  expect(bodyText, 'page body should not contain "Failed to load"').not.toMatch(
    /Failed to load|404 Not Found/i
  );

  if (skipA11y) return;

  const { violations } = await checkA11y(page, { exclude: axeExclude });
  const critical = violations.filter((v) => v.impact === 'critical');
  const nonCritical = violations.filter((v) => v.impact !== 'critical');

  expect(
    critical,
    `Critical a11y violations found:\n${formatA11yViolations(critical)}`
  ).toHaveLength(0);

  if (nonCritical.length > allowedNonCriticalViolations) {
    // Soft-fail with a detailed report so devs can see what's missing.
    throw new Error(
      `Non-critical a11y violations (${nonCritical.length}) exceed budget (${allowedNonCriticalViolations}):\n${formatA11yViolations(nonCritical)}`
    );
  }
}

/**
 * Assert that all `<a href>` elements inside the tool use portal-safe paths.
 * Portal-safe = relative (./, ../) or external (https?, mailto:, tel:) or fragment (#).
 * Absolute root paths like `href="/template-gallery"` are portal 404 risks (see TRK-104).
 *
 * Useful for catching TRK-104-style regressions in new tools during smoke.
 */
export async function assertNoAbsoluteRootHrefs(page: Page): Promise<void> {
  const badHrefs = await page.evaluate(() => {
    const anchors = Array.from(document.querySelectorAll<HTMLAnchorElement>('a[href]'));
    return anchors
      .map((a) => a.getAttribute('href') || '')
      .filter((href) => /^\/[a-zA-Z]/.test(href) && !href.startsWith('//'));
  });

  expect(
    badHrefs,
    `Hardcoded portal-absolute hrefs found (see TRK-104):\n${badHrefs.join('\n')}`
  ).toHaveLength(0);
}
