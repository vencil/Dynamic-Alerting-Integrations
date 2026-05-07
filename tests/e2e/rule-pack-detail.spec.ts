/**
 * Rule Pack Detail — smoke spec (TD-032e).
 *
 * See tests/e2e/README.md for the smoke-spec template + rationale.
 *
 * skipA11y default — TD-032c/d found the majority of newly-tested
 * tools have pre-existing WCAG 2.1 AA debt (form labels / select-name).
 * Setting skipA11y: true upfront and revisiting if the verification
 * run shows this tool is clean (then we'd remove the skip).
 */
import { test } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Rule Pack Detail @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'rule-pack-detail');
    await runToolSmokeChecks(page, { allowedNonCriticalViolations: 5 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'rule-pack-detail');
    await assertNoAbsoluteRootHrefs(page);
  });
});
