/**
 * Drift guard (#2033): every receiver the portal teaches carries the fields
 * the routing pipeline requires — not just the schema's. A playground
 * template, gallery template or schema-explorer insert with an email
 * receiver lacking `from` (or pagerduty lacking `service_key`) passes the
 * schema but its whole route is WARN-and-skipped by the generator/guard.
 * The required lists are read live from scripts/tools/_lib_constants.py
 * (RECEIVER_TYPES) and the Go guard (receiverTypeSpecs); see the helper.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import { buildYamlTemplates } from '../src/interactive/tools/playground.jsx';
import { SCHEMA, buildInsertYaml } from '../src/interactive/tools/schema-explorer.jsx';
import { RECEIVER_SOURCES, RECEIVER_REQUIRED, findReceivers, missingReceiverFields } from './helpers/receiver-required';

const __dirname = dirname(fileURLToPath(import.meta.url));
const GALLERY = JSON.parse(readFileSync(resolve(__dirname, '../../../docs/assets/template-data.json'), 'utf8'));

describe('required-field sources are read, not empty', () => {
  it('both Python RECEIVER_TYPES and Go receiverTypeSpecs list all six types', () => {
    for (const src of Object.values(RECEIVER_SOURCES)) {
      expect(Object.keys(src).sort()).toEqual(['email', 'pagerduty', 'rocketchat', 'slack', 'teams', 'webhook']);
    }
    // The two fields the schema does not require are the point of this guard.
    expect(RECEIVER_REQUIRED.email).toContain('from');
    expect(RECEIVER_REQUIRED.pagerduty).toContain('service_key');
  });
});

function check(label: string, doc: unknown) {
  const found = findReceivers(doc);
  for (const { path, receiver } of found) {
    expect({ where: `${label}:${path}`, missing: missingReceiverFields(receiver) })
      .toEqual({ where: `${label}:${path}`, missing: [] });
  }
  return found.length;
}

describe('portal receivers carry the pipeline-required fields', () => {
  it('playground templates', () => {
    let n = 0;
    for (const [name, src] of Object.entries(buildYamlTemplates(new Date('2026-09-25T10:00:00Z')))) {
      n += check(`playground/${name}`, load(src));
    }
    expect(n).toBeGreaterThanOrEqual(4);
  });

  it('gallery templates', () => {
    let n = 0;
    for (const tpl of GALLERY.templates) n += check(`gallery/${tpl.id}`, load(tpl.yaml));
    expect(n).toBeGreaterThanOrEqual(17);
  });

  it('schema-explorer inserts', () => {
    let n = 0;
    const walk = (nodes: any[], parent: string[] = []) => nodes.forEach(node => {
      const path = [...parent, node.key];
      if (node.children?.length) walk(node.children, path);
      else if (node.example !== undefined) n += check(`explorer/${path.join('.')}`, load(buildInsertYaml(path, node.example)));
    });
    walk(SCHEMA);
    expect(n).toBeGreaterThanOrEqual(4);
  });

  it('checker positive control: an email receiver without from is flagged', () => {
    expect(missingReceiverFields({ type: 'email', to: ['a@example.com'], smarthost: 'smtp:587' })).toEqual(['from']);
    expect(missingReceiverFields({ type: 'pagerduty', routing_key: 'x' })).toEqual(['service_key']);
  });
});
