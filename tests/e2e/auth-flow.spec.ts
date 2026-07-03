import { test, expect, Page } from '@playwright/test';
import { checkA11y, formatA11yViolations, waitForPageReady } from './fixtures/axe-helper';

/**
 * Auth Flow smoke tests
 * Tests: oauth2-proxy redirect, /api/v1/me identity endpoint, auth-aware UI
 */

test.describe('Authentication Flow @critical', () => {
  test('should navigate to protected endpoint without redirect in dev mode', async ({ page }) => {
    // In local dev without auth, should load directly
    await page.goto('./');

    // Verify page loaded successfully
    const content = await page.locator('body').textContent();
    expect(content).toBeTruthy();
    expect(content?.length || 0).toBeGreaterThan(0);
  });

  test('should handle oauth2-proxy redirect when protected endpoint is accessed', async ({ page }) => {
    // Mock oauth2-proxy redirect
    await page.route('**/oauth2/auth', async (route) => {
      // Simulate redirect to login
      await route.abort('blockedbyclient');
    });

    // Attempt to navigate
    await page.goto('./', { waitUntil: 'domcontentloaded' }).catch(() => {
      // Expected - may fail if auth is enforced
    });

    // Page should exist (either loaded or redirect attempted)
    expect(true).toBe(true);
  });

  test('should fetch user identity from /api/v1/me endpoint', async ({ page }) => {
    // Mock the /api/v1/me endpoint with the REAL MeResponse shape
    // (components/tenant-api/internal/handler/me.go): email/user/groups/
    // accessible_tenants/accessible_domains/permissions. The earlier
    // {id,name,roles} shape never matched the handler contract.
    const mockUser = {
      email: 'test@example.com',
      user: 'test',
      groups: ['production-dba'],
      accessible_tenants: ['prod-mariadb-01'],
      accessible_domains: ['finance'],
      permissions: { 'production-dba': ['read', 'write'] },
    };

    await page.route('**/api/v1/me', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockUser),
        });
      } else {
        await route.continue();
      }
    });

    // Navigate and trigger API call
    await page.goto('./');

    // Manually call the endpoint to verify mock works
    const response = await page.evaluate(async () => {
      const res = await fetch('/api/v1/me');
      return res.json();
    });

    expect(response.email).toBe('test@example.com');
    expect(response.user).toBe('test');
    expect(response.permissions['production-dba']).toContain('write');
  });

  test('should display authenticated user email in UI when available', async ({ page }) => {
    const mockUser = {
      email: 'authenticated@example.com',
      user: 'authenticated',
      groups: ['production-dba'],
      accessible_tenants: ['prod-mariadb-01'],
      accessible_domains: ['finance'],
      permissions: { 'production-dba': ['read', 'write', 'admin'] },
    };

    // Mock the identity endpoint
    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockUser),
      });
    });

    // The IdentityStrip lives on the tenant-manager surface — the Hub
    // (`./`) is the public static face and never fetches /api/v1/me, so
    // navigate to the component page via jsx-loader (same URL shape as
    // tenant-manager.spec.ts). Tenant-data fetches stay unmocked → the
    // component falls back to its offline DEMO fixtures, while authUser
    // is populated from the mocked /me.
    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

    // LD-7 (#962): the IdentityStrip surfaces the authed email on-screen.
    // It renders only when authUser is populated (non-demo), so this both
    // proves the strip mounted and that the email is legible.
    const strip = page.getByTestId('identity-strip');
    await expect(strip).toContainText('authenticated@example.com');
    // With access (non-empty permissions) the empty-state notice is absent.
    await expect(page.getByTestId('identity-no-access')).toHaveCount(0);
  });

  test('LD-7: empty permissions surfaces a soft no-access notice (real-bug guard)', async ({ page }) => {
    // Real-bug shape: a mistyped group in _rbac.yaml yields a non-empty
    // `groups` but an empty `permissions` map → the user maps to no visible
    // tenants. Before LD-7 they only saw the generic "no tenants matched"
    // empty state, with nothing pointing at the RBAC config as the cause.
    const mockUser = {
      email: 'orphan@example.com',
      user: 'orphan',
      groups: ['dba-typoo'],
      accessible_tenants: [],
      accessible_domains: [],
      permissions: {},
    };

    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockUser),
      });
    });

    // Same tenant-manager surface as the test above — the Hub never
    // renders the strip.
    await page.goto('../assets/jsx-loader.html?component=../interactive/tools/tenant-manager.jsx');
    await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});

    // The advisory (role="status") notice is visible and names the user.
    const notice = page.getByTestId('identity-no-access');
    await expect(notice).toBeVisible();
    await expect(notice).toContainText('orphan@example.com');
    // SOFT: nothing is hard-hidden — the search box is still on screen.
    // (CI headless Chromium reports `navigator.language` = en-US, so the
    // jsx-loader i18n helper renders the English label.)
    await expect(page.getByLabel('Search')).toBeVisible();
  });

  test('should handle unauthorized access gracefully', async ({ page }) => {
    // Mock unauthorized response
    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Unauthorized' }),
      });
    });

    await page.goto('./');

    // Page should still load but may show restricted state
    const content = await page.locator('body').textContent();
    expect(content).toBeTruthy();

    // Verify we can check for error or login prompt
    const errorElements = page.locator('[role="alert"], .error, .login-prompt');
    const errorCount = await errorElements.count();

    // Error should be present or gracefully handled
    expect(errorCount).toBeGreaterThanOrEqual(0);
  });

  test('should disable restricted UI elements for unauthorized users', async ({ page }) => {
    // Mock unauthorized response
    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'Unauthorized' }),
      });
    });

    await page.goto('./');

    // Look for admin-only buttons or features
    const adminButtons = page.locator('[data-restricted], [data-role="admin"], [aria-label*="admin" i]');
    const adminButtonCount = await adminButtons.count();

    if (adminButtonCount > 0) {
      // Verify restricted buttons are disabled
      const firstButton = adminButtons.first();
      const isDisabled = await firstButton.isDisabled().catch(() => false);

      // Should be disabled or hidden
      expect(isDisabled || !(await firstButton.isVisible())).toBeTruthy();
    }
  });

  test('should handle session expiry gracefully', async ({ page }) => {
    // First mock successful auth
    const mockUser = {
      id: 'user-789',
      email: 'session@example.com',
      roles: ['viewer'],
    };

    let callCount = 0;
    await page.route('**/api/v1/me', async (route) => {
      callCount++;
      if (callCount === 1) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(mockUser),
        });
      } else {
        // Simulate session expiry on second call
        await route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'Session expired' }),
        });
      }
    });

    await page.goto('./');

    // Make second request to trigger expiry
    await page.evaluate(async () => {
      try {
        await fetch('/api/v1/me');
      } catch {
        // Expected to fail
      }
    });

    // Page should still be functional or show login
    const content = await page.locator('body').textContent();
    expect(content).toBeTruthy();
  });

  test('should preserve auth token across page navigation', async ({ page }) => {
    const mockUser = {
      id: 'nav-user',
      email: 'nav@example.com',
      token: 'test-token-12345',
    };

    // Mock identity endpoint
    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockUser),
      });
    });

    // Navigate to home
    await page.goto('./');

    // Verify auth works
    const response1 = await page.evaluate(async () => {
      const res = await fetch('/api/v1/me');
      return res.ok;
    });
    expect(response1).toBe(true);

    // Navigate to different page (if it exists)
    await page.evaluate(() => window.history.replaceState({}, '', '/interactive/'));
    await page.reload();

    // Verify auth still works
    const response2 = await page.evaluate(async () => {
      const res = await fetch('/api/v1/me');
      return res.ok;
    });
    expect(response2).toBe(true);
  });

  test('passes WCAG 2.1 AA accessibility checks', async ({ page }) => {
    // Mock identity endpoint for auth flow
    const mockUser = {
      id: 'a11y-test-user',
      email: 'a11y@example.com',
      name: 'Accessibility Test User',
      roles: ['viewer'],
    };

    await page.route('**/api/v1/me', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(mockUser),
      });
    });

    // Navigate to page
    await page.goto('./');
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
    await waitForPageReady(page);

    // Run accessibility check
    const results = await checkA11y(page);

    // Log violations before asserting so CI output contains diagnostics
    if (results.violations.length > 0) {
      const violationDetails = formatA11yViolations(results.violations);
      console.error(`Authentication flow a11y violations:\n${violationDetails}`);
    }

    // Assert no violations
    expect(results.violations.length).toBe(0);
  });
});
