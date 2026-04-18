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
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders YAML editor region', async ({ page }) => {
    await loadPortalTool(page, 'playground');

    // v2.7.0 calibration (§8.11.4): playground.jsx:633-636 renders a
    // <textarea aria-label={t('租戶 YAML 編輯器', 'Tenant YAML editor')}>.
    // getByRole('textbox', { name: /YAML/i }) matches both locales.
    const editor = page.getByRole('textbox', { name: /YAML/i });
    await expect(editor).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'playground');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('primary action (Export .yaml) is present', async ({ page }) => {
    await loadPortalTool(page, 'playground');

    // v2.7.0 calibration (§8.11.4): Playground validation is reactive
    // (runs on every keystroke / template change — see playground.jsx:331)
    // so there is no "Validate" / "Run" / "Apply" button. The primary
    // user-triggered action is "Export .yaml" (playground.jsx:581-585).
    // Secondary actions: Reset / Diff / Share Link. We assert Export
    // since it's the terminal user action of the workflow.
    const exportBtn = page.getByRole('button', { name: /Export|匯出/i });
    await expect(exportBtn).toBeVisible({ timeout: 10000 });
  });
});
