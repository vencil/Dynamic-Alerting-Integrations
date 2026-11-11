/**
 * ADR-027 §4 / #1040 S6: federation tokens are issued, listed and revoked
 * from the portal (a tenant admin's human operation), against tenant-api's
 * /api/v1/federation/tokens. Every API body below goes through apiFixture
 * (#2833), so it can only carry what the generated spec says the API sends.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { apiFixture } from './helpers/api-fixture';
import { FederationTokensModal } from '../src/interactive/tools/tenant-manager/components/FederationTokensModal.jsx';

type Answer = { ok: boolean; status: number; data: any; retryAfter?: string | null };

const record = (over: Record<string, unknown> = {}) => apiFixture('FederationTokenRecord', {
  token_id: 'tok-1',
  tenant_id: 'tenant-x',
  issued_by: 'alice@example.com',
  description: 'grafana pull',
  issued_at: '2026-11-11T10:00:00Z',
  expires_at: '2026-11-12T10:00:00Z',
  ...over,
});

const ok = (data: any, status = 200): Answer => ({ ok: true, status, data });
const fail = (status: number, data: any): Answer => ({ ok: false, status, data });

function api(over: Partial<Record<'list' | 'issue' | 'revoke', any>> = {}) {
  return {
    list: vi.fn(() => Promise.resolve(ok([record()]))),
    issue: vi.fn(() => Promise.resolve(ok(apiFixture('CreateFederationTokenResponse', {
      token: 'eyJ.signed.jwt',
      record: record({ token_id: 'tok-2', description: 'new one' }),
    }), 201))),
    revoke: vi.fn(() => Promise.resolve(ok({
      status: 'revocation_initiated', token_id: 'tok-1',
      detail: 'revocation propagates to the gateway within ~2 minutes',
    }))),
    ...over,
  };
}

function open(a = api(), signIn = vi.fn(() => true)) {
  render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} api={a} signIn={signIn} />);
  return { a, signIn };
}

describe('FederationTokensModal — list', () => {
  it('lists the tenant\'s tokens without any signed token', async () => {
    const { a } = open(api({
      list: vi.fn(() => Promise.resolve(ok([record(), record({ token_id: 'tok-l', capability: 'logs', account_id: 7, description: '' })]))),
    }));
    await waitFor(() => expect(screen.getAllByTestId('fed-row')).toHaveLength(2));
    expect(a.list).toHaveBeenCalledWith('tenant-x');
    const [metrics, logs] = screen.getAllByTestId('fed-row');
    expect(metrics.textContent).toContain('tok-1');
    expect(metrics.textContent).toContain('metrics');
    expect(metrics.textContent).toContain('grafana pull');
    expect(metrics.textContent).toContain('alice@example.com');
    expect(logs.textContent).toContain('logs');
    expect(screen.queryByTestId('fed-new-token')).toBeNull();
  });

  it('an empty list says so', async () => {
    open(api({ list: vi.fn(() => Promise.resolve(ok([]))) }));
    await waitFor(() => expect(screen.getByTestId('fed-empty')).toBeInTheDocument());
  });

  it('404: federation is not enabled on this deployment — no issue form', async () => {
    open(api({ list: vi.fn(() => Promise.resolve(fail(404, { error: '404 page not found' }))) }));
    await waitFor(() => expect(screen.getByTestId('fed-unavailable')).toBeInTheDocument());
    expect(screen.queryByTestId('fed-issue')).toBeNull();
  });

  it('403: says why, with the permission remedy, and offers no issue form', async () => {
    open(api({
      list: vi.fn(() => Promise.resolve(fail(403, apiFixture('ErrorResponse', {
        error: 'admin permission required on tenant tenant-x', code: 'FORBIDDEN',
      })))),
    }));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.getByTestId('fed-error').getAttribute('data-remedy')).toBe('permission');
    expect(screen.queryByTestId('fed-issue')).toBeNull();
  });

  it('401: sends the browser to sign in', async () => {
    const { signIn } = open(api({ list: vi.fn(() => Promise.resolve(fail(401, { error: 'unauthorized' }))) }));
    await waitFor(() => expect(signIn).toHaveBeenCalled());
  });

  it('a 200 that is not a list is reported, not shown as "no tokens"', async () => {
    open(api({ list: vi.fn(() => Promise.resolve(ok({ tokens: [] }))) }));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.queryByTestId('fed-empty')).toBeNull();
  });
});

describe('FederationTokensModal — issue', () => {
  it('issues a metrics token, shows it once, and reloads the list', async () => {
    const { a } = open();
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('fed-description'), { target: { value: 'new one' } });
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
    expect(a.issue).toHaveBeenCalledWith({ tenant_id: 'tenant-x', description: 'new one', capability: 'metrics' });
    expect(screen.getByTestId('fed-new-token').textContent).toContain('eyJ.signed.jwt');
    expect(a.list).toHaveBeenCalledTimes(2);
  });

  it('issues a logs token when logs is picked', async () => {
    const { a } = open();
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-capability-logs'));
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(a.issue).toHaveBeenCalled());
    expect(a.issue).toHaveBeenCalledWith({ tenant_id: 'tenant-x', description: '', capability: 'logs' });
  });

  it('a description over 256 characters is refused before sending', async () => {
    const { a } = open();
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('fed-description'), { target: { value: 'x'.repeat(257) } });
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(a.issue).not.toHaveBeenCalled();
  });

  it('a refused issue shows its remedy and no token', async () => {
    open(api({
      issue: vi.fn(() => Promise.resolve(fail(503, apiFixture('ErrorResponse', {
        error: 'logs federation is not configured on this server', code: 'FEDERATION_NOT_CONFIGURED',
      })))),
    }));
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.getByTestId('fed-error').textContent).toContain('logs federation is not configured');
    expect(screen.queryByTestId('fed-new-token')).toBeNull();
  });

  it('cannot be sent twice while the first is in flight', async () => {
    let resolve: (v: Answer) => void = () => {};
    const issue = vi.fn(() => new Promise<Answer>((r) => { resolve = r; }));
    open(api({ issue }));
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    fireEvent.click(screen.getByTestId('fed-issue'));
    expect(issue).toHaveBeenCalledTimes(1);
    resolve(ok(apiFixture('CreateFederationTokenResponse', { token: 't', record: record() }), 201));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
  });
});

describe('FederationTokensModal — revoke', () => {
  it('asks first, then revokes, reloads and says revocation takes a while', async () => {
    const { a } = open();
    await waitFor(() => expect(screen.getAllByTestId('fed-row')).toHaveLength(1));
    const row = screen.getAllByTestId('fed-row')[0];
    fireEvent.click(within(row).getByTestId('fed-revoke'));
    expect(a.revoke).not.toHaveBeenCalled();
    fireEvent.click(within(row).getByTestId('fed-revoke-confirm'));
    await waitFor(() => expect(a.revoke).toHaveBeenCalledWith('tok-1'));
    await waitFor(() => expect(screen.getByTestId('fed-revoked')).toBeInTheDocument());
    expect(a.list).toHaveBeenCalledTimes(2);
  });

  it('404 on revoke: gone or not yours — the same answer, said once', async () => {
    open(api({ revoke: vi.fn(() => Promise.resolve(fail(404, { error: 'federation token not found: tok-1' }))) }));
    await waitFor(() => expect(screen.getAllByTestId('fed-row')).toHaveLength(1));
    const row = screen.getAllByTestId('fed-row')[0];
    fireEvent.click(within(row).getByTestId('fed-revoke'));
    fireEvent.click(within(row).getByTestId('fed-revoke-confirm'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.queryByTestId('fed-revoked')).toBeNull();
  });
});
