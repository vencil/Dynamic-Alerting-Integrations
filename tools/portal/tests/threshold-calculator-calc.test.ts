/**
 * threshold-calculator/calc.js — percentile → threshold suggestion engine.
 *
 * These functions were extracted from threshold-calculator.jsx (portal ROI
 * wave 6) where the suggestion math — the whole point of the tool — had 0%
 * coverage while buried in the JSX. Expected values below are hand-derived
 * from the documented heuristic (round(base×1.15)/round(base×1.4) normal;
 * max(0,round(base×0.85))/max(0,round(base×0.7)) inverted) so the suite locks
 * behavior and catches an algorithm regression, not merely "returns a number".
 *
 * Rounding note: JS Math.round is round-half-UP (toward +∞), but IEEE-754
 * makes ×1.15 subtly non-exact. Two "looks like .5" cases are pinned to show
 * the real behavior: 50×1.15 is 57.4999… (NOT 57.5) → rounds DOWN to 57;
 * 250×1.15 lands at/above 287.5 → 288. So the effective headroom near the .5
 * boundary is float-dependent — see the wave-6 report's flagged suspicious point.
 */
import { describe, it, expect } from 'vitest';
import {
  METRIC_PROFILES,
  PERCENTILES,
  suggestThreshold,
  generateYAML,
} from '../src/interactive/tools/threshold-calculator/calc.js';

describe('suggestThreshold — normal metric (mysql_connections)', () => {
  const p = METRIC_PROFILES.mysql_connections; // typical p50:50 p90:120 p95:180 p99:250

  it('p50 base=50 → warning 57 (50×1.15=57.4999… floors DOWN), critical round(70)=70', () => {
    expect(suggestThreshold(p, 'p50', {})).toEqual({ warning: 57, critical: 70 });
  });
  it('p90 base=120 → warning round(138)=138, critical round(168)=168', () => {
    expect(suggestThreshold(p, 'p90', {})).toEqual({ warning: 138, critical: 168 });
  });
  it('p95 base=180 → warning round(207)=207, critical round(252)=252', () => {
    expect(suggestThreshold(p, 'p95', {})).toEqual({ warning: 207, critical: 252 });
  });
  it('p99 base=250 → warning round(287.5)=288, critical round(350)=350 (half-up)', () => {
    expect(suggestThreshold(p, 'p99', {})).toEqual({ warning: 288, critical: 350 });
  });
});

describe('suggestThreshold — normal metrics, other profiles', () => {
  it('redis_evictions p99 base=3000 → 3450 / 4200', () => {
    expect(suggestThreshold(METRIC_PROFILES.redis_evictions, 'p99', {})).toEqual({
      warning: 3450,
      critical: 4200,
    });
  });
  it('kafka_lag p50 base=1000 → 1150 / 1400', () => {
    expect(suggestThreshold(METRIC_PROFILES.kafka_lag, 'p50', {})).toEqual({
      warning: 1150,
      critical: 1400,
    });
  });
  it('mysql_cpu p50 base=25 → 29 / 35 (smallest base; warning still < critical)', () => {
    expect(suggestThreshold(METRIC_PROFILES.mysql_cpu, 'p50', {})).toEqual({
      warning: 29,
      critical: 35,
    });
  });
});

describe('suggestThreshold — inverted metric (pg_cache_hit_ratio)', () => {
  const p = METRIC_PROFILES.pg_cache_hit_ratio; // inverted; typical p50:95 p90:98 p95:99 p99:99.5

  it('uses the 0.85 / 0.7 multipliers so warning > critical (threshold is a minimum)', () => {
    // p90 base=98 → warning max(0,round(83.3))=83, critical max(0,round(68.6))=69
    expect(suggestThreshold(p, 'p90', {})).toEqual({ warning: 83, critical: 69 });
  });
  it('p50 base=95 → warning round(80.75)=81, critical round(66.5)=67 (half-up)', () => {
    expect(suggestThreshold(p, 'p50', {})).toEqual({ warning: 81, critical: 67 });
  });
  it('p99 base=99.5 → warning round(84.575)=85, critical round(69.65)=70', () => {
    expect(suggestThreshold(p, 'p99', {})).toEqual({ warning: 85, critical: 70 });
  });
});

describe('suggestThreshold — customValues override', () => {
  const p = METRIC_PROFILES.mysql_connections;

  it('customValues[percentile] overrides profile.typical for the selected percentile', () => {
    // base=200 (not typical 120) → warning round(230)=230, critical round(280)=280
    expect(suggestThreshold(p, 'p90', { p90: 200 })).toEqual({ warning: 230, critical: 280 });
  });

  it('only the SELECTED percentile custom value is read (others ignored)', () => {
    // selected p90; custom sets p50 only → base falls back to typical.p90 = 120
    expect(suggestThreshold(p, 'p90', { p50: 9999 })).toEqual({ warning: 138, critical: 168 });
  });

  it('a custom value of 0 is honored (!== undefined), NOT treated as missing', () => {
    // 0 !== undefined → base=0 → both round(0)=0. A falsy-but-defined override.
    expect(suggestThreshold(p, 'p50', { p50: 0 })).toEqual({ warning: 0, critical: 0 });
  });

  it('omitting customValues equals passing an empty object (cv = customValues || {})', () => {
    expect(suggestThreshold(p, 'p90')).toEqual(suggestThreshold(p, 'p90', {}));
    expect(suggestThreshold(p, 'p90', null)).toEqual(suggestThreshold(p, 'p90', {}));
  });
});

describe('suggestThreshold — edge cases (documents current, unguarded behavior)', () => {
  const p = METRIC_PROFILES.mysql_connections;

  it('unknown percentile → base undefined → NaN thresholds (no guard)', () => {
    const r = suggestThreshold(p, 'p999', {});
    expect(Number.isNaN(r.warning)).toBe(true);
    expect(Number.isNaN(r.critical)).toBe(true);
  });

  it('missing profile throws (reads profile.typical unguarded)', () => {
    expect(() => suggestThreshold(undefined, 'p90', {})).toThrow();
  });

  it('inverted clamp floors negatives at 0 (max(0, ...))', () => {
    // Real METRIC_PROFILES bases are all positive, so the max(0, ...) clamp is
    // unreachable in production — use a synthetic NEGATIVE base to genuinely
    // exercise it. Without the clamp: round(-10*0.85)=-8, round(-10*0.7)=-7;
    // this assertion ({0,0}) fails if the clamp is ever removed.
    const inv = { inverted: true, typical: { pX: -10 } } as any;
    expect(suggestThreshold(inv, 'pX', {})).toEqual({ warning: 0, critical: 0 });
  });
});

describe('generateYAML', () => {
  it('emits only the two header lines for an empty basket', () => {
    expect(generateYAML([])).toBe('tenants:\n  my-app:');
  });

  it('emits <metric> + <metric>_critical as quoted strings for one selection', () => {
    const yaml = generateYAML([{ metric: 'mysql_connections', warning: 138, critical: 168 }]);
    expect(yaml).toBe(
      [
        'tenants:',
        '  my-app:',
        '    mysql_connections: "138"',
        '    mysql_connections_critical: "168"',
      ].join('\n'),
    );
  });

  it('preserves selection order and 2 lines per metric; label is not emitted', () => {
    const yaml = generateYAML([
      { metric: 'mysql_cpu', label: 'MySQL CPU', warning: 69, critical: 84 },
      { metric: 'redis_memory', label: 'Redis Memory', warning: 92, critical: 112 },
    ]);
    const lines = yaml.split('\n');
    expect(lines).toEqual([
      'tenants:',
      '  my-app:',
      '    mysql_cpu: "69"',
      '    mysql_cpu_critical: "84"',
      '    redis_memory: "92"',
      '    redis_memory_critical: "112"',
    ]);
    expect(yaml).not.toContain('MySQL CPU'); // label field ignored
    expect(lines.length).toBe(2 + 2 * 2);
  });

  it('round-trips suggestThreshold output into the YAML values', () => {
    const p = METRIC_PROFILES.mysql_connections;
    const { warning, critical } = suggestThreshold(p, 'p90', {}); // 138 / 168
    const yaml = generateYAML([{ metric: 'mysql_connections', warning, critical }]);
    expect(yaml).toContain('mysql_connections: "138"');
    expect(yaml).toContain('mysql_connections_critical: "168"');
  });
});

describe('METRIC_PROFILES — data integrity', () => {
  const entries = Object.entries(METRIC_PROFILES);

  it('PERCENTILES is the p50<p90<p95<p99 render order', () => {
    expect(PERCENTILES).toEqual(['p50', 'p90', 'p95', 'p99']);
  });

  it('every profile has label / unit / desc / typical with all 6 numeric keys', () => {
    for (const [key, m] of entries) {
      expect(typeof m.label, key).toBe('string');
      expect(typeof m.unit, key).toBe('string');
      expect(typeof m.desc, key).toBe('string');
      for (const field of ['min', 'max', ...PERCENTILES]) {
        expect(typeof m.typical[field], `${key}.${field}`).toBe('number');
      }
    }
  });

  it('percentiles are strictly monotonic p50 < p90 < p95 < p99', () => {
    for (const [key, m] of entries) {
      const { p50, p90, p95, p99 } = m.typical;
      expect(p50 < p90, `${key} p50<p90`).toBe(true);
      expect(p90 < p95, `${key} p90<p95`).toBe(true);
      expect(p95 < p99, `${key} p95<p99`).toBe(true);
    }
  });

  it('min <= p50 and p99 <= max and min < max for every profile', () => {
    for (const [key, m] of entries) {
      const { min, max, p50, p99 } = m.typical;
      expect(min <= p50, `${key} min<=p50`).toBe(true);
      expect(p99 <= max, `${key} p99<=max`).toBe(true);
      expect(min < max, `${key} min<max`).toBe(true);
    }
  });

  it('only pg_cache_hit_ratio is inverted', () => {
    for (const [key, m] of entries) {
      expect(Boolean((m as any).inverted), key).toBe(key === 'pg_cache_hit_ratio');
    }
  });
});

describe('suggestThreshold — structural invariant across all real profiles', () => {
  it('normal → warning < critical; inverted → warning > critical (all percentiles)', () => {
    for (const [key, m] of Object.entries(METRIC_PROFILES)) {
      for (const pct of PERCENTILES) {
        const { warning, critical } = suggestThreshold(m, pct, {});
        expect(Number.isFinite(warning), `${key} ${pct} warning finite`).toBe(true);
        expect(Number.isFinite(critical), `${key} ${pct} critical finite`).toBe(true);
        if ((m as any).inverted) {
          expect(warning > critical, `${key} ${pct} inverted warning>critical`).toBe(true);
        } else {
          expect(warning < critical, `${key} ${pct} normal warning<critical`).toBe(true);
        }
      }
    }
  });
});
