/**
 * Parked-tests register (A-13, #1449): every test that does not run to a pass
 * by design must name the issue that tracks it.
 *
 * "Parked" is judged from what the runner actually did, not from how the spec
 * is spelled: a result of `skipped`, or an expected status other than
 * `passed` (`test.fixme`, `test.skip`, `test.fail`, `testInfo.skip()`, an
 * aliased or computed call — every spelling ends here). Such a test must carry
 * at least one `issue` annotation, and every `issue` annotation on it must be
 * `#<number>`:
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
 * ⚠️ Not seen: tests the run never collects — `--grep-invert`, `testIgnore`,
 * a deleted test. Those leave no result to judge.
 */
import type { FullResult, Reporter, TestCase, TestResult } from '@playwright/test/reporter';

const ISSUE = /^#\d+$/;

export default class ParkedTestsReporter implements Reporter {
  private offenders: string[] = [];

  onTestEnd(test: TestCase, result: TestResult): void {
    const parked = result.status === 'skipped' || test.expectedStatus !== 'passed';
    if (!parked) return;
    const issues = test.annotations.filter((a) => a.type === 'issue');
    if (issues.length > 0 && issues.every((a) => ISSUE.test(a.description ?? ''))) return;
    const where = `${test.location.file}:${test.location.line}`;
    const entry = `${where} "${test.titlePath().slice(1).join(' › ')}" ` +
      `(status=${result.status}, expected=${test.expectedStatus})`;
    // A retried test reports once per attempt; name it once.
    if (!this.offenders.includes(entry)) this.offenders.push(entry);
  }

  onEnd(): { status?: FullResult['status'] } | undefined {
    if (this.offenders.length === 0) return undefined;
    process.stderr.write(
      '[A-13] parked test(s) without an issue annotation — add ' +
      "{ annotation: { type: 'issue', description: '#<n>' } } naming the " +
      'issue that tracks it, or let the test run (#1449):\n' +
      this.offenders.map((o) => `  ${o}\n`).join(''));
    return { status: 'failed' };
  }

  printsToStdio(): boolean {
    return false;
  }
}
