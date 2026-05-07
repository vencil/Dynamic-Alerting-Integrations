/**
 * Alert Timeline Replay — smoke spec (TD-032c).
 *
 * Minimum gate: tool loads via jsx-loader (dist bundle resolves),
 * page body has no "Failed to load" / 404, axe sees 0 critical
 * accessibility violations, hrefs are portal-safe (REG-004).
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

test.describe('Alert Timeline Replay @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'alert-timeline');
    await runToolSmokeChecks(page, {
      allowedNonCriticalViolations: 0,
      expectedTitleMatch: /Alert Timeline|告警時間軸/i,
    });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'alert-timeline');
    await assertNoAbsoluteRootHrefs(page);
  });
});
