/**
 * release-notes-generator/changelog.js — changelog parse/filter/summarize.
 *
 * Extracted from release-notes-generator.jsx (PR-portal-21), previously
 * 0%-covered. The test-setup mocks window.__t to return English, so the
 * summary assertions match the English strings.
 */
import { describe, it, expect } from 'vitest';
import {
  parseChangelogMarkdown,
  filterChangesByRole,
  generateAutoSummary,
} from '../src/interactive/tools/release-notes-generator/changelog.js';

const MD = `### Features
- [platform-engineer, sre] New routing dashboard
- [tenant-user] Self-service portal

### Fixes
- [sre] Fixed a memory leak`;

describe('parseChangelogMarkdown', () => {
  it('parses categories and role-tagged items', () => {
    const s = parseChangelogMarkdown(MD);
    expect(Object.keys(s)).toEqual(['Features', 'Fixes']);
    expect(s.Features).toHaveLength(2);
    expect(s.Features[0]).toEqual({ roles: ['platform-engineer', 'sre'], description: 'New routing dashboard' });
    expect(s.Fixes[0]).toEqual({ roles: ['sre'], description: 'Fixed a memory leak' });
  });

  it('ignores non-category headings and unrecognized lines', () => {
    const s = parseChangelogMarkdown('## Random\nsome prose\n### Features\n- [sre] thing');
    expect(Object.keys(s)).toEqual(['Features']);
  });
});

describe('filterChangesByRole', () => {
  const sections = parseChangelogMarkdown(MD);

  it('keeps only items relevant to the selected roles and drops empty categories', () => {
    const f = filterChangesByRole(sections, ['sre']);
    expect(f.Features).toHaveLength(1); // tenant-user item dropped
    expect(f.Features[0].description).toBe('New routing dashboard');
    expect(f.Fixes).toHaveLength(1);
  });

  it('returns no categories when nothing matches', () => {
    expect(filterChangesByRole(sections, ['nobody'])).toEqual({});
  });
});

describe('generateAutoSummary', () => {
  it('reports no relevant changes for an empty result', () => {
    expect(generateAutoSummary({}, ['sre'])).toMatch(/no changes relevant/i);
  });

  it('summarizes features with correct pluralization', () => {
    const one = generateAutoSummary({ Features: [{ roles: ['sre'], description: 'A' }] }, ['sre']);
    expect(one).toContain('1 new feature');
    expect(one).not.toContain('1 new features');
    expect(one).toMatch(/continuously improving/);

    const two = generateAutoSummary(
      { Features: [{ roles: ['sre'], description: 'A' }, { roles: ['sre'], description: 'B' }] },
      ['sre'],
    );
    expect(two).toContain('2 new features');
  });

  it('flags breaking changes as requiring attention', () => {
    const s = generateAutoSummary({ 'Breaking Changes': [{ roles: ['sre'], description: 'X' }] }, ['sre']);
    expect(s).toContain('1 breaking change');
    expect(s).toMatch(/require your attention/);
  });

  it('adds a highlight line for a single selected role', () => {
    const s = generateAutoSummary({ Features: [{ roles: ['sre'], description: 'Cool new thing' }] }, ['sre']);
    expect(s).toMatch(/Highlights include:/);
    expect(s).toContain('Cool new thing');
  });
});
