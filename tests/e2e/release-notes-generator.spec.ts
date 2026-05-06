/**
 * Release Notes Generator — smoke spec (TD-032d).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Release Notes Generator @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'release-notes-generator');
    await runToolSmokeChecks(page, { allowedNonCriticalViolations: 5 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'release-notes-generator');
    await assertNoAbsoluteRootHrefs(page);
  });
});
