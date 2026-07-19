/**
 * Unit tests for severityBadgeClass (_common/format/severity.js), portal ROI
 * refactor Cycle 5. Pins the behaviour-preserving binary mapping extracted
 * from the four hand-duplicated call sites: 'critical' → red, everything else
 * → amber. These assertions are the contract the four swapped sites rely on.
 */
import { describe, it, expect } from 'vitest';
import { severityBadgeClass } from '../src/interactive/tools/_common/format/severity.js';

describe('severityBadgeClass', () => {
  it('maps critical to the red colour-pair', () => {
    expect(severityBadgeClass('critical')).toBe('bg-red-100 text-red-700');
  });

  it('maps every non-critical value to the amber colour-pair (verbatim current behaviour)', () => {
    for (const sev of ['warning', 'info', '', undefined, null]) {
      expect(severityBadgeClass(sev as unknown as string)).toBe('bg-amber-100 text-amber-700');
    }
  });

  it('returns only colour classes (no padding/rounding), so call sites keep their own layout', () => {
    expect(severityBadgeClass('critical')).not.toMatch(/\bp[xy]?-|\brounded\b|\btext-xs\b/);
  });
});
