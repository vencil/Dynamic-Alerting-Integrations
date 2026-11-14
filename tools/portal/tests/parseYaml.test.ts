/**
 * Unit + property-based tests for parseYaml — TRK-232b (#TBD).
 *
 * Since #2033 the parser is js-yaml (CORE schema + merge key) behind the
 * `_common/validation/yaml-parser.js` wrapper. This file pins the
 * documented behaviour:
 *
 *   - top-level scalar key/value, YAML types kept
 *   - quoted-string values and quoted keys unquoted
 *   - inline and block lists as arrays, nesting at any depth
 *   - syntax errors (incl. duplicate keys) surface as errors with a line
 *   - prototype-pollution guard (UNSAFE_KEYS dropped at every depth)
 *   - hard size limit (100 KB returns error, no parse)
 *
 * Property: parseYaml never throws; for any string input it returns
 * { config, errors } with both fields defined. (The portal calls
 * parseYaml on user-typed YAML — throwing would crash the editor.)
 */
import { describe, it, expect } from 'vitest';
import fc from 'fast-check';
import * as parseYamlModule from '../src/interactive/tools/_common/validation/yaml-parser.js';

const { parseYaml, parseTenantYaml } = parseYamlModule;

// `l0: &l0 [x, x]`, `l1: &l1 [*l0, *l0]`, … `l24` — shared-alias DAG.
const ALIAS_CHAIN_24 = ['l0: &l0 [x, x]']
  .concat(Array.from({ length: 24 }, (_, i) => `l${i + 1}: &l${i + 1} [*l${i}, *l${i}]`))
  .join('\n');

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

describe('parseYaml — js-yaml regressions (#2033)', () => {
  it('reports a duplicate key as an error instead of merging it', () => {
    const { config, errors } = parseYaml('mysql_connection_usage_percent: "70"\nmysql_connection_usage_percent: "80"');
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/duplicated mapping key/);
    expect(errors[0]).toMatch(/line 2/);
    expect(config).toEqual({});
  });

  it('treats a quoted and an unquoted spelling as the same key (duplicate)', () => {
    const { errors } = parseYaml('"_silent_mode": warning\n_silent_mode: critical');
    expect(errors[0]).toMatch(/duplicated mapping key/);
  });

  it('unquotes quoted keys', () => {
    const { config, errors } = parseYaml('"_silent_mode": warning\n\'_routing\':\n  receiver_type: webhook');
    expect(errors).toEqual([]);
    expect(config).toEqual({ _silent_mode: 'warning', _routing: { receiver_type: 'webhook' } });
  });

  it('reports a syntax error with its line', () => {
    const { config, errors } = parseYaml('_routing:\n  a: 1\n   b: 2');
    expect(errors).toHaveLength(1);
    expect(errors[0]).toMatch(/line 3/);
    expect(config).toEqual({});
  });

  it('rejects a non-mapping root', () => {
    const { config, errors } = parseYaml('- a\n- b');
    expect(errors).toHaveLength(1);
    expect(config).toEqual({});
  });

  it('keeps YAML types and models block lists and deep nesting', () => {
    const { config } = parseYaml(
      'mysql_connection_usage_percent: 80\n_routing:\n  receiver:\n    type: webhook\n  group_by:\n    - alertname\n    - severity',
    );
    expect(config.mysql_connection_usage_percent).toBe(80);
    expect(config._routing).toEqual({ receiver: { type: 'webhook' }, group_by: ['alertname', 'severity'] });
  });

  it('keeps timestamps as strings and expands merge keys', () => {
    const { config } = parseYaml('base: &b {x: 1}\nm:\n  <<: *b\n  y: 2\nexpires: 2026-01-01T00:00:00Z');
    expect(config.m).toEqual({ x: 1, y: 2 });
    expect(config.expires).toBe('2026-01-01T00:00:00Z');
  });

  it('reads only the first document (trailing ---, second doc)', () => {
    expect(parseYaml('environment: prod\n---\n')).toEqual({ config: { environment: 'prod' }, errors: [] });
    expect(parseYaml('a: 1\n---\na: 2\nb: 3\n')).toEqual({ config: { a: 1 }, errors: [] });
  });

  it('reports an error in a later document (deliberate)', () => {
    const r = parseYaml('a: 1\n---\na: [\n');
    expect(r.errors).toHaveLength(1);
    expect(r.config).toEqual({});
    // A leading `...` must not hide an error in the document after it.
    expect(parseYaml('...\na: [\n').errors).toHaveLength(1);
    expect(parseYaml('a: [\n---\na: 2\n').errors).toHaveLength(1);
  });

  it('treats an empty stream (only ---) as empty config', () => {
    expect(parseYaml('---\n')).toEqual({ config: {}, errors: [] });
  });

  it('reports a self-referencing anchor instead of overflowing the stack', () => {
    let r: ReturnType<typeof parseYaml> | undefined;
    expect(() => { r = parseYaml('a: &r\n  b: *r\n'); }).not.toThrow();
    expect(r!.config).toEqual({});
    expect(r!.errors).toHaveLength(1);
    expect(r!.errors[0]).toMatch(/anchor 'r' value contains itself/);
  });

  it('accepts a shared (non-cyclic) alias', () => {
    const { config, errors } = parseYaml('x: &s {k: 1}\ny: *s\nz: *s\n');
    expect(errors).toEqual([]);
    expect(config).toEqual({ x: { k: 1 }, y: { k: 1 }, z: { k: 1 } });
  });

  it('handles a doubling alias chain (n=24) in linear time, no false cycle', () => {
    // l_i = [l_{i-1}, l_{i-1}]: 2^24 paths through 25 nodes. A walk that
    // re-expands shared subtrees takes seconds; each node once is instant.
    expect(ALIAS_CHAIN_24.length).toBeLessThan(600);
    const t0 = performance.now();
    const { loadYamlDocument } = parseYamlModule;
    const loaded = loadYamlDocument(ALIAS_CHAIN_24);
    const r = parseYaml(ALIAS_CHAIN_24);
    const ms = performance.now() - t0;
    expect(ms).toBeLessThan(500);
    // Only the absence of a false cycle report is pinned — not a verdict.
    const isCycle = (m: string) => /contains itself|自我參照/.test(m);
    expect(loaded.error === null || !isCycle(loaded.error.message)).toBe(true);
    expect(r.errors.filter(isCycle)).toEqual([]);
  });

  it('drops UNSAFE_KEYS nested below the top level', () => {
    const { config } = parseYaml('_routing:\n  __proto__:\n    polluted: yes\n  receiver_type: webhook');
    expect(Object.keys(config._routing)).toEqual(['receiver_type']);
    expect(Object.getPrototypeOf(config._routing)).toBe(Object.prototype);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
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

describe('parseTenantYaml — threshold keys keep the scalar text (#3919)', () => {
  it('a threshold key (top-level, no `_`) holds the text that was written; a null stays null', () => {
    const { config, errors } = parseTenantYaml(
      'a: 0x1F\nb: 0o17\nc: 0b101\nd: 12345678901234567890123\ne: 1e20\nf: 80\ng: "80"\nh: False\n'
      + 'i: ~\nj:\nk: .inf\nl: 1_000\nm: !!int 0x1F\nn: |\n  80\no: 80 # note\n');
    expect(errors).toEqual([]);
    expect(config).toEqual({
      a: '0x1F', b: '0o17', c: '0b101', d: '12345678901234567890123', e: '1e20', f: '80', g: '80', h: 'False',
      i: null, j: null, k: '.inf', l: '1_000', m: '0x1F', n: '80\n', o: '80',
    });
  });

  it('keeps the text at any depth under a threshold key (a scheduled value), through aliases and merge keys', () => {
    const { config } = parseTenantYaml(
      'k: {default: 0x1F, overrides: [{window: "01:00-02:00", value: 0b101}]}\nsrc: &x 0o17\nalias: *x\n<<: {merged: 1e20}\n');
    expect(config).toEqual({
      k: { default: '0x1F', overrides: [{ window: '01:00-02:00', value: '0b101' }] },
      src: '0o17', alias: '0o17', merged: '1e20',
    });
  });

  it('`_` keys are parseYaml\'s, typed; a threshold key is named as written', () => {
    const yaml = '_silent_mode: true\n_n: 0x1F\n_routing:\n  group_wait: 30s\n  repeat: 4\n0x1F: 5\nk: 0x1F\n';
    const { config } = parseTenantYaml(yaml);
    const typed = parseYaml(yaml).config;
    for (const key of ['_silent_mode', '_n', '_routing']) expect(config[key], key).toEqual(typed[key]);
    expect(config._n).toBe(31);
    expect(config._routing).toEqual({ group_wait: '30s', repeat: 4 });
    expect(config['0x1F']).toBe('5');
    expect(Object.keys(config)).toEqual(['_silent_mode', '_n', '_routing', '0x1F', 'k']);
  });

  it('parseYaml itself still types every value (other tools read it)', () => {
    expect(parseYaml('a: 0x1F\nb: 80\nc: False\nd: "80"\n').config).toEqual({ a: 31, b: 80, c: false, d: '80' });
  });

  it('drops UNSAFE_KEYS under a threshold key and at the top level', () => {
    const { config } = parseTenantYaml('__proto__: {polluted: 1}\nk: {default: 80, __proto__: {polluted: 1}, constructor: 1}\n');
    expect(config).toEqual({ k: { default: '80' } });
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });

  it('errors, the empty stream and a non-mapping root are parseYaml\'s', () => {
    for (const yaml of ['', '---\n', 'k: 1\nk: 2\n', '- a\n', 'a: [1,\n', 'a: &r\n  b: *r\n', 'k: !foo 5\n', 'x'.repeat(200_000)]) {
      expect(parseTenantYaml(yaml), JSON.stringify(yaml.slice(0, 20))).toEqual(parseYaml(yaml));
    }
  });

  it('property: never throws, reports the errors parseYaml reports, and agrees with it on every `_` key', () => {
    const line = fc.tuple(
      fc.constantFrom('k', '_k', 'a_critical', '_routing', 'x'),
      fc.oneof(
        fc.constantFrom('0x1F', '0b101', '1e20', '~', '', 'False', '"80"', '!!int 5', '!!null x', '{a: 0x1F}', '[1, 0o17]', '&a 5', '*a', '|\n  80'),
        fc.string({ maxLength: 12 }).filter((s) => !s.includes('\n')),
      ),
    ).map(([k, v]) => `${k}: ${v}`);
    const doc = fc.oneof(fc.string({ maxLength: 400 }), fc.array(line, { maxLength: 6 }).map((ls) => ls.join('\n')));
    fc.assert(
      fc.property(doc, (input) => {
        const typed = parseYaml(input);
        const got = parseTenantYaml(input);
        expect(got.errors).toEqual(typed.errors);
        const reserved = (c: Record<string, unknown>) => Object.fromEntries(Object.entries(c).filter(([k]) => k.startsWith('_')));
        expect(reserved(got.config)).toEqual(reserved(typed.config));
      }),
      { numRuns: 500 },
    );
  });

  it('an unsafe top-level key is dropped, as parseYaml drops it', () => {
    for (const yaml of ['constructor: 5\nprototype: 6\nk: 1\n', '__proto__: {x: 1}\nk: 1\n']) {
      const { config, errors } = parseTenantYaml(yaml);
      expect(errors).toEqual([]);
      expect(Object.keys(config)).toEqual(['k']);
      expect(Object.keys(config)).toEqual(Object.keys(parseYaml(yaml).config));
      expect(Object.getPrototypeOf(config)).toBe(Object.prototype);
      expect(config.k).toBe('1');
    }
  });

  it('a tagged scalar its tag cannot read is parseYaml\'s error, though the text schema alone would take it', () => {
    // The exporter's parse-tree refuses these files too. The text schema
    // resolves anything under the tag, so only the typed parse run first
    // keeps `!!int 1e20` from being simulated as 1e20.
    for (const yaml of ['k: !!int 1e20\n', 'k: !!bool off\n', 'k: !!int abc\n', 'k: !!float 0x1F\n']) {
      const got = parseTenantYaml(yaml);
      expect(got.errors, yaml).toHaveLength(1);
      expect(got, yaml).toEqual(parseYaml(yaml));
    }
  });

  it('an empty scalar under !!int / !!float / !!bool is the same error as parseYaml gives', () => {
    for (const tag of ['int', 'float', 'bool']) {
      const yaml = `k: !!${tag}\n`;
      const got = parseTenantYaml(yaml);
      expect(got.errors).toHaveLength(1);
      expect(got).toEqual(parseYaml(yaml));
    }
  });
});
