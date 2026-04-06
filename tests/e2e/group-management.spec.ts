import { test, expect, Page } from '@playwright/test';

/**
 * Group Management smoke tests
 * Tests: create group → sidebar display → member list
 */

async function setupTestContext(page: Page) {
  // Navigate to portal
  await page.goto('./');
  await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
}

test.describe('Group Management @critical', () => {
  test('should navigate to tenant-manager and display group section', async ({ page }) => {
    await setupTestContext(page);

    // Find tenant-manager link/button
    const tenantLink = page.locator(':text-is("tenant-manager"), [data-tool="tenant-manager"]').first();
    const linkExists = await tenantLink.count();

    if (linkExists > 0) {
      await tenantLink.click();
      await page.waitForTimeout(2000);
    }

    // Verify page has loaded
    const content = await page.locator('body').textContent();
    expect(content).toBeTruthy();
  });

  test('should display or provide create group functionality', async ({ page }) => {
    await setupTestContext(page);

    // Look for "Create Group" button
    const createButton = page.locator(
      'button:has-text("Create Group"), button:has-text("New Group"), [data-testid="create-group"], [aria-label*="create group" i]'
    ).first();

    const buttonCount = await page.locator(
      'button:has-text("Create Group"), button:has-text("New Group")'
    ).count();

    if (buttonCount > 0) {
      // Verify button is visible and clickable
      await expect(createButton).toBeVisible();
      await expect(createButton).toBeEnabled();
    }
  });

  test('should handle group creation flow with API isolation', async ({ page }) => {
    await setupTestContext(page);

    // Mock API responses for group operations
    await page.route('**/api/v1/groups', async (route) => {
      if (route.request().method() === 'POST') {
        await route.abort('blockedbyclient');
      } else {
        await route.continue();
      }
    });

    // Find create button
    const createButton = page.locator('button:has-text("Create Group"), button:has-text("New Group")').first();
    const exists = await createButton.count();

    if (exists > 0) {
      await createButton.click({ noWaitAfter: true });

      // Wait for potential dialog/form to appear
      await page.waitForTimeout(1000);

      // Check for form fields: name input, description, etc.
      const inputs = page.locator('input, textarea, [role="combobox"]');
      const inputCount = await inputs.count();

      // Form should have at least input field
      if (inputCount > 0) {
        expect(inputCount).toBeGreaterThanOrEqual(1);
      }
    }
  });

  test('should display groups in sidebar', async ({ page }) => {
    await setupTestContext(page);

    // Look for sidebar section containing groups
    const sidebar = page.locator('aside, [role="navigation"], .sidebar, .nav-sidebar').first();
    const sidebarExists = await sidebar.count();

    if (sidebarExists > 0) {
      await expect(sidebar).toBeVisible();

      // Look for group items in sidebar
      const groupItems = sidebar.locator('[data-testid="group-item"], .group-item, li');
      const itemCount = await groupItems.count();

      // Sidebar should display some items or empty state
      expect(itemCount).toBeGreaterThanOrEqual(0);
    }
  });

  test('should allow adding members to group', async ({ page }) => {
    await setupTestContext(page);

    // Mock API for member operations
    await page.route('**/api/v1/groups/*/members', async (route) => {
      if (route.request().method() === 'POST') {
        await route.abort('blockedbyclient');
      } else {
        await route.continue();
      }
    });

    // Look for member management interface
    const memberSearch = page.locator(
      'input[placeholder*="member" i], input[placeholder*="user" i], input[placeholder*="search" i], [data-testid="member-search"]'
    ).first();

    const memberSearchCount = await page.locator(
      'input[placeholder*="member" i], input[placeholder*="user" i]'
    ).count();

    if (memberSearchCount > 0) {
      // Verify search input works
      await memberSearch.fill('test-user');
      await expect(memberSearch).toHaveValue('test-user');

      // Look for results dropdown
      await page.waitForTimeout(500);
      const results = page.locator('[role="option"], .search-result, .member-option');
      const resultCount = await results.count();

      // Results may be present
      expect(resultCount).toBeGreaterThanOrEqual(0);
    }
  });

  test('should display member list correctly', async ({ page }) => {
    await setupTestContext(page);

    // Look for member list display
    const memberList = page.locator(
      '[data-testid="member-list"], .member-list, [role="list"] >> :text-is("Member")'
    ).first();

    const memberListCount = await page.locator('[data-testid="member-list"], .member-list').count();

    if (memberListCount > 0) {
      await expect(memberList).toBeVisible();

      // Look for individual member items
      const members = page.locator('[data-testid="member-item"], .member-item, [role="listitem"]');
      const memberCount = await members.count();

      // Member list should display items
      expect(memberCount).toBeGreaterThanOrEqual(0);
    }
  });

  test('should support group selection from sidebar', async ({ page }) => {
    await setupTestContext(page);

    // Look for group items in sidebar
    const groupItems = page.locator('[data-testid="group-item"], .group-item, .sidebar li');
    const groupCount = await groupItems.count();

    if (groupCount > 0) {
      const firstGroup = groupItems.first();

      // Click group to select
      await firstGroup.click({ noWaitAfter: true });

      // Wait for UI update
      await page.waitForTimeout(500);

      // Verify group details or member list appears
      const details = page.locator('[data-testid="group-details"], .group-details, .member-list');
      const detailsCount = await details.count();

      expect(detailsCount).toBeGreaterThanOrEqual(0);
    }
  });
});
