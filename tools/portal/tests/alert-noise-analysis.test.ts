/**
 * alert-noise-analyzer/analysis.js — alert-history analysis engine.
 *
 * Extracted from alert-noise-analyzer.jsx (PR-portal-22), previously
 * 0%-covered. analyzeAlerts derives counts, MTTR, flapping (>=3 fires in a
 * sliding 1h window), top-noisy, and dedup rate. Tests use fixed timestamps so
 * parseTime never hits its Date.now() fallback.
 */
import { describe, it, expect } from 'vitest';
import {
  parseTime,
  durationMinutes,
  formatDuration,
  analyzeAlerts,
} from '../src/interactive/tools/alert-noise-analyzer/analysis.js';

const ALERTS = [
  { alertname: 'A', tenant: 'db-a', severity: 'warning', startsAt: '2025-01-15T10:00:00Z', endsAt: '2025-01-15T10:10:00Z', status: 'resolved' },
  { alertname: 'A', tenant: 'db-a', severity: 'warning', startsAt: '2025-01-15T10:20:00Z', endsAt: '2025-01-15T10:30:00Z', status: 'resolved' },
  { alertname: 'A', tenant: 'db-a', severity: 'warning', startsAt: '2025-01-15T10:40:00Z', endsAt: '2025-01-15T10:50:00Z', status: 'resolved' },
  { alertname: 'B', tenant: 'db-b', severity: 'critical', startsAt: '2025-01-15T11:00:00Z', endsAt: '2025-01-15T11:30:00Z', status: 'resolved' },
];

describe('time helpers', () => {
  it('parseTime parses an ISO timestamp to epoch ms', () => {
    expect(parseTime('2025-01-15T10:00:00Z')).toBe(Date.parse('2025-01-15T10:00:00Z'));
  });

  it('durationMinutes converts and clamps negatives to 0', () => {
    expect(durationMinutes(0, 600000)).toBe(10);
    expect(durationMinutes(600000, 0)).toBe(0);
  });

  it('formatDuration renders seconds / minutes / hours', () => {
    expect(formatDuration(0.5)).toBe('30s');
    expect(formatDuration(12)).toBe('12m');
    expect(formatDuration(90)).toBe('1.5h');
  });
});

describe('analyzeAlerts', () => {
  it('returns null for an empty list', () => {
    expect(analyzeAlerts([])).toBeNull();
  });

  it('derives counts, MTTR, top-noisy, dedup and resolved totals', () => {
    const r = analyzeAlerts(ALERTS);
    expect(r.totalAlerts).toBe(4);
    expect(r.bySeverity).toEqual({ warning: 3, critical: 1 });
    expect(r.byTenant).toEqual({ 'db-a': 3, 'db-b': 1 });
    expect(r.resolvedCount).toBe(4);
    // durations: A=10m x3, B=30m -> mean (10+10+10+30)/4 = 15
    expect(r.mttr).toBeCloseTo(15, 10);
    // top-noisy sorted by count, with percentage of total
    expect(r.topNoisy[0]).toEqual({ name: 'A', count: 3, pct: '75.0' });
    // dedup candidates: db-a/A has 3 (=> +2), db-b/B has 1 (=> 0) -> 2/4 = 50.0%
    expect(r.dedupRate).toBe('50.0');
  });

  it('flags an alert firing >=3 times within an hour as flapping', () => {
    const r = analyzeAlerts(ALERTS);
    expect(r.flapping).toEqual([{ key: 'db-a/A', count: 3 }]);
  });

  it('does not flap a single-fire alert', () => {
    const single = [
      { alertname: 'C', tenant: 't', severity: 'info', startsAt: '2025-01-15T09:00:00Z', endsAt: '2025-01-15T09:05:00Z', status: 'resolved' },
    ];
    expect(analyzeAlerts(single).flapping).toEqual([]);
  });
});
