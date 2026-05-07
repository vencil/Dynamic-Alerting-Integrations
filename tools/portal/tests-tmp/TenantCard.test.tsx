/**
 * Unit tests for `TenantCard` — TECH-DEBT-030b first-batch.
 *
 * Pure rendering component. Tests verify badge rendering, pending-PR
 * banner, rule pack pills, and deep-link footer.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TenantCard } from '../../docs/interactive/tools/tenant-manager/components/TenantCard.jsx';

const tenantData = {
  environment: 'production',
  tier: 'tier-1',
  domain: 'finance',
  db_type: 'mariadb',
  owner: 'team-a',
  operational_mode: 'normal',
  routing_channel: '#alerts-finance',
  metric_count: 42,
  rule_packs: ['cpu', 'memory'],
  last_config_commit: 'abc1234567890def',
};

const noopHandlers = {
  onToggleSelect: () => {},
  onHoverEnter: () => {},
  onHoverLeave: () => {},
};

const modeColors = { normal: '#0f0', silent: '#888', maintenance: '#fa0' };

describe('TenantCard', () => {
  it('renders tenant name and environment + tier badges', () => {
    render(
      <TenantCard
        name="prod-mariadb-01"
        data={tenantData}
        isSelected={false}
        isHovered={false}
        pendingPR={null}
        modeColors={modeColors}
        {...noopHandlers}
      />,
    );
    expect(screen.getByText('prod-mariadb-01')).toBeInTheDocument();
    expect(screen.getByText('PRODUCTION')).toBeInTheDocument();
    expect(screen.getByText('TIER-1')).toBeInTheDocument();
  });

  it('renders pending-PR badge when pendingPR provided', () => {
    render(
      <TenantCard
        name="db-x"
        data={tenantData}
        isSelected={false}
        isHovered={false}
        pendingPR={{ html_url: 'https://github.com/org/repo/pull/42', number: 42 }}
        modeColors={modeColors}
        {...noopHandlers}
      />,
    );
    const link = screen.getByText(/PR #42/);
    expect(link).toBeInTheDocument();
    expect(link.closest('a')?.getAttribute('href')).toBe('https://github.com/org/repo/pull/42');
  });

  it('omits pending-PR badge when pendingPR is null', () => {
    render(
      <TenantCard
        name="db-x"
        data={tenantData}
        isSelected={false}
        isHovered={false}
        pendingPR={null}
        modeColors={modeColors}
        {...noopHandlers}
      />,
    );
    expect(screen.queryByText(/PR #/)).not.toBeInTheDocument();
  });

  it('renders rule pack pills', () => {
    render(
      <TenantCard
        name="db-x"
        data={{ ...tenantData, rule_packs: ['cpu', 'mem', 'io'] }}
        isSelected={false}
        isHovered={false}
        pendingPR={null}
        modeColors={modeColors}
        {...noopHandlers}
      />,
    );
    expect(screen.getByText('cpu')).toBeInTheDocument();
    expect(screen.getByText('mem')).toBeInTheDocument();
    expect(screen.getByText('io')).toBeInTheDocument();
  });

  it('emits deep-link URLs with tenant_id query param', () => {
    render(
      <TenantCard
        name="my-tenant"
        data={tenantData}
        isSelected={false}
        isHovered={false}
        pendingPR={null}
        modeColors={modeColors}
        {...noopHandlers}
      />,
    );
    const alertLink = screen.getByTestId('tenant-card-my-tenant-build-alert');
    expect(alertLink.getAttribute('href')).toContain('component=alert-builder');
    expect(alertLink.getAttribute('href')).toContain('tenant_id=my-tenant');
  });

  it('triggers onToggleSelect when checkbox toggled', () => {
    const onToggleSelect = vi.fn();
    render(
      <TenantCard
        name="db-x"
        data={tenantData}
        isSelected={false}
        isHovered={false}
        pendingPR={null}
        modeColors={modeColors}
        onToggleSelect={onToggleSelect}
        onHoverEnter={() => {}}
        onHoverLeave={() => {}}
      />,
    );
    const checkbox = screen.getByLabelText('Select db-x') as HTMLInputElement;
    checkbox.click();
    expect(onToggleSelect).toHaveBeenCalledTimes(1);
  });
});
