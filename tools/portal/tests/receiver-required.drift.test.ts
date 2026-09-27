/**
 * Drift guard (#2033, #2137): every receiver the portal teaches satisfies the
 * routing pipeline's field-presence contract. A playground template, gallery
 * template or schema-explorer insert with an email receiver lacking `from`, or
 * a pagerduty receiver with neither (or both) of service_key / routing_key,
 * has its whole route WARN-and-skipped by the generator/guard. The contract
 * is read from tenant-config.schema.json, to which the Python RECEIVER_TYPES
 * and the Go receiverTypeSpecs are each pinned by their own parity test; see
 * the helper.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
// js-yaml is the oracle (#2033), not a portal tool's own parser.
import { load } from 'js-yaml';
import { buildYamlTemplates } from '../src/interactive/tools/playground.jsx';
import { SCHEMA, buildInsertYaml } from '../src/interactive/tools/schema-explorer.jsx';
import { resolveTemplateData } from '../src/interactive/tools/template-gallery/placeholders.js';
import { RECEIVER_SPECS, findReceivers, receiverFieldProblems } from './helpers/receiver-required';

const __dirname = dirname(fileURLToPath(import.meta.url));
// As the gallery serves it: load-time placeholders resolved with a fixed now.
const GALLERY = resolveTemplateData(
  JSON.parse(readFileSync(resolve(__dirname, '../../../docs/assets/template-data.json'), 'utf8')),
  new Date('2026-09-25T10:00:00Z'),
);

describe('the receiver contract is read, not empty', () => {
  it('the schema defines all six types, incl. email from and the pagerduty key group', () => {
    expect(Object.keys(RECEIVER_SPECS).sort()).toEqual(['email', 'pagerduty', 'rocketchat', 'slack', 'teams', 'webhook']);
    expect(RECEIVER_SPECS.email.required).toContain('from');
    expect(RECEIVER_SPECS.pagerduty).toEqual({ required: [], exactlyOneOf: [['service_key', 'routing_key']] });
  });
});

function check(label: string, doc: unknown) {
  const found = findReceivers(doc);
  for (const { path, receiver } of found) {
    expect({ where: `${label}:${path}`, missing: receiverFieldProblems(receiver) })
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

  it('checker positive control: missing from, and a pagerduty key group with zero or two keys, are flagged', () => {
    expect(receiverFieldProblems({ type: 'email', to: ['a@example.com'], smarthost: 'smtp:587' })).toEqual(['from']);
    expect(receiverFieldProblems({ type: 'pagerduty' })).toEqual(['exactly one of service_key|routing_key']);
    expect(receiverFieldProblems({ type: 'pagerduty', service_key: 'k', routing_key: 'r' }))
      .toEqual(['exactly one of service_key|routing_key']);
    expect(receiverFieldProblems({ type: 'pagerduty', routing_key: 'r' })).toEqual([]);
    expect(receiverFieldProblems({ type: 'pagerduty', service_key: 'k' })).toEqual([]);
  });
});
