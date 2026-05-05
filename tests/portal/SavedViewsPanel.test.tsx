/**
 * Unit tests for `SavedViewsPanel` — TECH-DEBT-030b first-batch.
 *
 * RTL render. window.__styles + window.__t provided via test-setup.ts.
 * The panel takes `savedViews` as a prop (the hook return), so tests
 * pass plain objects rather than mocking the hook itself.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SavedViewsPanel } from '../../docs/interactive/tools/tenant-manager/components/SavedViewsPanel.jsx';

const baseHookReturn = {
  views: {},
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
        savedViews={{ ...baseHookReturn, views: {} }}
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
          views: {
            'prod-finance': { label: 'Production Finance', filters: { environment: 'production' } },
            'critical-silent': { label: 'Critical + Silent', filters: { tier: 'tier-1' } },
          },
        }}
      />,
    );
    const select = screen.getByTestId('saved-views-select');
    expect(select).toBeInTheDocument();
    // Both view ids should be present as <option value="...">
    expect(select.querySelector('option[value="prod-finance"]')).not.toBeNull();
    expect(select.querySelector('option[value="critical-silent"]')).not.toBeNull();
  });

  it('hides Save / Delete controls when canWrite=false (RBAC)', () => {
    render(
      <SavedViewsPanel
        currentFilters={{}}
        onApplyView={() => {}}
        canWrite={false}
        savedViews={{
          ...baseHookReturn,
          views: { 'a': { label: 'A', filters: {} } },
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
          views: { 'a': { label: 'A', filters: {} } },
        }}
      />,
    );
    expect(screen.getByTestId('saved-views-save-btn')).toBeInTheDocument();
    expect(screen.getByTestId('saved-views-delete-select')).toBeInTheDocument();
  });
});
