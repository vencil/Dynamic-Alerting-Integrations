/**
 * playground/validation.js — parse-layer check for a multi-tenant document.
 *
 * #2033 (owner decision R′): the playground judges only what the exporter's
 * YAML decoder (Go yaml.v3) would also refuse — syntax errors (duplicate
 * keys, tab indentation, bad indentation), a root mapping with `tenants:`,
 * `tenants` as a mapping, and each tenant as a mapping or null. It makes no
 * semantic verdict, so values the old hand-written rules rejected (and the
 * exporter accepts) must now come out valid.
 */
import { describe, it, expect } from 'vitest';
import {
  validateTenantConfig,
  parseYAML,
} from '../src/interactive/tools/playground/validation.js';

const VALID = `tenants:
  db-a:
    mysql_connections: "70"
    mysql_connections_critical: "95"
    mysql_threads_running: "80"
    _silent_mode: "disable"
    _routing:
      receiver_type: "webhook"
      webhook_url: "https://webhook.example.com/alerts"
      group_wait: "30s"
      repeat_interval: "4h"`;

const tenant = (body: string) => `tenants:\n  db-a:\n${body}`;

describe('parseYAML', () => {
  it('parses a nested tenants structure', () => {
    const r = parseYAML('tenants:\n  a:\n    mysql_connections: "1"');
    expect(r.success).toBe(true);
    expect(r.data.tenants.a.mysql_connections).toBe('1');
  });

  it('reports a syntax error with the js-yaml line number', () => {
    const r = parseYAML('tenants:\n  a:\n    x: 1\n    x: 2');
    expect(r.success).toBe(false);
    expect(r.line).toBe(4);
    expect(r.error).toMatch(/duplicated mapping key/);
    expect(r.error).toMatch(/line 4/);
  });
});

describe('validateTenantConfig — structure', () => {
  it('accepts a well-formed tenant config', () => {
    const r = validateTenantConfig(VALID);
    expect(r.valid).toBe(true);
    expect(r.errors).toHaveLength(0);
    expect(r.summary.tenants).toBe(1);
  });

  it('rejects YAML with no tenants root key', () => {
    const r = validateTenantConfig('foo: bar');
    expect(r.valid).toBe(false);
    expect(r.errors.length).toBeGreaterThan(0);
  });

  it('rejects a non-mapping root and an empty document', () => {
    expect(validateTenantConfig('- tenants').valid).toBe(false);
    expect(validateTenantConfig('').valid).toBe(false);
  });

  it('rejects tenants that is not a mapping', () => {
    expect(validateTenantConfig('tenants: [db-a]').valid).toBe(false);
    expect(validateTenantConfig('tenants: x').valid).toBe(false);
  });

  it('rejects a scalar tenant body (exporter unmarshal error)', () => {
    const r = validateTenantConfig('tenants:\n  db-a: foo');
    expect(r.valid).toBe(false);
    expect(r.errors[0].message).toContain('db-a');
  });

  it('accepts a null tenant body', () => {
    expect(validateTenantConfig('tenants:\n  db-a:').valid).toBe(true);
  });
});

describe('validateTenantConfig — syntax errors the old line parser swallowed (#2033)', () => {
  it('rejects a duplicate unquoted key', () => {
    expect(validateTenantConfig(tenant('    mysql_connections: "70"\n    mysql_connections: "80"')).valid).toBe(false);
  });

  it('rejects a duplicate key when one spelling is quoted', () => {
    const r = validateTenantConfig(tenant('    "_silent_mode": warning\n    _silent_mode: critical'));
    expect(r.valid).toBe(false);
    expect(r.errors[0].message).toMatch(/duplicated mapping key/);
  });

  it('rejects a duplicate tenant', () => {
    const r = validateTenantConfig('tenants:\n  db-a:\n    x: "1"\n  db-a:\n    y: "2"');
    expect(r.valid).toBe(false);
  });

  it('rejects the level4Key shape (6-space child after a scalar sibling)', () => {
    // The old parser kept `_routing` as the open block after a boolean
    // sibling and filed the 6-space line under it; YAML says bad indentation.
    const r = validateTenantConfig(tenant('    _routing:\n    _silent_mode: true\n      receiver_type: webhook'));
    expect(r.valid).toBe(false);
    expect(r.errors[0].message).toMatch(/line 5/);
  });

  it('rejects a self-referencing anchor (yaml.v3: "value contains itself")', () => {
    let r: ReturnType<typeof validateTenantConfig> | undefined;
    expect(() => { r = validateTenantConfig('tenants:\n  db-a: &r\n    self: *r\n'); }).not.toThrow();
    expect(r!.valid).toBe(false);
    expect(r!.errors[0].message).toMatch(/anchor 'r' value contains itself/);
  });

  it('rejects tab indentation', () => {
    expect(validateTenantConfig('tenants:\n\tdb-a:\n\t\tx: "1"').valid).toBe(false);
  });
});

describe('validateTenantConfig — multi-document streams (yaml.Unmarshal reads doc 1 only)', () => {
  it('accepts a trailing ---', () => {
    expect(validateTenantConfig('tenants:\n  db-a: {}\n---\n').valid).toBe(true);
  });

  it('judges only the first of two documents', () => {
    const r = validateTenantConfig('tenants:\n  db-a: {}\n---\nfoo: bar\n');
    expect(r.valid).toBe(true);
    expect(r.summary.tenants).toBe(1);
    // …and a first document without tenants: still fails even if doc 2 has it.
    expect(validateTenantConfig('foo: bar\n---\ntenants:\n  db-a: {}\n').valid).toBe(false);
  });

  it('reports a syntax error in a later document (stricter than yaml.v3, by design)', () => {
    expect(validateTenantConfig('tenants:\n  db-a: {}\n---\n\tfoo: 1\n').valid).toBe(false);
    expect(validateTenantConfig('tenants:\n  db-a: {}\n---\n"abc').valid).toBe(false);
  });
});

describe('validateTenantConfig — shared aliases', () => {
  it('checks a doubling alias chain (n=24) quickly and without a false cycle', () => {
    const body = ['    l0: &l0 [x, x]']
      .concat(Array.from({ length: 24 }, (_, i) => `    l${i + 1}: &l${i + 1} [*l${i}, *l${i}]`))
      .join('\n');
    const t0 = performance.now();
    const r = validateTenantConfig(tenant(body));
    expect(performance.now() - t0).toBeLessThan(500);
    expect(r.errors).toEqual([]);
    expect(r.valid).toBe(true);
  });
});

describe('validateTenantConfig — keys read as the exporter reads them', () => {
  it('resolves an escaped key to _silent_mode', () => {
    const src = tenant('    "_silent_mod\\x65": warning');
    expect(validateTenantConfig(src).valid).toBe(true);
    expect(parseYAML(src).data.tenants['db-a']).toEqual({ _silent_mode: 'warning' });
  });

  it('resolves a complex key to _silent_mode', () => {
    const src = tenant('    ? _silent_mode\n    : warning');
    expect(validateTenantConfig(src).valid).toBe(true);
    expect(parseYAML(src).data.tenants['db-a']).toEqual({ _silent_mode: 'warning' });
  });

  it('reads quoted block keys by their name', () => {
    const r = parseYAML(`"tenants":\n  db-a:\n    "_routing":\n      receiver_type: "webhook"`);
    expect(r.data.tenants['db-a']._routing).toEqual({ receiver_type: 'webhook' });
  });

  it('parses __proto__ as a tenant name without polluting the prototype', () => {
    const r = validateTenantConfig('tenants:\n  __proto__:\n    polluted: "yes"');
    expect(r.valid).toBe(true);
    expect(r.summary.tenants).toBe(1);
    const tenants = parseYAML('tenants:\n  __proto__:\n    polluted: "yes"').data.tenants;
    expect(Object.keys(tenants)).toEqual(['__proto__']);
    expect(Object.getPrototypeOf(tenants)).toBe(Object.prototype);
    expect(({} as Record<string, unknown>).polluted).toBeUndefined();
  });
});

describe('validateTenantConfig — no semantic verdicts (#2033)', () => {
  // Each of these was an error or warning under the removed rules and is
  // accepted by the exporter (or is the exporter's call, not this tool's).
  const accepted: Record<string, string> = {
    'decimal threshold': '    mysql_connections: 85.5',
    'disable three-state value': '    mysql_connections: disable',
    'value:severity form': '    mysql_connections: "500:critical"',
    'scalar _state_maintenance': '    _state_maintenance: enable',
    'compound duration': '    _routing:\n      group_wait: 1h30m',
    '_routing without receiver_type/profile': '    _routing:\n      group_wait: 30s',
    'metric key not in any hand list': '    not_a_real_metric: "50"',
    'unknown special key': '    _made_up: x',
  };
  for (const [name, body] of Object.entries(accepted)) {
    it(`accepts ${name}`, () => {
      const r = validateTenantConfig(tenant(body));
      expect(r.errors).toEqual([]);
      expect(r.valid).toBe(true);
    });
  }

  it('returns no warnings/metrics fields to render', () => {
    const r = validateTenantConfig(VALID) as Record<string, unknown>;
    expect(r.warnings).toBeUndefined();
    expect(r.metrics).toBeUndefined();
  });
});

describe('_silent_mode is not judged by the playground (#1988)', () => {
  const forms = ['warning', 'critical', 'all'];
  for (const v of forms) {
    it(`emits no error for _silent_mode: ${JSON.stringify(v)}`, () => {
      const r = validateTenantConfig(`tenants:\n  db-a:\n    mysql_connections: "70"\n    _silent_mode: ${v}`);
      expect(r.errors).toEqual([]);
    });
  }

  // A quoted key is the same key to the exporter; the parser unquotes it.
  for (const k of ['"_silent_mode"', "'_silent_mode'"]) {
    it(`reads the quoted key ${k} as _silent_mode`, () => {
      const r = validateTenantConfig(`tenants:\n  db-a:\n    mysql_connections: "70"\n    ${k}: warning`);
      expect(r.errors).toEqual([]);
      expect(parseYAML(`tenants:\n  db-a:\n    ${k}: warning`).data.tenants['db-a']).toHaveProperty('_silent_mode', 'warning');
    });
  }
});
