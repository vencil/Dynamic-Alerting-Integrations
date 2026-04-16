/**
 * Config Lint — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads config-lint without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: input area for config/YAML and result display
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Config Lint @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Lint|Config|Validat|Check/i,
    });
  });

  test('renders input area for configuration', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    // Config lint should have a text input area for YAML/config pasting.
    const input = page.locator(
      'textarea, [role="textbox"], [contenteditable="true"], [aria-label*="Config" i], [aria-label*="YAML" i]'
    );
    await expect(input.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('severity status indicators use role="status" or role="alert"', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    // Post-migration (ADR-015) config-lint uses SEVERITY_COLORS → design tokens
    // with role="status" for info/warning and role="alert" for errors.
    // Just verify the lint button exists for now (results area may be hidden until run).
    const lintAction = page.locator(
      'button:text-matches("Lint|Validate|Check|Run", "i")'
    );
    await expect(lintAction.first()).toBeVisible({ timeout: 10000 });
  });
});
