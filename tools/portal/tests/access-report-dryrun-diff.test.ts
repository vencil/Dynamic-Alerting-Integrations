/**
 * Unit tests for access-report-dryrun/diff-view.js (LD-6 P7c).
 *
 * JS-vs-JS golden assertions (⛔ NOT regex) over the pure presentation
 * logic. Each assertion is written to FAIL if its specific rule breaks
 * (mutation-consciousness): the fixtures are chosen so a happy-path
 * shortcut (subscripting by index, dropping a false axis, an empty-diff
 * "no change" claim) cannot pass vacuously.
 *
 * A ZH translator is passed so the golden strings are the Chinese-primary
 * render; the module's own fallback is en-only, exercised where noted.
 */
import { describe, it, expect } from 'vitest';
import {
  AXES,
  fmtAxis,
  fmtBool,
  axisRows,
  glossOutcome,
  glossVerdict,
  glossMode,
  glossFor,
  OUTCOME_GLOSS,
  findByIndex,
  grantSetIdentical,
  grantSignature,
  isEmptyDiff,
} from '../src/interactive/tools/access-report-dryrun/diff-view.js';

// ZH-primary translator → assert the exact Chinese-first render.
const tzh = (zh: string, _en: string) => zh;
// en-only fallback shape (what the module uses when no t is supplied).
const ten = (_zh: string, en: string) => en;

describe('AXES (R2 frozen render driver)', () => {
  it('is the three diffable axes in a fixed, frozen order', () => {
    expect(AXES.map((a) => a.key)).toEqual([
      'outcome_shadow',
      'outcome_enforce',
      'unsatisfiable',
    ]);
    // Frozen: a render-time push/reorder must throw, not silently mutate.
    expect(Object.isFrozen(AXES)).toBe(true);
    expect(() => {
      // @ts-expect-error deliberate mutation attempt
      AXES.push({ key: 'x' });
    }).toThrow();
  });
});

describe('fmtAxis (R2 absent = unchanged; false must print)', () => {
  it('renders an ABSENT axis as the 未變更 sentinel, not blank/undefined', () => {
    const r = fmtAxis(undefined, tzh);
    expect(r.absent).toBe(true);
    expect(r.text).toBe('未變更');
    // and null is treated the same as undefined
    expect(fmtAxis(null, tzh).text).toBe('未變更');
  });

  it('renders BoolDelta{from:false} as 否 (never the empty string React gives {false})', () => {
    const r = fmtAxis({ from: false, to: true }, tzh);
    expect(r.absent).toBe(false);
    expect(r.from).toBe('否'); // the load-bearing case: false → 否, not ''
    expect(r.to).toBe('是');
  });

  it('renders a string (outcome) delta via the outcome gloss', () => {
    const r = fmtAxis({ from: 'not_required', to: 'conditional_on_caller_org' }, tzh);
    expect(r.from).toBe('not_required（無需 org 範圍）');
    expect(r.to).toBe('conditional_on_caller_org（取決於呼叫者 org 值）');
  });
});

describe('fmtBool', () => {
  it('false → 否, true → 是 (ZH); en fallback', () => {
    expect(fmtBool(false, tzh)).toBe('否');
    expect(fmtBool(true, tzh)).toBe('是');
    expect(fmtBool(false, ten)).toBe('no');
  });
});

describe('axisRows (drives changed-card render off frozen AXES)', () => {
  it('a server-UNREACHABLE row with ZERO axis deltas still yields three absent rows (no crash)', () => {
    // The server never emits a changed entry with no deltas (changedEntry
    // returns nil) — but a fabricated / torn row must survive rendering.
    const rows = axisRows(
      { rule: 'x', live_index: 0, candidate_index: 0 } as any,
      tzh,
    );
    expect(rows).toHaveLength(3);
    expect(rows.map((r: any) => r.key)).toEqual([
      'outcome_shadow',
      'outcome_enforce',
      'unsatisfiable',
    ]);
    for (const r of rows as any[]) {
      expect(r.absent).toBe(true);
      expect(r.text).toBe('未變更');
    }
  });

  it('a mixed row renders the present axes and marks the rest unchanged', () => {
    const rows = axisRows(
      {
        rule: 'x',
        live_index: 2,
        candidate_index: 2,
        // only unsatisfiable changed; the two outcome axes are absent
        unsatisfiable: { from: false, to: true },
      } as any,
      tzh,
    );
    const byKey: any = Object.fromEntries((rows as any[]).map((r) => [r.key, r]));
    expect(byKey.outcome_shadow.absent).toBe(true);
    expect(byKey.outcome_enforce.absent).toBe(true);
    expect(byKey.unsatisfiable.absent).toBe(false);
    expect(byKey.unsatisfiable.from).toBe('否');
    expect(byKey.unsatisfiable.to).toBe('是');
  });
});

describe('token gloss + unknown/prototype passthrough (R8)', () => {
  it('glosses known outcome / verdict / mode tokens ZH-first', () => {
    expect(glossOutcome('fail_unlabeled', tzh)).toBe('fail_unlabeled（未標記租戶：enforce 拒絕）');
    expect(glossVerdict('open_read', tzh)).toBe('open_read（開放讀取——任何已認證者皆可讀）');
    expect(glossMode('rules', tzh)).toBe('rules（規則模式）');
  });

  it('passes an UNKNOWN token through verbatim (server grew a new enum)', () => {
    expect(glossOutcome('brand_new_token', tzh)).toBe('brand_new_token');
    expect(glossVerdict('brand_new_verdict', tzh)).toBe('brand_new_verdict');
    expect(glossMode('brand_new_mode', tzh)).toBe('brand_new_mode');
  });

  it('does NOT render an inherited prototype value as a gloss (own-property guard)', () => {
    // "constructor"/"toString" exist on Object.prototype but are not own
    // keys of the gloss map → passthrough verbatim, never a prototype object.
    expect(glossFor(OUTCOME_GLOSS, 'constructor', tzh)).toBe('constructor');
    expect(glossFor(OUTCOME_GLOSS, 'toString', tzh)).toBe('toString');
  });
});

describe('findByIndex (R5 — .index is a cfg position, never a grants[] offset)', () => {
  // SPARSE fixture: .index != array offset, so a grants[index] subscript
  // would return the WRONG element (or undefined). A happy-path fixture
  // where index==offset would hide exactly that bug.
  const grants = [{ index: 1, rule: 'x' }, { index: 4, rule: 'y' }];

  it('resolves by matching .index, not by position', () => {
    // index 4 lives at offset 1 — grants[4] would be undefined.
    expect(findByIndex(grants, 4)).toEqual({ index: 4, rule: 'y' });
    // index 1 lives at offset 0 — grants[1] would be the index:4 element.
    expect(findByIndex(grants, 1)).toEqual({ index: 1, rule: 'x' });
    expect((findByIndex(grants, 1) as any).rule).toBe('x');
  });

  it('returns undefined for an index that is not present (caller must guard)', () => {
    expect(findByIndex(grants, 0)).toBeUndefined();
    expect(findByIndex(grants, 2)).toBeUndefined();
    expect(findByIndex(undefined as any, 1)).toBeUndefined();
  });
});

describe('grantSetIdentical (R9 — multiset arithmetic, index excluded, single-directional)', () => {
  const g = (over: any = {}) => ({
    index: 0,
    rule: 'r',
    platform_wide: false,
    permissions: ['read'],
    effective: { read: true, write: false, admin: false },
    org_gate: { outcome_shadow: 'not_required', outcome_enforce: 'not_required', unsatisfiable: false },
    ...over,
  });

  it('is TRUE for the same set in a different order and with different indexes', () => {
    const a = [g({ index: 1, rule: 'a' }), g({ index: 2, rule: 'b' })];
    const b = [g({ index: 7, rule: 'b' }), g({ index: 9, rule: 'a' })];
    expect(grantSetIdentical(a, b)).toBe(true);
  });

  it('excludes index — a set differing ONLY by index is identical', () => {
    expect(grantSetIdentical([g({ index: 1 })], [g({ index: 99 })])).toBe(true);
  });

  it('is FALSE when a non-index field differs (e.g. permissions)', () => {
    expect(grantSetIdentical([g()], [g({ permissions: ['read', 'admin'] })])).toBe(false);
  });

  it('is FALSE on a length mismatch', () => {
    expect(grantSetIdentical([g(), g({ rule: 'b' })], [g()])).toBe(false);
  });

  it('grantSignature drops index but keeps every other field', () => {
    expect(grantSignature(g({ index: 5 }))).toBe(grantSignature(g({ index: 6 })));
    expect(grantSignature(g())).not.toBe(grantSignature(g({ platform_wide: true })));
  });
});

describe('isEmptyDiff (R1 — empty is a measurement, not a "no change" claim)', () => {
  it('true only when all three buckets are empty', () => {
    expect(isEmptyDiff({ changed: [], added: [], removed: [] })).toBe(true);
    expect(isEmptyDiff(null)).toBe(true);
    expect(isEmptyDiff({ changed: [{}], added: [], removed: [] })).toBe(false);
    expect(isEmptyDiff({ changed: [], added: [{}], removed: [] })).toBe(false);
    expect(isEmptyDiff({ changed: [], added: [], removed: [{}] })).toBe(false);
  });
});
