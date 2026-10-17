/**
 * #1988 D1 PR-3 — the tenant manager's mode column comes from the
 * tenant-api's `config_derived` (「依設定推算」), never from the raw
 * `silent_mode` / `maintenance` file values.
 *
 * Two layers: the pure mapping (operationalModeFromSummary /
 * derivationNoticeFromSearch) and the orchestrator fed a stubbed
 * /api/v1/tenants/search response, asserting what each card renders.
 * Tenant ids are synthetic.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import {
  operationalModeFromSummary,
  derivationNoticeFromSearch,
} from '../src/interactive/tools/tenant-manager/utils/operational-mode.js';

const searchBody = {
  items: [
    // raw `disable` is non-empty: must NOT read as maintenance
    { id: 't-maint-off', environment: 'prod', maintenance: 'disable',
      config_derived: { silent_targets: [], maintenance_active: false } },
    // silenced through a profile: the raw field is empty
    { id: 't-silent', environment: 'prod',
      config_derived: { silent_targets: ['critical', 'warning'], maintenance_active: false } },
    { id: 't-maint', environment: 'prod', maintenance: 'enable',
      config_derived: { silent_targets: [], maintenance_active: true } },
    // no derivation (load failed): unknown, not the raw value
    { id: 't-noderive', environment: 'prod', silent_mode: 'all' },
  ],
  total_matched: 4,
  page_size: 500,
  next_offset: null,
  config_derivation: {
    evaluated_at: '2099-01-01T00:00:00Z',
    config_loaded_at: '2099-01-01T00:00:00Z',
    parse_failed_files: ['t-broken.yaml'],
  },
};

describe('operationalModeFromSummary', () => {
  it('maintenance: "disable" with maintenance_active=false is not maintenance', () => {
    expect(operationalModeFromSummary(searchBody.items[0])).toBe('normal');
  });
  it('an active silence comes from silent_targets', () => {
    expect(operationalModeFromSummary(searchBody.items[1])).toBe('silent');
  });
  it('maintenance comes from maintenance_active', () => {
    expect(operationalModeFromSummary(searchBody.items[2])).toBe('maintenance');
  });
  it('a missing config_derived is unknown, whatever the raw values say', () => {
    expect(operationalModeFromSummary(searchBody.items[3])).toBe('unknown');
  });
});

describe('derivationNoticeFromSearch', () => {
  it('is null on a clean derivation', () => {
    expect(derivationNoticeFromSearch({ config_derivation: { parse_failed_files: [] } })).toBeNull();
    expect(derivationNoticeFromSearch({})).toBeNull();
  });
  it('carries parse-failed files and a load error', () => {
    expect(derivationNoticeFromSearch(searchBody)).toEqual({
      parseFailedFiles: ['t-broken.yaml'], loadError: '',
    });
    expect(derivationNoticeFromSearch({ config_derivation: { parse_failed_files: [], load_error: 'dup' } }))
      .toEqual({ parseFailedFiles: [], loadError: 'dup' });
  });
  it('has nothing to tell when no file is named and the load succeeded', () => {
    // #3123: tenant-api no longer counts files outside the caller's scope.
    expect(derivationNoticeFromSearch({ config_derivation: { parse_failed_files: [] } })).toBeNull();
  });
});

describe('TenantManager — API mode renders the derived mode', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).startsWith('/api/v1/tenants/search')) {
        return Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve(searchBody) });
      }
      return Promise.reject(new Error('offline test'));
    }));
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('each card shows the config-derived mode, labelled 依設定推算', async () => {
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-maint-off — prod normal')).toBeInTheDocument());
    expect(screen.getByLabelText('Tenant: t-silent — prod silent')).toBeInTheDocument();
    expect(screen.getByLabelText('Tenant: t-maint — prod maintenance')).toBeInTheDocument();
    expect(screen.getByLabelText('Tenant: t-noderive — prod unknown')).toBeInTheDocument();
    // jsx-loader's t() falls back to EN in tests.
    expect(screen.getAllByText('Mode (derived from config)').length).toBe(4);
  });

  it('surfaces the parse-failed files from config_derivation', async () => {
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByText('t-broken.yaml')).toBeInTheDocument());
    // #3123: no count of files outside the caller's scope any more.
    expect(screen.queryByText(/outside your access scope/)).toBeNull();
  });
});
