import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';

const noFetch = () => Promise.resolve([]);

// A recipe present in the schema enum but WITHOUT a FIELDS_BY_RECIPE entry
// (slo_burn_rate, ADR-031) must never render the generic name/threshold/window
// skeleton — threshold is REJECTED by that recipe and window is ignored, so the
// form could only be a dead end. Add-flow: option disabled + YAML-only tag.
// Edit-flow (existing declaration): option stays selectable, a callout renders
// instead of the wrong required fields.
describe('RecipeBuilder YAML-only recipe gate (ADR-031 slo_burn_rate)', () => {
  it('disables a form-unsupported recipe option in the add-flow and labels it YAML-only', () => {
    render(<RecipeBuilder tenantId="db-a" fetchMetrics={noFetch} />);
    const opt = screen.getByRole('option', { name: /slo_burn_rate \(YAML-only/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(true);
    // the default recipe (threshold) has full form support: no callout, form renders
    expect(screen.queryByTestId('recipe-form-unsupported')).toBeNull();
    expect(screen.getByTestId('field-name')).toBeInTheDocument();
  });

  it('shows the YAML-only callout instead of a wrong form when editing an existing slo declaration', () => {
    render(
      <RecipeBuilder
        tenantId="db-a"
        fetchMetrics={noFetch}
        onSubmit={() => {}}
        initialValue={{
          recipe: 'slo_burn_rate',
          name: 'avail',
          metric: 'err_total',
          denominator_metric: 'req_total',
          objective: '99.9',
        }}
      />,
    );
    // the existing declaration's option is NOT disabled (mirrors the eol B2-wide rule)
    const opt = screen.getByRole('option', { name: /slo_burn_rate \(YAML-only/ }) as HTMLOptionElement;
    expect(opt.disabled).toBe(false);
    expect(screen.getByTestId('recipe-form-unsupported')).toBeInTheDocument();
    // the dead-end skeleton must NOT render: no required fields, no summary, no submit
    expect(screen.queryByTestId('field-name')).toBeNull();
    expect(screen.queryByTestId('summary')).toBeNull();
    expect(screen.queryByTestId('submit')).toBeNull();
  });
});
