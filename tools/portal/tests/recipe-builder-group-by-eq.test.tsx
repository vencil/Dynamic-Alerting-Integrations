/**
 * #3958: the exporter refuses a non-empty group_by with op `==`
 * (pkg/config/custom_alert.go). The form has no group_by field and an edit
 * carries the stored one over (#3920), so offering `==` there produced a
 * record the save refuses for a reason the form does not show. With D9
 * undecided, the handoff's recommendation (a) is taken: while the record keeps
 * a stored group_by, the op select does not offer `==` and says why.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import RecipeBuilder from '../src/interactive/tools/recipe-builder.jsx';
import ENUMS from '../src/interactive/tools/_common/data/recipe-enums.json';
import {
  opOptions, opFor, eqBlockedByGroupBy, allRequiredValid, recipeSummary, initialFormState, buildRecipeObject,
} from '../src/interactive/tools/recipe-builder/engine.js';

const stored = { recipe: 'threshold', name: 'a1', metric: 'm', threshold: '80:warning', window: '5m', group_by: ['pod'] };

describe('engine: no == while the record keeps a stored group_by', () => {
  it('the ticket case: the form never produces a record with both', () => {
    const f = { ...initialFormState(stored), op: '==' };
    expect(opOptions('threshold', stored)).not.toContain('==');
    expect(opOptions('threshold', stored)).toContain('>');
    expect(eqBlockedByGroupBy('threshold', stored)).toBe(true);
    expect(allRequiredValid('threshold', f, stored)).toBe(false);
    expect(recipeSummary('threshold', f, stored)).toBeNull();
    expect(opFor('threshold', '==', stored)).toBe('>');
  });

  it('== stays offered when nothing carries a group_by (the controls)', () => {
    expect(opOptions('threshold')).toContain('==');
    expect(opOptions('threshold', { ...stored, group_by: undefined })).toContain('==');
    const { group_by: _g, ...noGroupBy } = stored;
    expect(opOptions('threshold', noGroupBy)).toContain('==');
    expect(opOptions('threshold', { ...stored, group_by: [] })).toContain('=='); // the exporter's len(...) > 0
    expect(opOptions('threshold', { ...stored, group_by: null })).toContain('==');
    const f = { ...initialFormState(noGroupBy), op: '==' };
    expect(allRequiredValid('threshold', f, noGroupBy)).toBe(true);
    expect(buildRecipeObject('threshold', f, noGroupBy).op).toBe('==');
  });

  it('every eq recipe takes group_by, so a kind change carries the stored one (eqBlockedByGroupBy relies on it)', () => {
    for (const r of ENUMS.eqRecipes) expect((ENUMS.recipeSpecs as any)[r].groupBy, r).toBe(true);
  });

  it('a kind change judges the group_by the new kind would carry', () => {
    // From rate (takes group_by) to threshold: carried over, so no ==.
    const rate = { ...stored, recipe: 'rate' };
    expect(buildRecipeObject('threshold', initialFormState(rate), rate).group_by).toEqual(['pod']);
    expect(opOptions('threshold', rate)).not.toContain('==');
    // To a kind that is not an eq recipe nothing changes.
    expect(opOptions('rate', stored)).toEqual(opOptions('rate'));
  });
});

describe('form: the op select on an edit with a stored group_by', () => {
  const onSubmit = vi.fn();
  const open = (initialValue: object) =>
    render(<RecipeBuilder tenantId="tenant-fixture" fetchMetrics={vi.fn(() => Promise.resolve([]))}
      onSubmit={onSubmit} initialValue={initialValue} />);
  const opValues = () => Array.from((screen.getByTestId('field-op') as HTMLSelectElement).options)
    .filter((o) => !o.disabled).map((o) => o.value);

  it('offers no == and says why, naming the group_by', () => {
    open(stored);
    expect(opValues()).not.toContain('==');
    const note = screen.getByTestId('op-eq-group-by');
    expect(note.textContent).toContain('group_by (["pod"])');
    expect(note.textContent).toContain('does not accept group_by with ==');
  });

  it('a stored record already holding both shows its == as unavailable and cannot be saved until the op changes', () => {
    onSubmit.mockClear();
    open({ ...stored, op: '==' });
    const select = screen.getByTestId('field-op') as HTMLSelectElement;
    expect(select.value).toBe('==');
    expect(within(select).getByRole('option', { name: '==' })).toHaveProperty('disabled', true);
    expect((screen.getByTestId('submit') as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(select, { target: { value: '>=' } });
    fireEvent.click(screen.getByTestId('submit'));
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ op: '>=', group_by: ['pod'] });
  });

  it('without a group_by the select offers == and shows no note (the control)', () => {
    const { group_by: _g, ...noGroupBy } = stored;
    open(noGroupBy);
    expect(opValues()).toContain('==');
    expect(screen.queryByTestId('op-eq-group-by')).toBeNull();
  });
});
