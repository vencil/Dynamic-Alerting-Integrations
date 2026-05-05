/**
 * Smoke unit test for `_common/validation/yaml-parser.js` parseDuration.
 *
 * First Vitest test landing in the repo (TD-030c). Validates that the
 * frontmatter-stripping plugin + ESM-named-export wiring actually work
 * end-to-end against a real `_common` file. Future PRs add deeper
 * coverage; this is a tracer bullet.
 */
import { describe, it, expect } from 'vitest';
import { parseDuration } from '../../docs/interactive/tools/_common/validation/yaml-parser.js';

describe('parseDuration (smoke)', () => {
  it('parses seconds', () => {
    expect(parseDuration('30s')).toBe(30);
  });

  it('parses minutes', () => {
    expect(parseDuration('5m')).toBe(5 * 60);
  });

  it('parses hours', () => {
    expect(parseDuration('2h')).toBe(2 * 3600);
  });

  it('parses days', () => {
    expect(parseDuration('1d')).toBe(86400);
  });

  it('parses fractional values', () => {
    expect(parseDuration('1.5h')).toBe(1.5 * 3600);
  });

  it('returns null for invalid input', () => {
    expect(parseDuration('abc')).toBeNull();
    expect(parseDuration('')).toBeNull();
    // 'ms' unit is not supported by this parser — the regex matches
    // only single-letter units. Locking the documented behavior.
    expect(parseDuration('500ms')).toBeNull();
  });
});
