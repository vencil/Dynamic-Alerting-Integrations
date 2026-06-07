/**
 * roi-calculator/calc.js — savings & reduction models.
 *
 * Extracted from roi-calculator.jsx (PR-portal-13), previously 0%-covered.
 * Expected values below are hand-derived from the documented formulas (clean
 * integer inputs) and paired with structural invariants (zero-guard, caps,
 * monotonicity) so the suite locks behavior rather than echoing the code.
 */
import { describe, it, expect } from 'vitest';
import {
  calcRuleMaintenance,
  calcAlertStormReduction,
  calcTimeToMarket,
  calcAnnualSavings,
} from '../src/interactive/tools/roi-calculator/calc.js';

describe('calcRuleMaintenance', () => {
  it('models O(N×M)→O(M): without = N×M×T×C/60, with = M×T×C/60', () => {
    const r = calcRuleMaintenance({ tenants: 20, packs: 15, changeMinutes: 30, changesPerMonth: 4 });
    expect(r.withoutHours).toBe(600);
    expect(r.withHours).toBe(30);
    expect(r.savedHours).toBe(570);
    expect(r.reduction).toBeCloseTo(95, 10);
  });

  it('guards reduction against zero workload (no divide-by-zero)', () => {
    const r = calcRuleMaintenance({ tenants: 0, packs: 15, changeMinutes: 30, changesPerMonth: 4 });
    expect(r.withoutHours).toBe(0);
    expect(r.savedHours).toBe(0);
    expect(r.reduction).toBe(0);
  });

  it('reduction never exceeds 100%', () => {
    const r = calcRuleMaintenance({ tenants: 999, packs: 50, changeMinutes: 60, changesPerMonth: 10 });
    expect(r.reduction).toBeLessThanOrEqual(100);
  });
});

describe('calcAlertStormReduction', () => {
  it('combines dedup/maintenance/silent with diminishing returns', () => {
    // combined = 1 - 0.6*0.75*0.85 = 0.6175
    const r = calcAlertStormReduction({ stormsPerMonth: 8, avgAlertsPerStorm: 15 });
    expect(r.totalAlertsMonth).toBe(120);
    expect(r.reductionPct).toBeCloseTo(61.75, 10);
    expect(r.reducedAlerts).toBe(74); // round(120 * 0.6175)
  });

  it('reductionPct is constant regardless of volume', () => {
    const a = calcAlertStormReduction({ stormsPerMonth: 1, avgAlertsPerStorm: 1 });
    const b = calcAlertStormReduction({ stormsPerMonth: 100, avgAlertsPerStorm: 50 });
    expect(a.reductionPct).toBeCloseTo(b.reductionPct, 10);
  });
});

describe('calcTimeToMarket', () => {
  it('saves manual minus the fixed 5-minute automated path, per tenant', () => {
    const r = calcTimeToMarket({ tenants: 20, manualOnboardMinutes: 120 });
    expect(r.manualHours).toBe(40);
    expect(r.automatedHours).toBeCloseTo(100 / 60, 10);
    expect(r.totalSavedHours).toBeCloseTo(2300 / 60, 10);
    expect(r.reduction).toBeCloseTo((115 / 120) * 100, 10);
  });

  it('clamps savings to zero when manual is already faster than automation', () => {
    const r = calcTimeToMarket({ tenants: 10, manualOnboardMinutes: 3 });
    expect(r.totalSavedHours).toBe(0);
    expect(r.reduction).toBe(0);
  });
});

describe('calcAnnualSavings', () => {
  it('sums rule (×12), one-time ttm, and on-call fatigue (20% × 12) annual savings', () => {
    const r = calcAnnualSavings({
      ruleSavedHours: 570,
      ttmSavedHours: 2300 / 60,
      hourlyRate: 75,
      alertReduction: 74,
      oncallStaff: 3,
    });
    expect(r.ruleAnnual).toBe(513000);       // 570 * 12 * 75
    expect(r.ttmAnnual).toBeCloseTo(2875, 8); // (2300/60) * 75
    expect(r.alertAnnual).toBeCloseTo(39960, 8); // 74*3*75*0.2*12
    expect(r.total).toBeCloseTo(513000 + 2875 + 39960, 6);
  });

  it('total rises with hourly rate', () => {
    const base = { ruleSavedHours: 100, ttmSavedHours: 10, alertReduction: 50, oncallStaff: 2 };
    const cheap = calcAnnualSavings({ ...base, hourlyRate: 50 });
    const pricey = calcAnnualSavings({ ...base, hourlyRate: 150 });
    expect(pricey.total).toBeGreaterThan(cheap.total);
  });
});
