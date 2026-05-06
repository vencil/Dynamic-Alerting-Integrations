/**
 * Unit + property-based tests for parseYaml — TD-032b (#TBD).
 *
 * The parser is a hand-rolled YAML subset for tenant config (see
 * `_common/validation/yaml-parser.js` docstring for rationale —
 * portal is zero-build so js-yaml's 70 KB is too expensive). This
 * file pins the documented behaviour:
 *
 *   - top-level scalar key/value
 *   - quoted-string values
 *   - inline arrays `[a, b, c]`
 *   - one level of nesting under `_routing` / `_metadata`
 *   - prototype-pollution guard (UNSAFE_KEYS dropped)
 *   - hard size limit (100 KB returns error, no parse)
 *
 * Property: parseYaml never throws; for any string input it returns
 * { config, errors } with both fields defined. (The portal calls
 * parseYaml on user-typed YAML — throwing would crash the editor.)
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import { parseYaml } from '../../docs/interactive/tools/_common/validation/yaml-parser.js';

describe('parseYaml — unit', () => {
  it('parses top-level scalar string', () => {
    const { config, errors } = parseYaml('environment: production');
    expect(errors).toEqual([]);
    expect(config).toEqual({ environment: 'production' });
  });

  it('strips double-quoted values', () => {
    const { config } = parseYaml('label: "hello world"');
    expect(config).toEqual({ label: 'hello world' });
  });

  it('strips single-quoted values', () => {
    const { config } = parseYaml("label: 'hello world'");
    expect(config).toEqual({ label: 'hello world' });
  });

  it('parses inline array', () => {
    const { config } = parseYaml('tags: [alpha, beta, gamma]');
    expect(config.tags).toEqual(['alpha', 'beta', 'gamma']);
  });

  it('parses nested _routing block', () => {
    const yaml = ['_routing:', '  webhook_url: https://example.com/hook'].join('\n');
    const { config, errors } = parseYaml(yaml);
    expect(errors).toEqual([]);
    expect(config._routing).toBeDefined();
    expect(config._routing.webhook_url).toBe('https://example.com/hook');
  });

  it('drops UNSAFE_KEYS at top level (prototype-pollution guard)', () => {
    const { config } = parseYaml('__proto__: bad');
    expect(config.__proto__).not.toBe('bad');
    // Object.prototype must not have been polluted.
    expect(({} as Record<string, unknown>).__proto__).not.toBe('bad');
  });

  it('rejects oversized input', () => {
    const huge = 'x'.repeat(200_000);
    const { config, errors } = parseYaml(huge);
    expect(errors.length).toBeGreaterThan(0);
    expect(errors[0]).toMatch(/size limit|大小限制/);
    expect(config).toEqual({});
  });

  it('skips comment lines', () => {
    const yaml = ['# a comment', 'environment: prod'].join('\n');
    const { config } = parseYaml(yaml);
    expect(config).toEqual({ environment: 'prod' });
  });

  it('handles empty input', () => {
    const { config, errors } = parseYaml('');
    expect(config).toEqual({});
    expect(errors).toEqual([]);
  });
});

describe('parseYaml — property', () => {
  it('never throws for any string input', () => {
    fc.assert(
      fc.property(fc.string({ maxLength: 1000 }), (input) => {
        // The portal calls parseYaml on user-typed YAML in an
        // edit-as-you-go editor. Throwing would unmount the
        // component — this test asserts non-throwing behavior
        // for arbitrary strings.
        expect(() => parseYaml(input)).not.toThrow();
      }),
      { numRuns: 200 },
    );
  });

  it('always returns { config: object, errors: array }', () => {
    fc.assert(
      fc.property(fc.string({ maxLength: 1000 }), (input) => {
        const result = parseYaml(input);
        return (
          typeof result === 'object' &&
          result !== null &&
          typeof result.config === 'object' &&
          Array.isArray(result.errors)
        );
      }),
      { numRuns: 200 },
    );
  });

  it('UNSAFE_KEYS never appear as own properties of the parsed config', () => {
    // Generate `key: value` lines that may include an UNSAFE_KEY.
    // Verify the dangerous keys are filtered out.
    const unsafeKeys = ['__proto__', 'constructor', 'prototype'];
    fc.assert(
      fc.property(
        fc.array(
          fc.tuple(
            fc.oneof(
              fc.constantFrom(...unsafeKeys),
              fc.stringMatching(/^[a-z][a-z0-9_]*$/),
            ),
            fc.string({ minLength: 1, maxLength: 20 }).filter((s) => !s.includes('\n')),
          ),
          { minLength: 1, maxLength: 10 },
        ),
        (pairs) => {
          const yaml = pairs.map(([k, v]) => `${k}: ${v}`).join('\n');
          const { config } = parseYaml(yaml);
          for (const k of unsafeKeys) {
            // hasOwnProperty (not `in`) — dangerous keys must not
            // become own properties even if present in input.
            if (Object.prototype.hasOwnProperty.call(config, k)) {
              return false;
            }
          }
          return true;
        },
      ),
      { numRuns: 100 },
    );
  });
});
