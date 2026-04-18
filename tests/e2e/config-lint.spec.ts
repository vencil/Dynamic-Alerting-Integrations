/**
 * Config Lint — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads config-lint without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: input area for config/YAML and result display
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Config Lint @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Lint|Config|Validat|Check/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders input area for configuration', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    // v2.7.0 calibration (§8.11.4): config-lint.jsx:373-375 renders a
    // <textarea aria-label={t('Tenant YAML 輸入區', 'Tenant YAML input')}>.
    const input = page.getByRole('textbox', { name: /YAML/i });
    await expect(input).toBeVisible({ timeout: 10000 });
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('severity status indicators use role="status" or role="alert"', async ({ page }) => {
    await loadPortalTool(page, 'config-lint');

    // v2.7.0 calibration (§8.11.4): config-lint is fully reactive — there is
    // no Lint / Validate / Run button. Findings are emitted into two
    // semantic regions:
    //   - config-lint.jsx:387 `<div role="status" aria-live="polite">` — summary
    //   - config-lint.jsx:410 `<div role="alert" aria-label="Lint findings">` — findings
    // Both are always rendered (findings region is just empty when no issues),
    // so we assert the presence of either region. This doubles as a
    // TECH-DEBT-002 regression guard (the role="alert" fix).
    const summary = page.getByRole('status');
    const findings = page.getByRole('alert');
    // Either region being attached to the DOM proves the a11y contract holds.
    // Note: `.or()` must wrap whole locators (not `.first()`-narrowed ones) and
    // the `.first()` call has to come AFTER `.or()` — otherwise strict-mode
    // fires on the outer union when multiple matches exist (v2.7.0 hotfix).
    const anyRegion = summary.or(findings).first();
    await expect(anyRegion).toBeVisible({ timeout: 10000 });
  });
});
