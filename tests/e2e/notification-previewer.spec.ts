/**
 * Notification Previewer — E2E smoke test (Phase .a A-6, v2.7.0)
 *
 * Validates:
 *   - jsx-loader loads notification-previewer without 404 / JS error
 *   - Page title matches expected pattern
 *   - Core UI: channel selector or notification type tabs visible
 *   - No REG-004-style hardcoded portal-absolute hrefs
 *   - axe-core WCAG 2.1 AA: 0 Critical violations
 */
import { test, expect } from '@playwright/test';
import {
  loadPortalTool,
  runToolSmokeChecks,
  assertNoAbsoluteRootHrefs,
} from './fixtures/portal-tool-smoke';

test.describe('Notification Previewer @critical', () => {
  test('loads via jsx-loader and passes smoke checks', async ({ page }) => {
    await loadPortalTool(page, 'notification-previewer');

    await runToolSmokeChecks(page, {
      expectedTitleMatch: /Notification|Preview|Alert|Channel/i,
      allowedNonCriticalViolations: 5,
    });
  });

  test('renders channel selector (receiver buttons for Slack/Email/PagerDuty/Teams/etc.)', async ({ page }) => {
    // Calibrated against notification-previewer.jsx L839-852 (receiver button map).
    // Each RECEIVER_TYPES entry renders `<button aria-pressed aria-label="Select receiver:<Label>">`.
    await loadPortalTool(page, 'notification-previewer');

    const receiverButtons = page.getByRole('button', { name: /^Select receiver:/i });
    await expect(receiverButtons.first()).toBeVisible({ timeout: 10000 });
    // Sanity: we should see at least 5 receiver types (Slack, Webhook, Email, PagerDuty, Teams are required).
    expect(await receiverButtons.count()).toBeGreaterThanOrEqual(5);
  });

  test('uses portal-safe hrefs (REG-004 regression guard)', async ({ page }) => {
    await loadPortalTool(page, 'notification-previewer');
    await assertNoAbsoluteRootHrefs(page);
  });

  test('live preview region is present (title + body preview boxes)', async ({ page }) => {
    // Calibrated against notification-previewer.jsx L745-787 (LivePreview component).
    // Two tabindex=0 previewBox <div>s with aria-label "Notification {title,body} preview".
    await loadPortalTool(page, 'notification-previewer');

    const preview = page.getByLabel(/notification (title|body) preview/i);
    await expect(preview.first()).toBeVisible({ timeout: 10000 });
    // Both title + body preview boxes should exist.
    expect(await preview.count()).toBeGreaterThanOrEqual(2);
  });
});
