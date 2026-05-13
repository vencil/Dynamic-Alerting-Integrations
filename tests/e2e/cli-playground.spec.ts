/**
 * da-tools CLI Playground — smoke spec (TRK-232c).
 *
 * Minimum gate: tool loads via jsx-loader (dist bundle resolves),
 * page body has no "Failed to load" / 404, axe sees 0 critical
 * accessibility violations, hrefs are portal-safe (TRK-104).
 *
 * Add interaction tests inside this describe as the tool's flow
 * stabilizes — don't open a second spec file per tool.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('CLI Playground @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'cli-playground');
    await runToolSmokeChecks(page, {
      allowedNonCriticalViolations: 2,
      expectedTitleMatch: /CLI Playground|CLI 遊樂場/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'cli-playground');
    await assertNoAbsoluteRootHrefs(page);
  });
});
