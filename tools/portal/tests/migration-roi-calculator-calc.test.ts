/**
 * migration-roi-calculator/calc.js — migration estimation models.
 *
 * Extracted from migration-roi-calculator.jsx (PR-portal-14), previously
 * 0%-covered. estimatePlatformCoverage was de-globalized: it now takes
 * `platformRules` as an argument instead of reading the module-scope
 * TOTAL_RULES. Expected values are hand-derived from the documented formulas;
 * invariants (zero/edge guards, param sensitivity) lock behavior.
 */
import { describe, it, expect } from 'vitest';
import {
  estimatePlatformCoverage,
  estimateMigrationEffort,
  estimateMaintenanceSavings,
  estimateBreakEven,
  estimateAnnualSavings,
} from '../src/interactive/tools/migration-roi-calculator/calc.js';

describe('estimatePlatformCoverage', () => {
  it('applies 70% overlap against capped platform/user rule ratio', () => {
    // maxCoverage = min(238/200*100,100)=100 ; coverage = min(round(0.7*100),100)=70
    expect(estimatePlatformCoverage({ totalRules: 200, platformRules: 238 })).toBe(70);
    // maxCoverage = min(238/1000*100,100)=23.8 ; round(0.7*23.8)=round(16.66)=17
    expect(estimatePlatformCoverage({ totalRules: 1000, platformRules: 238 })).toBe(17);
  });

  it('uses the platformRules argument (de-globalized), not a fixed constant', () => {
    const low = estimatePlatformCoverage({ totalRules: 1000, platformRules: 100 });
    const high = estimatePlatformCoverage({ totalRules: 1000, platformRules: 238 });
    expect(high).toBeGreaterThan(low);
  });

  it('never reports more than 100% coverage', () => {
    expect(estimatePlatformCoverage({ totalRules: 1, platformRules: 9999 })).toBeLessThanOrEqual(100);
  });
});

describe('estimateMigrationEffort', () => {
  it('blends 70% simple (5m) + 30% complex (15m) = 8 min/rule, rounded to hours', () => {
    // 500 * 8 / 60 = 66.67 → 67
    expect(estimateMigrationEffort({ totalRules: 500, recordingRules: 100, alertRules: 80 })).toBe(67);
  });
});

describe('estimateMaintenanceSavings', () => {
  it('scales savings by (1 - 1/tenants), rounded', () => {
    // round(40 * (1 - 1/20)) = round(38) = 38
    expect(estimateMaintenanceSavings({ currentMonthlyHours: 40, tenants: 20 })).toBe(38);
  });

  it('returns 0 for a single tenant (no O(N×M) leverage)', () => {
    expect(estimateMaintenanceSavings({ currentMonthlyHours: 40, tenants: 1 })).toBe(0);
  });
});

describe('estimateBreakEven', () => {
  it('computes migration/monthly-savings to one decimal', () => {
    // 67 / 38 = 1.763 → 1.8
    expect(estimateBreakEven({ migrationHours: 67, monthlySavingsHours: 38 })).toBeCloseTo(1.8, 10);
  });

  it('is Infinity when there are no monthly savings', () => {
    expect(estimateBreakEven({ migrationHours: 67, monthlySavingsHours: 0 })).toBe(Infinity);
  });
});

describe('estimateAnnualSavings', () => {
  it('annualizes monthly hours at the hourly rate', () => {
    // 38 * 12 * 75 = 34200
    expect(estimateAnnualSavings({ monthlySavingsHours: 38, hourlyRate: 75 })).toBe(34200);
  });
});
