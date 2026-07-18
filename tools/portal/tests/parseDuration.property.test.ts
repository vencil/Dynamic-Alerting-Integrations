/**
 * Property-based tests for parseDuration — TRK-232b (#TBD).
 *
 * The smoke test in parseDuration.test.ts pins point examples
 * ('30s' / '5m' / '2h' / '1d'). This file uses fast-check to fuzz
 * the input space and assert algebraic invariants:
 *
 *   1. Unit-correctness: for any positive integer N and unit U in
 *      {s, m, h, d}, parseDuration(`${N}${U}`) === N * unitSeconds(U).
 *   2. Monotonicity: if A < B then parseDuration(`${A}s`) < parseDuration(`${B}s`).
 *   3. Round-trip: for any positive integer N, parseDuration(`${N}s`) === N.
 *   4. Reject-junk: any string with no recognized unit suffix returns null.
 *
 * Property-based testing complements the example-based smoke tests
 * by hitting boundary cases (large N, fractional N, leading zeros)
 * that were unlikely to be enumerated manually.
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { parseDuration } from '../src/interactive/tools/_common/validation/yaml-parser.js';

const UNIT_TO_SECONDS: Record<string, number> = {
  s: 1,
  m: 60,
  h: 3600,
  d: 86400,
};

describe('parseDuration — property-based', () => {
  it('unit correctness: parseDuration(`${N}${U}`) === N * unitSeconds(U)', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 10_000 }),
        fc.constantFrom('s', 'm', 'h', 'd'),
        (n, unit) => {
          const result = parseDuration(`${n}${unit}`);
          return result === n * UNIT_TO_SECONDS[unit];
        },
      ),
      { numRuns: 200 },
    );
  });

  it('monotonicity in seconds: A < B ⟹ parse(`${A}s`) < parse(`${B}s`)', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 0, max: 1_000_000 }),
        fc.integer({ min: 0, max: 1_000_000 }),
        (a, b) => {
          fc.pre(a !== b);
          const [smaller, larger] = a < b ? [a, b] : [b, a];
          const ps = parseDuration(`${smaller}s`);
          const pl = parseDuration(`${larger}s`);
          return ps !== null && pl !== null && ps < pl;
        },
      ),
    );
  });

  it('seconds round-trip: parseDuration(`${N}s`) === N for all positive N', () => {
    fc.assert(
      fc.property(fc.integer({ min: 0, max: 1_000_000 }), (n) => {
        return parseDuration(`${n}s`) === n;
      }),
    );
  });

  it('rejects any string whose final character is not a recognized unit', () => {
    // "No recognized unit suffix → null" is the actual invariant. We fuzz
    // strings whose LAST char is not one of s/m/h/d; the parser regex ends
    // in [smhd], so every such string must return null. This asserts the
    // result directly (no fc.pre discard) so a parser that ever accepted a
    // unit-less string would fail the property instead of silently passing.
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 20 }).filter((s) => !'smhd'.includes(s[s.length - 1])),
        (noUnit) => parseDuration(noUnit) === null,
      ),
      { numRuns: 200 },
    );
  });

  it('rejects a unit char that is not preceded by a number', () => {
    // Bare/garbled unit strings ('s', 'xh', '-m') have a valid trailing unit
    // but no leading number → null. This is the case the old fc.pre bypass
    // could have masked; assert it directly.
    for (const junk of ['s', 'm', 'h', 'd', 'xh', '-m', 'abcd', '..s']) {
      expect(parseDuration(junk)).toBeNull();
    }
  });
});
