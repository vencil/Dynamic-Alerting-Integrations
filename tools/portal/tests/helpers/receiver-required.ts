/**
 * Receiver field-presence contract per type (#2033, #2137): which fields are
 * required, and which groups need EXACTLY ONE field set (pagerduty
 * service_key / routing_key). A receiver that breaks it makes the routing
 * pipeline WARN-and-skip the whole route.
 *
 * Read from docs/schemas/tenant-config.schema.json, the hub of the three
 * copies of this contract: the Python route generator's RECEIVER_TYPES
 * (scripts/tools/_lib_constants.py) is pinned to it by
 * tests/shared/test_receiver_spec_parity.py, and the Go guard's
 * receiverTypeSpecs by TestReceiverTypeSpecs_MatchSchema. Each copy is
 * compared as data against the schema's JSON, so none is regex-parsed out of
 * another language's source. Read shape, identical in all three readers:
 *   `required` minus "type"                                  → required
 *   `oneOf` whose every branch is {"required": [<one field>]} → one group
 * Any other presence keyword on a receiver definition throws (fail loud,
 * never read as "no constraint").
 */
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
// Literal path on purpose: tests/ops/test_ci_path_filter_coverage.py scans
// Vitest out-of-tree reads to keep ci.yml's `portal` filter complete.
const SCHEMA_PATH = resolve(here, '../../../../docs/schemas/tenant-config.schema.json');

export interface ReceiverSpec { required: string[]; exactlyOneOf: string[][] }

const UNMODELLED = ['anyOf', 'allOf', 'not', 'if', 'dependencies', 'dependentRequired'];

export const RECEIVER_SPECS: Record<string, ReceiverSpec> = (() => {
  const defs = JSON.parse(readFileSync(SCHEMA_PATH, 'utf8')).definitions;
  const out: Record<string, ReceiverSpec> = {};
  for (const { $ref } of defs.receiver.oneOf) {
    const name = String($ref).replace(/^#\/definitions\//, '');
    const def = defs[name];
    if (!def) throw new Error(`receiver $ref ${$ref} does not resolve`);
    const bad = UNMODELLED.filter(k => k in def);
    if (bad.length) throw new Error(`${name} uses ${bad.join(', ')}, which this reader does not model`);
    const exactlyOneOf: string[][] = [];
    if (def.oneOf) {
      exactlyOneOf.push(def.oneOf.map((br: any, i: number) => {
        if (Object.keys(br).join() !== 'required' || br.required.length !== 1) {
          throw new Error(`${name}.oneOf[${i}] is not {"required": [<one field>]}`);
        }
        return br.required[0];
      }));
    }
    out[def.properties.type.const] = {
      required: (def.required ?? []).filter((f: string) => f !== 'type').sort(),
      exactlyOneOf,
    };
  }
  return out;
})();

/** Every `receiver` object (anywhere in the document) with its path. */
export function findReceivers(doc: any, path: string[] = []): Array<{ path: string; receiver: any }> {
  if (!doc || typeof doc !== 'object') return [];
  const out: Array<{ path: string; receiver: any }> = [];
  for (const [k, v] of Object.entries(doc)) {
    if (k === 'receiver' && v && typeof v === 'object') out.push({ path: [...path, k].join('.'), receiver: v });
    out.push(...findReceivers(v, [...path, k]));
  }
  return out;
}

/**
 * Presence problems of this receiver ([] = complete): a missing required
 * field is reported by name; an exactly-one group as `exactly one of a|b`.
 */
export function receiverFieldProblems(receiver: any): string[] {
  const spec = RECEIVER_SPECS[receiver?.type];
  if (!spec) return [`<unknown type ${JSON.stringify(receiver?.type)}>`];
  const out = spec.required.filter(f => !(f in receiver));
  for (const group of spec.exactlyOneOf) {
    if (group.filter(f => f in receiver).length !== 1) out.push(`exactly one of ${group.join('|')}`);
  }
  return out;
}
