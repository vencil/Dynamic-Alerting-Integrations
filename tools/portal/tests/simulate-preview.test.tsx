/**
 * simulate-preview — error attribution (#2125).
 *
 * The shipped portal deployments do NOT proxy /api/v1/tenants/simulate
 * (the endpoint lives in threshold-exporter); both nginx configs answer
 * it with a fixed 501 JSON `{code: SIMULATE_NOT_PROVIDED}`. The widget
 * must recognise exactly that pair and say "not provided by this
 * deployment"; every other failure is a generic error that must NOT
 * point the operator at tenant-api.
 *
 * The auto-simulate effect fires on mount (seeded sample inputs) after
 * a 500ms debounce, so each case just stubs fetch and waits.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import SimulatePreview, {
  SIMULATE_NOT_PROVIDED_CODE,
} from '../src/interactive/tools/simulate-preview.jsx';

function stubFetch(impl: () => Promise<Response>) {
  const fn = vi.fn(impl);
  vi.stubGlobal('fetch', fn);
  return fn;
}

function jsonResponse(status: number, body: unknown, statusText = ''): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { 'Content-Type': 'application/json' },
  });
}

const WAIT = { timeout: 3000 };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('SimulatePreview error attribution (#2125)', () => {
  it('501 + SIMULATE_NOT_PROVIDED → dedicated "not provided" state', async () => {
    const fetchMock = stubFetch(async () =>
      jsonResponse(501, { code: SIMULATE_NOT_PROVIDED_CODE, error: 'not provided' }),
    );
    render(<SimulatePreview />);

    const box = await screen.findByTestId('simulate-preview-state-not-provided', {}, WAIT);
    expect(box.textContent).toMatch(/not provided by this deployment/i);
    expect(box.textContent).toMatch(/threshold-exporter/);
    expect(box.textContent).not.toMatch(/tenant-api/);
    expect(screen.queryByTestId('simulate-preview-state-error')).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/tenants/simulate',
      expect.objectContaining({ method: 'POST' }),
    );
  });

  it('501 with a different code → generic error, not the "not provided" state', async () => {
    stubFetch(async () => jsonResponse(501, { code: 'SOMETHING_ELSE', error: 'nope' }, 'Not Implemented'));
    render(<SimulatePreview />);

    const err = await screen.findByTestId('simulate-preview-state-error', {}, WAIT);
    expect(err.textContent).toContain('501');
    expect(err.textContent).toContain('nope');
    expect(err.textContent).not.toMatch(/tenant-api/);
    expect(screen.queryByTestId('simulate-preview-state-not-provided')).toBeNull();
  });

  it('non-501 status carrying the code (e.g. 405) → generic error', async () => {
    stubFetch(async () =>
      jsonResponse(405, { code: SIMULATE_NOT_PROVIDED_CODE, error: 'method not allowed' }),
    );
    render(<SimulatePreview />);

    const err = await screen.findByTestId('simulate-preview-state-error', {}, WAIT);
    expect(err.textContent).toContain('405');
    expect(err.textContent).not.toMatch(/tenant-api/);
    expect(screen.queryByTestId('simulate-preview-state-not-provided')).toBeNull();
  });

  it('non-JSON 405 (empty body) → generic error with status, no tenant-api hint', async () => {
    stubFetch(async () => new Response('', { status: 405, statusText: 'Method Not Allowed' }));
    render(<SimulatePreview />);

    const err = await screen.findByTestId('simulate-preview-state-error', {}, WAIT);
    expect(err.textContent).toContain('405');
    expect(err.textContent).toContain('Method Not Allowed');
    expect(err.textContent).not.toMatch(/tenant-api/);
  });

  it('network failure → generic unreachable error, no tenant-api hint', async () => {
    stubFetch(async () => {
      throw new TypeError('Failed to fetch');
    });
    render(<SimulatePreview />);

    const err = await screen.findByTestId('simulate-preview-state-error', {}, WAIT);
    expect(err.textContent).toMatch(/Could not reach the simulate endpoint/);
    expect(err.textContent).not.toMatch(/tenant-api/);
    expect(screen.queryByTestId('simulate-preview-state-not-provided')).toBeNull();
  });

  it('200 still renders the ready state', async () => {
    stubFetch(async () =>
      jsonResponse(200, {
        tenant_id: 'example-tenant',
        source_hash: 'aaaa',
        merged_hash: 'bbbb',
        defaults_chain: [],
        effective_config: { cpu_threshold: 70 },
      }),
    );
    render(<SimulatePreview />);

    await waitFor(
      () => expect(screen.getByTestId('simulate-preview-state-ready')).toBeInTheDocument(),
      WAIT,
    );
    expect(screen.getByTestId('simulate-preview-merged-hash').textContent).toBe('bbbb');
  });
});
