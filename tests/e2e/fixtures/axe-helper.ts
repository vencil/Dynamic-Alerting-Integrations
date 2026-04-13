import { Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/**
 * Axe accessibility helper for WCAG 2.1 AA compliance testing
 * Integrates axe-core with Playwright for automated accessibility testing
 */

export interface A11yCheckOptions {
  exclude?: string[];
  tags?: string[];
}

/**
 * Wait for the page to be meaningfully loaded before running a11y checks.
 * In CI, Python http.server can be slow, causing networkidle to timeout
 * and axe to run against an empty page (yielding false document-title /
 * html-has-lang violations).
 */
export async function waitForPageReady(page: Page, selector?: string) {
  const target = selector || 'title';
  // Wait for the <title> element to exist and be non-empty, or wait for a
  // specific selector that indicates the page content has rendered.
  if (target === 'title') {
    await page.waitForFunction(
      () => document.title.length > 0,
      { timeout: 15000 }
    );
  } else {
    await page.waitForSelector(target, { state: 'visible', timeout: 15000 });
  }
}

/**
 * Run axe-core accessibility checks on a page
 * @param page - Playwright page object
 * @param options - Optional configuration (exclude selectors, tags)
 * @returns Results object with violations and passes
 */
export async function checkA11y(page: Page, options: A11yCheckOptions = {}) {
  const { exclude = [], tags = ['wcag2a', 'wcag2aa'] } = options;

  const builder = new AxeBuilder({ page }).withTags(tags);

  // Exclude selectors if provided
  if (exclude.length > 0) {
    for (const selector of exclude) {
      builder.exclude(selector);
    }
  }

  const results = await builder.analyze();
  return results;
}

/**
 * Format accessibility violations for error reporting
 * @param violations - Array of violation objects from axe results
 * @returns Formatted string detailing violations
 */
export function formatA11yViolations(violations: any[]): string {
  if (!violations || violations.length === 0) {
    return 'No violations found';
  }

  const violationDetails = violations
    .map((violation, index) => {
      const nodes = violation.nodes
        .slice(0, 3) // Limit to first 3 nodes per violation
        .map((node: any) => `    - ${node.html || node.target?.join(' > ')}`)
        .join('\n');

      return `${index + 1}. ${violation.id} (Impact: ${violation.impact || 'unknown'})
   Description: ${violation.description}
   Nodes affected:
${nodes}${violation.nodes.length > 3 ? `\n    ... and ${violation.nodes.length - 3} more` : ''}`;
    })
    .join('\n\n');

  return `Found ${violations.length} accessibility violation(s):\n\n${violationDetails}`;
}
