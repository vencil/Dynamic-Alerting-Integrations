/**
 * Test helper utilities for E2E tests
 * Common operations, waits, and assertions
 */

import { Page, expect } from '@playwright/test';

/**
 * Wait for portal to be ready
 */
export async function waitForPortalReady(page: Page, timeout = 10000) {
  await page.goto('./');
  await page.waitForLoadState('networkidle', { timeout }).catch(() => {
    // Graceful degradation - portal may load without network idle
  });
}

/**
 * Mock API endpoints with response
 */
export async function mockApiEndpoint(
  page: Page,
  pattern: string,
  response: any,
  status = 200
) {
  await page.route(pattern, async (route) => {
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(response),
    });
  });
}

/**
 * Mock API error response
 */
export async function mockApiError(
  page: Page,
  pattern: string,
  errorMessage: string,
  status = 400
) {
  await page.route(pattern, async (route) => {
    await route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify({ error: errorMessage }),
    });
  });
}

/**
 * Wait for element with flexible retry
 */
export async function waitForElement(
  page: Page,
  selector: string,
  timeout = 5000
) {
  try {
    await page.locator(selector).waitFor({ timeout });
    return true;
  } catch {
    return false;
  }
}

/**
 * Find tool card by name
 */
export async function findToolCard(page: Page, toolName: string) {
  const card = page.locator(
    `:text-is("${toolName}"), [data-tool="${toolName}"], [data-testid="tool-${toolName}"]`
  ).first();

  const exists = await card.count();
  return exists > 0 ? card : null;
}

/**
 * Click tool card and wait for load
 */
export async function clickToolAndWait(
  page: Page,
  toolName: string,
  timeout = 5000
) {
  const card = await findToolCard(page, toolName);
  if (!card) return false;

  await card.click();
  await page.waitForTimeout(Math.min(timeout, 2000)); // Wait up to 2s for load
  return true;
}

/**
 * Mock authentication flow
 */
export async function mockAuthFlow(page: Page, user: any) {
  await mockApiEndpoint(page, '**/api/v1/me', user);
  return user;
}

/**
 * Mock batch operation
 */
export async function mockBatchOperation(
  page: Page,
  operation: string,
  affectedCount = 5
) {
  const response = {
    success: true,
    operationId: `op-${Date.now()}`,
    operation,
    affected: affectedCount,
    message: `${operation} applied to ${affectedCount} items`,
  };

  await mockApiEndpoint(page, '**/api/v1/**/operations', response);
  return response;
}

/**
 * Assert element is visible and enabled
 */
export async function assertElementActive(
  page: Page,
  selector: string
) {
  const element = page.locator(selector);
  await expect(element).toBeVisible();
  await expect(element).toBeEnabled();
}

/**
 * Assert element is disabled or hidden
 */
export async function assertElementInactive(
  page: Page,
  selector: string
) {
  const element = page.locator(selector);
  const isVisible = await element.isVisible().catch(() => false);
  const isEnabled = await element.isEnabled().catch(() => false);

  // Element should either be hidden or disabled
  if (isVisible) {
    expect(isEnabled).toBe(false);
  }
}

/**
 * Get count of elements matching selector
 */
export async function countElements(page: Page, selector: string) {
  return await page.locator(selector).count();
}

/**
 * Check if any elements match selector
 */
export async function elementExists(page: Page, selector: string) {
  return (await countElements(page, selector)) > 0;
}

/**
 * Fill form field and verify
 */
export async function fillFormField(
  page: Page,
  selector: string,
  value: string
) {
  const field = page.locator(selector);
  await field.fill(value);
  await expect(field).toHaveValue(value);
}

/**
 * Click button and wait for navigation/loading
 */
export async function clickButton(
  page: Page,
  selector: string,
  waitFor?: string
) {
  const button = page.locator(selector);
  await button.click({ noWaitAfter: !waitFor });

  if (waitFor) {
    await page.locator(waitFor).waitFor({ state: 'visible' });
  } else {
    await page.waitForTimeout(500); // Brief wait for UI update
  }
}

/**
 * Verify success notification appears
 */
export async function verifySuccess(
  page: Page,
  timeout = 5000
) {
  const successMessage = page.locator(
    '[role="status"], .toast.success, .notification.success, :text-i("success"), :text-i("applied")'
  ).first();

  try {
    await successMessage.waitFor({ state: 'visible', timeout });
    return true;
  } catch {
    return false;
  }
}

/**
 * Verify error notification appears
 */
export async function verifyError(
  page: Page,
  timeout = 5000
) {
  const errorMessage = page.locator(
    '[role="alert"], .toast.error, .notification.error, :text-i("error"), :text-i("failed")'
  ).first();

  try {
    await errorMessage.waitFor({ state: 'visible', timeout });
    return true;
  } catch {
    return false;
  }
}

/**
 * Get page content as text
 */
export async function getPageText(page: Page) {
  return await page.locator('body').textContent();
}

/**
 * Verify page title contains text
 */
export async function assertPageTitle(page: Page, text: string) {
  const title = await page.title();
  expect(title).toContain(text);
}

/**
 * Capture screenshot with timestamp
 */
export async function captureScreenshot(
  page: Page,
  name: string
) {
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  await page.screenshot({ path: `./test-results/${name}-${timestamp}.png` });
}

/**
 * Get local storage value
 */
export async function getLocalStorage(page: Page, key: string) {
  return await page.evaluate((k) => localStorage.getItem(k), key);
}

/**
 * Set local storage value
 */
export async function setLocalStorage(page: Page, key: string, value: string) {
  await page.evaluate(
    ({ k, v }) => localStorage.setItem(k, v),
    { k: key, v: value }
  );
}

/**
 * Clear local storage
 */
export async function clearLocalStorage(page: Page) {
  await page.evaluate(() => localStorage.clear());
}

/**
 * Check network request was made
 */
export async function captureNetworkRequest(
  page: Page,
  pattern: string
) {
  let capturedRequest: any = null;

  await page.route(pattern, async (route) => {
    capturedRequest = {
      url: route.request().url(),
      method: route.request().method(),
      headers: route.request().headers(),
      postData: route.request().postDataJSON().catch(() => null),
    };
    await route.continue();
  });

  return async () => capturedRequest;
}
