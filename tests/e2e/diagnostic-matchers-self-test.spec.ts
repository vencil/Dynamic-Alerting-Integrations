/**
 * Self-tests for `toBeVisibleWithDiagnostics` (S#98).
 *
 * Verifies the runtime contract from `tests/e2e/fixtures/diagnostic-matchers.ts`:
 *
 *   1. Pass-through path — when the locator IS visible, the matcher
 *      passes silently with no diagnostic overhead.
 *   2. Failure path — when the locator is NOT visible, the failure
 *      message contains:
 *        - "Expected locator to be visible but was not"
 *        - "Currently visible testids on page (N):"
 *        - the actual testids that ARE visible
 *        - the LL §11 cross-ref hint
 *
 * Strategy: serve a tiny synthetic HTML page via `data:` URL so we
 * don't depend on the docs site or any spec under test. The page
 * has 3 visible testids and the test asserts a 4th (which is absent)
 * — the diagnostic message must list all 3.
 */
import { test, expect } from '@playwright/test';
import './fixtures/diagnostic-matchers';

const SYNTHETIC_HTML = `
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>diagnostic-matcher self-test</title></head>
<body>
  <div data-testid="visible-alpha">Alpha</div>
  <div data-testid="visible-beta">Beta</div>
  <div data-testid="visible-gamma">Gamma</div>
  <div data-testid="hidden-delta" style="display:none">Delta</div>
</body>
</html>
`;

const DATA_URL = 'data:text/html;base64,' + Buffer.from(SYNTHETIC_HTML).toString('base64');

test.describe('toBeVisibleWithDiagnostics @critical', () => {
  test('passes silently when locator is visible', async ({ page }) => {
    await page.goto(DATA_URL);
    // Should NOT throw.
    await expect(
      page.getByTestId('visible-alpha')
    ).toBeVisibleWithDiagnostics({ timeout: 2000 });
  });

  test('failure message lists all currently-visible testids', async ({ page }) => {
    await page.goto(DATA_URL);

    // Trigger the failure path by asserting on a testid that doesn't
    // exist. We catch the error and inspect the message body; the
    // matcher's contract is that the message MUST list every visible
    // testid so the dev sees the actual page state without re-running.
    let errorMessage = '';
    try {
      await expect(
        page.getByTestId('absent-zeta')
      ).toBeVisibleWithDiagnostics({ timeout: 1000 });
    } catch (err) {
      errorMessage = (err as Error).message;
    }

    // Sanity — failure was actually triggered.
    expect(errorMessage).not.toBe('');
    // Header line.
    expect(errorMessage).toContain(
      'Expected locator to be visible but was not'
    );
    // Testid count line — exactly 3 visible testids in our synthetic page.
    expect(errorMessage).toMatch(/Currently visible testids on page \(3\)/);
    // Each visible testid must appear in the diagnostic.
    expect(errorMessage).toContain('visible-alpha');
    expect(errorMessage).toContain('visible-beta');
    expect(errorMessage).toContain('visible-gamma');
    // The hidden one MUST NOT appear (would defeat the purpose).
    expect(errorMessage).not.toContain('hidden-delta');
    // Cross-ref to playbook for action context.
    expect(errorMessage).toContain('LL §11');
  });

  test('failure message handles zero-testid pages gracefully', async ({ page }) => {
    // Page with NO data-testid anywhere — diagnostic should report
    // that fact (and not crash trying to enumerate empty list).
    const blankUrl =
      'data:text/html;base64,' +
      Buffer.from(
        '<!DOCTYPE html><html><body><p>nothing here</p></body></html>'
      ).toString('base64');
    await page.goto(blankUrl);

    let errorMessage = '';
    try {
      await expect(
        page.getByTestId('absent')
      ).toBeVisibleWithDiagnostics({ timeout: 1000 });
    } catch (err) {
      errorMessage = (err as Error).message;
    }

    expect(errorMessage).not.toBe('');
    expect(errorMessage).toContain(
      'No data-testid elements are currently visible'
    );
    // Different hint set when the page has no testids — points the
    // dev at "is the JSX even mounted?" which is a different class
    // of bug than "wrong state".
    expect(errorMessage).toContain('JSX component mount');
  });
});
