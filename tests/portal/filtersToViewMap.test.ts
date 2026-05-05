/**
 * Unit tests for `filtersToViewMap` — TECH-DEBT-030b first-batch.
 *
 * Pure function; no React / DOM. Defined inside SavedViewsPanel.jsx and
 * exported as a sibling of the React component (TD-030b dual-track export).
 * Backend `validateFilters` rejects empty values, so this helper's job is
 * to drop empty-string entries before serialising to the saved-views YAML.
 */
import { describe, it, expect } from 'vitest';
import { filtersToViewMap } from '../../docs/interactive/tools/tenant-manager/components/SavedViewsPanel.jsx';

describe('filtersToViewMap', () => {
  it('returns empty object for all-empty state', () => {
    expect(
      filtersToViewMap({
        q: '',
        environment: '',
        tier: '',
        operational_mode: '',
        domain: '',
        db_type: '',
      }),
    ).toEqual({});
  });

  it('drops empty-string entries (backend rejects empty values)', () => {
    expect(
      filtersToViewMap({
        q: 'production',
        environment: 'production',
        tier: '',
        operational_mode: '',
        domain: 'finance',
        db_type: '',
      }),
    ).toEqual({
      q: 'production',
      environment: 'production',
      domain: 'finance',
    });
  });

  it('trims whitespace from q and drops if empty after trim', () => {
    expect(filtersToViewMap({ q: '   ', environment: 'prod' })).toEqual({
      environment: 'prod',
    });
    expect(filtersToViewMap({ q: '  hello  ', environment: 'prod' })).toEqual({
      q: 'hello',
      environment: 'prod',
    });
  });

  it('handles missing keys without throwing', () => {
    // Caller may pass partial state — function should be defensive.
    expect(() => filtersToViewMap({})).not.toThrow();
    expect(filtersToViewMap({})).toEqual({});
  });
});
