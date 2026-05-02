/**
 * Diagnostic matchers for Playwright assertions (S#98 / LL §11 Tier 1 v2).
 *
 * Background — why this exists
 * ----------------------------
 * S#95 PR #185 first CI run failed because spec asserted state-ready
 * testid was visible, but on cold-start the component rendered
 * state-empty. The CI error message said only "element(s) not found"
 * — the dev had to re-run locally to discover what state the
 * component actually was in.
 *
 * S#97 PR #187 attempted a commit-time lint that enumerated input-
 * establishment patterns (.fill / .click / page.route / ...). User
 * review caught the mistake: that's whack-a-mole because Playwright
 * keeps adding interaction methods, custom helpers wrap them, and the
 * actual bug class (mental-model mismatch) has countless failure
 * modes beyond cold-start.
 *
 * The deeper architecture: shift the diagnostic burden to ASSERTION
 * FAILURE TIME. When `.toBeVisible()` fails, dump every currently-
 * visible testid on the page so the dev sees the actual state and
 * can correct their mental model without re-running.
 *
 * Why this is right shape
 * -----------------------
 * - **Zero enumeration**: works for every Playwright version + every
 *   custom helper + every state-machine pattern. No pattern list to
 *   maintain.
 * - **Catches more than cold-start**: any state-mismatch failure
 *   (async race, validation reject, stale mock, selector miss,
 *   re-mount) gets a self-explanatory error.
 * - **Spec-author-friendly**: opt-in via `.toBeVisibleWithDiagnostics()`,
 *   no migration cost. Or globally swap once spec authors trust it.
 *
 * Trade-offs vs commit-time lint
 * ------------------------------
 * - Failures surface at CI time, not commit time. Acceptable: CI is
 *   the source-of-truth gate, and the diagnostic output makes the
 *   round-trip cheap (no re-run-locally needed).
 * - No "you forgot to fill input" pre-warning. We accept this because
 *   a lint that tries to enforce that is fundamentally enumeration-
 *   based (S#97 PR #187 closed unmerged for this reason).
 *
 * Usage
 * -----
 * Specs import this file once (or it's auto-loaded via
 * playwright.config.ts side-effect import) and then use:
 *
 *     await expect(page.getByTestId('my-state')).toBeVisibleWithDiagnostics();
 *
 * On failure the error message contains every currently-visible
 * testid, so the dev can compare expected vs actual state at a
 * glance.
 *
 * References
 * ----------
 * - testing-playbook.md §v2.8.0 LL §11 (read-time SOT + 3-tier
 *   feasibility, Tier 1 v2 = this matcher)
 * - testing-playbook.md §v2.8.0 LL §12 (closed-vs-open enumeration
 *   discipline check; S#97 PR #187 lessons)
 * - PR #185 fix commit `3beb127` (motivating cold-start incident)
 * - PR #187 closed-unmerged (the wrong-shape lint)
 */
import { expect, type Locator } from '@playwright/test';

declare global {
  namespace PlaywrightTest {
    interface Matchers<R> {
      /**
       * Same contract as `.toBeVisible()`, but on failure the error
       * message lists every currently-visible `data-testid` on the
       * page. Use for assertions where the component state is
       * dynamic (e.g. `*-state-ready`, `*-state-error`) and the
       * default Playwright error ("element(s) not found") wastes a
       * round-trip.
       */
      toBeVisibleWithDiagnostics(options?: { timeout?: number }): Promise<R>;
    }
  }
}

interface VisibleTestIdSnapshot {
  count: number;
  testids: string[];
}

async function collectVisibleTestIds(
  locator: Locator
): Promise<VisibleTestIdSnapshot> {
  return locator.page().evaluate(() => {
    const all = Array.from(document.querySelectorAll('[data-testid]'));
    const visible = all.filter((el) => {
      const rect = (el as HTMLElement).getBoundingClientRect();
      // Bounding-rect heuristic: matches Playwright's own visibility
      // notion closely enough for diagnostic purposes (we don't need
      // perfect agreement; we're listing candidates for the dev).
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        (el as HTMLElement).offsetParent !== null
      );
    });
    const testids = visible
      .map((el) => el.getAttribute('data-testid'))
      .filter((t): t is string => typeof t === 'string')
      .sort();
    return { count: testids.length, testids };
  });
}

expect.extend({
  async toBeVisibleWithDiagnostics(
    locator: Locator,
    options?: { timeout?: number }
  ) {
    // Phase 1 — try the standard Playwright visibility wait. If it
    // succeeds, we pass through with no overhead.
    let pass = false;
    try {
      await expect(locator).toBeVisible({ timeout: options?.timeout ?? 5000 });
      pass = true;
    } catch (_err) {
      pass = false;
    }
    if (pass) {
      return {
        pass: true,
        message: () => 'locator is visible',
      };
    }

    // Phase 2 — failure path: collect diagnostic context. We do this
    // AFTER the wait timed out, not before, so successful assertions
    // pay zero cost.
    let snapshot: VisibleTestIdSnapshot;
    try {
      snapshot = await collectVisibleTestIds(locator);
    } catch (collectErr) {
      // Best-effort — if collection itself fails (e.g. page closed),
      // we still want to fail with the original semantic, not mask it.
      return {
        pass: false,
        message: () =>
          `Expected locator to be visible but was not. ` +
          `(Diagnostic collection also failed: ${
            (collectErr as Error).message
          })`,
      };
    }

    const lines: string[] = [];
    lines.push('Expected locator to be visible but was not.');
    lines.push('');
    if (snapshot.count === 0) {
      lines.push(
        'No data-testid elements are currently visible on the page.'
      );
      lines.push('Hints:');
      lines.push(
        '  - Did the JSX component mount? Check console for runtime errors.'
      );
      lines.push(
        '  - Did the page navigation complete? loadPortalTool may need more wait time.'
      );
    } else {
      lines.push(
        `Currently visible testids on page (${snapshot.count}):`
      );
      for (const testid of snapshot.testids) {
        lines.push(`  - ${testid}`);
      }
      lines.push('');
      lines.push('Hints:');
      lines.push(
        '  - Compare the visible testids above against your expected one.'
      );
      lines.push(
        '  - Did you fill / click / route required inputs before this assertion?'
      );
      lines.push(
        '  - Is the component in a different state than expected (cold-start)?'
      );
      lines.push('  - Check for testid spelling drift.');
    }
    lines.push('');
    lines.push(
      'See testing-playbook.md §v2.8.0 LL §11 for cold-start state contract.'
    );

    return {
      pass: false,
      message: () => lines.join('\n'),
    };
  },
});

// Marker export so static analysis (and humans) can confirm the
// matcher module loaded; importers can use this to gate test setup.
export const DIAGNOSTIC_MATCHERS_LOADED = true;
