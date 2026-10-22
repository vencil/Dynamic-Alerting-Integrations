/**
 * Unit tests for `SavedViewsPanel` — TECH-DEBT-030b first-batch.
 *
 * RTL render. window.__t provided via test-setup.ts.
 * The panel takes `savedViews` as a prop (the hook return), so tests
 * pass plain objects rather than mocking the hook itself.
 *
 * `views` is the GET /api/v1/views body, so it goes through
 * `apiFixture('ViewResponse[]', …)` (#2833 / #3231).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { SavedViewsPanel } from '../src/interactive/tools/tenant-manager/components/SavedViewsPanel.jsx';
import { apiFixture } from './helpers/api-fixture';

const views = (list: unknown[]) => apiFixture('ViewResponse[]', list);

const baseHookReturn = {
  views: views([]),
  loading: false,
  reachable: true,
  reload: vi.fn(),
  save: vi.fn(),
  remove: vi.fn(),
};

describe('SavedViewsPanel', () => {
  it('renders nothing when reachable=false (demo-mode contract)', () => {
    const { container } = render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={true}
        savedViews={{ ...baseHookReturn, reachable: false }}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows empty-state hint when reachable but no views', () => {
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={true}
        savedViews={{ ...baseHookReturn, views: views([]) }}
      />,
    );
    expect(screen.getByTestId('saved-views-panel')).toBeInTheDocument();
    expect(screen.getByTestId('saved-views-empty')).toBeInTheDocument();
    expect(screen.queryByTestId('saved-views-select')).not.toBeInTheDocument();
  });

  it('lists saved views in a select dropdown when populated', () => {
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={true}
        savedViews={{
          ...baseHookReturn,
          views: views([
            { id: 'prod-finance', label: 'Production Finance', filters: { environment: 'production' } },
            { id: 'critical-silent', label: 'Critical + Silent', filters: { tier: 'tier-1' } },
          ]),
        }}
      />,
    );
    const select = screen.getByTestId('saved-views-select');
    expect(select).toBeInTheDocument();
    // Both view ids should be present as <option value="...">
    expect(select.querySelector('option[value="prod-finance"]')).not.toBeNull();
    expect(select.querySelector('option[value="critical-silent"]')).not.toBeNull();
  });

  // #3231: the option value and the applied / deleted view are the view's
  // `id`, not its position in the list — reading the array as an id map
  // made them '0' / '1' and sent apply and delete to the wrong view.
  const twoViews = views([
    { id: 'prod-finance', label: 'Production Finance', filters: { environment: 'production' } },
    { id: 'critical-silent', label: 'Critical + Silent', filters: { tier: 'tier-1' } },
  ]);

  it('applies the filters of the view whose id was picked', () => {
    const onApplyView = vi.fn();
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={onApplyView}
        canWrite={false}
        savedViews={{ ...baseHookReturn, views: twoViews }}
      />,
    );
    const select = screen.getByTestId('saved-views-select') as HTMLSelectElement;
    const values = Array.from(select.options).map(o => o.value).filter(Boolean);
    expect(values).toEqual(['critical-silent', 'prod-finance']);
    fireEvent.change(select, { target: { value: 'prod-finance' } });
    expect(onApplyView).toHaveBeenCalledWith({ environment: 'production' });
  });

  it('deletes the view whose id was picked', async () => {
    const remove = vi.fn(() => Promise.resolve(true));
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={true}
        savedViews={{ ...baseHookReturn, views: twoViews, remove }}
      />,
    );
    fireEvent.change(screen.getByTestId('saved-views-delete-select'), { target: { value: 'critical-silent' } });
    await act(async () => {
      fireEvent.click(screen.getByTestId('saved-views-delete-confirm'));
    });
    expect(remove).toHaveBeenCalledWith('critical-silent');
  });

  it('hides Save / Delete controls when canWrite=false (RBAC)', () => {
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={false}
        savedViews={{
          ...baseHookReturn,
          views: views([{ id: 'a', label: 'A', filters: {} }]),
        }}
      />,
    );
    expect(screen.queryByTestId('saved-views-save-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('saved-views-delete-select')).not.toBeInTheDocument();
    // List + apply still works.
    expect(screen.getByTestId('saved-views-select')).toBeInTheDocument();
  });

  it('shows Save / Delete controls when canWrite=true', () => {
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={true}
        savedViews={{
          ...baseHookReturn,
          views: views([{ id: 'a', label: 'A', filters: {} }]),
        }}
      />,
    );
    expect(screen.getByTestId('saved-views-save-btn')).toBeInTheDocument();
    expect(screen.getByTestId('saved-views-delete-select')).toBeInTheDocument();
  });
});
