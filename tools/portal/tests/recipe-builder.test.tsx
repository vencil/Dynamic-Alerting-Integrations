import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';

/** A mock S6a fetcher: returns names that start with the query prefix. */
function mockFetch(catalog: string[]) {
  return vi.fn((_tenant: string, q: string) =>
    Promise.resolve(catalog.filter((m) => m.startsWith(q || ''))));
}

function fill(testid: string, value: string) {
  fireEvent.change(screen.getByTestId(testid), { target: { value } });
}

describe('RecipeBuilder', () => {
  it('renders recipe + name fields', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch([])} />);
    expect(screen.getByTestId('recipe-builder')).toBeInTheDocument();
    expect(screen.getByTestId('field-recipe')).toBeInTheDocument();
    expect(screen.getByTestId('field-name')).toBeInTheDocument();
  });

  it('conditional fields: ratio shows denominator_metric; forecast shows horizon not window', () => {
    const { rerender } = render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch([])} />);
    // default threshold: window present, no denominator/horizon
    expect(screen.getByTestId('field-window')).toBeInTheDocument();
    expect(screen.queryByTestId('field-denominator_metric')).toBeNull();

    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'ratio' } });
    expect(screen.getByTestId('field-denominator_metric')).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'forecast' } });
    expect(screen.getByTestId('field-horizon')).toBeInTheDocument();
    expect(screen.queryByTestId('field-window')).toBeNull();
    expect(screen.getByTestId('field-capacity_metric')).toBeInTheDocument();
    rerender(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch([])} />);
  });

  it('summary is a state machine: placeholder until required filled, then dynamic', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} />);
    expect(screen.getByTestId('summary').textContent).toMatch(/Waiting for required/i);

    fill('field-name', 'queue_high');
    fill('field-metric', 'queue_depth');
    fill('field-window', '5m');
    fill('field-threshold', '100:warning');

    expect(screen.getByTestId('summary').textContent).toMatch(/fires warning when queue_depth > 100 over 5m/);
  });

  it('ghost soft-warn: a metric absent from discovery warns on blur (decoupled validation)', async () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['real_metric'])} />);
    const input = screen.getByTestId('field-metric');
    fireEvent.change(input, { target: { value: 'ghost_metric' } });
    fireEvent.blur(input, { target: { value: 'ghost_metric' } });
    await waitFor(() =>
      expect(screen.getByTestId('field-metric-ghost')).toBeInTheDocument());
  });

  it('no ghost-warn when the metric exists in discovery', async () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['real_metric'])} />);
    const input = screen.getByTestId('field-metric');
    fireEvent.change(input, { target: { value: 'real_metric' } });
    fireEvent.blur(input, { target: { value: 'real_metric' } });
    // give the async validation a tick; ghost element must NOT appear
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('field-metric-ghost')).toBeNull();
  });

  it('GitOps persona (no onSubmit): emits a YAML snippet with the full wrapper', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} />);
    fill('field-name', 'queue_high');
    fill('field-metric', 'queue_depth');
    fill('field-window', '5m');
    fill('field-threshold', '100:warning');
    const yaml = screen.getByTestId('yaml-output').textContent || '';
    expect(yaml).toContain('tenants:');
    expect(yaml).toContain('db-a:');
    expect(yaml).toContain('_custom_alerts:');
    expect(yaml).toContain('- recipe: threshold');
    expect(yaml).toContain('name: queue_high');
  });

  it('tenant-manager persona (onSubmit): hands off the recipe object, no YAML', () => {
    const onSubmit = vi.fn();
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} onSubmit={onSubmit} />);
    expect(screen.queryByTestId('yaml-output')).toBeNull();

    fill('field-name', 'queue_high');
    fill('field-metric', 'queue_depth');
    fill('field-window', '5m');
    fill('field-threshold', '100:warning');
    fireEvent.click(screen.getByTestId('submit'));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toMatchObject({
      recipe: 'threshold', name: 'queue_high', metric: 'queue_depth',
      window: '5m', threshold: '100:warning',
    });
  });

  it('forecast ratio mode: floor outside (0,1) blocks readiness', () => {
    const onSubmit = vi.fn();
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['avail', 'cap'])} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'forecast' } });
    fill('field-name', 'disk_fill');
    fill('field-metric', 'avail');
    fill('field-capacity_metric', 'cap');
    fill('field-threshold', '5:warning'); // 5 is not in (0,1) → invalid floor
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(true);

    fill('field-threshold', '0.15:warning');
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(false);
  });
});
