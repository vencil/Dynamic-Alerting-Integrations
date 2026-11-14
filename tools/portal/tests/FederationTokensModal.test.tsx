/**
 * ADR-027 §4 / #1040 S6: federation tokens are issued, listed and revoked
 * from the portal (a tenant admin's human operation), against tenant-api's
 * /api/v1/federation/tokens. Every API body below goes through apiFixture
 * (#2833), so it can only carry what the generated spec says the API sends.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { apiFixture } from './helpers/api-fixture';
import { FederationTokensModal } from '../src/interactive/tools/tenant-manager/components/FederationTokensModal.jsx';
import { signInHeld, SIGN_IN_ATTEMPT_KEY } from '../src/interactive/tools/_common/auth/sign-in.js';
import { apiFetch } from '../src/interactive/tools/_common/api/api-fetch.js';

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

function open(a = api()) {
  render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} api={a} />);
  return { a };
}

// The dialog's own requests (no `api` prop): fetch answers per method, in
// order; the last answer repeats. location.assign records leaving for sign-in.
type Wire = { status: number; data: any };
function liveFetch(answers: Partial<Record<'GET' | 'POST' | 'DELETE', Wire[]>>) {
  const seen: Record<string, number> = {};
  const f = vi.fn((url: string, init?: RequestInit) => {
    const method = (init && init.method) || 'GET';
    const list = answers[method as 'GET'] || [{ status: 500, data: null }];
    const i = Math.min(seen[method] = (seen[method] ?? -1) + 1, list.length - 1);
    const { status, data } = list[i];
    return Promise.resolve({
      ok: status >= 200 && status < 300, status, headers: new Headers(),
      json: () => Promise.resolve(data),
    } as any);
  });
  vi.stubGlobal('fetch', f);
  return f;
}
const TOKEN_ANSWER = () => ({ token: 'eyJ.signed.jwt', record: record({ token_id: 'tok-2', description: 'new one' }) });
let assign: ReturnType<typeof vi.fn>;
beforeEach(() => {
  window.sessionStorage.clear();
  assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '', hash: '', assign });
});
afterEach(() => { vi.unstubAllGlobals(); });

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
    const f = liveFetch({ GET: [{ status: 401, data: { error: 'unauthorized' } }] });
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(f).toHaveBeenCalledWith('/api/v1/federation/tokens?tenant_id=tenant-x');
    expect(screen.queryByTestId('fed-error')).toBeNull();
  });

  it('401 right after signing in: stays and says it is still refused', async () => {
    window.sessionStorage.setItem(SIGN_IN_ATTEMPT_KEY, String(Date.now()));
    liveFetch({ GET: [{ status: 401, data: { error: 'unauthorized' } }] });
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('fed-error').textContent).toContain('Still refused'));
    expect(assign).not.toHaveBeenCalled();
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

  it('a refused logs issue (no account registry) shows its remedy and no token', async () => {
    open(api({
      issue: vi.fn(() => Promise.resolve(fail(503, apiFixture('ErrorResponse', {
        error: 'logs federation is not configured on this server', code: 'FEDERATION_NOT_CONFIGURED',
      })))),
    }));
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-capability-logs'));
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.getByTestId('fed-error').textContent).toContain('logs federation is not configured');
    expect(screen.queryByTestId('fed-new-token')).toBeNull();
  });

  it('409 TOKEN_LIMIT_REACHED: says an existing token stands in the way', async () => {
    open(api({
      issue: vi.fn(() => Promise.resolve(fail(409, apiFixture('ErrorResponse', {
        error: 'tenant tenant-x already has 16 active federation tokens', code: 'TOKEN_LIMIT_REACHED',
      })))),
    }));
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.getByTestId('fed-error').getAttribute('data-remedy')).toBe('existing');
  });

  it('429 RATE_LIMITED: says how long to wait, from Retry-After', async () => {
    open(api({
      issue: vi.fn(() => Promise.resolve({
        ...fail(429, apiFixture('ErrorResponse', { error: 'issuance rate limit', code: 'RATE_LIMITED' })),
        retryAfter: '17',
      })),
    }));
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.getByTestId('fed-error').getAttribute('data-remedy')).toBe('retry_after');
    expect(screen.getByTestId('fed-error').textContent).toContain('17');
  });

  it('256 characters count as characters, not UTF-16 units (Go counts runes)', async () => {
    const { a } = open();
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('fed-description'), { target: { value: '😀'.repeat(256) } });
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(a.issue).toHaveBeenCalled());
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

describe('FederationTokensModal — the one-time token is never dropped silently', () => {
  it('a 401 elsewhere on the page while the issue request is in flight does not leave', async () => {
    let answerIssue: (v: any) => void = () => {};
    const wire = (status: number, data: any) => ({ ok: status < 300, status, headers: new Headers(), json: () => Promise.resolve(data) });
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (init && init.method === 'POST') return new Promise((r) => { answerIssue = r; });
      if (String(url).startsWith('/api/v1/prs')) return Promise.resolve(wire(401, { error: 'unauthorized' }));
      return Promise.resolve(wire(200, [record()]));
    }));
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect((fetch as any).mock.calls.some((c: any[]) => c[1] && c[1].method === 'POST')).toBe(true));
    // the page's background poll, answered 401 while the token is on its way
    await apiFetch('/api/v1/prs');
    expect(assign).not.toHaveBeenCalled();
    answerIssue(wire(201, TOKEN_ANSWER()));
    await waitFor(() => expect(screen.getByTestId('fed-new-token').textContent).toContain('eyJ.signed.jwt'));
    expect(assign).not.toHaveBeenCalled();
  });

  it('a 401 on the issue request itself still sends the browser to sign in', async () => {
    liveFetch({
      GET: [{ status: 200, data: [record()] }],
      POST: [{ status: 401, data: { error: 'unauthorized' } }],
    });
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(screen.queryByTestId('fed-error')).toBeNull();
  });

  async function issueOne(a = api()) {
    const onClose = vi.fn();
    render(<FederationTokensModal tenantId="tenant-x" onClose={onClose} api={a} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
    return { onClose, a };
  }

  it('Close asks first while the token is shown; "close anyway" closes', async () => {
    const { onClose } = await issueOne();
    fireEvent.click(screen.getByTestId('fed-close'));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId('fed-close-anyway'));
    expect(onClose).toHaveBeenCalled();
  });

  it('Esc asks first too', async () => {
    const { onClose } = await issueOne();
    fireEvent.keyDown(document, { key: 'Escape' });
    await waitFor(() => expect(screen.getByTestId('fed-close-confirm')).toBeInTheDocument());
    expect(onClose).not.toHaveBeenCalled();
  });

  it('after "I have saved it", Close closes at once and Issue is enabled again', async () => {
    const { onClose } = await issueOne();
    expect((screen.getByTestId('fed-issue') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId('fed-token-saved'));
    expect(screen.queryByTestId('fed-new-token')).toBeNull();
    expect((screen.getByTestId('fed-issue') as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(screen.getByTestId('fed-close'));
    expect(onClose).toHaveBeenCalled();
  });

  it('a 401 while the token is shown does not leave for sign-in', async () => {
    liveFetch({
      GET: [{ status: 200, data: [record()] }],
      POST: [{ status: 201, data: TOKEN_ANSWER() }],
      DELETE: [{ status: 401, data: { error: 'unauthorized' } }],
    });
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
    const row = screen.getAllByTestId('fed-row')[0];
    fireEvent.click(within(row).getByTestId('fed-revoke'));
    fireEvent.click(within(row).getByTestId('fed-revoke-confirm'));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByTestId('fed-new-token').textContent).toContain('eyJ.signed.jwt');
  });
});

describe('FederationTokensModal — other states', () => {
  it('a list 401 right after signing in shows the refusal, not "Loading…" forever', async () => {
    window.sessionStorage.setItem(SIGN_IN_ATTEMPT_KEY, String(Date.now()));
    open(api({ list: vi.fn(() => Promise.resolve(fail(401, { error: 'unauthorized' }))) }));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(screen.queryByText('Loading…')).toBeNull();
  });

  it('without a tenant id it asks for one (a removed tenant has no card), then lists it', async () => {
    const a = api();
    render(<FederationTokensModal tenantId="" onClose={() => {}} api={a} />);
    expect(a.list).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId('fed-tenant-id'), { target: { value: ' gone-tenant ' } });
    fireEvent.click(screen.getByTestId('fed-tenant-open'));
    await waitFor(() => expect(a.list).toHaveBeenCalledWith('gone-tenant'));
    await waitFor(() => expect(screen.getAllByTestId('fed-row')).toHaveLength(1));
  });
});

describe('FederationTokensModal — CodeRabbit review (#3740)', () => {
  it('Close is ignored while the issue request is in flight; the token then shows', async () => {
    let resolve: (v: Answer) => void = () => {};
    const issue = vi.fn(() => new Promise<Answer>((r) => { resolve = r; }));
    const onClose = vi.fn();
    render(<FederationTokensModal tenantId="tenant-x" onClose={onClose} api={api({ issue })} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    fireEvent.click(screen.getByTestId('fed-close'));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).not.toHaveBeenCalled();
    resolve(ok(apiFixture('CreateFederationTokenResponse', { token: 'tok-late', record: record() }), 201));
    await waitFor(() => expect(screen.getByTestId('fed-new-token').textContent).toContain('tok-late'));
  });

  it('a 401 on the reload right after issuing keeps the token and does not sign in', async () => {
    const f = liveFetch({
      GET: [{ status: 200, data: [record()] }, { status: 401, data: { error: 'unauthorized' } }],
      POST: [{ status: 201, data: TOKEN_ANSWER() }],
    });
    render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(f.mock.calls.filter((c) => !c[1]).length).toBe(2));
    await waitFor(() => expect(screen.getByTestId('fed-error')).toBeInTheDocument());
    expect(assign).not.toHaveBeenCalled();
    expect(screen.getByTestId('fed-new-token').textContent).toContain('eyJ.signed.jwt');
    // still copyable: the copy button writes the issued token to the clipboard
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    fireEvent.click(screen.getByTestId('fed-copy-token'));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(String(writeText.mock.calls[0][0])).toContain('eyJ.signed.jwt');
    expect(assign).not.toHaveBeenCalled();
  });

  it('holds sign-in for the whole page while the token is shown, and lets go after', async () => {
    const { unmount } = render(<FederationTokensModal tenantId="tenant-x" onClose={() => {}} api={api()} />);
    await waitFor(() => expect(screen.getByTestId('fed-issue')).toBeInTheDocument());
    expect(signInHeld()).toBe(false);
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
    expect(signInHeld()).toBe(true);
    fireEvent.click(screen.getByTestId('fed-token-saved'));
    expect(signInHeld()).toBe(false);
    fireEvent.click(screen.getByTestId('fed-issue'));
    await waitFor(() => expect(screen.getByTestId('fed-new-token')).toBeInTheDocument());
    expect(signInHeld()).toBe(true);
    unmount();
    expect(signInHeld()).toBe(false);
  });
});
