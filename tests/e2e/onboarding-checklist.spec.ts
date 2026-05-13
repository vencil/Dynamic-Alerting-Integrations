/**
 * Onboarding Checklist Generator — smoke spec (TRK-232d).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Onboarding Checklist @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'onboarding-checklist');
    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Onboarding|上線檢查/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'onboarding-checklist');
    await assertNoAbsoluteRootHrefs(page);
  });
});
