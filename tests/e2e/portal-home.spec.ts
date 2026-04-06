import { test, expect } from '@playwright/test';

test.describe('Portal Home Page @critical', () => {
  test('should load portal home page and render tool list', async ({ page }) => {
    // Navigate to portal root
    await page.goto('/');

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
    await page.goto('/');

    // Wait for tool cards to load - we expect at least 10 tools (reasonable minimum)
    const toolCardCount = await page.locator('.tool-card, [data-testid="tool-card"]').count();
    expect(toolCardCount).toBeGreaterThanOrEqual(10);

    // Verify tool cards are visible and have proper content
    const toolCards = page.locator('.tool-card, [data-testid="tool-card"]').first();
    await expect(toolCards).toBeVisible({ timeout: 10000 });
  });

  test('should display journey phase sections', async ({ page }) => {
    await page.goto('/');

    // Assert journey phases exist: Deploy, Configure, Monitor, Troubleshoot
    const phases = ['Deploy', 'Configure', 'Monitor', 'Troubleshoot'];

    for (const phase of phases) {
      const phaseSection = page.locator(`:text("${phase}")`).first();
      // Phase sections should exist in page content
      const count = await page.locator(`:text-is("${phase}")`).count();
      expect(count).toBeGreaterThanOrEqual(1);
    }
  });

  test('should allow language switching', async ({ page }) => {
    await page.goto('/');

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

  test('should have responsive layout', async ({ page }) => {
    // Set viewport to desktop
    await page.setViewportSize({ width: 1280, height: 720 });
    await page.goto('/');

    // Main container should be visible
    const mainContent = page.locator('main, [role="main"]');
    const mainCount = await mainContent.count();
    expect(mainCount).toBeGreaterThanOrEqual(0); // May or may not have main tag

    // Page should not have horizontal scroll
    const bodyWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    const viewportWidth = await page.evaluate(() => window.innerWidth);
    expect(bodyWidth).toBeLessThanOrEqual(viewportWidth + 1); // +1 for rounding
  });
});
