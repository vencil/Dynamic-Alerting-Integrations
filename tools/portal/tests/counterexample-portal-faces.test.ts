/**
 * The two portal faces that hand a platform threshold over in the BROWSER
 * (#1176, faces found by blind review in #1344).
 *
 * Neither can be asserted from pytest — one renders inside a React component,
 * the other is produced by a JS function at click time — so the content check
 * lives here. `tests/lint/test_counterexample_coverage.py` holds the other half
 * of the pair: that each component still routes through the shared renderer.
 * Wiring without content passes over a broken renderer; content without wiring
 * passes over a component that stopped calling it. Both halves or neither.
 *
 *   1. Config Template Gallery — `template-data.json` ships
 *      `oracle_sessions_active: "200"` and `db2_connections_active: "200"` as
 *      ONE-CLICK COPY yaml. Both are platform-asserted values with a measured
 *      counter-example, and the templates said nothing about either. The
 *      caveat is injected at render time rather than written into the asset:
 *      the asset is hand-maintained, so an authored caveat would be one more
 *      copy to go stale on the next recalibration.
 *   2. Multi-Tenant Comparison — labels a number "default" and flags which
 *      tenants match it, i.e. presents it as the value to align on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const RULE_PACKS_MOD = '../src/interactive/tools/_common/data/rule-packs.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoFile = (rel: string) => readFileSync(resolve(__dirname, '../../../', rel), 'utf8');

const platformData = JSON.parse(repoFile('docs/assets/platform-data.json'));
const templateData = JSON.parse(repoFile('docs/assets/template-data.json'));

/** Every (key → counterexample) the generated data carries, both tiers. */
function counterexamplesFromPlatformData(): Record<string, any> {
  const out: Record<string, any> = {};
  for (const pack of Object.values(platformData.rulePacks ?? {}) as any[]) {
    for (const [key, meta] of Object.entries(pack?.defaults ?? {}) as any[]) {
      if (meta?.valueCounterexample) out[key] = meta.valueCounterexample;
    }
  }
  for (const rows of Object.values(platformData.declaredKeys ?? {}) as any[]) {
    for (const row of rows ?? []) {
      if (row?.valueCounterexample) out[row.key] = row.valueCounterexample;
    }
  }
  return out;
}

const CE = counterexamplesFromPlatformData();

/**
 * Which counter-example keys a template hands over.
 *
 * ⛔ Deliberately NOT the annotator's own regex. The first version of this
 * helper was a character-for-character copy of the `/^([A-Za-z_]…)/` in
 * `rule-packs.js`, so any key shape the annotator failed to match dropped out
 * of the expected set too and the assertion passed over it — the two-copies-
 * one-bug shape (blind review, #1344). A token search for the KNOWN key names
 * is a different method: it finds the key wherever and however it is written,
 * including the dimensional form `key{label=~"x"}: "90"` that the annotator's
 * line-anchored regex does match but a future edit could break.
 */
function counterexampleKeysIn(yamlText: string): string[] {
  return Object.keys(CE).filter(k =>
    new RegExp(`(^|[^A-Za-z0-9_])${k}([^A-Za-z0-9_]|$)`, 'm').test(yamlText));
}

describe('counter-example data is actually present', () => {
  it('platform-data carries counter-examples on BOTH tiers', () => {
    // Guard the guard: with an empty CE map every assertion below is vacuous
    // and would read as "all portal faces covered".
    expect(Object.keys(CE).length).toBeGreaterThan(0);
    const fromDefaults = Object.values(platformData.rulePacks ?? {} as any)
      .flatMap((p: any) => Object.values(p?.defaults ?? {}))
      .filter((m: any) => m?.valueCounterexample).length;
    const fromDeclared = Object.values(platformData.declaredKeys ?? {} as any)
      .flat().filter((r: any) => r?.valueCounterexample).length;
    expect(fromDefaults).toBeGreaterThan(0);
    expect(fromDeclared).toBeGreaterThan(0);
  });
});

describe('counterexampleVerdict — lookup, not a ternary', () => {
  beforeEach(() => { vi.resetModules(); });
  afterEach(() => { delete (window as any).__t; });

  it('renders each direction with its OWN wording', async () => {
    const { counterexampleVerdict } = await import(RULE_PACKS_MOD);
    const over = counterexampleVerdict({ direction: 'over_fire' });
    const under = counterexampleVerdict({ direction: 'under_fire' });
    expect(over).toBeTruthy();
    expect(under).toBeTruthy();
    // The whole reason `direction` is an enum: a blanket caveat is false for
    // one of them. If these ever collapse to one string the enum is decoration.
    expect(over).not.toEqual(under);
  });

  it('returns null for an unrecognised direction instead of the other one', async () => {
    const { counterexampleVerdict, counterexampleComment } = await import(RULE_PACKS_MOD);
    // Both callers used to write `d === 'over_fire' ? A : B`, so a third enum
    // value rendered as under_fire's sentence — telling an operator the fault
    // is being MISSED when the measurement said it fires on benign load.
    for (const bad of [{ direction: 'sideways' }, { direction: undefined }, undefined]) {
      expect(counterexampleVerdict(bad as any)).toBeNull();
      expect(counterexampleComment(bad as any)).toBeNull();
    }
  });
});

describe('config template gallery — one-click copy YAML', () => {
  beforeEach(() => { vi.resetModules(); });

  /** The platform-asserted number for a key, read from the generated data. */
  function platformValueOf(key: string): number | null {
    for (const pack of Object.values(platformData.rulePacks ?? {}) as any[]) {
      const meta = pack?.defaults?.[key];
      if (meta?.value != null) return Number(meta.value);
    }
    for (const rows of Object.values(platformData.declaredKeys ?? {}) as any[]) {
      for (const row of rows ?? []) {
        if (row.key === key && row.value != null) return Number(row.value);
      }
    }
    return null;
  }

  /** `key: "200"` lines whose value IS the measured number. */
  function measuredLines(yamlText: string): string[] {
    return yamlText.split('\n').flatMap(line => {
      const m = /^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$/.exec(line);
      if (!m || !CE[m[1]]) return [];
      const n = Number(m[2].replace(/^["']|["']$/g, ''));
      return Number.isFinite(n) && n === platformValueOf(m[1]) ? [m[1]] : [];
    });
  }

  it('the shipped templates really do hand over the measured numbers', () => {
    const owed = new Set(
      (templateData.templates ?? []).flatMap((t: any) => measuredLines(t.yaml ?? '')),
    );
    expect(owed.size).toBeGreaterThan(0);
  });

  it('annotates every counter-example key a template presents AT the measured value', async () => {
    const { annotateCounterexamples } = await import(RULE_PACKS_MOD);
    for (const tpl of templateData.templates ?? []) {
      const annotated = annotateCounterexamples(tpl.yaml ?? '');
      for (const key of measuredLines(tpl.yaml ?? '')) {
        // Per key, its OWN observed clause — a shared marker would let one
        // annotated key vouch for every other key in the same template.
        expect(annotated, `template ${tpl.id} / key ${key}`)
          .toContain(CE[key].observed);
      }
    }
  });

  it('does NOT staple the measurement onto a recalibrated value', async () => {
    // `enterprise-db` ships `oracle_sessions_active: "150"` and
    // `finance-compliance` ships `"100"` — neither is the number that was
    // measured, so neither gets the sentence. The same rule as `_critical`,
    // applied to the base key.
    const { annotateCounterexamples } = await import(RULE_PACKS_MOD);
    const key = Object.keys(CE).find(k => platformValueOf(k) != null)!;
    const off = `${key}: "${platformValueOf(key)! + 7}"\n`;
    expect(annotateCounterexamples(off)).toEqual(off);
    const on = `${key}: "${platformValueOf(key)}"\n`;
    expect(annotateCounterexamples(on)).toContain(CE[key].observed);
  });

  it('renders nothing when the measurement itself is unusable', async () => {
    const { counterexampleComment } = await import(RULE_PACKS_MOD);
    // A blank clause renders `⚠ #1 measured counter-example: (fires on benign
    // load)`, which reads as "measured and fine" — worse than silence.
    for (const bad of [
      { issue: 1, direction: 'over_fire', observed: '' },
      { issue: 1, direction: 'over_fire', observed: '   ' },
      { issue: 1, direction: 'over_fire' },
      { direction: 'over_fire', observed: 'x' },
    ]) {
      expect(counterexampleComment(bad as any)).toBeNull();
    }
  });

  it('does NOT attach a base key\'s measurement to its _critical variant', async () => {
    // ⛔ A DECISION, pinned. `enterprise-db` ships both
    // `oracle_sessions_active: "150"` and `oracle_sessions_active_critical:
    // "250"`, and the base's measurement is "a healthy peak reaches 240".
    // Inheriting it would be actively WRONG: 240 < 250, so the critical
    // threshold does not fire on that scenario at all. A counter-example is
    // about one specific number, not about a key. Absence here means "not
    // measured for this threshold", which is the truth.
    const { getValueCounterexample, annotateCounterexamples } = await import(RULE_PACKS_MOD);
    const base = Object.keys(CE).find(k => !k.endsWith('_critical'))!;
    expect(getValueCounterexample(`${base}_critical`)).toBeNull();
    // Even at the base key's own measured value, the _critical KEY is a
    // different threshold and gets nothing.
    const yaml = `${base}_critical: "${platformValueOf(base)}"\n`;
    expect(annotateCounterexamples(yaml)).toEqual(yaml);
  });

  it('leaves a template with nothing measured byte-identical', async () => {
    const { annotateCounterexamples } = await import(RULE_PACKS_MOD);
    // Absence must render nothing at all: a template gaining a stray comment
    // would read as "checked, nothing found" rather than "never measured".
    const clean = '# nothing\nsome_unknown_key: "1"\n_metadata:\n  owner: x\n';
    expect(annotateCounterexamples(clean)).toEqual(clean);
  });

  it('ignores indented keys — only a top-level threshold is pasteable', async () => {
    const { annotateCounterexamples } = await import(RULE_PACKS_MOD);
    const key = Object.keys(CE)[0];
    const nested = `_metadata:\n  ${key}: "1"\n`;
    expect(annotateCounterexamples(nested)).toEqual(nested);
  });
});

describe('multi-tenant comparison — the metrics it labels "default"', () => {
  it('every DEFAULTS key with a counter-example resolves through the accessor', async () => {
    const { getValueCounterexample, counterexampleVerdict } = await import(RULE_PACKS_MOD);
    const { DEFAULTS } = await import(
      '../src/interactive/tools/multi-tenant-comparison/calc.js');
    const owed = Object.keys(DEFAULTS).filter(k => CE[k]);
    // Non-vacuity: this view must actually present at least one such key, or
    // the component's caveat block is unreachable and the face is theatre.
    expect(owed.length).toBeGreaterThan(0);
    for (const key of owed) {
      const ce = getValueCounterexample(key);
      expect(ce, `accessor lost ${key}`).toBeTruthy();
      expect(ce.observed).toEqual(CE[key].observed);
      expect(counterexampleVerdict(ce), `no verdict for ${key}`).toBeTruthy();
    }
  });

  it('the number it calls "default" is still the platform number', async () => {
    // The caveat is derived, but the figure beside it is a hand-written
    // literal in `calc.js`. Fresh sentence next to a stale number is worse
    // than either alone, so pin the ones the registry owns. Keys with no
    // registry entry are this tool's own demo metrics and are out of scope.
    const { RULE_PACK_DATA } = await import(RULE_PACKS_MOD);
    const { DEFAULTS } = await import(
      '../src/interactive/tools/multi-tenant-comparison/calc.js');
    const platform: Record<string, number> = {};
    for (const pack of Object.values(RULE_PACK_DATA) as any[]) {
      for (const [k, meta] of Object.entries(pack?.defaults ?? {}) as any[]) {
        platform[k] = Number(meta.value);
      }
    }
    const shared = Object.keys(DEFAULTS).filter(k => k in platform);
    expect(shared.length).toBeGreaterThan(0);
    for (const key of shared) {
      expect(Number(DEFAULTS[key]), `${key} drifted from the platform value`)
        .toEqual(platform[key]);
    }
  });
});
