import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// The committed recipe-status.json is all-active, so inject a deprecated + an eol
// recipe to exercise the lifecycle UX (ADR-024 §8 / #741 #6 C1). Isolated to this
// file so it never perturbs the main recipe-builder tests.
vi.mock('../src/interactive/tools/_common/data/recipe-status.json', () => ({
  default: { statuses: { rate: 'deprecated', absence: 'eol' } },
}));

import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';

const noFetch = () => Promise.resolve([]);

describe('RecipeBuilder lifecycle UX (ADR-024 §8)', () => {
  it('shows a deprecated warning (still selectable/addable) when a deprecated recipe is picked', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={noFetch} />);
    fireEvent.change(screen.getByTestId('field-recipe'), { target: { value: 'rate' } });
    expect(screen.getByTestId('recipe-deprecated')).toBeInTheDocument();
    expect(screen.queryByTestId('recipe-eol')).toBeNull();
    const opt = screen.getByRole('option', { name: /rate \(deprecated\)/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(false); // deprecated stays addable
  });

  it('disables an eol recipe option in the add-flow (cannot adopt a new eol recipe)', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={noFetch} />);
    const opt = screen.getByRole('option', { name: /absence \(EOL/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
    expect(screen.queryByTestId('recipe-eol')).toBeNull(); // default recipe is active
  });

  it('keeps an existing eol recipe selectable + shows the B2-wide eol banner in the edit-flow', () => {
    render(
      <RecipeBuilder
        tenantId="db-a"
        fetchMetrics={noFetch}
        onSubmit={() => {}}
        initialValue={{ recipe: 'absence', name: 'gone_metric', metric: 'up', window: '5m', threshold: '1:warning' }}
      />,
    );
    // the existing eol recipe's option is NOT disabled, so its params stay editable
    const opt = screen.getByRole('option', { name: /absence \(EOL/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(false);
    // the banner explains B2-wide: save existing, but no new alerts using it
    const banner = screen.getByTestId('recipe-eol');
    expect(banner).toBeInTheDocument();
    expect(banner.textContent || '').toMatch(/save changes to this existing alert/i);
  });
});
