/**
 * Required receiver fields per type, as the ROUTING PIPELINE demands them
 * (#2033). The tenant schema only requires e.g. email to+smarthost, but the
 * route generator (scripts/tools/_lib_constants.py RECEIVER_TYPES, used by
 * _grar_merge.build_receiver_config) and the Go guard
 * (components/threshold-exporter/app/internal/guard/routing.go
 * receiverTypeSpecs) also require email `from` and pagerduty `service_key`;
 * a receiver without them makes the whole route WARN-and-skip.
 *
 * Both sources are read live and unioned, so a field either side adds
 * reddens the portal tests. Reading fails loudly (throws), never empty:
 * the Python half runs the real module (stdlib-only) via python3, which the
 * Portal Tests job already has (it runs check_portal_bundle_size.py).
 */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
// Literal paths on purpose: tests/ops/test_ci_path_filter_coverage.py scans
// Vitest out-of-tree reads to keep ci.yml's `portal` filter complete.
const LIB_CONSTANTS = resolve(here, '../../../../scripts/tools/_lib_constants.py');
const GUARD_ROUTING = resolve(here, '../../../../components/threshold-exporter/app/internal/guard/routing.go');

function fromPython(): Record<string, string[]> {
  const out = execFileSync('python3', ['-c', [
    'import json, sys',
    'sys.path.insert(0, sys.argv[1])',
    'from _lib_constants import RECEIVER_TYPES',
    "print(json.dumps({k: v['required'] for k, v in RECEIVER_TYPES.items()}))",
  ].join('\n'), dirname(LIB_CONSTANTS)], { encoding: 'utf8' });
  return JSON.parse(out);
}

function fromGo(): Record<string, string[]> {
  const src = readFileSync(GUARD_ROUTING, 'utf8');
  const block = src.match(/var receiverTypeSpecs = map\[string\]\[\]string\{([\s\S]*?)\n\}/);
  if (!block) throw new Error('receiverTypeSpecs literal not found in routing.go');
  const out: Record<string, string[]> = {};
  for (const m of block[1].matchAll(/"([a-z]+)":\s*\{([^}]*)\}/g)) {
    out[m[1]] = [...m[2].matchAll(/"([a-z_]+)"/g)].map(x => x[1]);
  }
  return out;
}

export const RECEIVER_SOURCES = { python: fromPython(), go: fromGo() };

export const RECEIVER_REQUIRED: Record<string, string[]> = (() => {
  const u: Record<string, Set<string>> = {};
  for (const src of Object.values(RECEIVER_SOURCES)) {
    for (const [type, req] of Object.entries(src)) {
      u[type] ||= new Set();
      req.forEach(f => u[type].add(f));
    }
  }
  return Object.fromEntries(Object.entries(u).map(([k, v]) => [k, [...v].sort()]));
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

/** Fields the pipeline requires that this receiver lacks ([] = complete). */
export function missingReceiverFields(receiver: any): string[] {
  const req = RECEIVER_REQUIRED[receiver?.type];
  if (!req) return [`<unknown type ${JSON.stringify(receiver?.type)}>`];
  return req.filter(f => !(f in receiver));
}
