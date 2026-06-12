/**
 * Self-Service Portal — smoke spec (TRK-232e).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 * a11y budget 1: known color-contrast debt (text-gray-400 secondary
 * labels) per the TRK-232c/d pattern. The previous budget of 0 was
 * measured against a blank page — the committed bundle threw at
 * module load and never rendered, which the pre-hardening smoke
 * checks could not detect.
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Self-Service Portal @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'self-service-portal');
    await runToolSmokeChecks(page, { allowedNonCriticalViolations: 1 });
  });

  test('uses portal-safe hrefs (TRK-104 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'self-service-portal');
    await assertNoAbsoluteRootHrefs(page);
  });
});
