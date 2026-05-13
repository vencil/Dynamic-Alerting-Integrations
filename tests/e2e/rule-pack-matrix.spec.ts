/**
 * Rule Pack Matrix — smoke spec (TRK-232e).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 * skipA11y: defaulting on per TRK-232c/d a11y-debt pattern (revisit
 * if local verification shows this tool is clean).
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Rule Pack Matrix @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'rule-pack-matrix');
    await runToolSmokeChecks(page, { allowedNonCriticalViolations: 1 });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'rule-pack-matrix');
    await assertNoAbsoluteRootHrefs(page);
  });
});
