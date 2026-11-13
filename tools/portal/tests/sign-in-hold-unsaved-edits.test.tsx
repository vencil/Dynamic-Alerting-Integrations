/**
 * A 401 must not take away work that exists only on the page (ADR-027 §5;
 * #3809 review). Every request goes through apiFetch, which sends the browser
 * to sign in on a 401 — including a background poll the user never sees. So
 * a component holding unsaved edits holds sign-in (holdSignIn) for as long as
 * they are unsaved, and a 401 on its own save says the session expired and
 * keeps the edits on screen, instead of navigating away.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { CustomAlertsModal } from '../src/interactive/tools/tenant-manager/components/CustomAlertsModal.jsx';
import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';
import { apiFetch } from '../src/interactive/tools/_common/api/api-fetch.js';
import { signInHeld } from '../src/interactive/tools/_common/auth/sign-in.js';
import { apiFixture } from './helpers/api-fixture';

const wire = (status: number, data: any = {}) => ({
  ok: status >= 200 && status < 300, status, headers: new Headers(),
  json: () => Promise.resolve(data),
});

let assign: ReturnType<typeof vi.fn>;
beforeEach(() => {
  window.sessionStorage.clear();
  assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, pathname: '/interactive/', search: '', hash: '', assign });
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(wire(401, { error: 'unauthorized' }))));
});
afterEach(() => { vi.unstubAllGlobals(); });

const tenant = () => vi.fn(() => Promise.resolve(apiFixture('TenantDetail', {
  custom_alerts: [{ recipe: 'threshold', name: 'gone', metric: 'm', threshold: '1', window: '5m' }],
  source_hash: 'h1',
})));
const noMetrics = vi.fn(() => Promise.resolve([]));

describe('CustomAlertsModal: unsaved edits hold sign-in', () => {
  it('holds nothing while there is nothing unsaved', async () => {
    render(<CustomAlertsModal tenantId="t" onClose={() => {}} fetchTenant={tenant()} fetchMetrics={noMetrics} />);
    await waitFor(() => expect(screen.getByTestId('recipe-gone')).toBeInTheDocument());
    expect(signInHeld()).toBe(false);
  });

  it('a 401 on save keeps the edits and says the session expired', async () => {
    const { unmount } = render(<CustomAlertsModal tenantId="t" onClose={() => {}} fetchTenant={tenant()} fetchMetrics={noMetrics} />);
    await waitFor(() => expect(screen.getByTestId('recipe-gone')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-gone')); // an unsaved edit
    expect(signInHeld()).toBe(true);
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('save-error').textContent).toMatch(/session has expired/i));
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByTestId('recipe-gone')).toBeNull(); // the edit is still there
    unmount();
    expect(signInHeld()).toBe(false);
  });

  it('a 401 from a background request does not navigate away from unsaved edits', async () => {
    render(<CustomAlertsModal tenantId="t" onClose={() => {}} fetchTenant={tenant()} fetchMetrics={noMetrics} />);
    await waitFor(() => expect(screen.getByTestId('recipe-gone')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-gone'));
    await apiFetch('/api/v1/prs'); // tenant-manager's poll
    expect(assign).not.toHaveBeenCalled();
  });

  it('a successful save releases the hold', async () => {
    const save = vi.fn(() => Promise.resolve({ ok: true, status: 200, data: apiFixture('PutCustomAlertsResponse', { source_hash: 'h2' }) }));
    render(<CustomAlertsModal tenantId="t" onClose={() => {}} fetchTenant={tenant()} fetchMetrics={noMetrics} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('recipe-gone')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-gone'));
    expect(signInHeld()).toBe(true);
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('notice')).toBeInTheDocument());
    expect(signInHeld()).toBe(false);
  });
});

describe('RecipeBuilder (standalone): an unsaved draft holds sign-in', () => {
  it('holds nothing before the first edit, holds once edited, lets go on unmount', () => {
    const { unmount } = render(<RecipeBuilder tenantId="t" fetchMetrics={noMetrics} />);
    expect(signInHeld()).toBe(false);
    fireEvent.change(screen.getByTestId('field-name'), { target: { value: 'my alert' } });
    expect(signInHeld()).toBe(true);
    unmount();
    expect(signInHeld()).toBe(false);
  });

  it('a 401 from metrics discovery does not navigate away from the draft', async () => {
    render(<RecipeBuilder tenantId="t" />); // default fetchMetrics: apiFetch
    fireEvent.change(screen.getByTestId('field-name'), { target: { value: 'my alert' } });
    fireEvent.change(screen.getByTestId('field-metric'), { target: { value: 'queue' } });
    await waitFor(() => expect(fetch).toHaveBeenCalled(), { timeout: 3000 });
    await new Promise((r) => setTimeout(r, 0));
    expect(assign).not.toHaveBeenCalled();
    expect((screen.getByTestId('field-name') as HTMLInputElement).value).toBe('my alert');
  });
});
