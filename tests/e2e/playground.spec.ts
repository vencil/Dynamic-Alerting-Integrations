/**
 * Playground — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads playground without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI contract: YAML editor region is visible
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Playground @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'playground');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Playground|Config|YAML|Alert/i,
    });
  });

  test('renders YAML editor region', async ({ page }) => {
    await loadPortalTool(page, 'playground');

    // Playground should present a YAML editing area and a preview/result panel.
    // The editor may be a textarea, code-mirror, or a labeled region.
    const editor = page.locator(
      'textarea, [role="textbox"], [aria-label*="YAML" i], [aria-label*="Config" i], [data-testid*="editor"]'
    );
    await expect(editor.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'playground');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('validate button or action is present', async ({ page }) => {
    await loadPortalTool(page, 'playground');

    // Playground should have a primary action button to validate/run the config.
    const action = page.locator(
      'button:text-matches("Validate|Run|Check|Apply|Submit", "i")'
    );
    await expect(action.first()).toBeVisible({ timeout: 10000 });
  });
});
