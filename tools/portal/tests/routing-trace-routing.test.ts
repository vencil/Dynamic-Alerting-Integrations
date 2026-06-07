/**
 * routing-trace/routing.js — Alertmanager route-resolution algorithm.
 *
 * Extracted from routing-trace.jsx (PR-portal-18), previously 0%-covered
 * despite being the tool's correctness-critical core. computeTrace must honor:
 * first-match-wins, AND-semantics across a route's match labels, empty-match
 * skip, and fall-through to the default receiver.
 */
import { describe, it, expect } from 'vitest';
import {
  isValidAlertName,
  isValidLabelKey,
  computeTrace,
  canAdvance,
} from '../src/interactive/tools/routing-trace/routing.js';

const DEFAULT = { receiver: 'default-pager' };

describe('computeTrace', () => {
  it('routes to the first child route whose match labels are all satisfied', () => {
    const r = computeTrace({
      alert: { alertname: 'HighCPU', severity: 'critical', labels: {} },
      defaultRoute: DEFAULT,
      childRoutes: [
        { match: { severity: 'critical' }, receiver: 'pager' },
        { match: { severity: 'critical' }, receiver: 'second-pager' },
      ],
    });
    expect(r.matchedRoute).toBe(0); // top-to-bottom: first wins
    expect(r.receiver).toBe('pager');
    expect(r.reasons.length).toBeGreaterThan(0);
  });

  it('falls through to the default receiver when nothing matches', () => {
    const r = computeTrace({
      alert: { alertname: 'X', severity: 'warning', labels: {} },
      defaultRoute: DEFAULT,
      childRoutes: [{ match: { severity: 'critical' }, receiver: 'pager' }],
    });
    expect(r.matchedRoute).toBeNull();
    expect(r.receiver).toBe('default-pager');
  });

  it('requires ALL of a route\'s match labels (AND semantics)', () => {
    const route = { match: { severity: 'critical', team: 'db' }, receiver: 'db-pager' };
    const miss = computeTrace({
      alert: { alertname: 'X', severity: 'critical', labels: {} }, // no team
      defaultRoute: DEFAULT,
      childRoutes: [route],
    });
    expect(miss.matchedRoute).toBeNull();

    const hit = computeTrace({
      alert: { alertname: 'X', severity: 'critical', labels: { team: 'db' } },
      defaultRoute: DEFAULT,
      childRoutes: [route],
    });
    expect(hit.matchedRoute).toBe(0);
    expect(hit.receiver).toBe('db-pager');
  });

  it('skips a route with no match labels rather than matching everything', () => {
    const r = computeTrace({
      alert: { alertname: 'X', severity: 'warning', labels: {} },
      defaultRoute: DEFAULT,
      childRoutes: [{ match: {}, receiver: 'never' }],
    });
    expect(r.matchedRoute).toBeNull();
    expect(r.receiver).toBe('default-pager');
  });
});

describe('canAdvance', () => {
  it('step 0 requires a valid alert name', () => {
    expect(canAdvance(0, { alert: { alertname: 'HighCPU' } })).toBe(true);
    expect(canAdvance(0, { alert: { alertname: '123bad' } })).toBe(false);
  });

  it('step 1 requires a non-empty default receiver', () => {
    expect(canAdvance(1, { defaultRoute: { receiver: 'pager' } })).toBe(true);
    expect(canAdvance(1, { defaultRoute: { receiver: '   ' } })).toBe(false);
  });

  it('step 2 accepts no child routes but rejects an incomplete one', () => {
    expect(canAdvance(2, { childRoutes: [] })).toBe(true);
    expect(canAdvance(2, { childRoutes: [{ receiver: 'x', match: { a: 'b' } }] })).toBe(true);
    expect(canAdvance(2, { childRoutes: [{ receiver: '', match: { a: 'b' } }] })).toBe(false);
    expect(canAdvance(2, { childRoutes: [{ receiver: 'x', match: {} }] })).toBe(false);
  });

  it('defaults to allowing advance on the final step', () => {
    expect(canAdvance(3, {})).toBe(true);
  });
});

describe('validators', () => {
  it('isValidAlertName: letter-led identifier, no dashes', () => {
    expect(isValidAlertName('HighCPU')).toBe(true);
    expect(isValidAlertName('with-dash')).toBe(false);
    expect(isValidAlertName('123')).toBe(false);
    expect(isValidAlertName('')).toBe(false);
    expect(isValidAlertName(null)).toBe(false);
  });

  it('isValidLabelKey: lowercase-led snake identifier', () => {
    expect(isValidLabelKey('severity')).toBe(true);
    expect(isValidLabelKey('team_a')).toBe(true);
    expect(isValidLabelKey('Severity')).toBe(false);
  });
});
