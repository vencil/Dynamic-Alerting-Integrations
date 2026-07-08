import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { CustomAlertsModal } from '../src/interactive/tools/tenant-manager/components/CustomAlertsModal.jsx';

const mockMetrics = (catalog: string[]) =>
  vi.fn((_t: string, q: string) => Promise.resolve(catalog.filter((m) => m.startsWith(q || ''))));

function fetchTenant(custom_alerts: any[], source_hash = 'h1') {
  return vi.fn(() => Promise.resolve({ custom_alerts, source_hash }));
}

function fillRecipeForm(name: string, metric: string) {
  fireEvent.change(screen.getByTestId('field-name'), { target: { value: name } });
  fireEvent.change(screen.getByTestId('field-metric'), { target: { value: metric } });
  fireEvent.change(screen.getByTestId('field-window'), { target: { value: '5m' } });
  fireEvent.change(screen.getByTestId('field-threshold'), { target: { value: '100' } });
}

describe('CustomAlertsModal', () => {
  beforeEach(() => { vi.restoreAllMocks(); });

  it('Reef 9: JIT-fetches fresh on open and lists existing recipes', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'existing', metric: 'm', threshold: '1', window: '5m' }]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    expect(screen.getByTestId('loading')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('recipe-existing')).toBeInTheDocument());
    expect(ft).toHaveBeenCalledTimes(1); // fresh fetch, not grid cache
  });

  it('add → submit appends the recipe to the list', async () => {
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics(['queue_depth'])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('add'));
    fillRecipeForm('new_one', 'queue_depth');
    fireEvent.click(screen.getByTestId('submit'));
    await waitFor(() => expect(screen.getByTestId('recipe-new_one')).toBeInTheDocument());
  });

  it('Reef 5: editing with a rename replaces by ORIGINAL name (no duplicate)', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'old_name', metric: 'm', threshold: '1', window: '5m' }]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics(['m'])} />);
    await waitFor(() => expect(screen.getByTestId('recipe-old_name')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('edit-old_name'));
    fireEvent.change(screen.getByTestId('field-name'), { target: { value: 'renamed' } });
    fireEvent.click(screen.getByTestId('submit'));
    await waitFor(() => expect(screen.getByTestId('recipe-renamed')).toBeInTheDocument());
    expect(screen.queryByTestId('recipe-old_name')).toBeNull(); // no duplicate / orphan
  });

  it('delete removes a recipe', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'gone', metric: 'm', threshold: '1', window: '5m' }]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    await waitFor(() => expect(screen.getByTestId('recipe-gone')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('delete-gone'));
    expect(screen.queryByTestId('recipe-gone')).toBeNull();
  });

  it('save sends {custom_alerts, base_hash} and shows a notice on 200', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'a', metric: 'm', threshold: '1', window: '5m' }], 'h1');
    const save = vi.fn(() => Promise.resolve({ ok: true, status: 200, data: { source_hash: 'h2' } }));
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('notice')).toBeInTheDocument());
    expect(save).toHaveBeenCalledWith('db-a', expect.objectContaining({ base_hash: 'h1' }));
    expect(save.mock.calls[0][1].custom_alerts).toHaveLength(1);
  });

  it('Reef 6: a 409 is non-destructive — conflict shown, recipes preserved', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'keep', metric: 'm', threshold: '1', window: '5m' }]);
    const save = vi.fn(() => Promise.resolve({ ok: false, status: 409, data: { current_source_hash: 'hX' } }));
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('conflict')).toBeInTheDocument());
    expect(screen.getByTestId('recipe-keep')).toBeInTheDocument(); // input preserved, not wiped
  });

  it('Reef 4: a 400 surfaces violations and flags the offending recipe', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'legacy_bad', metric: 'a:b', threshold: '1', window: '5m' }]);
    // real backend format from ValidateTenantCustomAlerts: `_custom_alerts[N] (name): ...`
    const save = vi.fn(() => Promise.resolve({ ok: false, status: 400, data: { violations: [{ field: '_custom_alerts', reason: '_custom_alerts[0] (legacy_bad): metric "a:b" is not a valid identifier' }] } }));
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('violations')).toBeInTheDocument());
    expect(screen.getByTestId('violations').textContent).toMatch(/legacy_bad/);
  });

  it('Reef 4: badge anchors by array index, not by name-in-reason text', async () => {
    // index [0] is the offender; recipe at [1] is innocently named "metric"
    // and the reason text legitimately contains the word "metric" — index
    // mapping must badge [0] only, never the "metric" recipe at [1].
    const ft = fetchTenant([
      { recipe: 'threshold', name: 'CamelOffender', metric: 'a:b', threshold: '1', window: '5m' },
      { recipe: 'threshold', name: 'metric', metric: 'ok_metric', threshold: '1', window: '5m' },
    ]);
    const save = vi.fn(() => Promise.resolve({
      ok: false, status: 400,
      data: { violations: [{ field: '_custom_alerts', reason: '_custom_alerts[0] (CamelOffender): metric "a:b" is not a valid identifier' }] },
    }));
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('violations')).toBeInTheDocument());
    // offender at index 0 IS badged
    expect(within(screen.getByTestId('recipe-CamelOffender')).getByText('invalid')).toBeInTheDocument();
    // recipe at index 1 named "metric" is NOT badged despite "metric" in the reason text
    expect(within(screen.getByTestId('recipe-metric')).queryByText('invalid')).toBeNull();
  });

  it('Reef 8: closing with unsaved changes prompts a confirm', async () => {
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={onClose} fetchTenant={ft} fetchMetrics={mockMetrics(['m'])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    // make it dirty
    fireEvent.click(screen.getByTestId('add'));
    fillRecipeForm('d', 'm');
    fireEvent.click(screen.getByTestId('submit'));
    await waitFor(() => expect(screen.getByTestId('recipe-d')).toBeInTheDocument());
    // attempt close → confirm fires, returns false → stays open
    fireEvent.click(screen.getByTestId('close'));
    expect(confirmSpy).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Reef 7: a double-click on Save fires the PUT only once', async () => {
    const ft = fetchTenant([{ recipe: 'threshold', name: 'a', metric: 'm', threshold: '1', window: '5m' }]);
    // a save that stays pending so the second click lands while isSubmitting
    let resolveSave: (v: any) => void = () => {};
    const save = vi.fn(() => new Promise((res) => { resolveSave = res; }));
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} saveCustomAlerts={save as any} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    const saveBtn = screen.getByTestId('save') as HTMLButtonElement;
    fireEvent.click(saveBtn);
    expect(saveBtn.disabled).toBe(true); // locked immediately
    fireEvent.click(saveBtn); // the double-click
    expect(save).toHaveBeenCalledTimes(1); // ...but only one PUT
    resolveSave({ ok: true, status: 200, data: { source_hash: 'h2' } });
  });

  it('a clean modal closes without a confirm', async () => {
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={onClose} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('close'));
    expect(confirmSpy).not.toHaveBeenCalled(); // not dirty
    expect(onClose).toHaveBeenCalled();
  });

  // ── useModalFocusTrap adoption (replaces the hand-rolled Esc listener) ──

  it('a11y: auto-focuses into the modal on open (focus enters the dialog)', async () => {
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    // The shared hook focuses the ref'd panel on mount — focus must land
    // *inside* the modal, not on document.body as before the adoption.
    const modal = screen.getByTestId('custom-alerts-modal');
    expect(modal.contains(document.activeElement)).toBe(true);
  });

  it('a11y: the focused panel is a labelled dialog (role/aria-modal/aria-labelledby)', async () => {
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={() => {}} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    // The ref'd (focused) panel must expose dialog semantics for SR users —
    // matching the sibling tenant-manager modal, not a bare focusable div.
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    // labelled by the visible title (the <h2> carrying the tenant id)
    const labelId = dialog.getAttribute('aria-labelledby');
    expect(labelId).toBeTruthy();
    expect(document.getElementById(labelId as string)?.textContent).toMatch(/db-a/);
  });

  it('Reef 8 via Esc: Escape with unsaved changes prompts a confirm (stays open on cancel)', async () => {
    // Regression guard for the stale-closure trap: the hook subscribes ONCE
    // (constant modalType), so a direct `requestClose` would freeze at mount
    // (isDirty=false) and let Esc close silently. The ref keeps it fresh, so
    // Esc after an edit must see isDirty=true and fire the confirm.
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={onClose} fetchTenant={ft} fetchMetrics={mockMetrics(['m'])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    // make it dirty
    fireEvent.click(screen.getByTestId('add'));
    fillRecipeForm('d', 'm');
    fireEvent.click(screen.getByTestId('submit'));
    await waitFor(() => expect(screen.getByTestId('recipe-d')).toBeInTheDocument());
    // Escape → hook routes to the LATEST requestClose → confirm fires → cancel keeps it open
    fireEvent.keyDown(screen.getByTestId('custom-alerts-modal'), { key: 'Escape' });
    expect(confirmSpy).toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('Esc on a clean modal closes without a confirm', async () => {
    const onClose = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const ft = fetchTenant([]);
    render(<CustomAlertsModal tenantId="db-a" onClose={onClose} fetchTenant={ft} fetchMetrics={mockMetrics([])} />);
    await waitFor(() => expect(screen.getByTestId('add')).toBeInTheDocument());
    fireEvent.keyDown(screen.getByTestId('custom-alerts-modal'), { key: 'Escape' });
    expect(confirmSpy).not.toHaveBeenCalled(); // not dirty
    expect(onClose).toHaveBeenCalled();
  });
});
