/**
 * Parked-tests register (A-13, #1449): every test that does not run to a pass
 * by design must name the issue that tracks it.
 *
 * "Parked" is judged from what the runner actually did, not from how the spec
 * is spelled: a result of `skipped`, or an expected status other than
 * `passed` (`test.fixme`, `test.skip`, `test.fail`, `testInfo.skip()`, an
 * aliased or computed call — every spelling, for every test the run collects).
 * Such a test must DECLARE at least one `issue` annotation, and every declared
 * `issue` annotation on it must be `#<number>`. Only the final attempt counts,
 * and a skip carries no parking mark (`skip` / `fixme`) when it is collateral
 * (a hook failed, `--max-failures`), so those are not reported:
 *
 *   test.fixme('title', { annotation: { type: 'issue', description: '#123' } },
 *     async ({ page }) => { ... });
 *
 * The annotations ARE the register: `npx playwright test --list
 * --reporter=json` lists the declared ones, so there is no second, hand-kept
 * table to drift (docs/internal/frontend-quality-backlog.md points here).
 *
 * ⛔ A parked test without a valid issue fails the run (onEnd returns
 * `failed`), wherever `playwright test` runs with the config's reporters.
 * playwright.config.ts is the one place reporters are listed; a `--reporter`
 * flag on the command line replaces that list and drops this check, which is
 * why tests/lint/test_e2e_spec_lint.py rejects one in the CI invocation.
 *
 * ⚠️ Not seen: tests the run never collects or never runs — `--grep` /
 * `--grep-invert`, `testIgnore` (config or project), `--shard`,
 * `--last-failed`, `--only-changed`, a deleted test. Those leave no result
 * to judge. Nor whether the named issue exists or is still open.
 */
import type {
  FullResult, Reporter, Suite, TestCase, TestResult,
} from '@playwright/test/reporter';

// A real issue number; `#0` names nothing.
const ISSUE = /^#[1-9]\d*$/;
const PARKING_MARKS = new Set(['skip', 'fixme']);

export default class ParkedTestsReporter implements Reporter {
  // Issue annotations as DECLARED, captured before anything runs: an issue
  // pushed at run time (`testInfo.annotations.push`) is invisible to
  // `--list`, i.e. outside the register, so it does not count.
  private declaredIssues = new Map<string, string[]>();
  private lastResult = new Map<string, { test: TestCase; result: TestResult }>();

  onBegin(_config: unknown, suite: Suite): void {
    for (const test of suite.allTests()) {
      this.declaredIssues.set(test.id, test.annotations
        .filter((a) => a.type === 'issue').map((a) => a.description ?? ''));
    }
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    // Only the final attempt decides: a retry that passes clears an earlier
    // failure, and siblings skipped because a hook failed once are not parked.
    this.lastResult.set(test.id, { test, result });
  }

  onEnd(): { status?: FullResult['status'] } | undefined {
    const offenders: string[] = [];
    for (const { test, result } of this.lastResult.values()) {
      const marked = [...test.annotations, ...result.annotations]
        .some((a) => PARKING_MARKS.has(a.type));
      const parked = test.expectedStatus !== 'passed'
        || (result.status === 'skipped' && marked);
      if (!parked) continue;
      const issues = this.declaredIssues.get(test.id) ?? [];
      if (issues.length > 0 && issues.every((d) => ISSUE.test(d))) continue;
      offenders.push(`${test.location.file}:${test.location.line} ` +
        `"${test.titlePath().slice(1).join(' › ')}" ` +
        `(status=${result.status}, expected=${test.expectedStatus})`);
    }
    if (offenders.length === 0) return undefined;
    process.stderr.write(
      '[A-13] parked test(s) without a declared issue annotation — add ' +
      "{ annotation: { type: 'issue', description: '#<n>' } } to the test's " +
      'declaration, naming the issue that tracks it, or let the test run ' +
      '(#1449):\n' + offenders.map((o) => `  ${o}\n`).join(''));
    return { status: 'failed' };
  }

  printsToStdio(): boolean {
    return false;
  }
}
