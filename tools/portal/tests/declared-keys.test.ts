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
 *      rulePacks[*] — and NOT because the existing drift gate forces it.
 *      `carried()` in rule-packs-fallback-drift.test.ts is a ten-field
 *      whitelist, so a new per-pack field would be dropped on both sides and
 *      never compared. That is precisely why: a per-pack field would still
 *      need hand-copying into the inline catalog for the offline path, with
 *      nothing to catch it. Top-level gets its own drift gate — this file.
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
    // Pins invariant 2. Moving it under rulePacks[*] would put it inside a
    // subtree whose drift gate whitelists ten fields — so the inline fallback
    // would silently stop mirroring platform-data with nothing turning red.
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

  it('offline fallback mirrors platform-data.json key-for-key', async () => {
    // No __PLATFORM_DATA at all — the standalone / file:// / fetch-failed path.
    // An EMPTY fallback would put the validator straight back to calling these
    // keys unknown, which is the whole bug this change exists to remove; so the
    // fallback carries the real list and this is its drift gate (same shape as
    // images-fallback-drift.test.ts).
    const { DECLARED_KEYS, getDeclaredKeys } = await import(RULE_PACKS_MOD);
    expect(Object.keys(DECLARED_KEYS).sort()).toEqual(Object.keys(PD_DECLARED).sort());
    for (const pack of Object.keys(PD_DECLARED)) {
      expect(DECLARED_KEYS[pack]).toEqual(PD_DECLARED[pack]);
    }
    expect(getDeclaredKeys([PACK]).length).toBeGreaterThan(0);
  });

  it('offline still refuses to call a declared key unknown', async () => {
    // The property the fallback exists for, asserted end-to-end rather than by
    // inspecting the constant.
    (window as any).__t = (_zh: string, en: string) => en;
    const { validateConfig } = await import(ENGINE_MOD);
    const out = validateConfig({ [DECLARED_KEY]: '400' }, [PACK]);
    const m = out.issues.filter((i: any) => i.field === DECLARED_KEY).map((i: any) => i.msg).join(' ');
    expect(m).not.toMatch(/not found in selected Rule Pack defaults/);
    expect(m).toMatch(/no platform default/i);
    delete (window as any).__t;
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

  it('never tells a tenant that <declared_base>_critical will take effect', async () => {
    // ⛔ The one place the two membership sets must NOT be unioned.
    // ValidateTenantKeys refuses this shape with a BLOCKING error (its comment
    // in pkg/config/resolve.go says exactly that), because resolveCriticalRows
    // keys off defaults[base] and drops the row when the base has no value.
    // Stripping `_critical` and matching the declared set would produce a
    // confident "it takes effect once you set it" for a write tenant-api
    // answers with HTTP 400 — worse than the vague message it replaced.
    const { validateConfig } = await import(ENGINE_MOD);
    const field = `${DECLARED_KEY}_critical`;
    const m = msgs(validateConfig({ [field]: '500' }, [PACK]), field).join(' ');
    expect(m).not.toMatch(/takes effect only once you set it/);
    expect(m).toMatch(/rejected on write/i);
    expect(m).toContain(DECLARED_KEY);
  });
});

describe('generateSampleYaml — the starter template must not hide the tier', () => {
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

  it('lists the declared keys, commented, with no live value', async () => {
    const { generateSampleYaml } = await import(ENGINE_MOD);
    const yaml = generateSampleYaml([PACK], false);
    for (const row of PD_DECLARED[PACK]) {
      const line = yaml.split('\n').find((l: string) => l.includes(row.key));
      expect(line, `${row.key} missing from the starter template`).toBeTruthy();
      // Commented — an uncommented `key: "300"` would arm a number the
      // platform does not stand behind (#1176 measured these false-alarming).
      expect(line!.trimStart().startsWith('#')).toBe(true);
      expect(line).not.toMatch(new RegExp(`^\\s*${row.key}\\s*:`));
    }
  });

  it('still emits the real defaults as live values', async () => {
    // Counter-case: the commenting rule must not leak onto keys that DO have a
    // platform value.
    const { generateSampleYaml } = await import(ENGINE_MOD);
    const yaml = generateSampleYaml([PACK], false);
    const withDefault = Object.keys(platformData.rulePacks[PACK].defaults)[0];
    expect(yaml).toMatch(new RegExp(`^${withDefault}: "`, 'm'));
  });
});
