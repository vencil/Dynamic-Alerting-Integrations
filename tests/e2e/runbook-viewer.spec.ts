/**
 * Runbook Viewer — smoke spec (TRK-232d).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Runbook Viewer @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'runbook-viewer');
    await runToolSmokeChecks(page, {
      allowedNonCriticalViolations: 0,
      expectedTitleMatch: /Runbook|檢視器/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'runbook-viewer');
    await assertNoAbsoluteRootHrefs(page);
  });
});
