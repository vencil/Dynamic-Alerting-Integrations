/**
 * Property-based tests for parseDuration — TRK-232b, rewritten for #2711.
 *
 * parseDuration returns milliseconds Alertmanager reads from a value, or
 * null (see parseDuration.test.ts for the point cases and the shared Go
 * verdict table). This file fuzzes the input space for invariants:
 *
 *   1. Unit-correctness: for any non-negative integer N and unit U in
 *      {ms, s, m, h, d, w}, parseDuration(`${N}${U}`) === N * unitMs(U).
 *   2. Additivity: a compound value in unit order is the sum of its parts.
 *   3. Out-of-order: two units given smallest first are refused.
 *   4. Fractions: `${A}.${B}${U}` is always refused.
 *   5. Reject-junk: no trailing unit letter → null (except the bare `0`).
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { parseDuration } from '../src/interactive/tools/_common/validation/yaml-parser.js';

// Largest unit first, as Alertmanager requires.
const UNITS: [string, number][] = [
  ['w', 604_800_000],
  ['d', 86_400_000],
  ['h', 3_600_000],
  ['m', 60_000],
  ['s', 1_000],
  ['ms', 1],
];
const UNIT_MS = Object.fromEntries(UNITS);

describe('parseDuration — property-based', () => {
  it('unit correctness: parseDuration(`${N}${U}`) === N * unitMs(U)', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 10_000 }),
        fc.constantFrom(...UNITS.map(([u]) => u)),
        (n, unit) => parseDuration(`${n}${unit}`) === n * UNIT_MS[unit],
      ),
      { numRuns: 200 },
    );
  });

  it('additivity: a compound value in unit order is the sum of its parts', () => {
    fc.assert(
      fc.property(
        fc.array(fc.option(fc.integer({ min: 0, max: 999 }), { nil: undefined }),
          { minLength: UNITS.length, maxLength: UNITS.length }),
        (counts) => {
          const parts = UNITS.map(([u, ms], i) => (counts[i] === undefined ? null : [`${counts[i]}${u}`, counts[i]! * ms] as const))
            .filter((p): p is readonly [string, number] => p !== null);
          fc.pre(parts.length > 0);
          const text = parts.map(([s]) => s).join('');
          return parseDuration(text) === parts.reduce((a, [, ms]) => a + ms, 0);
        },
      ),
      { numRuns: 200 },
    );
  });

  it('out of order: smaller unit first is refused', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: UNITS.length - 1 }),
        fc.integer({ min: 0, max: UNITS.length - 1 }),
        fc.integer({ min: 1, max: 99 }),
        fc.integer({ min: 1, max: 99 }),
        (i, j, a, b) => {
          fc.pre(i < j);
          // UNITS[j] is smaller than UNITS[i]; give it first.
          return parseDuration(`${a}${UNITS[j][0]}${b}${UNITS[i][0]}`) === null;
        },
      ),
      { numRuns: 200 },
    );
  });

  it('fractions are refused for every unit', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 1000 }),
        fc.integer({ min: 0, max: 99 }),
        fc.constantFrom(...UNITS.map(([u]) => u), 'y'),
        (a, b, unit) => parseDuration(`${a}.${b}${unit}`) === null,
      ),
      { numRuns: 200 },
    );
  });

  it('rejects any string whose final character is not a unit letter', () => {
    // Every legal value but `0` ends in y/w/d/h/m/s; asserting the result
    // directly (no fc.pre discard) so a parser that accepted a unit-less
    // string would fail here instead of silently passing.
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 20 })
          .filter((s) => !'ywdhms'.includes(s[s.length - 1]) && s !== '0'),
        (noUnit) => parseDuration(noUnit) === null,
      ),
      { numRuns: 200 },
    );
  });

  it('rejects a unit char that is not preceded by a number', () => {
    for (const junk of ['s', 'm', 'h', 'd', 'ms', 'xh', '-m', 'abcd', '..s']) {
      expect(parseDuration(junk)).toBeNull();
    }
  });
});
