/**
 * declared-keys — the portal side of the "platform recognises it but asserts
 * no value" tier (#1321, after #1318/#1325 shipped the runtime slot).
 *
 * Why this file exists, in one sentence: before it, typing one of these keys
 * into the YAML validator produced "Metric key not found in selected Rule Pack
 * defaults" — while the platform's own tenant documentation told the reader to
 * set exactly those keys. That is an active counter-signal, not a gap, and it
 * is the thing pinned below.
 *
 * Two invariants worth stating because they are easy to "simplify" away:
 *   1. declared keys are NOT part of getAllMetricKeys. That list means "has a
 *      platform default" and its consumers spread `value` from it; folding
 *      these in would hand them a number the platform does not stand behind.
 *   2. DECLARED_KEYS lives at the TOP LEVEL of platform-data.json, not under
 *      rulePacks[*]. The inline catalog in rule-packs.js is a hand-written
 *      mirror that rule-packs-fallback-drift.test.ts deep-equals per pack; a
 *      per-pack field would have to be hand-copied into it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const RULE_PACKS_MOD = '../src/interactive/tools/_common/data/rule-packs.js';
const ENGINE_MOD = '../src/interactive/tools/_common/sim/alert-engine.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const platformData = JSON.parse(
  readFileSync(resolve(__dirname, '../../../docs/assets/platform-data.json'), 'utf8'),
);
const PD_DECLARED: Record<string, Array<{ key: string }>> = platformData.declaredKeys;

const PACK = 'oracle';
const DECLARED_KEY = 'oracle_process_count';

describe('declaredKeys — generated data', () => {
  it('platform-data.json actually carries a non-empty declaredKeys block', () => {
    // Guard the guard: if the generator stopped emitting it, every assertion
    // below would pass vacuously against undefined.
    expect(PD_DECLARED).toBeTypeOf('object');
    expect(Object.keys(PD_DECLARED).length).toBeGreaterThan(0);
    const all = Object.values(PD_DECLARED).flat().map(r => r.key);
    expect(all).toContain(DECLARED_KEY);
  });

  it('is top-level, not nested under rulePacks', () => {
    // Pins invariant 2. If someone moves it under rulePacks[*], the hand-written
    // fallback in rule-packs.js silently stops mirroring platform-data.
    for (const pack of Object.values(platformData.rulePacks as Record<string, any>)) {
      expect(pack).not.toHaveProperty('declaredKeys');
    }
  });

  it('never carries a _critical spelling', () => {
    // resolveCriticalRows resolves those off defaults[base]; they are not part
    // of this tier and putting them here would advertise a dead path (#1311).
    const all = Object.values(PD_DECLARED).flat().map(r => r.key);
    expect(all.filter(k => k.endsWith('_critical'))).toEqual([]);
  });
});

describe('rule-packs.js accessors', () => {
  beforeEach(() => {
    vi.resetModules();
    delete (window as any).__PLATFORM_DATA;
  });
  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
  });

  it('reads declaredKeys from window.__PLATFORM_DATA', async () => {
    (window as any).__PLATFORM_DATA = { declaredKeys: PD_DECLARED };
    const { DECLARED_KEYS, getDeclaredKeys } = await import(RULE_PACKS_MOD);
    expect(Object.keys(DECLARED_KEYS).sort()).toEqual(Object.keys(PD_DECLARED).sort());
    expect(getDeclaredKeys([PACK]).map((m: any) => m.key))
      .toEqual(PD_DECLARED[PACK].map(r => r.key));
  });

  it('degrades to an empty map offline instead of throwing', async () => {
    // No __PLATFORM_DATA at all — the standalone/offline bundle path.
    const { DECLARED_KEYS, getDeclaredKeys } = await import(RULE_PACKS_MOD);
    expect(DECLARED_KEYS).toEqual({});
    expect(getDeclaredKeys([PACK])).toEqual([]);
    expect(getDeclaredKeys()).toEqual([]);
  });

  it('filters by selected pack', async () => {
    (window as any).__PLATFORM_DATA = { declaredKeys: PD_DECLARED };
    const { getDeclaredKeys } = await import(RULE_PACKS_MOD);
    const other = Object.keys(PD_DECLARED).find(p => p !== PACK)!;
    const got = getDeclaredKeys([PACK]).map((m: any) => m.key);
    const otherKeys = PD_DECLARED[other].map(r => r.key);
    expect(got.some((k: string) => otherKeys.includes(k))).toBe(false);
  });

  it('keeps declared keys OUT of getAllMetricKeys', async () => {
    // Pins invariant 1. Consumers of getAllMetricKeys read `value` as a
    // platform default; these keys have none.
    (window as any).__PLATFORM_DATA = {
      declaredKeys: PD_DECLARED,
      rulePacks: platformData.rulePacks,
      packOrder: platformData.packOrder,
    };
    const { getAllMetricKeys } = await import(RULE_PACKS_MOD);
    const defaults = getAllMetricKeys([PACK]).map((m: any) => m.key);
    for (const row of PD_DECLARED[PACK]) expect(defaults).not.toContain(row.key);
  });
});

describe('alert-engine — a declared key must not be called unknown', () => {
  beforeEach(() => {
    vi.resetModules();
    (window as any).__t = (_zh: string, en: string) => en;
    (window as any).__PLATFORM_DATA = {
      declaredKeys: PD_DECLARED,
      rulePacks: platformData.rulePacks,
      packOrder: platformData.packOrder,
    };
  });
  afterEach(() => {
    delete (window as any).__PLATFORM_DATA;
    delete (window as any).__t;
  });

  const msgs = (out: any, field: string) =>
    out.issues.filter((i: any) => i.field === field).map((i: any) => i.msg);

  it('says the true thing about a declared key, not "not found"', async () => {
    const { validateConfig } = await import(ENGINE_MOD);
    const out = validateConfig({ [DECLARED_KEY]: '400' }, [PACK]);
    const m = msgs(out, DECLARED_KEY);
    expect(m.join(' ')).not.toMatch(/not found in selected Rule Pack defaults/);
    expect(m.join(' ')).toMatch(/no platform default/i);
  });

  it('still flags a genuinely unknown key', async () => {
    // The counter-case: the original message must survive for keys that really
    // are unknown, otherwise this change just deletes a useful signal.
    const { validateConfig } = await import(ENGINE_MOD);
    const out = validateConfig({ oracle_not_a_real_key: '1' }, [PACK]);
    expect(msgs(out, 'oracle_not_a_real_key').join(' '))
      .toMatch(/not found in selected Rule Pack defaults/);
  });

  it('leaves a key that does have a platform default alone', async () => {
    const { validateConfig } = await import(ENGINE_MOD);
    const withDefault = Object.keys(platformData.rulePacks[PACK].defaults)[0];
    const m = msgs(validateConfig({ [withDefault]: '50' }, [PACK]), withDefault);
    expect(m.join(' ')).not.toMatch(/not found in selected Rule Pack defaults/);
    expect(m.join(' ')).not.toMatch(/no platform default/i);
  });

  it('does not change the _critical base-key path', async () => {
    // `<base>_critical` resolves via defaults[base]; that lookup predates this
    // change and must keep working.
    const { validateConfig } = await import(ENGINE_MOD);
    const withDefault = Object.keys(platformData.rulePacks[PACK].defaults)[0];
    const field = `${withDefault}_critical`;
    expect(msgs(validateConfig({ [field]: '95' }, [PACK]), field).join(' '))
      .not.toMatch(/not found in selected Rule Pack defaults/);
  });
});
