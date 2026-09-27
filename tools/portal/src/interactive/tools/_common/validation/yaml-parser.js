---
title: "_common — YAML parser + duration helper"
purpose: |
  Tenant-config YAML parsing on top of js-yaml, plus a parseDuration helper
  that turns "30s" / "5m" / "2h" / "1d" into seconds.

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
    parseDuration(str)     parse '30s' / '5m' / '2h' / '1d' to seconds (or null)
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
  + MAX_YAML_SIZE are ESM-imported from ./constants.js.

  Consumers import these directly via ESM (dev-rules §S6).
---

import { loadAll, CORE_SCHEMA, types } from 'js-yaml';
import { UNSAFE_KEYS, MAX_YAML_SIZE } from './constants.js';

const YAML_SCHEMA = CORE_SCHEMA.extend({ implicit: [types.merge] });

function parseDuration(str) {
  if (!str) return null;
  const m = String(str).match(/^(\d+\.?\d*)([smhd])$/);
  if (!m) return null;
  const multi = { s: 1, m: 60, h: 3600, d: 86400 };
  return parseFloat(m[1]) * (multi[m[2]] || 1);
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
