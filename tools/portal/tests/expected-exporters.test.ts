/**
 * `_expected_exporters` entries (#4097): an exporter kind or `kind@cluster`.
 * The card shows an entry as written; the filter and its options go BY KIND
 * (the part before `@`) — the rule the tenant-api search filter has
 * (components/tenant-api/internal/handler/tenant_search.go declaresExporter),
 * so the portal's offline filter and the server's agree.
 */
import { describe, it, expect } from 'vitest';
import {
  expectedExportersOf,
  expectedExporterKind,
  expectedExporterKindsOf,
  declaresExporter,
} from '../src/interactive/tools/tenant-manager/utils/expected-exporters.js';

describe('expectedExportersOf', () => {
  it('keeps entries as written, per-cluster ones included', () => {
    expect(expectedExportersOf(['mariadb@east', 'mariadb@west', 'redis'])).toEqual([
      'mariadb@east', 'mariadb@west', 'redis',
    ]);
  });
  it('reads a lone string as one entry and anything else as none', () => {
    expect(expectedExportersOf('mariadb@east')).toEqual(['mariadb@east']);
    expect(expectedExportersOf(undefined)).toEqual([]);
    expect(expectedExportersOf(['', 5, null, 'redis'])).toEqual(['redis']);
  });
});

describe('expectedExporterKind', () => {
  it.each([
    ['mariadb@east', 'mariadb'],
    ['redis', 'redis'],
    ['@east', ''],
    ['a@b@c', 'a'],
  ])('%s -> %s', (entry, kind) => {
    expect(expectedExporterKind(entry)).toBe(kind);
  });
});

describe('expectedExporterKindsOf', () => {
  it('names each kind once, in the order first written', () => {
    expect(expectedExporterKindsOf(['mariadb@east', 'redis', 'mariadb@west'])).toEqual(['mariadb', 'redis']);
    expect(expectedExporterKindsOf(['nginx'])).toEqual(['nginx']);
    expect(expectedExporterKindsOf(undefined)).toEqual([]);
  });
});

describe('declaresExporter', () => {
  const both = ['mariadb@east', 'mariadb@west', 'redis'];
  it('a kind matches by kind, however the kind is declared', () => {
    expect(declaresExporter(both, 'mariadb')).toBe(true);
    expect(declaresExporter(both, 'redis')).toBe(true);
    expect(declaresExporter(['mariadb'], 'mariadb')).toBe(true);
  });
  it('is exact, never a substring or a cluster name', () => {
    expect(declaresExporter(both, 'maria')).toBe(false);
    expect(declaresExporter(both, 'east')).toBe(false);
    expect(declaresExporter(both, 'kafka')).toBe(false);
  });
  it('a value with @ is a whole entry', () => {
    expect(declaresExporter(both, 'mariadb@east')).toBe(true);
    expect(declaresExporter(both, 'mariadb@south')).toBe(false);
    expect(declaresExporter(both, 'redis@east')).toBe(false);
    // the bare kind says nothing about that cluster
    expect(declaresExporter(['mariadb'], 'mariadb@east')).toBe(false);
    expect(declaresExporter(both, 'mariadb@')).toBe(false);
  });
  it('nothing declared matches nothing', () => {
    expect(declaresExporter([], 'mariadb')).toBe(false);
    expect(declaresExporter(undefined, 'mariadb')).toBe(false);
  });
});
