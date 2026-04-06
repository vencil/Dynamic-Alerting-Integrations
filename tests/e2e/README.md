# Playwright E2E Smoke Tests

Critical path smoke tests for the Dynamic Alerting Portal using Playwright.

## Tests Overview

5 critical path tests covering core portal functionality:

### 1. Portal Home (`portal-home.spec.ts`)
- Portal page loads and displays title
- Tool cards are rendered (minimum 10 tools)
- Journey phase sections exist (Deploy, Configure, Monitor, Troubleshoot)
- Language switcher is functional
- Responsive layout works correctly

### 2. Tenant Manager (`tenant-manager.spec.ts`)
- Tenant manager tool loads
- Filter inputs work correctly
- Metadata filtering (environment/domain) applies properly
- Result count reflects filter criteria
- Graceful degradation on data load errors

### 3. Group Management (`group-management.spec.ts`)
- Navigate to tenant-manager tool
- Group creation flow with validation
- Groups display in sidebar
- Member search and selection works
- Member list displays correctly
- Group selection updates details view

### 4. Authentication Flow (`auth-flow.spec.ts`)
- Portal loads in dev mode without auth redirect
- OAuth2-proxy redirect handling
- `/api/v1/me` endpoint returns user identity
- User email displayed in authenticated UI
- Unauthorized users see restricted UI disabled
- Session expiry handled gracefully
- Auth token preserved across navigation

### 5. Batch Operations (`batch-operations.spec.ts`)
- Select group from sidebar
- Batch operation menu appears
- Silent Mode selection works
- Confirmation dialog shown
- API call made with correct payload
- Success feedback displayed
- Errors handled gracefully

## Setup

### Prerequisites
- Node.js 18+ or 20+
- Python 3.8+ (for serving portal in dev mode)

### Installation

```bash
cd tests/e2e
npm install
npx playwright install chromium
```

## Running Tests Locally

```bash
# All tests
npm test

# Interactive UI mode (recommended for development)
npm run test:ui

# Headed mode (see browser)
npm run test:headed

# Debug mode (step through)
npm run test:debug

# Single test file
npm run test:portal
npm run test:tenant
npm run test:group
npm run test:auth
npm run test:batch

# Tests marked with @critical tag
npm run test:critical

# Verbose output
npm test -- --verbose
```

## Configuration

Configuration is in `playwright.config.ts`:

```typescript
// Base URL (default: http://localhost:8080)
baseURL: process.env.BASE_URL || 'http://localhost:8080'

// Browsers: chromium only for smoke tests (faster)
projects: [{ name: 'chromium' }]

// Timeouts
timeout: 30 * 1000        // Per test
expect.timeout: 5000      // Per assertion

// Auto-retry in CI
retries: process.env.CI ? 1 : 0

// Auto-start dev server (local only)
webServer: {
  command: 'npm run serve:portal',
  url: 'http://localhost:8080',
  reuseExistingServer: !process.env.CI
}
```

### Environment Variables

```bash
# Custom portal URL (default: http://localhost:8080)
BASE_URL=http://my-portal.dev npm test

# Enable debug logging
DEBUG=pw:api npm test

# Specific browser (chromium, firefox, webkit)
npx playwright test --project=chromium
```

## CI Integration

GitHub Actions workflow: `.github/workflows/playwright.yml`

Triggers:
- Push to main/develop branches
- Pull requests to main

Runs:
1. Install dependencies
2. Install Playwright browsers (chromium)
3. Start portal server on localhost:8080
4. Run smoke tests with 1 retry in CI
5. Upload test results artifact
6. Upload videos on failure

## Test Isolation & Mocking

Tests use Playwright's route interception to isolate from real backend:

```typescript
// Mock API response
await page.route('**/api/v1/me', async (route) => {
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ id: 'user-123', email: 'test@example.com' })
  });
});

// Block request
await page.route('**/api/v1/groups', async (route) => {
  await route.abort('blockedbyclient');
});
```

Mock data fixtures available in `fixtures/mock-data.ts`:
- `mockUser` - authenticated user
- `mockTenants` - tenant data
- `mockGroups` - group data with members
- `mockAlerts` - alert samples
- `mockBatchOperationResponse` - batch operation results

## Debugging

### UI Mode
Interactive mode with time-travel debugging:
```bash
npm run test:ui
```

### Headed Mode
See browser during test execution:
```bash
npm run test:headed
```

### Debug Mode
Step through code with DevTools:
```bash
npm run test:debug
```

### Screenshot on Failure
Screenshots automatically saved to `test-results/` on failure.

### HTML Report
After test run:
```bash
npx playwright show-report
```

## Troubleshooting

### Portal not starting
```bash
# Check if port 8080 is in use
lsof -i :8080

# Manually start portal
cd tests/e2e
npm run serve:portal
# In another terminal
npm test
```

### Tests timeout
- Increase `timeout` in `playwright.config.ts`
- Check network conditions: `page.route()` might be slow
- Verify portal is responding: `curl http://localhost:8080`

### Element not found
- Use `npm run test:ui` to inspect element selectors
- Check CSS selectors in test file
- Verify element is not hidden/disabled
- Add debug output: `console.log(await page.locator('selector').count())`

### Mock API not working
- Verify route pattern matches request URL
- Check request method (GET, POST, etc.)
- Log intercepted requests:
  ```typescript
  await page.route('**/api/**', route => {
    console.log('URL:', route.request().url());
    console.log('Method:', route.request().method());
  });
  ```

## Best Practices

1. **Use data-testid attributes** in portal components for reliable selectors:
   ```typescript
   // Preferred
   page.locator('[data-testid="group-item"]')

   // Less reliable
   page.locator('.group-item')
   ```

2. **Mock external APIs** to isolate tests:
   ```typescript
   await page.route('**/api/**', handleRequest);
   ```

3. **Use explicit waits** instead of arbitrary delays:
   ```typescript
   // Bad
   await page.waitForTimeout(2000);

   // Better
   await page.locator('[data-testid="loader"]').waitFor({ state: 'hidden' });
   ```

4. **Test user flows** not implementation details:
   ```typescript
   // Bad - testing internal state
   expect(await page.evaluate(() => store.getState())).toEqual(...)

   // Good - testing visible behavior
   expect(page.locator('[data-testid="result"]')).toBeVisible();
   ```

5. **Handle graceful degradation**:
   ```typescript
   // Some features may not exist depending on context
   const count = await page.locator('selector').count();
   if (count > 0) {
     // Test feature
   }
   ```

## Adding New Tests

1. Create new `.spec.ts` file in `tests/e2e/`
2. Import `{ test, expect }` from `@playwright/test`
3. Use `@critical` tag for smoke tests:
   ```typescript
   test.describe('Feature @critical', () => {
     test('should do something', async ({ page }) => {
       // test code
     });
   });
   ```
4. Add test script to `package.json` if needed
5. Update this README with test description

## Performance

Smoke tests target sub-30s execution:
- Chromium only (vs all browsers)
- Single worker in CI (vs parallel)
- No extra retries except CI (1 retry on failure)
- Screenshots/videos only on failure

Expected runtime: 15-25 seconds total

## Related Docs

- [Playwright Documentation](https://playwright.dev)
- [Testing Playbook](../../docs/internal/testing-playbook.md)
- [Portal Architecture](../../docs/architecture-and-design.md)
- [Getting Started - QA Role](../../docs/getting-started/for-qa.md)

## Support

For issues or questions:
1. Check Playwright docs: https://playwright.dev/docs/troubleshooting
2. Review test file comments
3. Check GitHub Actions logs in CI
4. Run `npm run test:ui` for interactive debugging
