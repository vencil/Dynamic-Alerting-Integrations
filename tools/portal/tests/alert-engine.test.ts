/**
 * Unit tests for _common/sim/alert-engine.js — Vitest next-batch (PR-3).
 *
 * Focus on the pure / easily-testable surface:
 *   - simulateAlerts: pure (config + metricValues → alerts) — no globals
 *   - resolveRoutingLayers: 4-layer routing model (uses window.__ROUTING_*
 *     globals, mocked here)
 *
 * Skipped (would need extensive global mocking, lower ROI):
 *   - generateSampleYaml: heavy on window.__t / window.__RULE_PACK_DATA
 *   - validateConfig: pulls 7+ globals; coverage better via E2E spec
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import {
  simulateAlerts,
  resolveRoutingLayers,
} from '../src/interactive/tools/_common/sim/alert-engine.js';

// ─────────────────────────────────────────────────────────────────────
// simulateAlerts — pure function, no globals
// ─────────────────────────────────────────────────────────────────────

describe('simulateAlerts', () => {
  it('returns no-threshold severity when metric has no config entry', () => {
    const alerts = simulateAlerts(
      {},
      { mysql_connections: { current: 50, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({
      metric: 'mysql_connections',
      current: 50,
      threshold: null,
      firing: false,
      severity: 'no-threshold',
    });
  });

  it('returns disabled severity when threshold is "disable"', () => {
    const alerts = simulateAlerts(
      { mysql_connections: 'disable' },
      { mysql_connections: { current: 999, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].severity).toBe('disabled');
    expect(alerts[0].firing).toBe(false);
  });

  it('alerts fire when current >= threshold', () => {
    const alerts = simulateAlerts(
      { mysql_connections: '80' },
      { mysql_connections: { current: 90, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].severity).toBe('warning');
    expect(alerts[0].threshold).toBe(80);
  });

  it('alerts do NOT fire when current < threshold', () => {
    const alerts = simulateAlerts(
      { mysql_connections: '80' },
      { mysql_connections: { current: 70, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(false);
    expect(alerts[0].severity).toBe('ok');
  });

  it('boundary: current == threshold counts as firing (>=)', () => {
    const alerts = simulateAlerts(
      { mysql_connections: '80' },
      { mysql_connections: { current: 80, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
  });

  it('escalates to critical when current >= _critical threshold', () => {
    const alerts = simulateAlerts(
      { mysql_connections: '80', mysql_connections_critical: '95' },
      { mysql_connections: { current: 95, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].critical_firing).toBe(true);
    expect(alerts[0].severity).toBe('critical');
    expect(alerts[0].critical_threshold).toBe(95);
  });

  it('warning (not critical) when between threshold and _critical', () => {
    const alerts = simulateAlerts(
      { mysql_connections: '80', mysql_connections_critical: '95' },
      { mysql_connections: { current: 85, unit: 'count', packLabel: 'mysql' } },
    );
    expect(alerts[0].firing).toBe(true);
    expect(alerts[0].critical_firing).toBe(false);
    expect(alerts[0].severity).toBe('warning');
  });

  it('skips entries with non-numeric threshold (NaN)', () => {
    const alerts = simulateAlerts(
      { mysql_connections: 'not-a-number' },
      { mysql_connections: { current: 90, unit: 'count', packLabel: 'mysql' } },
    );
    // parseFloat('not-a-number') → NaN → continue, no alert emitted.
    expect(alerts).toHaveLength(0);
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
});

// ─────────────────────────────────────────────────────────────────────
// resolveRoutingLayers — needs ROUTING_DEFAULTS / ROUTING_PROFILES globals
// ─────────────────────────────────────────────────────────────────────

describe('resolveRoutingLayers', () => {
  let savedDefaults: any;
  let savedProfiles: any;

  beforeEach(() => {
    savedDefaults = (globalThis as any).window?.__ROUTING_DEFAULTS;
    savedProfiles = (globalThis as any).window?.__ROUTING_PROFILES;
    (globalThis as any).window.__ROUTING_DEFAULTS = {
      receiver_type: 'webhook',
      group_wait: '30s',
      repeat_interval: '4h',
    };
    (globalThis as any).window.__ROUTING_PROFILES = {
      'team-sre-apac': {
        receiver_type: 'pagerduty',
        group_wait: '15s',
      },
    };
  });

  afterEach(() => {
    (globalThis as any).window.__ROUTING_DEFAULTS = savedDefaults;
    (globalThis as any).window.__ROUTING_PROFILES = savedProfiles;
  });

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
