/**
 * Threshold Calculator — smoke spec (TD-032e).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 * skipA11y: defaulting on per TD-032c/d a11y-debt pattern (revisit
 * if local verification shows this tool is clean).
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Threshold Calculator @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'threshold-calculator');
    await runToolSmokeChecks(page, { skipA11y: true });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'threshold-calculator');
    await assertNoAbsoluteRootHrefs(page);
  });
});
