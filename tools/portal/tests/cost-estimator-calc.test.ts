/**
 * cost-estimator/calc.js — pure resource & cost math.
 *
 * These functions were extracted from cost-estimator.jsx (PR-portal-12) where
 * they had 0% coverage while buried in the JSX. The cases below mix
 * independently hand-derived expected values (clean integer inputs) with
 * structural invariants (mode deltas, replica linearity, storage floor) so the
 * suite locks behavior rather than merely echoing the implementation.
 */
import { describe, it, expect } from 'vitest';
import {
  PRICING,
  calcExporterResources,
  calcPrometheusResources,
  calcAlertmanagerResources,
  calcOperatorResources,
  calcTotalResources,
} from '../src/interactive/tools/cost-estimator/calc.js';

describe('calcExporterResources', () => {
  it('applies the base+per-(tenant×pack) memory and per-tenant cpu model × replicas', () => {
    // memoryPerReplica = 50 + 10*5*2 = 150 ; cpuPerReplica = 0.1 + 10*0.01 = 0.2
    const r = calcExporterResources(10, 5, 2);
    expect(r.memoryPerReplica).toBe(150);
    expect(r.cpuPerReplica).toBeCloseTo(0.2, 10);
    expect(r.memoryMB).toBe(300);
    expect(r.cpuCores).toBeCloseTo(0.4, 10);
  });

  it('scales memoryMB/cpuCores linearly with replicas', () => {
    const one = calcExporterResources(7, 3, 1);
    const three = calcExporterResources(7, 3, 3);
    expect(three.memoryMB).toBeCloseTo(one.memoryMB * 3, 10);
    expect(three.cpuCores).toBeCloseTo(one.cpuCores * 3, 10);
  });
});

describe('calcPrometheusResources', () => {
  it('computes time series as tenants×packs×12×3 and storage from sample volume', () => {
    // timeSeries = 10*5*12*3 = 1800 ; samplesPerSeries = (15*86400)/15 = 86400
    // storageGB = (1800*1.5*86400)/1024^3
    const r = calcPrometheusResources(10, 5, 15, 15);
    expect(r.timeSeries).toBe(1800);
    expect(r.storageGB).toBeCloseTo((1800 * 1.5 * 86400) / 1024 ** 3, 9);
    expect(r.memoryMB).toBeCloseTo((1800 * 0.1 * 2) / 1024, 10);
  });

  it('floors storageGB at 0.1 for tiny deployments', () => {
    // timeSeries=36, samplesPerSeries=288 → ~1.4e-5 GB, below the 0.1 floor
    const r = calcPrometheusResources(1, 1, 300, 1);
    expect(r.storageGB).toBe(0.1);
  });

  it('storage grows with retention and shrinks with longer scrape interval', () => {
    const base = calcPrometheusResources(50, 5, 15, 15);
    const longerRetention = calcPrometheusResources(50, 5, 15, 30);
    const slowerScrape = calcPrometheusResources(50, 5, 30, 15);
    expect(longerRetention.storageGB).toBeGreaterThan(base.storageGB);
    expect(slowerScrape.storageGB).toBeLessThan(base.storageGB);
  });
});

describe('calcAlertmanagerResources', () => {
  it('uses 64MB base + 1MB/100 tenants and fixed 0.05 cpu × replicas', () => {
    const r = calcAlertmanagerResources(10, 2);
    expect(r.memoryPerReplica).toBeCloseTo(64.1, 10);
    expect(r.memoryMB).toBeCloseTo(128.2, 10);
    expect(r.cpuCores).toBeCloseTo(0.1, 10);
  });
});

describe('calcOperatorResources', () => {
  it('is a fixed shared overhead', () => {
    expect(calcOperatorResources()).toEqual({ memoryMB: 128, cpuCores: 0.1 });
  });
});

describe('calcTotalResources', () => {
  const args = [10, 5, 15, 15, 2] as const;

  it('aggregates components and prices them at PRICING for configmap mode', () => {
    const r = calcTotalResources(...args, 'configmap');
    // totalCpu = exporter 0.4 + prometheus 0.25 + am 0.1 + operator 0 = 0.75
    expect(r.summary.totalCpuCores).toBeCloseTo(0.75, 10);
    // totalMem = 300 + (1800*0.1*2/1024) + 128.2 + 0
    const expectedMem = 300 + (1800 * 0.1 * 2) / 1024 + 128.2;
    expect(r.summary.totalMemoryMB).toBeCloseTo(expectedMem, 8);
    expect(r.costs.cpuCost).toBeCloseTo(0.75 * 730 * PRICING.cpuPerHour, 8);
    expect(r.costs.memoryCost).toBeCloseTo((expectedMem / 1024) * 730 * PRICING.memoryPerGBHour, 8);
    expect(r.costs.totalMonthlyCost).toBeCloseTo(r.costs.cpuCost + r.costs.memoryCost, 10);
  });

  it('operator mode adds exactly the operator overhead vs configmap', () => {
    const cm = calcTotalResources(...args, 'configmap');
    const op = calcTotalResources(...args, 'operator');
    expect(op.summary.totalMemoryMB - cm.summary.totalMemoryMB).toBeCloseTo(128, 8);
    expect(op.summary.totalCpuCores - cm.summary.totalCpuCores).toBeCloseTo(0.1, 10);
    expect(op.components.operator).toEqual({ memoryMB: 128, cpuCores: 0.1 });
  });

  it('configmap mode contributes zero operator overhead', () => {
    const cm = calcTotalResources(...args, 'configmap');
    expect(cm.components.operator).toEqual({ memoryMB: 0, cpuCores: 0 });
  });

  it('total monthly cost rises with tenant count', () => {
    const small = calcTotalResources(5, 5, 15, 15, 2, 'configmap');
    const large = calcTotalResources(500, 5, 15, 15, 2, 'configmap');
    expect(large.costs.totalMonthlyCost).toBeGreaterThan(small.costs.totalMonthlyCost);
  });
});
