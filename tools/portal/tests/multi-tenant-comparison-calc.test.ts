/**
 * multi-tenant-comparison/calc.js — cross-tenant threshold statistics.
 *
 * These functions were extracted from multi-tenant-comparison.jsx (mirroring the
 * threshold-calculator wave-6 extraction) where the comparison math — outlier
 * detection, divergence ranking, common-setting detection — had 0% coverage
 * while buried in the JSX. Every expected value below is hand-derived from the
 * verbatim formulas and independently recomputed, so the suite LOCKS current
 * behavior and catches an algorithm regression rather than merely "returns
 * something".
 *
 * Several assertions PIN behavior that is deliberately surprising; each is
 * flagged inline so a reviewer / PM can decide whether it is a real bug or
 * intended. These tests assert the CURRENT behavior — do not "fix" the code to
 * make a nicer number pass:
 *   - computeStats uses POPULATION variance (/N, not /(N-1)).
 *   - median = sorted[floor(len/2)] → EVEN-length arrays take the UPPER-middle
 *     element ([1,2,3,4] → 3, not 2.5).
 *   - mean & stddev are rounded to 1 decimal, but min/max/median are NOT.
 *   - findCommonSettings marks a metric "common" when it is ABSENT from every
 *     tenant (all undefined === each other) and for an EMPTY tenant list
 *     (vacuous .every()).
 */
import { describe, it, expect } from 'vitest';
import {
  computeStats,
  detectOutliers,
  findCommonSettings,
  findDivergent,
  DEFAULTS,
} from '../src/interactive/tools/multi-tenant-comparison/calc.js';

// Helper: build tenants from a list of metric values (unknown metric "m" ⇒ defaultVal 0).
const mk = (metric: string, ...vals: (number | null | undefined)[]) =>
  vals.map((v, i) => ({ name: `t${i}`, thresholds: v === undefined ? {} : { [metric]: v } }));

describe('computeStats — population variance & upper-middle median', () => {
  it('odd [10,20,30]: PINNED population variance → stddev 8.2 (sample would be 10.0)', () => {
    // mean = 60/3 = 20; variance = (100+0+100)/3 = 66.666… ; sqrt = 8.16496…
    // round(8.16496×10)/10 = 82/10 = 8.2.  /(N-1) sample would give 100 → 10.0.
    // median = sorted[floor(3/2)] = sorted[1] = 20.  defaultVal = DEFAULTS['m']||0 = 0.
    expect(computeStats(mk('m', 10, 20, 30), 'm')).toEqual({
      min: 10, max: 30, mean: 20, median: 20, stddev: 8.2, count: 3, defaultVal: 0,
    });
  });

  it('even [1,2,3,4]: PINNED upper-middle median = 3 (NOT 2.5) and population stddev 1.1', () => {
    // sorted=[1,2,3,4]; median = sorted[floor(4/2)] = sorted[2] = 3 (upper of the two middles).
    // mean = 10/4 = 2.5; variance = (2.25+0.25+0.25+2.25)/4 = 1.25; sqrt = 1.118… → 1.1.
    // (sample /(N-1) would be 5/3 → sqrt 1.29 → 1.3, so this distinguishes the two.)
    expect(computeStats(mk('m', 1, 2, 3, 4), 'm')).toEqual({
      min: 1, max: 4, mean: 2.5, median: 3, stddev: 1.1, count: 4, defaultVal: 0,
    });
  });

  it('PINNED asymmetric rounding: mean/stddev to 1 dp, but min/max/median NOT rounded', () => {
    // [1,2,4] → mean = 7/3 = 2.333… → 2.3 (rounded); min=1,max=4 exact (unrounded ints);
    // median = sorted[1] = 2 (exact). variance = ((1-2.333)²+(2-2.333)²+(4-2.333)²)/3
    //   = (1.7778+0.1111+2.7778)/3 = 1.5556 → sqrt 1.2472 → 1.2.
    const s = computeStats(mk('m', 1, 2, 4), 'm');
    expect(s).toEqual({ min: 1, max: 4, mean: 2.3, median: 2, stddev: 1.2, count: 3, defaultVal: 0 });
  });

  it('null / undefined values are filtered before stats (count reflects only real values)', () => {
    // [10, null, 20, <missing>] → values [10,20]; mean 15; variance (25+25)/2=25 → stddev 5.
    // median = sorted[floor(2/2)] = sorted[1] = 20 (upper-middle of even length).
    expect(computeStats(mk('m', 10, null, 20, undefined), 'm')).toEqual({
      min: 10, max: 20, mean: 15, median: 20, stddev: 5, count: 2, defaultVal: 0,
    });
  });

  it('empty input and all-null input both return null', () => {
    expect(computeStats([], 'm')).toBeNull();
    expect(computeStats([{ name: 'x', thresholds: {} }], 'm')).toBeNull();
    expect(computeStats(mk('m', null, undefined), 'm')).toBeNull();
  });

  it('defaultVal reads DEFAULTS[metric]; single value → stddev 0', () => {
    // DEFAULTS.mysql_connections = 80. Single [100] → mean 100, stddev 0, median 100.
    expect(computeStats([{ name: 'x', thresholds: { mysql_connections: 100 } }], 'mysql_connections'))
      .toEqual({ min: 100, max: 100, mean: 100, median: 100, stddev: 0, count: 1, defaultVal: 80 });
  });
});

describe('detectOutliers — z-score fence on ROUNDED mean/stddev', () => {
  // [10,10,10,10,100]: mean = 140/5 = 28; variance = (18²×4 + 72²)/5 = (1296+5184)/5 = 1296;
  // stddev = sqrt(1296) = 36 (exact). Only "100" clears the fence.
  const set = [
    { name: 'a', thresholds: { m: 10 } }, { name: 'b', thresholds: { m: 10 } },
    { name: 'c', thresholds: { m: 10 } }, { name: 'd', thresholds: { m: 10 } },
    { name: 'e', thresholds: { m: 100 } },
  ];

  it('default threshold 1.5: |val-28| > 1.5×36 (=54) → only e; zscore = (100-28)/36 = 2', () => {
    expect(detectOutliers(set, 'm')).toEqual([{ tenant: 'e', value: 100, zscore: 2 }]);
  });

  it('threshold is applied: at 2.5 the fence is 90 > 72, so NO outliers ([])', () => {
    expect(detectOutliers(set, 'm', 2.5)).toEqual([]);
  });

  it('default parameter really is 1.5 (omitting threshold === passing 1.5)', () => {
    expect(detectOutliers(set, 'm')).toEqual(detectOutliers(set, 'm', 1.5));
  });

  it('stddev === 0 short-circuits to [] (all values identical)', () => {
    expect(detectOutliers([{ name: 'a', thresholds: { m: 50 } }, { name: 'b', thresholds: { m: 50 } }], 'm'))
      .toEqual([]);
  });

  it('LOAD-BEARING guard: values differ but ROUNDED stddev is 0 → [] (no ±Infinity z-scores)', () => {
    // [5.00, 5.03]: mean 5.015, raw stddev sqrt(0.000225)=0.015 → Math.round(0.015×10)/10 = 0.
    // The `stats.stddev === 0` short-circuit is NOT redundant here: without it the fence is
    // `|val-5.015| > 1.5×0 = 0`, which BOTH differing values clear, and zscore = ±0.015/0 =
    // ±Infinity. (The "all identical" test above passes with OR without the guard because the
    // fence alone yields [] there; only this differing-but-rounds-to-0 case makes the guard bite.)
    expect(detectOutliers([{ name: 'a', thresholds: { m: 5.00 } }, { name: 'b', thresholds: { m: 5.03 } }], 'm'))
      .toEqual([]);
  });

  it('zscore rounds to 2 dp (computed from stats.stddev, the 1-dp-rounded 8.2)', () => {
    // [10,20,30]: mean 20, stddev 8.2 (rounded). threshold 1 → fence 8.2.
    // lo: |10-20|=10 > 8.2 ✓ zscore (10-20)/8.2 = -1.21951… → round(-121.95)/100 = -1.22
    // hi: |30-20|=10 > 8.2 ✓ zscore (30-20)/8.2 =  1.21951… →  1.22.  mid filtered (0 ≯ 8.2).
    const s = [
      { name: 'lo', thresholds: { m: 10 } }, { name: 'mid', thresholds: { m: 20 } },
      { name: 'hi', thresholds: { m: 30 } },
    ];
    expect(detectOutliers(s, 'm', 1)).toEqual([
      { tenant: 'lo', value: 10, zscore: -1.22 },
      { tenant: 'hi', value: 30, zscore: 1.22 },
    ]);
  });
});

describe('findCommonSettings — value shared across all tenants', () => {
  const base = {
    mysql_connections: 80, container_cpu: 80, container_memory: 85,
    oracle_sessions_active: 200, oracle_tablespace_used_pct: 85, db2_connections_active: 200,
  };

  it('returns the metrics all tenants agree on, excluding the one that differs', () => {
    // Only mysql_cpu differs (75 vs 90) → the other 6 DEFAULTS metrics are "common".
    const t1 = { name: 'a', thresholds: { ...base, mysql_cpu: 75 } };
    const t2 = { name: 'b', thresholds: { ...base, mysql_cpu: 90 } };
    expect(findCommonSettings([t1, t2])).toEqual([
      'mysql_connections', 'container_cpu', 'container_memory',
      'oracle_sessions_active', 'oracle_tablespace_used_pct', 'db2_connections_active',
    ]);
  });

  it('FLAG: a metric MISSING from all tenants is reported "common" (undefined===undefined), but PARTIAL-missing is not', () => {
    // t1 has mysql_connections+mysql_cpu; t2 has only mysql_connections.
    //   mysql_connections: [80,80]        → common
    //   mysql_cpu:         [75, undefined] → every(v===75)? undefined≠75 → NOT common
    //   container_cpu … db2 (5 metrics):  [undefined, undefined] → all undefined → COMMON (surprising)
    const t1 = { name: 'a', thresholds: { mysql_connections: 80, mysql_cpu: 75 } };
    const t2 = { name: 'b', thresholds: { mysql_connections: 80 } };
    expect(findCommonSettings([t1, t2])).toEqual([
      'mysql_connections', 'container_cpu', 'container_memory',
      'oracle_sessions_active', 'oracle_tablespace_used_pct', 'db2_connections_active',
    ]);
  });

  it('FLAG: two tenants with EMPTY thresholds report ALL 7 metrics common (every value undefined)', () => {
    expect(findCommonSettings([{ name: 'a', thresholds: {} }, { name: 'b', thresholds: {} }]))
      .toEqual(Object.keys(DEFAULTS));
  });

  it('FLAG: empty tenant list returns ALL 7 metrics (vacuous [].every() === true)', () => {
    expect(findCommonSettings([])).toEqual(Object.keys(DEFAULTS));
  });
});

describe('findDivergent — filter stddev>0, sort by stddev DESC', () => {
  // Three tenants. mysql_connections [10,20,100] spread widest; container_cpu [80,70,90] next;
  // all other 5 DEFAULTS metrics identical across tenants → stddev 0 → filtered out.
  const rest = {
    mysql_cpu: 80, container_memory: 85, oracle_sessions_active: 200,
    oracle_tablespace_used_pct: 85, db2_connections_active: 200,
  };
  const t1 = { name: 'a', thresholds: { mysql_connections: 10, container_cpu: 80, ...rest } };
  const t2 = { name: 'b', thresholds: { mysql_connections: 20, container_cpu: 70, ...rest } };
  const t3 = { name: 'c', thresholds: { mysql_connections: 100, container_cpu: 90, ...rest } };

  it('drops the 5 zero-stddev metrics and orders the survivors by stddev descending', () => {
    // mysql_connections: mean 43.333 → stddev sqrt(1622.22)=40.2769… → 40.3 (rank 1)
    // container_cpu:      mean 80     → stddev sqrt(66.667)=8.16496… → 8.2  (rank 2)
    const d = findDivergent([t1, t2, t3]);
    expect(d.map(i => i.metric)).toEqual(['mysql_connections', 'container_cpu']);
    expect(d.map(i => i.stats.stddev)).toEqual([40.3, 8.2]);
  });

  it('the top entry carries the full (unrounded min/max/median) stats object', () => {
    const top = findDivergent([t1, t2, t3])[0];
    // sorted [10,20,100] → min 10, max 100, median sorted[1]=20, mean round(43.333×10)/10=43.3,
    // defaultVal DEFAULTS.mysql_connections = 80.
    expect(top.stats).toEqual({
      min: 10, max: 100, mean: 43.3, median: 20, stddev: 40.3, count: 3, defaultVal: 80,
    });
  });

  it('all-identical tenants → no divergence (every metric stddev 0 → [])', () => {
    const same = { name: 'x', thresholds: { mysql_connections: 80, container_cpu: 80, ...rest } };
    expect(findDivergent([same, { ...same, name: 'y' }])).toEqual([]);
  });
});

describe('DEFAULTS — the extracted default-threshold table', () => {
  it('exposes the 7 canonical metrics with their default values', () => {
    expect(DEFAULTS).toEqual({
      mysql_connections: 80, mysql_cpu: 80, container_cpu: 80,
      container_memory: 85, oracle_sessions_active: 200,
      oracle_tablespace_used_pct: 85, db2_connections_active: 200,
    });
  });
});
