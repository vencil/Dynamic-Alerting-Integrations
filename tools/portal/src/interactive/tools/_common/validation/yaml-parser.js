---
title: "_common — YAML parser + duration helper"
purpose: |
  Tenant-config YAML parsing on top of js-yaml, plus a parseDuration helper
  that reads an Alertmanager duration ("30s" / "5m" / "1h30m" / "1d") into
  milliseconds.

  Why js-yaml (#2033): the previous hand-rolled line parser disagreed with
  the exporter's decoder (Go gopkg.in/yaml.v3) — duplicate keys silently
  merged, escaped / complex keys misread, syntax errors swallowed, tab and
  odd indentation accepted. The old reason for rolling our own ("portal is
  zero-build, js-yaml would need a CDN dep") no longer holds: portal tools
  are esbuild bundles and js-yaml lands in a shared chunk.

  Schema = js-yaml CORE_SCHEMA + the merge key (`<<`): `<<` is expanded
  and timestamps stay strings (js-yaml's default schema would turn them
  into Date objects).

  Deliberate choices:
    - Multi-document streams: the value is the FIRST document (a trailing
      `---` is fine; an empty stream is null). Any error anywhere in the
      stream, including a later document, is a parse error.
    - A self-referencing alias (`a: &r\n  b: *r`) is a parse error instead
      of a cyclic object that would send recursive consumers into a stack
      overflow.

  js-yaml is not yaml.v3 (the exporter's decoder): they read some edge
  cases differently, in both directions. This module does not try to
  emulate yaml.v3 and does not claim to list every difference; the
  exporter is the authority.

  Public API:
    parseDuration(str)     milliseconds Alertmanager reads from a routing
                           timing value ('30s', '1h30m', '500ms', '0'), or
                           null when Alertmanager refuses it ('1.5h',
                           '30m1h', '1h1h', '1ns', '', a non-string). The
                           syntax is tenant-config.schema.json
                           definitions.duration (#2490), read from the
                           generated ../data/am-duration.json (#2711;
                           gen_am_duration_json.py, drift-gated). On top of
                           it, the one check a pattern cannot state:
                           model.ParseDuration's int64-nanosecond overflow
                           (about 292y) is refused too, so every value
                           returned is far below 2^53 ms and exact.
    loadYamlDocument(text) -> { doc, error } — first document of a js-yaml
                           loadAll with the schema above + size guard +
                           cycle check. `error` is { message, line, column }
                           (1-based, from the js-yaml mark; null for errors
                           without a position) or null. `doc` keeps a
                           `__proto__` key as an own property (js-yaml
                           defines it, never assigns it) — no pollution;
                           `doc` is guaranteed acyclic.
    parseYaml(text)        -> { config, errors } for single-tenant config
                           (the shape YamlValidatorTab / AlertPreviewTab /
                           RoutingTraceTab edit). `config` is always a plain
                           object; UNSAFE_KEYS (__proto__, constructor,
                           prototype) are dropped at every depth so
                           consumers can assign into it freely. `errors` is
                           a list of strings (syntax errors carry the line).

  Behaviour notes:
    Values keep their YAML types: `80` is a number, `"80"` a string,
    `true` a boolean, block and flow lists are arrays, nested maps are
    objects at any depth. Duplicate keys, tab indentation and other
    syntax errors are errors (js-yaml default).
    A non-mapping document root yields an error and config = {}.

  Closure deps: window.__t (host-page i18n thunk, per-call). UNSAFE_KEYS
  + MAX_YAML_SIZE are ESM-imported from ./constants.js; the duration rule
  from ../data/am-duration.json.

  Consumers import these directly via ESM (dev-rules §S6).
---

import { loadAll, CORE_SCHEMA, types } from 'js-yaml';
import { UNSAFE_KEYS, MAX_YAML_SIZE } from './constants.js';
// #2711: generated from the schema's definitions.duration. Do not write a
// second duration regex here; run `make am-duration-json` after a schema change.
import AM_DURATION_RULE from '../data/am-duration.json';

const YAML_SCHEMA = CORE_SCHEMA.extend({ implicit: [types.merge] });

const AM_DURATION_RE = new RegExp(AM_DURATION_RULE.pattern);
// prometheus/common model.ParseDuration's units (time.go unitMap), in
// nanoseconds. Which units are legal, and in what order, is the schema
// pattern's call; this table only prices them. A unit the pattern admits but
// this table lacks makes the value unreadable (null), never a guess;
// tests/parseDuration.test.ts pins that the table covers the pattern.
const AM_DURATION_UNIT_NS = {
  y: 365n * 86400n * 1000000000n,
  w: 7n * 86400n * 1000000000n,
  d: 86400n * 1000000000n,
  h: 3600n * 1000000000n,
  m: 60n * 1000000000n,
  s: 1000000000n,
  ms: 1000000n,
};
const INT64_MAX = (1n << 63n) - 1n;
const NS_PER_MS = 1000000n;

function parseDuration(str) {
  // Only a string is a duration: Alertmanager reads the YAML text, and an
  // unquoted `30` has no unit (as in the route generator's am_duration_seconds).
  if (typeof str !== 'string') return null;
  if (str.length < AM_DURATION_RULE.minLength || !AM_DURATION_RE.test(str)) return null;
  if (str === '0') return 0;
  // The pattern has already fixed the shape; this only splits the value into
  // <digits><unit> tokens.
  let totalNs = 0n;
  for (const [, digits, unit] of str.matchAll(/([0-9]+)([^0-9]+)/g)) {
    if (!Object.prototype.hasOwnProperty.call(AM_DURATION_UNIT_NS, unit)) return null;
    const mult = AM_DURATION_UNIT_NS[unit];
    const v = BigInt(digits);
    // ParseDuration's overflow check: per unit, then the running sum.
    if (v > (1n << 63n) / mult) return null;
    totalNs += v * mult;
    if (totalNs > INT64_MAX) return null;
  }
  return Number(totalNs / NS_PER_MS);
}

function isPlainMap(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

function loadYamlDocument(text) {
  const t = window.__t || ((zh, en) => en);

  const src = typeof text === 'string' ? text : '';
  if (src.length > MAX_YAML_SIZE) {
    return {
      doc: undefined,
      error: { message: t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)'), line: null, column: null },
    };
  }
  let loaded;
  try {
    loaded = loadFirstDocument(src);
  } catch (e) {
    const mark = e && e.mark;
    const line = mark && typeof mark.line === 'number' ? mark.line + 1 : null;
    const column = mark && typeof mark.column === 'number' ? mark.column + 1 : null;
    const reason = (e && (e.reason || e.message)) || String(e);
    const message = line !== null
      ? t(`第 ${line} 行第 ${column} 欄：${reason}`, `line ${line}, column ${column}: ${reason}`)
      : reason;
    return { doc: undefined, error: { message, line, column } };
  }

  const cyclic = findCycle(loaded.doc);
  if (cyclic) {
    const name = anchorNameOf(cyclic, loaded.anchorMaps);
    const reason = name !== null
      ? t(`anchor '${name}' 的值包含它自己（自我參照）`, `anchor '${name}' value contains itself (self-reference)`)
      : t('anchor 的值包含它自己（自我參照）', 'anchor value contains itself (self-reference)');
    return { doc: undefined, error: { message: reason, line: null, column: null } };
  }
  return { doc: loaded.doc, error: null };
}

// First document of the stream (null when empty). Any error in any
// document throws. The listener collects anchor maps so a cycle can be
// reported by anchor name.
function loadFirstDocument(src) {
  const anchorMaps = new Set();
  const docs = loadAll(src, null, {
    schema: YAML_SCHEMA,
    listener(_event, state) { if (state && state.anchorMap) anchorMaps.add(state.anchorMap); },
  });
  return { doc: docs.length ? docs[0] : null, anchorMaps };
}

// First object/array that is its own ancestor (a self-referencing alias),
// or null. Shared, non-cyclic aliases (the same anchor used twice) are fine
// and each node is expanded once (`done`), so a DAG of shared aliases is
// linear, not exponential. Iterative (explicit stack) so a deeply nested
// but acyclic document cannot overflow the call stack here.
function findCycle(root) {
  const onPath = new Set();
  const done = new Set();
  const stack = [{ node: root, exit: false }];
  while (stack.length) {
    const { node, exit } = stack.pop();
    if (exit) { onPath.delete(node); done.add(node); continue; }
    if (node === null || typeof node !== 'object' || done.has(node)) continue;
    if (onPath.has(node)) return node;
    onPath.add(node);
    stack.push({ node, exit: true });
    const children = Array.isArray(node) ? node : Object.keys(node).map((k) => node[k]);
    for (const c of children) stack.push({ node: c, exit: false });
  }
  return null;
}

function anchorNameOf(node, anchorMaps) {
  for (const map of anchorMaps) {
    for (const name of Object.keys(map)) {
      if (map[name] === node) return name;
    }
  }
  return null;
}

// Copy a parsed value, dropping prototype-pollution keys at every depth.
// `memo` maps each source node to its copy, so a subtree shared through an
// alias is copied once (linear in nodes) and stays shared in the output.
// Input is acyclic (loadYamlDocument guarantees it).
function stripUnsafeKeys(v, memo = new Map()) {
  if (v === null || typeof v !== 'object') return v;
  if (memo.has(v)) return memo.get(v);
  if (Array.isArray(v)) {
    const out = [];
    memo.set(v, out);
    for (const x of v) out.push(stripUnsafeKeys(x, memo));
    return out;
  }
  if (isPlainMap(v)) {
    const out = {};
    memo.set(v, out);
    for (const k of Object.keys(v)) {
      if (UNSAFE_KEYS.has(k)) continue;
      out[k] = stripUnsafeKeys(v[k], memo);
    }
    return out;
  }
  return v;
}

function parseYaml(text) {
  const t = window.__t || ((zh, en) => en);

  const { doc, error } = loadYamlDocument(text);
  if (error) return { config: {}, errors: [error.message] };
  if (doc === undefined || doc === null) return { config: {}, errors: [] };
  if (!isPlainMap(doc)) {
    return { config: {}, errors: [t('YAML 根節點必須是 mapping（key: value）', 'YAML root must be a mapping (key: value)')] };
  }
  return { config: stripUnsafeKeys(doc), errors: [] };
}

export { parseDuration, parseYaml, loadYamlDocument, isPlainMap };
