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
  /* `_*` files and directories are scratch probes (dev-rules §11);
   * eslint.config.mjs ignores the same two patterns, so the runner must not
   * collect what the linter never read. */
  testIgnore: ['**/_*', '**/_*/**'],
  /* Run tests in files in parallel */
  fullyParallel: true,
  /* Fail the build on CI if you accidentally left test.only in the source code. */
  forbidOnly: !!process.env.CI,
  /* Retry on CI only */
  retries: process.env.CI ? 1 : 0,
  /* CI workers default. Local benchmarks at workers=4 + ThreadingHTTP
   * server: 1.5 min vs workers=1 = 5.2 min (~3.5x). The CI workflow
   * (.github/workflows/ci.yml, smoke-tests-run) passes `--workers=4` explicitly,
   * which overrides this; aligning the default here so that a future
   * workflow drop of the flag (or someone setting CI=true locally) lands
   * on the actually-fast configuration instead of silently reverting to
   * single-worker.
   */
  workers: process.env.CI ? 4 : undefined,
  /* The ONE reporter list. parked-tests fails the run when a skipped / fixme /
   * fail-expected test names no issue (#1449). A `--reporter` flag replaces
   * this list and drops that check, so CI passes none (pinned by
   * tests/lint/test_e2e_spec_lint.py). */
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never' }], ['./reporters/parked-tests.ts']]
    : [['html'], ['./reporters/parked-tests.ts']],
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
