/**
 * Unit tests for the Platform Health dashboard — da-portal ROI refactor
 * Wave 5b (token migration + component/fixture split).
 *
 * Covers:
 *   1. Fixture data integrity (PLATFORM_HEALTH_DATA) + window registration.
 *   2. StatusDot — each status maps to the correct --da-color-* token class.
 *   3. MetricCard — each status tint + label/value/subtitle rendering.
 *   4. Orchestrator — renders all sections (window.__t = English via setup).
 *
 * jsdom env + window.__t=English provided by test-setup.ts.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PLATFORM_HEALTH_DATA } from '../src/interactive/tools/platform-health/fixtures/platform-data.js';
import { StatusDot } from '../src/interactive/tools/platform-health/components/StatusDot.jsx';
import { MetricCard } from '../src/interactive/tools/platform-health/components/MetricCard.jsx';
import PlatformHealth from '../src/interactive/tools/platform-health.jsx';

describe('PLATFORM_HEALTH_DATA fixture', () => {
  it('registers on window under the collision-safe __PLATFORM_HEALTH_DATA key', () => {
    // Must NOT reuse __PLATFORM_DATA (the platform-injected rule-pack global).
    expect((window as unknown as Record<string, unknown>).__PLATFORM_HEALTH_DATA).toBe(
      PLATFORM_HEALTH_DATA,
    );
    expect((window as unknown as Record<string, unknown>).__PLATFORM_DATA).toBeUndefined();
  });

  it('has the three core components all healthy', () => {
    expect(PLATFORM_HEALTH_DATA.exporter.status).toBe('healthy');
    expect(PLATFORM_HEALTH_DATA.prometheus.status).toBe('healthy');
    expect(PLATFORM_HEALTH_DATA.alertmanager.status).toBe('healthy');
  });

  it('has 5 tenants with the required shape', () => {
    expect(PLATFORM_HEALTH_DATA.tenants).toHaveLength(5);
    for (const tenant of PLATFORM_HEALTH_DATA.tenants) {
      expect(typeof tenant.name).toBe('string');
      expect(['normal', 'maintenance', 'silent']).toContain(tenant.state);
      expect(Array.isArray(tenant.packs)).toBe(true);
      expect(typeof tenant.metrics).toBe('number');
      expect(typeof tenant.alertsFiring).toBe('number');
      expect(typeof tenant.lastUpdate).toBe('string');
    }
  });

  it('has exactly one tenant in maintenance and one firing alert total', () => {
    const inMaintenance = PLATFORM_HEALTH_DATA.tenants.filter((t) => t.state === 'maintenance');
    const totalFiring = PLATFORM_HEALTH_DATA.tenants.reduce((s, t) => s + t.alertsFiring, 0);
    expect(inMaintenance).toHaveLength(1);
    expect(inMaintenance[0].name).toBe('staging-pg');
    expect(totalFiring).toBe(1);
  });

  it('prometheus rule counts add up (recording + alert = total)', () => {
    const { recordingRules, alertRules, rulesLoaded } = PLATFORM_HEALTH_DATA.prometheus;
    expect(recordingRules + alertRules).toBe(rulesLoaded);
  });
});

describe('StatusDot', () => {
  const cases: Array<[string, string]> = [
    ['healthy', 'bg-[color:var(--da-color-success)]'],
    ['normal', 'bg-[color:var(--da-color-success)]'],
    ['degraded', 'bg-[color:var(--da-color-warning)]'],
    ['maintenance', 'bg-[color:var(--da-color-warning)]'],
    ['down', 'bg-[color:var(--da-color-error)]'],
    ['silent', 'bg-[color:var(--da-color-muted)]'],
  ];

  it.each(cases)('status "%s" → %s (token, no hardcoded palette)', (status, expected) => {
    const { container } = render(<StatusDot status={status} />);
    const dot = container.querySelector('span');
    expect(dot).not.toBeNull();
    expect(dot!.className).toContain(expected);
    // No hardcoded Tailwind palette class leaked through.
    expect(dot!.className).not.toMatch(/bg-(green|yellow|red|gray)-\d/);
  });

  it('unknown status falls back to the neutral surface-border token', () => {
    const { container } = render(<StatusDot status="bogus" />);
    const dot = container.querySelector('span');
    expect(dot!.className).toContain('bg-[color:var(--da-color-surface-border)]');
  });
});

describe('MetricCard', () => {
  it('renders label, value and subtitle', () => {
    render(<MetricCard label="Tenants" value={5} subtitle="1 in maintenance" />);
    expect(screen.getByText('Tenants')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('1 in maintenance')).toBeInTheDocument();
  });

  it('warning status → warning-soft tint tokens', () => {
    const { container } = render(<MetricCard label="Alerts" value={2} status="warning" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('bg-[color:var(--da-color-warning-soft)]');
    expect(card.className).toContain('border-[color:var(--da-color-warning)]');
  });

  it('error status → error-soft tint tokens', () => {
    const { container } = render(<MetricCard label="Errors" value={9} status="error" />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('bg-[color:var(--da-color-error-soft)]');
    expect(card.className).toContain('border-[color:var(--da-color-error)]');
  });

  it('default (no status) → neutral card tokens', () => {
    const { container } = render(<MetricCard label="Metrics" value={35} />);
    const card = container.firstChild as HTMLElement;
    expect(card.className).toContain('bg-[color:var(--da-color-card-bg)]');
    expect(card.className).toContain('border-[color:var(--da-color-card-border)]');
    expect(card.className).not.toMatch(/bg-(white|yellow|red)-?\d?/);
  });
});

describe('PlatformHealth orchestrator', () => {
  it('renders the header, banner and all four sections', () => {
    render(<PlatformHealth />);
    expect(screen.getByText('Platform Health Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Platform Operational')).toBeInTheDocument();
    expect(screen.getByText('Component Health')).toBeInTheDocument();
    expect(screen.getByText('Tenant Overview')).toBeInTheDocument();
    expect(screen.getByText('Rule Pack Usage')).toBeInTheDocument();
    expect(screen.getByText('Recent Events Timeline')).toBeInTheDocument();
  });

  it('renders the three core component cards from the fixture', () => {
    render(<PlatformHealth />);
    expect(screen.getByText('threshold-exporter')).toBeInTheDocument();
    expect(screen.getByText('Prometheus')).toBeInTheDocument();
    expect(screen.getByText('Alertmanager')).toBeInTheDocument();
  });

  it('renders every fixture tenant name in the overview table', () => {
    render(<PlatformHealth />);
    for (const tenant of PLATFORM_HEALTH_DATA.tenants) {
      expect(screen.getByText(tenant.name)).toBeInTheDocument();
    }
  });
});
