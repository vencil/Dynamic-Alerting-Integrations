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

  it('discovery unavailable: a rejected fetch degrades to a non-blocking note (no ghost)', async () => {
    const failing = vi.fn(() => Promise.reject(new Error('discovery HTTP 503')));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={failing} />);
    const input = screen.getByTestId('field-metric');
    fireEvent.change(input, { target: { value: 'anything' } });
    fireEvent.blur(input, { target: { value: 'anything' } });
    // it must NOT raise a hard ghost-warn when discovery itself is down
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByTestId('field-metric-ghost')).toBeNull();
    // and authoring is not blocked: filling the rest still yields YAML
    fill('field-name', 'q'); fill('field-window', '5m'); fill('field-threshold', '1:warning');
    expect(screen.getByTestId('yaml-output').textContent).toContain('metric: anything');
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

  it('severity dropdown is honored in the emitted threshold (not a no-op)', () => {
    const onSubmit = vi.fn();
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} onSubmit={onSubmit} />);
    fill('field-name', 'queue_high');
    fill('field-metric', 'queue_depth');
    fill('field-window', '5m');
    fill('field-threshold', '100');
    fireEvent.change(screen.getByTestId('field-severity'), { target: { value: 'critical' } });
    fireEvent.click(screen.getByTestId('submit'));
    expect(onSubmit.mock.calls[0][0].threshold).toBe('100:critical');
  });

  it('optional capacity_metric: a malformed value blocks readiness (not just required fields)', () => {
    const onSubmit = vi.fn();
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['avail'])} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'forecast' } });
    fill('field-name', 'disk');
    fill('field-metric', 'avail');
    fill('field-threshold', '50'); // raw mode (no capacity) → any number ok
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(false);
    // now a garbage OPTIONAL capacity_metric must block (it would emit bad YAML)
    fill('field-capacity_metric', 'bad!@#');
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('field-capacity_metric-badformat')).toBeInTheDocument();
  });

  it('p99_latency quantile: out-of-range / non-numeric blocks readiness', () => {
    const onSubmit = vi.fn();
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['lat'])} onSubmit={onSubmit} />);
    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'p99_latency' } });
    fill('field-name', 'lat_p99');
    fill('field-metric', 'lat');
    fill('field-window', '5m');
    fill('field-threshold', '2');
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(false); // default quantile 0.99
    fill('field-quantile', '9'); // not in (0,1)
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(true);
    fill('field-quantile', 'abc'); // non-numeric
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(true);
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

describe('RecipeBuilder would-fire preview (#657)', () => {
  // threshold recipe ready = name + metric + window + threshold all valid
  function fillThresholdRecipe() {
    fill('field-name', 'queue_high');
    fill('field-metric', 'queue_depth');
    fill('field-window', '5m');
    fill('field-threshold', '100:warning');
  }

  it('firing verdict: renders the firing badge + backend reason verbatim (dumb handoff)', async () => {
    const previewFetch = vi.fn(() => Promise.resolve({
      supported: true, states: [{ state: 'firing', reason: 'value 1500 > threshold 100' }], warnings: [],
    }));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={previewFetch} />);
    fillThresholdRecipe();
    fill('wouldfire-value', '1500');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-firing')).toBeInTheDocument());
    const txt = screen.getByTestId('wouldfire-firing').textContent || '';
    expect(txt).toMatch(/Would fire/);
    expect(txt).toMatch(/value 1500 > threshold 100/);          // backend reason shown verbatim
    // the recipe object + scenario were handed to the service (never re-derived here)
    expect(previewFetch).toHaveBeenCalledWith(
      'db-a', expect.objectContaining({ recipe: 'threshold', metric: 'queue_depth' }),
      { value: 1500 }, expect.anything());
  });

  it('inactive verdict: renders the neutral "would not fire"', async () => {
    const previewFetch = vi.fn(() => Promise.resolve({ supported: true, states: [{ state: 'inactive' }], warnings: [] }));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={previewFetch} />);
    fillThresholdRecipe();
    fill('wouldfire-value', '5');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-inactive')).toBeInTheDocument());
    expect(screen.getByTestId('wouldfire-inactive').textContent).toMatch(/Would not fire/);
  });

  it('unsupported type: surfaces the backend warning verbatim, never blank/fake-OK (§7)', async () => {
    const previewFetch = vi.fn(() => Promise.resolve({
      supported: false, states: [], warnings: ['preview for rate is coming soon'],
    }));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['http_requests_total'])} previewFetch={previewFetch} />);
    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'rate' } });
    fill('field-name', 'req_rate');
    fill('field-metric', 'http_requests_total');
    fill('field-window', '5m');
    fill('field-threshold', '100:warning');
    fill('wouldfire-value', '50');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-state-unsupported')).toBeInTheDocument());
    expect(screen.getByTestId('wouldfire-state-unsupported').textContent).toMatch(/coming soon/);
    // must NOT claim a firing/inactive verdict for an unsupported type
    expect(screen.queryByTestId('wouldfire-firing')).toBeNull();
    expect(screen.queryByTestId('wouldfire-inactive')).toBeNull();
  });

  it('a failed request surfaces the error with its HTTP status (visible, not blank)', async () => {
    const previewFetch = vi.fn(() => {
      const e = new Error('rate limit exceeded for this tenant') as Error & { status?: number };
      e.status = 429;
      return Promise.reject(e);
    });
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={previewFetch} />);
    fillThresholdRecipe();
    fill('wouldfire-value', '1500');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-state-error')).toBeInTheDocument());
    const txt = screen.getByTestId('wouldfire-state-error').textContent || '';
    expect(txt).toMatch(/429/);
    expect(txt).toMatch(/rate limit exceeded/);
  });

  it('Run is disabled with a reason until tenant + valid recipe + numeric value', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={vi.fn()} />);
    // recipe incomplete → disabled, reason names the missing fields
    expect((screen.getByTestId('wouldfire-run') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('wouldfire-blocker').textContent).toMatch(/required fields/i);
    fillThresholdRecipe();
    // recipe ready but no test value → still disabled, reason updates
    expect((screen.getByTestId('wouldfire-run') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('wouldfire-blocker').textContent).toMatch(/number/i);
    fill('wouldfire-value', '1500');
    expect((screen.getByTestId('wouldfire-run') as HTMLButtonElement).disabled).toBe(false);
  });

  it('shows a loading state while the request is in flight', async () => {
    let resolveIt: (v: unknown) => void = () => {};
    const previewFetch = vi.fn(() => new Promise((r) => { resolveIt = r; }));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={previewFetch} />);
    fillThresholdRecipe();
    fill('wouldfire-value', '1500');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-state-loading')).toBeInTheDocument());
    resolveIt({ supported: true, states: [{ state: 'inactive' }], warnings: [] });
    await waitFor(() => expect(screen.getByTestId('wouldfire-inactive')).toBeInTheDocument());
  });

  it('invalidates a stale verdict when the recipe is edited after a run', async () => {
    const previewFetch = vi.fn(() => Promise.resolve({
      supported: true, states: [{ state: 'firing', reason: 'value 1500 > threshold 100' }], warnings: [],
    }));
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={mockFetch(['queue_depth'])} previewFetch={previewFetch} />);
    fillThresholdRecipe();
    fill('wouldfire-value', '1500');
    fireEvent.click(screen.getByTestId('wouldfire-run'));
    await waitFor(() => expect(screen.getByTestId('wouldfire-firing')).toBeInTheDocument());
    // edit the threshold (raising it above the test value) without re-running:
    // the now-false "firing" verdict must clear — no preview beats a wrong preview
    fill('field-threshold', '2000:warning');
    expect(screen.queryByTestId('wouldfire-firing')).toBeNull();
    expect(screen.getByTestId('wouldfire-run')).not.toBeDisabled();   // recipe still valid → can re-run
  });
});
