import { defineConfig, devices } from '@playwright/test';

/**
 * Read environment variables from file.
 * https://github.com/motdotla/dotenv
 */
// require('dotenv').config();

/**
 * See https://playwright.dev/docs/test-configuration.
 *
 * The Portal (docs/interactive/index.html) fetches assets via relative paths
 * like "../assets/tool-registry.yaml". To resolve these correctly, the HTTP
 * server must serve from docs/ (not docs/interactive/), and baseURL must
 * point to the /interactive/ subdirectory.
 */
export default defineConfig({
  testDir: './',
  /* Run tests in files in parallel */
  fullyParallel: true,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 1 : 0,
  /* Opt out of parallel tests on CI. */
  workers: process.env.CI ? 1 : undefined,
  /* Reporter to use. See https://playwright.dev/docs/test-reporters */
  reporter: 'html',
  /* Shared settings for all the projects below. See https://playwright.dev/docs/api/class-testoptions. */
  use: {
    /* Base URL to use in actions like `await page.goto('./')`. */
    baseURL: process.env.BASE_URL || 'http://localhost:8080/interactive/',
    /* Collect trace when retrying the failed test. See https://playwright.dev/docs/trace-viewer */
    trace: 'on-first-retry',
    /* Screenshot on failure */
    screenshot: 'only-on-failure',
  },

  /* Configure projects for major browsers */
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
    // Disable other browsers for smoke tests (faster CI)
    // {
    //   name: 'firefox',
    //   use: { ...devices['Desktop Firefox'] },
    // },
    // {
    //   name: 'webkit',
    //   use: { ...devices['Desktop Safari'] },
    // },
  ],

  /* Run your local dev server before starting the tests */
  webServer: process.env.CI
    ? undefined
    : {
        command: 'npm run serve:portal',
        url: 'http://localhost:8080/interactive/',
        reuseExistingServer: !process.env.CI,
        timeout: 120 * 1000,
      },

  /* Global timeout */
  timeout: 30 * 1000,
  expect: {
    timeout: 5000,
  },
});
