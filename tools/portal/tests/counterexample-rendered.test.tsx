/**
 * What the three LIVE-value portal faces actually RENDER (#1176 / #1344).
 *
 * ⛔ Why a second file: `counterexample-portal-faces.test.ts` asserts the
 * shared renderers behave, and `tests/lint/test_counterexample_coverage.py`
 * asserts each component still imports and calls them. Neither reads the
 * OUTPUT of a component. Blind review walked straight through that gap: delete
 * `{ce.observed}` from the comparison view, or the declared-tier caveat from
 * the starter template, and every gate stayed green. A wiring check plus a
 * unit check is not a rendering check.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const repoFile = (rel: string) => readFileSync(resolve(__dirname, '../../../', rel), 'utf8');
const platformData = JSON.parse(repoFile('docs/assets/platform-data.json'));

/** (key, counterexample) for the DECLARED tier — the one with no platform value. */
function declaredCounterexamples(): Array<[string, any]> {
  const out: Array<[string, any]> = [];
  for (const rows of Object.values(platformData.declaredKeys ?? {}) as any[]) {
    for (const row of rows ?? []) {
      if (row?.valueCounterexample) out.push([row.key, row.valueCounterexample]);
    }
  }
  return out;
}

/** (key, counterexample) for the DEFAULTS tier, with its owning pack. */
function defaultsCounterexamples(): Array<[string, string, any]> {
  const out: Array<[string, string, any]> = [];
  for (const [packId, pack] of Object.entries(platformData.rulePacks ?? {}) as any[]) {
    for (const [key, meta] of Object.entries(pack?.defaults ?? {}) as any[]) {
      if (meta?.valueCounterexample) out.push([packId, key, meta.valueCounterexample]);
    }
  }
  return out;
}

// ⛔ Every test here pins the locale explicitly. `window.__t` is a process
// global that other portal suites set and do not always restore, and since
// #1344 the rendered clause DEPENDS on it (`observed` vs `observed_zh`) — so a
// test that inherits the ambient locale asserts the wrong string and hangs on
// `findBy*`. Passed alone, failed in the full run; that difference was the bug.
const EN = (_zh: string, en: string) => en;
const ZH = (zh: string, _en: string) => zh;

describe('multi-tenant comparison — rendered output', () => {
  // ⛔ No `vi.resetModules()` here. `test-setup.ts` already pins `window.__t`
  // to the English helper for every test, and these two components are the
  // most expensive transforms in the suite — resetting the module graph before
  // each test made them re-transform under full-suite CPU contention and blow
  // the 5s timeout, while passing in isolation. Only the ZH case, which has to
  // re-evaluate a module-scope `const t = window.__t`, pays that cost.
  beforeEach(() => { (window as any).__t = EN; });

  it('renders the selected metric\'s OWN measurement, not just a verdict', async () => {
    const { DEFAULTS } = await import(
      '../src/interactive/tools/multi-tenant-comparison/calc.js');
    const { default: MultiTenantComparison } = await import(
      '../src/interactive/tools/multi-tenant-comparison.jsx');
    const { getValueCounterexample } = await import(
      '../src/interactive/tools/_common/data/rule-packs.js');

    const key = Object.keys(DEFAULTS).find(k => getValueCounterexample(k));
    expect(key, 'no comparison metric carries a counter-example — face is dead').toBeTruthy();
    const ce = getValueCounterexample(key!);

    render(<MultiTenantComparison />);
    const select = screen.getByRole('combobox') as HTMLSelectElement;
    // The default selection is a metric with nothing measured, which is itself
    // the "absence renders nothing" case.
    expect(screen.queryByTestId('counterexample-note')).toBeNull();

    fireEvent.change(select, { target: { value: key! } });

    const note = await screen.findByTestId('counterexample-note');
    expect(note.textContent).toContain(ce.observed);
    expect(note.textContent).toContain(String(ce.issue));
    // ⛔ 20s, not the 5s default. This is the first test in the file to pull in
    // the comparison component's whole module graph, and under full-suite
    // worker contention that import alone exceeded 5s — it passed in isolation
    // and timed out in `vitest run`. A sibling integration test in this suite
    // already takes 3.7s. The assertion is unchanged; only the clock is.
  }, 20_000);

  it('renders the ZH clause, not the English one, in the ZH locale', async () => {
    // The point of `observed_zh` (owner decision, #1344): the ZH reader gets a
    // Chinese sentence, not a Chinese frame around an English clause.
    (window as any).__t = ZH;
    vi.resetModules();
    const { DEFAULTS } = await import(
      '../src/interactive/tools/multi-tenant-comparison/calc.js');
    const { default: MultiTenantComparison } = await import(
      '../src/interactive/tools/multi-tenant-comparison.jsx');
    const { getValueCounterexample } = await import(
      '../src/interactive/tools/_common/data/rule-packs.js');
    const key = Object.keys(DEFAULTS).find(k => getValueCounterexample(k))!;
    const ce = getValueCounterexample(key);
    expect(ce.observed_zh, `${key} has no observed_zh`).toBeTruthy();

    render(<MultiTenantComparison />);
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: key } });
    const note = await screen.findByTestId('counterexample-note');
    expect(note.textContent).toContain(ce.observed_zh);
    expect(note.textContent).not.toContain(ce.observed);
  });

  it('does not put CJK brackets in an English sentence', async () => {
    (window as any).__t = EN;
    vi.resetModules();
    const { DEFAULTS } = await import(
      '../src/interactive/tools/multi-tenant-comparison/calc.js');
    const { default: MultiTenantComparison } = await import(
      '../src/interactive/tools/multi-tenant-comparison.jsx');
    const { getValueCounterexample } = await import(
      '../src/interactive/tools/_common/data/rule-packs.js');
    const key = Object.keys(DEFAULTS).find(k => getValueCounterexample(k))!;

    render(<MultiTenantComparison />);
    const select = screen.getAllByRole('combobox')[0] as HTMLSelectElement;
    fireEvent.change(select, { target: { value: key } });

    const note = await screen.findByTestId('counterexample-note');
    // The punctuation used to be literal `：（）` while only the words went
    // through t(), so EN read `counter-example: …（fires on benign load）`.
    expect(note.textContent).not.toMatch(/[：（）]/);
  });
});

describe('starter YAML — both tiers carry their caveat', () => {
  // ⛔ No `vi.resetModules()` here. `test-setup.ts` already pins `window.__t`
  // to the English helper for every test, and these two components are the
  // most expensive transforms in the suite — resetting the module graph before
  // each test made them re-transform under full-suite CPU contention and blow
  // the 5s timeout, while passing in isolation. Only the ZH case, which has to
  // re-evaluate a module-scope `const t = window.__t`, pays that cost.
  beforeEach(() => { (window as any).__t = EN; });

  it('annotates the DECLARED tier, not only the defaults loop', async () => {
    const { generateSampleYaml } = await import(
      '../src/interactive/tools/_common/sim/alert-engine.js');
    const declared = declaredCounterexamples();
    // Non-vacuity: `declared-keys.test.ts` proves the same loop with
    // `declaredKeys: {}` injected, which is precisely why this tier's caveat
    // had zero coverage. Assert against the REAL shipped data.
    expect(declared.length).toBeGreaterThan(0);

    const packs = Object.keys(platformData.declaredKeys ?? {});
    const yaml = generateSampleYaml(packs, false);
    for (const [key, ce] of declared) {
      expect(yaml, `declared key ${key}`).toContain(ce.observed);
    }
  });

  it('annotates the DEFAULTS tier for the pack that owns each key', async () => {
    const { generateSampleYaml } = await import(
      '../src/interactive/tools/_common/sim/alert-engine.js');
    const rows = defaultsCounterexamples();
    expect(rows.length).toBeGreaterThan(0);
    for (const [packId, key, ce] of rows) {
      const yaml = generateSampleYaml([packId], false);
      expect(yaml, `${packId} / ${key}`).toContain(ce.observed);
    }
  });
});

describe('YAML validator — insert-metric hands over a live value', () => {
  // ⛔ No `vi.resetModules()` here. `test-setup.ts` already pins `window.__t`
  // to the English helper for every test, and these two components are the
  // most expensive transforms in the suite — resetting the module graph before
  // each test made them re-transform under full-suite CPU contention and blow
  // the 5s timeout, while passing in isolation. Only the ZH case, which has to
  // re-evaluate a module-scope `const t = window.__t`, pays that cost.
  beforeEach(() => { (window as any).__t = EN; });

  it('inserts the caveat immediately above the value it inserts', async () => {
    const { insertMetricIntoYaml } = await import(
      '../src/interactive/tools/YamlValidatorTab.jsx');
    const { RULE_PACK_DATA } = await import(
      '../src/interactive/tools/_common/data/rule-packs.js');
    const [packId, key, ce] = defaultsCounterexamples()[0];
    const meta = (RULE_PACK_DATA as any)[packId].defaults[key];

    const out = insertMetricIntoYaml('_routing_profile: x\n',
      { key, value: meta.value, desc: meta.desc });
    const lines = out.split('\n');
    const valueIdx = lines.findIndex(l => l.trim().startsWith(`${key}:`));
    expect(valueIdx, `${key} was not inserted`).toBeGreaterThan(-1);
    // ⛔ Adjacency AND order: a caveat below the value it qualifies reads as
    // belonging to the next key.
    expect(lines[valueIdx - 1]).toContain(ce.observed);
    expect(lines[valueIdx - 1].trim().startsWith('#')).toBe(true);
  });

  it('inserts exactly one line for a key with nothing measured', async () => {
    const { insertMetricIntoYaml } = await import(
      '../src/interactive/tools/YamlValidatorTab.jsx');
    // Absence must stay absence: an extra comment would read as "checked,
    // nothing found" rather than "never measured".
    const out = insertMetricIntoYaml('', { key: 'no_such_metric', value: 1, desc: 'd' });
    expect(out.split('\n').filter(l => l.trim()).length).toBe(1);
  });
});

describe('config template gallery — what the Copy button actually gets', () => {
  // ⛔ No `vi.resetModules()` here. `test-setup.ts` already pins `window.__t`
  // to the English helper for every test, and these two components are the
  // most expensive transforms in the suite — resetting the module graph before
  // each test made them re-transform under full-suite CPU contention and blow
  // the 5s timeout, while passing in isolation. Only the ZH case, which has to
  // re-evaluate a module-scope `const t = window.__t`, pays that cost.
  beforeEach(() => { (window as any).__t = EN; });

  it('preview and clipboard carry the same annotated text', async () => {
    // ⛔ The component, not the annotator. Wiring (pytest) plus unit (the
    // sibling Vitest) both pass while the preview is annotated and the Copy
    // handler sends `raw.yaml` — the reader sees a caveat, clicks Copy, and
    // pastes a bare threshold. That is worse than never annotating.
    const { default: TemplateGallery } = await import(
      '../src/interactive/tools/template-gallery.jsx');
    const templateData = JSON.parse(repoFile('docs/assets/template-data.json'));
    // Pick the template that actually carries a measured key at its measured
    // value, and drive the UI to THAT card — clicking whichever card happens
    // to be first is how this assertion would silently move off its subject.
    const platform = new Map<string, number>();
    const measured = new Map<string, any>();
    for (const pack of Object.values(platformData.rulePacks ?? {}) as any[]) {
      for (const [k, meta] of Object.entries(pack?.defaults ?? {}) as any[]) {
        if (meta?.value != null) platform.set(k, Number(meta.value));
        if (meta?.valueCounterexample) measured.set(k, meta.valueCounterexample);
      }
    }
    // ⛔ At the MEASURED value. `enterprise-db` ships
    // `oracle_sessions_active: "150"`, which is deliberately NOT annotated —
    // picking it would have made this test assert the opposite of the rule.
    const atMeasured = (yaml: string) => [...measured.keys()].find(k => {
      const m = new RegExp(`^${k}:\\s*"?([0-9.]+)"?`, 'm').exec(yaml ?? '');
      return m && Number(m[1]) === platform.get(k);
    });
    const tpl = (templateData.templates ?? []).find((t: any) => atMeasured(t.yaml));
    expect(tpl, 'no shipped template hands over a key AT its measured value').toBeTruthy();
    const key = atMeasured(tpl.yaml)!;
    const ce = measured.get(key)!;

    const copied: string[] = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: (t: string) => { copied.push(t); return Promise.resolve(); } },
    });
    (globalThis as any).fetch = () =>
      Promise.resolve({ ok: true, json: () => Promise.resolve(templateData) });

    render(<TemplateGallery />);
    // The gallery opens on `scenarios`; the measured keys ship in the
    // quick-start templates, so switch views the way a reader would.
    const allBtns = await screen.findAllByRole('button', { name: /^(全部|All)/ });
    allBtns.forEach(b => fireEvent.click(b));
    const card = await screen.findByText(tpl.name.en ?? tpl.name.zh);
    fireEvent.click(card);
    await waitFor(() => {
      const pres = Array.from(document.querySelectorAll('pre'))
        .map(p => p.textContent ?? '');
      expect(pres.some(txt => txt.includes(key))).toBe(true);
    });
    const shown = Array.from(document.querySelectorAll('pre'))
      .map(p => p.textContent ?? '').find(txt => txt.includes(key))!;
    expect(shown).toContain(ce.observed);

    const copyBtn = screen.getAllByRole('button')
      .find(b => /copy|複製/i.test(b.textContent ?? ''));
    expect(copyBtn, 'no copy control').toBeTruthy();
    fireEvent.click(copyBtn!);
    // The hook writes in a microtask, so the assertion has to wait for it —
    // a synchronous check here would pass on an empty array forever.
    await waitFor(() => expect(copied.length).toBeGreaterThan(0));
    expect(copied.join('\n')).toContain(ce.observed);
    expect(copied.join('\n')).toEqual(shown);
    // Same reason as the comparison test above: heavy first-import under
    // full-suite contention, not a hang.
  }, 20_000);
});
