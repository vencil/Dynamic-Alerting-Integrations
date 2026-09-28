/**
 * #2068 — a DEGRADED row from /api/v1/tenants/search (#1680: only `id` +
 * `config_error`) must reach the card as a visible "config file unusable"
 * state, not look like an ordinary tenant with environment=unknown.
 *
 * Each case pairs the degraded row with a healthy row from the SAME
 * response (the must-fire control): the healthy card must carry no such
 * state, so a card that always shows it would fail. A third row with
 * no environment / no config_derived (`t-unlabeled`) is the "unknown but
 * readable" control: it must stay in the unknown stat buckets and in the
 * batch YAML, where the degraded row must not. Tenant ids are synthetic.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, waitFor, renderHook, within, fireEvent } from '@testing-library/react';
import TenantManager from '../src/interactive/tools/tenant-manager.jsx';
import { useTenantData } from '../src/interactive/tools/tenant-manager/hooks/useTenantData.js';
import { TenantCard } from '../src/interactive/tools/tenant-manager/components/TenantCard.jsx';

const searchBody = {
  items: [
    { id: 't-broken', config_error: 'malformed_yaml' },
    { id: 't-healthy', environment: 'prod', domain: 'finance',
      config_derived: { silent_targets: [], maintenance_active: false } },
    { id: 't-unlabeled' },
  ],
  total_matched: 3,
  page_size: 500,
  next_offset: null,
};

function stubSearch(body: unknown) {
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (String(url).startsWith('/api/v1/tenants/search')) {
      return Promise.resolve({ ok: true, status: 200, headers: new Headers(), json: () => Promise.resolve(body) });
    }
    return Promise.reject(new Error('offline test'));
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useTenantData — config_error reaches the row', () => {
  it('carries config_error on the degraded row only', async () => {
    stubSearch(searchBody);
    const { result } = renderHook(() => useTenantData({ setApiNotification: () => {}, t: (_zh: string, en: string) => en }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    // Control: the API path was taken (not the static / demo fallback).
    expect(result.current.dataSource).toBe('api');
    expect(result.current.tenants['t-healthy'].environment).toBe('prod');
    expect(result.current.tenants['t-broken'].config_error).toBe('malformed_yaml');
    expect(result.current.tenants['t-healthy'].config_error).toBeFalsy();
  });
});

describe('TenantCard — config error state', () => {
  const base = {
    environment: 'unknown', tier: '', domain: '', db_type: '', owner: '',
    operational_mode: 'unknown', routing_channel: '', metric_count: 0,
    rule_packs: [], last_config_commit: '',
  };
  const props = {
    isSelected: false, isHovered: false, pendingPR: null,
    modeColors: { normal: '#0f0', unknown: '#888' },
    onToggleSelect: () => {}, onHoverEnter: () => {}, onHoverLeave: () => {},
  };

  it.each([
    ['unreadable', /could not be read/i],
    ['not_regular_file', /not a regular file/i],
    ['malformed_yaml', /not valid YAML/i],
    ['invalid_config', /not a valid tenant config/i],
  ])('shows the state and the %s reason', (reason, text) => {
    render(<TenantCard name="t-broken" data={{ ...base, config_error: reason }} {...props} />);
    const box = screen.getByTestId('tenant-card-t-broken-config-error');
    expect(box).toHaveTextContent('Config file unusable');
    expect(box).toHaveTextContent(text);
    expect(box).toHaveTextContent(reason);
    // The placeholder fields must not render (they are defaults, not metadata).
    const card = within(box.closest('article') as HTMLElement);
    expect(card.queryByText('UNKNOWN')).not.toBeInTheDocument();
    expect(card.queryByText('Mode')).not.toBeInTheDocument();
    expect(card.queryByText('Domain')).not.toBeInTheDocument();
    expect(card.queryByText('Metrics')).not.toBeInTheDocument();
  });

  it('an unrecognised reason is still shown, verbatim', () => {
    render(<TenantCard name="t-broken" data={{ ...base, config_error: 'some_new_reason' }} {...props} />);
    const box = screen.getByTestId('tenant-card-t-broken-config-error');
    expect(box).toHaveTextContent('Config file unusable');
    expect(box).toHaveTextContent('some_new_reason');
  });

  it('a healthy card has no config error state (control)', () => {
    render(<TenantCard name="t-healthy" data={{ ...base, environment: 'prod', operational_mode: 'normal' }} {...props} />);
    expect(screen.queryByTestId('tenant-card-t-healthy-config-error')).not.toBeInTheDocument();
    expect(screen.queryByText(/Config file unusable/)).not.toBeInTheDocument();
  });
});

describe('TenantManager — API mode marks the degraded card', () => {
  it('the degraded card shows the state; the healthy card from the same response does not', async () => {
    stubSearch(searchBody);
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-healthy — prod normal')).toBeInTheDocument());
    const broken = screen.getByLabelText('Tenant: t-broken — config error: malformed_yaml');
    expect(broken).toHaveTextContent('Config file unusable');
    expect(screen.getByTestId('tenant-card-t-broken-config-error')).toBeInTheDocument();
    expect(screen.queryByTestId('tenant-card-t-healthy-config-error')).not.toBeInTheDocument();
    expect(screen.getAllByText(/Config file unusable/).length).toBe(1);
    // Placeholder fields absent on the degraded card, present on the healthy one.
    const b = within(broken);
    expect(b.queryByText('UNKNOWN')).not.toBeInTheDocument();
    expect(b.queryByText('Mode (derived from config)')).not.toBeInTheDocument();
    expect(b.queryByText('Domain')).not.toBeInTheDocument();
    const h = within(screen.getByLabelText('Tenant: t-healthy — prod normal'));
    expect(h.getByText('PROD')).toBeInTheDocument();
    expect(h.getByText('Mode (derived from config)')).toBeInTheDocument();
  });
});

// Value of the stat card whose label is exactly `label`, or null.
function statValue(label: string): string | null {
  const bar = screen.getByText('Total Tenants').parentElement!.parentElement as HTMLElement;
  const el = within(bar).queryByText(label, { selector: 'div' });
  return el ? (el.previousElementSibling?.textContent ?? null) : null;
}

describe('TenantManager — stats keep config errors out of the unknown buckets', () => {
  it('counts the degraded row as a config error, not as env/mode unknown', async () => {
    stubSearch(searchBody);
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-healthy — prod normal')).toBeInTheDocument());
    expect(statValue('Total Tenants')).toBe('3');
    // Only t-unlabeled is "unknown" (one env card + one mode card share the label); t-broken is not.
    const bar = screen.getByText('Total Tenants').parentElement!.parentElement as HTMLElement;
    const unknownValues = within(bar).getAllByText('unknown', { selector: 'div' })
      .map(el => el.previousElementSibling?.textContent);
    expect(unknownValues).toEqual(['1', '1']);
    expect(statValue('Config errors')).toBe('1');
  });

  it('shows no config-error stat card when no row has a config error (control)', async () => {
    stubSearch({ ...searchBody, items: searchBody.items.filter(i => !('config_error' in i)), total_matched: 2 });
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-healthy — prod normal')).toBeInTheDocument());
    expect(statValue('Config errors')).toBe(null);
  });
});

describe('TenantManager — batch YAML excludes degraded rows', () => {
  it.each(['Maintenance YAML', 'Silent Mode YAML'])('%s: no fragment for t-broken, listed as excluded', async (opener) => {
    stubSearch(searchBody);
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-healthy — prod normal')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Select All Filtered' }));
    fireEvent.click(await screen.findByRole('button', { name: opener }));
    const dialog = await screen.findByRole('dialog');
    const text = dialog.textContent || '';
    // Readable rows (incl. the unlabeled control) still get a fragment.
    expect(text).toContain('# t-healthy');
    expect(text).toContain('# t-unlabeled');
    expect(text).not.toContain('# t-broken');
    const excluded = within(dialog).getByTestId('modal-excluded');
    expect(excluded).toHaveTextContent('t-broken');
    expect(excluded).toHaveTextContent('malformed_yaml');
    expect(excluded).toHaveTextContent(/not valid YAML/);
    expect(excluded).not.toHaveTextContent('t-healthy');
  });

  it('no excluded list when nothing selected is degraded (control)', async () => {
    stubSearch(searchBody);
    render(<TenantManager />);
    await waitFor(() => expect(screen.getByLabelText('Tenant: t-healthy — prod normal')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText('Select t-healthy'));
    fireEvent.click(await screen.findByRole('button', { name: 'Maintenance YAML' }));
    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('# t-healthy');
    expect(within(dialog).queryByTestId('modal-excluded')).not.toBeInTheDocument();
  });
});
