/**
 * Unit tests for _common/sim/alert-engine.js — Vitest next-batch (PR-3).
 *
 * Focus on the pure / easily-testable surface:
 *   - simulateAlerts: pure (config + metricValues → alerts) — no deps
 *   - resolveRoutingLayers: 4-layer routing model; the routing data it
 *     ESM-imports from routing-profiles.js is injected via vi.mock (below)
 *
 * Skipped (would need mocking several data modules, lower ROI):
 *   - generateSampleYaml: pulls RULE_PACK_DATA + window.__t
 *   - validateConfig: pulls 7+ data modules; coverage better via E2E spec
 *     (except its routing timing checks, #2711, at the bottom of this file)
 */
import { describe, it, expect, vi } from 'vitest';
import {
  validateConfig,
  isFiring,
  simulateAlerts,
  simulateWithDedup,
  resolveRoutingLayers,
} from '../src/interactive/tools/_common/sim/alert-engine.js';

// alert-engine.js ESM-imports ROUTING_DEFAULTS / ROUTING_PROFILES from
// routing-profiles.js (TRK-230z Wave 2). Mock that module to inject the
// routing fixtures these resolveRoutingLayers tests assert against — the
// former window.__ROUTING_* beforeEach injection no longer reaches the engine.
vi.mock('../src/interactive/tools/_common/data/routing-profiles.js', () => ({
  ROUTING_DEFAULTS: {
    receiver_type: 'webhook',
    group_wait: '30s',
    repeat_interval: '4h',
  },
  ROUTING_PROFILES: {
    'team-sre-apac': {
      receiver_type: 'pagerduty',
      group_wait: '15s',
    },
  },
  DOMAIN_POLICIES: {},
}));

// ─────────────────────────────────────────────────────────────────────
// simulateAlerts — pure function, no globals
// ─────────────────────────────────────────────────────────────────────

describe('simulateAlerts', () => {
  it('returns no-threshold severity when metric has no config entry', () => {
    const alerts = simulateAlerts(
      {},
      { mysql_connection_usage_percent: { current: 50, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({
      metric: 'mysql_connection_usage_percent',
      current: 50,
      threshold: null,
      firing: false,
      severity: 'no-threshold',
    });
  });

  it('returns disabled severity when threshold is "disable"', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: 'disable' },
      { mysql_connection_usage_percent: { current: 99, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].severity).toBe('disabled');
    expect(alerts[0].firing).toBe(false);
  });

  it('alerts fire when current > threshold', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80' },
      { mysql_connection_usage_percent: { current: 90, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].severity).toBe('warning');
    expect(alerts[0].threshold).toBe(80);
  });

  it('alerts do NOT fire when current < threshold', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80' },
      { mysql_connection_usage_percent: { current: 70, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(false);
    expect(alerts[0].severity).toBe('ok');
  });

  it('boundary: current == threshold does NOT fire (strict >, Prometheus-faithful)', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80' },
      { mysql_connection_usage_percent: { current: 80, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(false);
    expect(alerts[0].severity).toBe('ok');
  });

  it('fires when current is just above threshold (strict >)', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80' },
      { mysql_connection_usage_percent: { current: 81, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].severity).toBe('warning');
  });

  it('escalates to critical when current > _critical threshold (strict >)', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '95' },
      { mysql_connection_usage_percent: { current: 96, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].critical_firing).toBe(true);
    expect(alerts[0].severity).toBe('critical');
    expect(alerts[0].critical_threshold).toBe(95);
  });

  it('critical boundary: current == _critical does NOT escalate (strict >)', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '95' },
      { mysql_connection_usage_percent: { current: 95, unit: '%', packLabel: 'mysql' } },
    );
    // 95 > 80 → warning fires; 95 > 95 is false → stays warning, not critical.
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].critical_firing).toBe(false);
    expect(alerts[0].severity).toBe('warning');
  });

  it('warning (not critical) when between threshold and _critical', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '95' },
      { mysql_connection_usage_percent: { current: 85, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].critical_firing).toBe(false);
    expect(alerts[0].severity).toBe('warning');
  });

  it('a threshold the exporter cannot read is a row that says so, not a dropped one (#3865)', () => {
    const alerts = simulateAlerts(
      { mysql_connection_usage_percent: 'not-a-number' },
      { mysql_connection_usage_percent: { current: 90, unit: '%', packLabel: 'mysql' } },
    );
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({
      severity: 'unreadable', raw: 'not-a-number', threshold: null, firing: false, critical_firing: false,
    });
  });

  it('a scheduled value (a map) has no row: no schedule is modelled', () => {
    const alerts = simulateAlerts(
      { x: { default: '80', overrides: [] } },
      { x: { current: 90, unit: '%', packLabel: 'p' } },
    );
    expect(alerts).toEqual([]);
  });

  it('handles multiple metrics independently', () => {
    const alerts = simulateAlerts(
      { metric_a: '50', metric_b: '100' },
      {
        metric_a: { current: 60, unit: 'x', packLabel: 'p' },
        metric_b: { current: 50, unit: 'y', packLabel: 'q' },
      },
    );
    expect(alerts).toHaveLength(2);
    const byMetric = Object.fromEntries(alerts.map((a) => [a.metric, a]));
    expect(byMetric.metric_a.firing).toBe(true);
    expect(byMetric.metric_b.firing).toBe(false);
  });

  it('preserves unit + packLabel from input on every alert', () => {
    const alerts = simulateAlerts(
      { x: '10' },
      { x: { current: 5, unit: 'bytes', packLabel: 'redis' } },
    );
    expect(alerts[0].unit).toBe('bytes');
    expect(alerts[0].packLabel).toBe('redis');
  });

  it('returns empty array for empty metricValues', () => {
    expect(simulateAlerts({ x: '10' }, {})).toEqual([]);
  });

  it('treats a numeric 0 threshold (unquoted YAML, #2033) as a real threshold', () => {
    // parseYaml keeps YAML types, so `x: 0` arrives as the number 0 — a
    // falsy value that must not read as "no threshold".
    const alerts = simulateAlerts(
      { x: 0, x_critical: 0 },
      { x: { current: 1, unit: 'count', packLabel: 'p' } },
    );
    expect(alerts[0].threshold).toBe(0);
    expect(alerts[0].critical_threshold).toBe(0);
    expect(alerts[0].severity).toBe('critical');
  });
});

// ─────────────────────────────────────────────────────────────────────
// resolveRoutingLayers — routing data injected via vi.mock (top of file)
// ─────────────────────────────────────────────────────────────────────

describe('resolveRoutingLayers', () => {
  it('always returns 4 layers in order', () => {
    const out = resolveRoutingLayers({});
    expect(out.layers).toHaveLength(4);
    expect(out.layers.map((l: any) => l.layer)).toEqual([1, 2, 3, 4]);
  });

  it('L1 always reflects platform defaults', () => {
    const out = resolveRoutingLayers({});
    expect(out.layers[0].source).toBe('platform');
    expect(out.layers[0].values.receiver_type).toBe('webhook');
  });

  it('L2 marked "skip" when no _routing_profile', () => {
    const out = resolveRoutingLayers({});
    expect(out.layers[1].source).toBe('skip');
  });

  it('L2 applies profile overrides + records them', () => {
    const out = resolveRoutingLayers({ _routing_profile: 'team-sre-apac' });
    expect(out.layers[1].source).toBe('profile');
    expect(out.layers[1].values.receiver_type).toBe('pagerduty');
    expect(out.layers[1].overrides.receiver_type).toEqual({
      from: 'webhook',
      to: 'pagerduty',
    });
    // group_wait also overridden
    expect(out.layers[1].overrides.group_wait).toBeDefined();
  });

  it('L3 marked "skip" when no _routing block', () => {
    const out = resolveRoutingLayers({});
    expect(out.layers[2].source).toBe('skip');
  });

  it('L3 tenant override takes precedence over profile', () => {
    const out = resolveRoutingLayers({
      _routing_profile: 'team-sre-apac',
      _routing: { receiver_type: 'slack' },
    });
    expect(out.layers[2].source).toBe('tenant');
    expect(out.layers[2].values.receiver_type).toBe('slack');
    expect(out.layers[2].overrides.receiver_type).toEqual({
      from: 'pagerduty',
      to: 'slack',
    });
  });

  it('L4 enforced layer mirrors L3 (no overrides at this layer)', () => {
    const out = resolveRoutingLayers({
      _routing: { receiver_type: 'slack' },
    });
    expect(out.layers[3].source).toBe('enforced');
    expect(out.layers[3].values.receiver_type).toBe('slack');
    expect(out.layers[3].overrides).toEqual({});
  });

  it('resolved equals L3 final values', () => {
    const out = resolveRoutingLayers({
      _routing: { receiver_type: 'slack', group_wait: '60s' },
    });
    expect(out.resolved).toEqual(out.layers[2].values);
  });

  it('handles non-existent profile gracefully (no crash, no L2 overrides)', () => {
    const out = resolveRoutingLayers({ _routing_profile: 'nonexistent' });
    // profile lookup returns null → L2 has no overrides
    expect(out.layers[1].overrides).toEqual({});
    expect(out.layers[1].values).toEqual(out.layers[0].values);
  });

  it('skips _routing.overrides field when applying tenant overrides', () => {
    const out = resolveRoutingLayers({
      _routing: {
        receiver_type: 'slack',
        // 'overrides' is intentionally skipped per the engine rules
        overrides: { somekey: 'value' },
      },
    });
    expect(out.layers[2].overrides.somekey).toBeUndefined();
    expect(out.layers[2].overrides.receiver_type).toBeDefined();
  });
});

// ─────────────────────────────────────────────────────────────────────
// isFiring — canonical threshold-crossing primitive (strict >, < inverted)
// ─────────────────────────────────────────────────────────────────────

describe('isFiring', () => {
  it('fires strictly above threshold; equality does NOT fire', () => {
    expect(isFiring(90, 80)).toBe(true);
    expect(isFiring(80, 80)).toBe(false); // Prometheus-faithful strict >
    expect(isFiring(70, 80)).toBe(false);
  });

  it('inverted (lower-bound) fires strictly below; equality does NOT fire', () => {
    expect(isFiring(40, 50, true)).toBe(true);
    expect(isFiring(50, 50, true)).toBe(false);
    expect(isFiring(60, 50, true)).toBe(false);
  });
});

// ─────────────────────────────────────────────────────────────────────
// simulateWithDedup — alert-simulator model relocated from the jsx.
// alertDefs (ALERT_DEFS) is injected by the caller (shape adapter).
// ─────────────────────────────────────────────────────────────────────

describe('simulateWithDedup', () => {
  const DEFS = {
    mysql_connection_usage_percent: { alert: 'MariaDBHighConnections', severity: 'warning' },
    mysql_connection_usage_percent_critical: { alert: 'MariaDBHighConnectionsCritical', severity: 'critical' },
    pg_cache_hit_ratio: { alert: 'PostgreSQLLowCacheHit', severity: 'warning', inverted: true },
  };

  it('buckets a firing warning into firing (strict >)', () => {
    const out = simulateWithDedup({ mysql_connection_usage_percent: '80' }, { mysql_connection_usage_percent: 85 }, true, DEFS);
    expect(out.firing.map((f: any) => f.key)).toEqual(['mysql_connection_usage_percent']);
    expect(out.ok).toEqual([]);
    expect(out.suppressed).toEqual([]);
  });

  it('boundary: current == threshold stays OK (strict >)', () => {
    const out = simulateWithDedup({ mysql_connection_usage_percent: '80' }, { mysql_connection_usage_percent: 80 }, true, DEFS);
    expect(out.firing).toEqual([]);
    expect(out.ok.map((o: any) => o.key)).toEqual(['mysql_connection_usage_percent']);
  });

  it('inverted metric fires strictly below threshold; equality is OK', () => {
    const below = simulateWithDedup({ pg_cache_hit_ratio: '90' }, { pg_cache_hit_ratio: 80 }, true, DEFS);
    expect(below.firing.map((f: any) => f.key)).toEqual(['pg_cache_hit_ratio']);
    const equal = simulateWithDedup({ pg_cache_hit_ratio: '90' }, { pg_cache_hit_ratio: 90 }, true, DEFS);
    expect(equal.firing).toEqual([]);
    expect(equal.ok.map((o: any) => o.key)).toEqual(['pg_cache_hit_ratio']);
  });

  it('severity dedup: firing critical suppresses matching warning', () => {
    const config = { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '90' };
    const metrics = { mysql_connection_usage_percent: 85, mysql_connection_usage_percent_critical: 95 };
    const out = simulateWithDedup(config, metrics, true, DEFS);
    expect(out.firing.map((f: any) => f.key)).toEqual(['mysql_connection_usage_percent_critical']);
    expect(out.suppressed.map((s: any) => s.key)).toEqual(['mysql_connection_usage_percent']);
    expect(out.suppressed[0].reason).toMatch(/severity dedup/i);
  });

  it('dedup disabled keeps both warning and critical firing', () => {
    const config = { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '90' };
    const metrics = { mysql_connection_usage_percent: 85, mysql_connection_usage_percent_critical: 95 };
    const out = simulateWithDedup(config, metrics, false, DEFS);
    expect(out.firing.map((f: any) => f.key).sort()).toEqual(
      ['mysql_connection_usage_percent', 'mysql_connection_usage_percent_critical'],
    );
    expect(out.suppressed).toEqual([]);
  });

  it('skips keys with no def / non-numeric threshold / missing metric', () => {
    const out = simulateWithDedup(
      { unknown_metric: '10', mysql_connection_usage_percent: 'abc', pg_cache_hit_ratio: '90' },
      { unknown_metric: 50, mysql_connection_usage_percent: 85 /* pg_cache_hit_ratio missing */ },
      true,
      DEFS,
    );
    expect(out.firing).toEqual([]);
    expect(out.ok).toEqual([]);
    expect(out.suppressed).toEqual([]);
  });

  it('tolerates a missing alertDefs argument (empty defs → no alerts)', () => {
    const out = simulateWithDedup({ mysql_connection_usage_percent: '80' }, { mysql_connection_usage_percent: 85 }, true);
    expect(out).toEqual({ firing: [], suppressed: [], ok: [], unreadable: [] });
  });

  // Equivalence guard: reproduce the pre-refactor alert-simulator.jsx
  // `simulate()` inline and assert byte-identical output across a matrix
  // of inputs. This is the "same input → same result before/after
  // convergence" property; the ONLY sanctioned behaviour change (engine
  // simulateAlerts >= → >) does not touch this path, which was already >.
  // #3865 added the `unreadable` bucket and changed how a threshold is READ;
  // for thresholds both readers read alike (plain decimals) the three legacy
  // buckets are unchanged. Where the readers differ is
  // tests/threshold-value-simulator.test.ts.
  it('is behaviourally identical to the pre-refactor simulate() across a matrix', () => {
    const legacySimulate = (config: any, metrics: any, dedupEnabled: boolean) => {
      const firing: any[] = [];
      const suppressed: any[] = [];
      const ok: any[] = [];
      Object.entries(config).forEach(([key, thresholdStr]) => {
        const def = (DEFS as any)[key];
        if (!def) return;
        const threshold = parseFloat(thresholdStr as string);
        if (isNaN(threshold)) return;
        const current = metrics[key];
        if (current === undefined || current === '') return;
        const val = parseFloat(current);
        const wouldFire = def.inverted ? val < threshold : val > threshold;
        if (wouldFire) firing.push({ key, def, threshold, current: val });
        else ok.push({ key, def, threshold, current: val });
      });
      if (dedupEnabled) {
        const criticalFiring = new Set(
          firing.filter((f) => f.def.severity === 'critical').map((f) => f.key.replace('_critical', '')),
        );
        firing.forEach((f) => {
          if (f.def.severity === 'warning' && criticalFiring.has(f.key)) {
            suppressed.push({ ...f, reason: 'Suppressed by severity dedup (critical alert active)' });
          }
        });
        const suppressedKeys = new Set(suppressed.map((s) => s.key));
        return { firing: firing.filter((f) => !suppressedKeys.has(f.key)), suppressed, ok };
      }
      return { firing, suppressed: [], ok };
    };

    const configs = [
      { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '90' },
      { pg_cache_hit_ratio: '90' },
      { mysql_connection_usage_percent: '80', mysql_connection_usage_percent_critical: '90', pg_cache_hit_ratio: '90' },
      { unknown: '5' },
    ];
    const metricSets = [
      { mysql_connection_usage_percent: 85, mysql_connection_usage_percent_critical: 95, pg_cache_hit_ratio: 80 },
      { mysql_connection_usage_percent: 80, mysql_connection_usage_percent_critical: 90, pg_cache_hit_ratio: 90 }, // all at boundary
      { mysql_connection_usage_percent: 50, pg_cache_hit_ratio: 95 },
      {},
    ];
    for (const config of configs) {
      for (const metrics of metricSets) {
        for (const dedup of [true, false]) {
          expect(simulateWithDedup(config, metrics, dedup, DEFS)).toEqual(
            { ...legacySimulate(config, metrics, dedup), unreadable: [] },
          );
        }
      }
    }
  });
});

// ─────────────────────────────────────────────────────────────────────
// validateConfig — routing timing values (#2711)
// ─────────────────────────────────────────────────────────────────────

describe('validateConfig — routing timing values (#2711)', () => {
  type Issue = { level: string; field: string; msg: string };
  const timingIssues = (routing: Record<string, unknown>): Issue[] =>
    validateConfig({ _routing: { receiver_type: 'webhook', ...routing } }, [])
      .issues.filter((i: Issue) => /^_routing\.(group_wait|group_interval|repeat_interval)$/.test(i.field));

  it('an unreadable value is an error, not skipped', () => {
    for (const bad of ['1.5h', '30m1h', '1h1h', '1ns', 30]) {
      const issues = timingIssues({ group_wait: bad });
      expect(issues, String(bad)).toHaveLength(1);
      expect(issues[0].level).toBe('error');
      expect(issues[0].field).toBe('_routing.group_wait');
      expect(issues[0].msg).toContain(String(bad));
      expect(issues[0].msg).toContain('not an Alertmanager duration');
    }
  });

  it('a compound value is read and checked against the guardrail', () => {
    // 1h30m = 5400s: inside repeat_interval's bounds, above group_wait's 300s max.
    expect(timingIssues({ repeat_interval: '1h30m' })).toEqual([]);
    const over = timingIssues({ group_wait: '1h30m' });
    expect(over).toHaveLength(1);
    expect(over[0].level).toBe('warning');
    expect(over[0].msg).toContain('exceeds maximum 300s');
  });

  it('an empty value is left to the platform default, as the generator does', () => {
    expect(timingIssues({ group_wait: '' })).toEqual([]);
  });
});

// ─────────────────────────────────────────────────────────────────────
// validateConfig — judges no key name (ADR-038 §11)
// ─────────────────────────────────────────────────────────────────────

describe('validateConfig — judges no key name (ADR-038 §11)', () => {
  type Issue = { level: string; field: string; msg: string };

  it('a `_` key the registry does not list gets no line here', () => {
    // Whether a key is read is the shared key verdict's (validate-config,
    // `da-tools explain-keys`, tenant-api's write gate), not this page's.
    for (const key of ['_routing_defaults', '_domain_policy', '_silentmode']) {
      const fields = validateConfig({ [key]: 'x' }, []).issues.map((i: Issue) => i.field);
      expect(fields, key).not.toContain(key);
    }
  });
});
