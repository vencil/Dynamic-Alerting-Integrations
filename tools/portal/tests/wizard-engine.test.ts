/**
 * Getting Started Wizard — engine unit tests.
 *
 * Directly characterizes the pure helpers extracted from wizard.jsx into
 * wizard/engine.js. Before the split these were exercised only INDIRECTLY,
 * through the .tsx component test's happy paths — this file pins the
 * branch/edge behaviour that test never asserts: docUrl's three path shapes,
 * the path set-diff columns, the hash (de)serialization round-trip, and the
 * null-safe key derivation.
 *
 * jsdom env (window.location/history) comes from vitest.config.ts.
 */
import { describe, it, expect, beforeEach } from 'vitest';
import {
  REPO_BASE,
  docUrl,
  pathsForRole,
  diffDocPaths,
  readHash,
  writeHash,
  recommendationKeyFor,
} from '../src/getting-started/wizard/engine.js';

describe('docUrl — three path shapes resolve to the right GitHub blob URL', () => {
  it('../rule-packs/* drops the ../ and is NOT prefixed with docs/', () => {
    expect(docUrl('../rule-packs/README.md')).toBe(`${REPO_BASE}/rule-packs/README.md`);
  });
  it('../* (non-rule-packs) maps one level up under docs/', () => {
    expect(docUrl('../architecture-and-design.md')).toBe(`${REPO_BASE}/docs/architecture-and-design.md`);
  });
  it('a bare relative path resolves under docs/getting-started/', () => {
    expect(docUrl('for-tenants.md')).toBe(`${REPO_BASE}/docs/getting-started/for-tenants.md`);
  });
});

describe('pathsForRole — filters RECOMMENDATIONS by the "<role>-" key prefix', () => {
  const recs = {
    'platform-setup': { title: 'Platform Initial Setup' },
    'platform-migration': { title: 'Migration from Legacy Systems' },
    'tenant-onboard': { title: 'Getting Your Team Onboarded' },
    'domain-redis': { title: 'Redis Threshold Configuration' },
  };
  it('returns only same-role entries, each {key,label}', () => {
    expect(pathsForRole('platform', recs)).toEqual([
      { key: 'platform-setup', label: 'Platform Initial Setup' },
      { key: 'platform-migration', label: 'Migration from Legacy Systems' },
    ]);
  });
  it('no role → empty list (never leaks other roles)', () => {
    expect(pathsForRole(null, recs)).toEqual([]);
    expect(pathsForRole('', recs)).toEqual([]);
  });
  it('a role with no matching keys → empty', () => {
    expect(pathsForRole('nonexistent', recs)).toEqual([]);
  });
});

describe('diffDocPaths — set-diff of two docs lists by path', () => {
  const a = { docs: [{ path: 'x.md', name: 'X' }, { path: 'y.md', name: 'Y' }] };
  const b = { docs: [{ path: 'y.md', name: 'Y' }, { path: 'z.md', name: 'Z' }] };
  it('splits into shared / onlyA / onlyB by doc.path', () => {
    const { shared, onlyA, onlyB } = diffDocPaths(a, b);
    expect(shared.map(d => d.path)).toEqual(['y.md']);
    expect(onlyA.map(d => d.path)).toEqual(['x.md']);
    expect(onlyB.map(d => d.path)).toEqual(['z.md']);
  });
  it('no compare rec → everything is onlyA; shared and onlyB empty', () => {
    const { shared, onlyA, onlyB } = diffDocPaths(a, null);
    expect(shared).toEqual([]);
    expect(onlyA.map(d => d.path)).toEqual(['x.md', 'y.md']);
    expect(onlyB).toEqual([]);
  });
});

describe('readHash / writeHash — round-trip through window.location.hash', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', '/getting-started/');
  });
  it('parses role / option / read from the hash', () => {
    window.history.replaceState(null, '', '/getting-started/#role=tenant&option=routing&read=a.md,b.md');
    const s = readHash();
    expect(s.role).toBe('tenant');
    expect(s.option).toBe('routing');
    expect([...s.readDocs].sort()).toEqual(['a.md', 'b.md']);
  });
  it('absent params → nulls + empty readDocs set', () => {
    const s = readHash();
    expect(s.role).toBeNull();
    expect(s.option).toBeNull();
    expect(s.readDocs.size).toBe(0);
  });
  it('writeHash serializes role + option + read', () => {
    writeHash('tenant', 'routing', new Set(['a.md']));
    expect(window.location.hash).toBe('#role=tenant&option=routing&read=a.md');
  });
  it('writeHash with no state clears the hash', () => {
    window.history.replaceState(null, '', '/getting-started/#role=tenant');
    writeHash(null, null, null);
    expect(window.location.hash).toBe('');
  });
  it('round-trips: writeHash then readHash yields the same state', () => {
    writeHash('platform', 'setup', new Set(['g.md']));
    const s = readHash();
    expect(s.role).toBe('platform');
    expect(s.option).toBe('setup');
    expect([...s.readDocs]).toEqual(['g.md']);
  });
});

describe('recommendationKeyFor — "<role>-<option>" key, null-safe', () => {
  it('builds the composite key', () => {
    expect(recommendationKeyFor('tenant', 'routing')).toBe('tenant-routing');
    expect(recommendationKeyFor('platform', 'setup')).toBe('platform-setup');
  });
  it('returns null if either part is missing', () => {
    expect(recommendationKeyFor(null, 'routing')).toBeNull();
    expect(recommendationKeyFor('tenant', null)).toBeNull();
    expect(recommendationKeyFor(null, null)).toBeNull();
  });
});
