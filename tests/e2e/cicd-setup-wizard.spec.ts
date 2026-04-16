/**
 * CI/CD Setup Wizard — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads cicd-setup-wizard without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: CI/CD provider selection visible
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('CI/CD Setup Wizard @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'cicd-setup-wizard');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /CI\/?CD|Pipeline|Setup|Deploy|Integration/i,
    });
  });

  test('renders CI/CD provider selection', async ({ page }) => {
    await loadPortalTool(page, 'cicd-setup-wizard');

    // Should show provider options like GitHub Actions, GitLab CI, ArgoCD.
    const providerEl = page.locator(
      ':text-matches("GitHub|GitLab|Argo|Jenkins|Pipeline|Actions", "i")'
    );
    await expect(providerEl.first()).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'cicd-setup-wizard');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('YAML output or config preview section exists', async ({ page }) => {
    await loadPortalTool(page, 'cicd-setup-wizard');

    // CI/CD wizard generates pipeline YAML; there should be a code/config output area.
    const output = page.locator(
      'pre, code, [role="region"][aria-label*="output" i], [role="region"][aria-label*="config" i], textarea[readonly], :text-matches("pipeline|workflow|stage", "i")'
    );
    await expect(output.first()).toBeVisible({ timeout: 10000 });
  });
});
