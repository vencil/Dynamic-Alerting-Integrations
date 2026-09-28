/**
 * Drift gate for the facts glossary.jsx states in prose.
 *
 * The glossary is hand-written English, so a number or a list in it rots
 * silently: it said "limit of 500 metrics" as if fixed (it is a default) and
 * pointed "Related" chips at terms it did not define (the chip searches for the
 * term and shows "No terms match"). Each check below reads the source of truth
 * from disk instead of restating it.
 *
 * GLOSSARY is not exported (the jsx-loader forbids named exports), so the
 * source is parsed as text.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, existsSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(__dirname, '../../../');
const read = (rel: string) => readFileSync(resolve(REPO, rel), 'utf8');

const SRC = read('tools/portal/src/interactive/tools/glossary.jsx');
const entries = [...SRC.matchAll(/\{ term: '([^']+)',[^\n]*\n/g)].map(m => m[0]);
const terms = entries.map(e => /term: '([^']+)'/.exec(e)![1]);
const defOf = (term: string) => {
  const e = entries.find(x => x.includes(`term: '${term}'`));
  expect(e, `no glossary entry "${term}"`).toBeTruthy();
  return /def: '((?:[^'\\]|\\.)*)'/.exec(e!)![1];
};

describe('glossary facts', () => {
  it('parses the entries (control: an empty scan would pass everything below)', () => {
    expect(terms.length).toBeGreaterThanOrEqual(20);
  });

  it('every Related chip names a term the glossary defines', () => {
    const known = new Set(terms);
    const dangling = entries.flatMap(e => {
      const rel = /related: \[([^\]]*)\]/.exec(e);
      return rel ? [...rel[1].matchAll(/'([^']+)'/g)].map(m => m[1]).filter(r => !known.has(r)) : [];
    });
    expect(dangling).toEqual([]);
  });

  it('Receiver lists exactly the receiver types the tenant schema accepts', () => {
    const schema = JSON.parse(read('docs/schemas/tenant-config.schema.json'));
    const fromSchema = schema.definitions.receiver.oneOf
      .map((o: { $ref: string }) => schema.definitions[o.$ref.split('/').pop()!].properties.type.const)
      .sort();
    const listed = /Supported types: ([^.]+)\./.exec(defOf('Receiver'))![1].split(', ').sort();
    expect(fromSchema.length).toBeGreaterThan(0);
    expect(listed).toEqual(fromSchema);
  });

  it('Cardinality Guard states the Go default cap', () => {
    const m = /const DefaultMaxMetricsPerTenant = (\d+)/.exec(
      read('components/threshold-exporter/app/pkg/config/types.go'));
    expect(m, 'DefaultMaxMetricsPerTenant not found').toBeTruthy();
    expect(defOf('Cardinality Guard')).toContain(`${m![1]} by default`);
  });

  it('every "Try it" link names a tool that exists', () => {
    const links = [...SRC.matchAll(/component=\.\.\/([\w-]+\.jsx)/g)].map(m => m[1]);
    expect(links.length).toBeGreaterThan(0);
    const missing = links.filter(f => !existsSync(resolve(REPO, 'tools/portal/src/interactive/tools', f)));
    expect(missing).toEqual([]);
  });
});
