/**
 * capacity-planner/calc.js — TSDB / memory / routing sizing model.
 *
 * Extracted from an inline useMemo in capacity-planner.jsx (PR-portal-15),
 * previously 0%-covered. `packs` (filtered pack objects) is passed in rather
 * than read from the platform-data RULE_PACKS global, so the model is pure.
 * Expected values are hand-derived from the documented heuristics; floor and
 * route-override branches are exercised as invariants.
 */
import { describe, it, expect } from 'vitest';
import { computeCapacityEstimates } from '../src/interactive/tools/capacity-planner/calc.js';

const PACKS = [
  { recording: 11, alerts: 8, metrics: 6, seriesPerInstance: 80 },
  { recording: 7, alerts: 4, metrics: 4, seriesPerInstance: 40 },
  { recording: 0, alerts: 4, metrics: 2, seriesPerInstance: 10 },
];

describe('computeCapacityEstimates', () => {
  it('aggregates pack totals and series', () => {
    const r = computeCapacityEstimates({ packs: PACKS, tenantCount: 10, instancesPerTenant: 2, scrapeInterval: 15, retentionDays: 15 });
    expect(r.packs).toBe(3);
    expect(r.totalRecording).toBe(18);
    expect(r.totalAlerts).toBe(16);
    expect(r.totalMetrics).toBe(12);
    expect(r.totalInstances).toBe(20);
    expect(r.totalSeries).toBe(2600); // (80+40+10) * 10 * 2
  });

  it('sizes TSDB / memory / reload from the documented heuristics', () => {
    const r = computeCapacityEstimates({ packs: PACKS, tenantCount: 10, instancesPerTenant: 2, scrapeInterval: 15, retentionDays: 15 });
    // samplesPerDay = 2600 * 86400/15 = 14,976,000 ; tsdb = that*15*1.5 / 1024^3
    expect(r.samplesPerDay).toBe(14976000);
    expect(r.tsdbGB).toBeCloseTo((2600 * (86400 / 15) * 15 * 1.5) / 1024 ** 3, 9);
    expect(r.exporterMB).toBeCloseTo(50 + (2600 * 0.1) / 1024, 9);
    expect(r.promMemMB).toBeCloseTo((2600 * (7200 / 15) * 2) / 1024 ** 2 + 256, 9);
    expect(r.reloadMs).toBeCloseTo(50 + 10 * 5 + 2600 * 0.01, 9); // 126
    expect(r.amInhibits).toBe(36); // 10*2 + 16
  });

  it('adds per-pack route overrides only when more than 3 packs are selected', () => {
    const three = computeCapacityEstimates({ packs: PACKS, tenantCount: 10, instancesPerTenant: 1, scrapeInterval: 15, retentionDays: 15 });
    expect(three.amRoutes).toBe(10); // 10 * (1 + 0), 3 packs ≯ 3

    const fourPacks = [...PACKS, { recording: 9, alerts: 6, metrics: 3, seriesPerInstance: 30 }];
    const four = computeCapacityEstimates({ packs: fourPacks, tenantCount: 10, instancesPerTenant: 1, scrapeInterval: 15, retentionDays: 15 });
    expect(four.amRoutes).toBe(50); // 10 * (1 + 4)
  });

  it('applies floors for an empty / zero deployment', () => {
    const r = computeCapacityEstimates({ packs: [], tenantCount: 0, instancesPerTenant: 0, scrapeInterval: 15, retentionDays: 15 });
    expect(r.totalSeries).toBe(0);
    expect(r.tsdbGB).toBe(0.01);
    expect(r.exporterMB).toBe(50);
    expect(r.promMemMB).toBe(256);
    expect(r.reloadMs).toBe(50);
    expect(r.amRoutes).toBe(0);
  });

  it('TSDB grows with retention and shrinks with longer scrape interval', () => {
    const base = computeCapacityEstimates({ packs: PACKS, tenantCount: 50, instancesPerTenant: 2, scrapeInterval: 15, retentionDays: 15 });
    const longer = computeCapacityEstimates({ packs: PACKS, tenantCount: 50, instancesPerTenant: 2, scrapeInterval: 15, retentionDays: 30 });
    const slower = computeCapacityEstimates({ packs: PACKS, tenantCount: 50, instancesPerTenant: 2, scrapeInterval: 30, retentionDays: 15 });
    expect(longer.tsdbGB).toBeGreaterThan(base.tsdbGB);
    expect(slower.tsdbGB).toBeLessThan(base.tsdbGB);
  });
});
