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
