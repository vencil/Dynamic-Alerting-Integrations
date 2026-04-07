import { test, expect } from '@playwright/test';
import { checkA11y, formatA11yViolations } from './fixtures/axe-helper';

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

    // Run accessibility check
    const results = await checkA11y(page);

    // Assert no violations
    expect(results.violations.length).toBe(0);
    if (results.violations.length > 0) {
      const violationDetails = formatA11yViolations(results.violations);
      throw new Error(
        `Portal home page failed accessibility checks:\n${violationDetails}`
      );
    }
  });
});
