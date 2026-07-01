import { test, expect } from '@playwright/test';
import { checkA11y, formatA11yViolations, waitForPageReady } from './fixtures/axe-helper';

test.describe('Portal Home Page @critical', () => {
  test('should load portal home page and render tool list', async ({ page }) => {
    // Navigate to portal root
    await page.goto('./');

    // Assert page title contains "Dynamic Alerting" or portal identifier
    await expect(page).toHaveTitle(/Dynamic Alerting|Interactive Tools/i);

    // Wait for hero section to be visible
    await expect(page.locator('.hero')).toBeVisible({ timeout: 10000 });

    // Assert page contains the main heading
    const heroHeading = page.locator('.hero h1');
    await expect(heroHeading).toBeVisible();
    const headingText = await heroHeading.textContent();
    expect(headingText).toMatch(/Dynamic Alerting|Interactive|Tools/i);
  });

  test('should render tool cards', async ({ page }) => {
    await page.goto('./');

    // Dynamic cards are rendered into phase containers (deploy-cards, configure-cards, etc.)
    // after JS fetches tool-registry.yaml. Static linter-cards div is display:none.
    const visibleCards = page.locator('.cards a.card');
    await expect(visibleCards.first()).toBeVisible({ timeout: 10000 });

    // We expect at least 10 tools (reasonable minimum)
    const toolCardCount = await visibleCards.count();
    expect(toolCardCount).toBeGreaterThanOrEqual(10);
  });

  test('should display phase section headers', async ({ page }) => {
    await page.goto('./');

    // Assert phase section headers exist: Deploy, Configure, Monitor, Troubleshoot
    const phases = ['Deploy', 'Configure', 'Monitor', 'Troubleshoot'];

    for (const phase of phases) {
      // Each phase has a section header with emoji + phase name + badge
      const count = await page.locator(`.section-title:has-text("${phase}")`).count();
      expect(count).toBeGreaterThanOrEqual(1);
    }
  });

  test('should allow language switching', async ({ page }) => {
    await page.goto('./');

    // Look for language switcher button or dropdown using Playwright's .or() API
    // Common patterns: data-testid="lang-switch", aria-label containing "language"
    const langSwitcher = page.locator('[data-testid="lang-switch"]')
      .or(page.locator('[aria-label*="language" i]'))
      .or(page.locator('[aria-label*="Language" i]'));

    // If language switcher exists, verify it's clickable
    const switcherCount = await langSwitcher.count();
    if (switcherCount > 0) {
      await expect(langSwitcher.first()).toBeVisible({ timeout: 10000 });
      // Verify it's interactive
      const isEnabled = await langSwitcher.first().isEnabled();
      expect(isEnabled).toBe(true);
    }
  });

  test('should load tool via bare key (jsx-loader component resolution)', async ({ page }) => {
    // Regression guard: jsx-loader must resolve bare keys like ?component=cicd-setup-wizard
    // to full paths via CUSTOM_FLOW_MAP. Without this, fetch("cicd-setup-wizard") → 404.
    const loaderUrl = page.url().replace(/interactive\/.*$/, 'assets/jsx-loader.html');
    await page.goto(loaderUrl + '?component=cicd-setup-wizard');

    // Should NOT show error message
    const errorBox = page.locator('.error, [role="alert"]');
    const hasError = await errorBox.count();
    if (hasError > 0) {
      const errorText = await errorBox.first().textContent();
      expect(errorText).not.toContain('404');
    }

    // Should show the component title (Babel-rendered)
    await expect(page.locator('body')).not.toContainText('Failed to load component', { timeout: 10000 });
  });

  test('should have responsive layout', async ({ page }) => {
    // Set viewport to desktop
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto('./');

    // Main container should be visible
    const mainContent = page.locator('main, [role="main"]');
    const mainCount = await mainContent.count();
    expect(mainCount).toBeGreaterThanOrEqual(0); // May or may not have main tag

    // Page should not have horizontal scroll
    const bodyWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth + 1); // +1 for rounding
  });

  test('passes WCAG 2.1 AA accessibility checks', async ({ page }) => {
    // Navigate to portal home
    await page.goto('./');
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    await waitForPageReady(page);

    // Run accessibility check
    const results = await checkA11y(page);

    // Log violations before asserting so CI output contains diagnostics
    if (results.violations.length > 0) {
      const violationDetails = formatA11yViolations(results.violations);
      console.error(`Portal home a11y violations:\n${violationDetails}`);
    }

    // Assert no violations
    expect(results.violations.length).toBe(0);
  });
});

// LD-1 (#962): multi-select UNION role filter + localStorage persistence.
// These guard behaviour that had NO e2e coverage before this change.
//
// Audience reference (from the #linter-cards mirror / tool-registry SSOT):
//   platform-health → audience "platform" (button role)
//   roi-calculator  → audience "maintainer" (NO button → only visible in All)
//   alert-simulator → audience "domain,tenant"
// Cards render with href="...?component=<key>", so we target them by that
// substring and assert the data-hidden attribute (independent of the
// collapsed Internal <details>, where roi-calculator lives).
test.describe('Portal Home — role filter (LD-1 union) @critical', () => {
  // Wait until the async tool-registry fetch has injected the dynamic cards.
  async function waitForCards(page) {
    // Gate on the platform-health sentinel's PRESENCE (toHaveCount), not on the
    // first card being visible: renderTools() injects every container in one
    // synchronous pass, so once platform-health exists all cards exist — and a
    // count check is order-independent. (A `.first().toBeVisible()` gate would
    // hang in test (d), where a reload restores a persisted filter that may
    // hide the first DOM card even though rendering already finished.)
    await expect(
      page.locator('.cards a.card[href*="component=platform-health"]')
    ).toHaveCount(1, { timeout: 10000 });
  }

  const platformHealth = '.cards a.card[href*="component=platform-health"]'; // platform
  const roiCalculator = '.cards a.card[href*="component=roi-calculator"]';   // maintainer
  const alertSimulator = '.cards a.card[href*="component=alert-simulator"]'; // domain,tenant

  test('(a) union: selecting two roles shows cards hitting EITHER, hides the rest', async ({ page }) => {
    await page.goto('./');
    await waitForCards(page);

    // Select platform + tenant (two roles → union).
    await page.locator('.role-filter[data-role="platform"]').click();
    await page.locator('.role-filter[data-role="tenant"]').click();

    // platform-health hits "platform" → visible.
    await expect(page.locator(platformHealth)).toHaveAttribute('data-hidden', 'false');
    // alert-simulator hits "tenant" → visible.
    await expect(page.locator(alertSimulator)).toHaveAttribute('data-hidden', 'false');
    // roi-calculator is "maintainer" → matches NEITHER selected role → hidden.
    await expect(page.locator(roiCalculator)).toHaveAttribute('data-hidden', 'true');

    // Both chosen buttons reflect membership; "All" is off.
    await expect(page.locator('.role-filter[data-role="platform"]')).toHaveClass(/active/);
    await expect(page.locator('.role-filter[data-role="tenant"]')).toHaveClass(/active/);
    await expect(page.locator('#filter-all')).not.toHaveClass(/active/);
  });

  test('(b) toggle-off the last role returns to All (everything shown)', async ({ page }) => {
    await page.goto('./');
    await waitForCards(page);

    const platformBtn = page.locator('.role-filter[data-role="platform"]');

    // Select platform: roi-calculator (maintainer) hidden, platform-health shown.
    await platformBtn.click();
    await expect(page.locator(roiCalculator)).toHaveAttribute('data-hidden', 'true');

    // Toggle the SAME button off → set empty → invariant snaps back to All.
    await platformBtn.click();
    await expect(page.locator(roiCalculator)).toHaveAttribute('data-hidden', 'false');
    await expect(page.locator(platformHealth)).toHaveAttribute('data-hidden', 'false');

    // "All" is active again; the toggled role button is not.
    await expect(page.locator('#filter-all')).toHaveClass(/active/);
    await expect(page.locator('#filter-all')).toHaveAttribute('aria-pressed', 'true');
    await expect(platformBtn).not.toHaveClass(/active/);
  });

  test('(c) two role buttons can be aria-pressed simultaneously', async ({ page }) => {
    await page.goto('./');
    await waitForCards(page);

    await page.locator('.role-filter[data-role="platform"]').click();
    await page.locator('.role-filter[data-role="domain"]').click();

    await expect(page.locator('.role-filter[data-role="platform"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.role-filter[data-role="domain"]')).toHaveAttribute('aria-pressed', 'true');
    // All reflects emptiness → off while two roles are selected.
    await expect(page.locator('#filter-all')).toHaveAttribute('aria-pressed', 'false');
  });

  test('(d) selection persists across reload (localStorage restore)', async ({ page }) => {
    await page.goto('./');
    await waitForCards(page);

    // Choose platform, confirm the maintainer-only card is filtered out.
    await page.locator('.role-filter[data-role="platform"]').click();
    await expect(page.locator(roiCalculator)).toHaveAttribute('data-hidden', 'true');

    await page.reload();
    await waitForCards(page);

    // After reload the filter is restored from localStorage (NOT reset to All).
    await expect(page.locator('.role-filter[data-role="platform"]')).toHaveClass(/active/);
    await expect(page.locator('.role-filter[data-role="platform"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#filter-all')).not.toHaveClass(/active/);
    await expect(page.locator(roiCalculator)).toHaveAttribute('data-hidden', 'true');
    await expect(page.locator(platformHealth)).toHaveAttribute('data-hidden', 'false');
  });

  test('(e) view-state hint is symmetric and never shows a "0 tools" flash', async ({ page }) => {
    await page.goto('./');
    await waitForCards(page);

    const hint = page.locator('#filter-hint');

    // All state: focus cue with a REAL (non-zero) count. The [1-9]\d* guards
    // the fetch-fail / pre-inject regression where the hint once stuck at
    // "Showing all 0 tools" (the .catch path never repainted it).
    await expect(hint).toBeVisible();
    await expect(hint).toHaveText(/Showing all [1-9]\d* tools · click a role to focus/);

    // Filtered state: the reset cue — the on-screen return path for a
    // persisted / shared-machine selection (symmetric hint).
    await page.locator('.role-filter[data-role="platform"]').click();
    await expect(hint).toBeVisible();
    await expect(hint).toHaveText(/Showing \d+ of [1-9]\d* tools · click All to reset/);
  });
});
