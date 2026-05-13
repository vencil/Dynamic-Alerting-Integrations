/**
 * Platform Demo — smoke spec (TRK-232d).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Platform Demo @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'platform-demo');
    await runToolSmokeChecks(page, {
      allowedNonCriticalViolations: 1,
      expectedTitleMatch: /Platform Demo|平台展示/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'platform-demo');
    await assertNoAbsoluteRootHrefs(page);
  });
});
