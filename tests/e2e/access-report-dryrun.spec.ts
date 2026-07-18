/**
 * Access Report Dry-Run — smoke spec (LD-6 P7c).
 *
 * Minimum gate: tool loads via jsx-loader (dist bundle resolves),
 * page body has no "Failed to load" / 404, axe sees 0 critical
 * accessibility violations, hrefs are portal-safe (TRK-104).
 *
 * The dry-run tool renders backend-free on load: with no tenant API
 * reachable, useTenantData falls through to demo data and the tenant
 * dropdown renders DISABLED with a reason (the gate is part of the
 * default view), so the smoke checks exercise a real render without a
 * live tenant-api. Interaction (select tenant → Run → diff) is covered
 * by the Vitest render spec; add richer flows here as the tool
 * stabilizes — don't open a second spec file per tool.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Access Report Dry-Run @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'access-report-dryrun');
    await runToolSmokeChecks(page, {
      allowedNonCriticalViolations: 1,
      // jsx-loader derives the <title> from the tool KEY (access-report-dryrun →
      // "Access Report Dryrun", :467-469), not the registry title — so "Dryrun"
      // is one segment, no hyphen. The in-page H1 is the correct "Dry-Run".
      expectedTitleMatch: /Access Report Dry-?run/i,
    });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'access-report-dryrun');
    await assertNoAbsoluteRootHrefs(page);
  });
});
