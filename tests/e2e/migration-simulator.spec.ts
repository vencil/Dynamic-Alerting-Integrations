/**
 * Migration Dry-Run Simulator — smoke spec (TRK-232d).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Migration Dry-Run Simulator @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'migration-simulator');
    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Migration|遷移模擬器/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'migration-simulator');
    await assertNoAbsoluteRootHrefs(page);
  });
});
