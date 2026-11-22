/**
 * The expected-exporter dropdown shows the filter in effect (#4097).
 *
 * The dropdown lists exporter KINDS. A saved view may carry a whole entry
 * (`expected_exporters: "mariadb@east"`), which the filter applies as an
 * exact-entry match — a value no kind option is. A <select> whose value is
 * not among its options displays another option, so the page would say
 * "All kinds" while filtering to one cluster's entry. The value in effect
 * is therefore listed; the kind options stay one per kind.
 *
 * SavedViewsPanel is replaced by a button that applies such a view: the
 * real panel is hidden without a reachable /api/v1/views, and what is
 * tested here is the orchestrator's dropdown, not the panel.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';

const demoOverride = vi.hoisted(() => ({ tenants: null as any }));
vi.mock('../src/interactive/tools/tenant-manager/fixtures/demo-tenants.js', async (importOriginal) => {
  const actual = (await importOriginal()) as any;
  return {
    get DEMO_TENANTS() { return demoOverride.tenants ?? actual.DEMO_TENANTS; },
    get DEMO_GROUPS() { return actual.DEMO_GROUPS; },
  };
});
vi.mock('../src/interactive/tools/tenant-manager/components/SavedViewsPanel.jsx', () => ({
  SavedViewsPanel: ({ onApplyView }: any) => (
    <button type="button" onClick={() => onApplyView({ expected_exporters: 'mariadb@east' })}>
      apply-entry-view
    </button>
  ),
}));

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new Error('offline test'))));
});

afterEach(() => {
  demoOverride.tenants = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('TenantManager — expected-exporter dropdown and a saved view with a whole entry', () => {
  it('lists the entry in effect next to the kinds, and shows it selected', async () => {
    const base = {
      environment: 'production', region: 'x', tier: 'tier-1', domain: 'd', owner: 'x', routing_channel: 'x',
      operational_mode: 'normal', metric_count: 0, last_config_commit: 'x', tags: [], groups: [],
    };
    demoOverride.tenants = {
      't-both': { ...base, expected_exporters: ['mariadb@east', 'mariadb@west', 'redis'] },
      't-west': { ...base, expected_exporters: ['mariadb@west'] },
    };
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Search')).toBeInTheDocument());
    const select = document.getElementById('filter-expected-exporter') as HTMLSelectElement;
    const options = () => Array.from(select.options).map((o) => o.value).filter((v) => v !== '');
    expect(options()).toEqual(['mariadb', 'redis']);

    fireEvent.click(screen.getByText('apply-entry-view'));
    // the exact-entry filter is applied …
    await waitFor(() => expect(screen.queryAllByText('t-west').length).toBe(0));
    expect(screen.getAllByText('t-both').length).toBeGreaterThan(0);
    // … and the dropdown says so: kinds once each, plus the entry, selected
    expect(options()).toEqual(['mariadb', 'redis', 'mariadb@east']);
    expect(select.value).toBe('mariadb@east');
    expect(select.selectedOptions[0].textContent).toBe('mariadb@east');

    // choosing a kind drops the entry from the list again
    fireEvent.change(select, { target: { value: 'mariadb' } });
    await waitFor(() => expect(options()).toEqual(['mariadb', 'redis']));
    expect(select.value).toBe('mariadb');
  });
});
