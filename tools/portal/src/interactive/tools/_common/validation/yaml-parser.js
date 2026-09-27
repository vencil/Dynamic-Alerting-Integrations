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

  Schema = js-yaml CORE_SCHEMA + the merge key (`<<`). That is chosen to
  sit closest to yaml.v3 decoding into interface{}: `<<` is expanded (as
  yaml.v3 does), and timestamps stay strings (js-yaml's default schema
  would turn them into Date objects).

  yaml.v3 parity kept on purpose (measured against yaml.v3 v3.0.1, the
  exporter's `yaml.Unmarshal`):
    - multi-document streams: only the FIRST document counts; a trailing
      `---` or a second document (even a malformed one) is ignored; an
      empty stream is null.
    - a self-referencing anchor (`a: &r\n  b: *r`) is an error
      ("anchor 'r' value contains itself") instead of a cyclic object that
      would send every recursive consumer into a stack overflow.
  Known remaining differences: `010` is 10 here (YAML 1.2) but 8 in
  yaml.v3 (`0o10` is 8 in both); `1_000` is the string "1_000" here but
  1000 in yaml.v3.

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
    syntax errors are errors (js-yaml default), same as yaml.v3.
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

// Offset where the first document of a stream ends: the second `---`
// document-start marker (or the first `...` end marker) at column 0.
// Document markers cannot occur inside content, so a line scan is exact
// enough for "where does document 1 stop".
function firstDocumentEnd(src) {
  let started = false;
  let offset = 0;
  for (const line of src.split('\n')) {
    if (/^---(\s|$)/.test(line)) {
      if (started) return offset;
      started = true;
    } else if (/^\.\.\.(\s|$)/.test(line)) {
      return offset;
    } else if (!/^\s*(#.*)?\r?$/.test(line) && !/^%/.test(line)) {
      started = true;
    }
    offset += line.length + 1;
  }
  return src.length;
}

// yaml.Unmarshal decodes the first document only and never looks at the
// rest, so a later document — even a malformed one — must not fail the
// parse. loadAll parses the whole stream; if it throws past the end of
// document 1, re-parse document 1 alone.
function loadFirstDocument(src) {
  const anchorMaps = new Set();
  const opts = {
    schema: YAML_SCHEMA,
    listener(_event, state) { if (state && state.anchorMap) anchorMaps.add(state.anchorMap); },
  };
  let docs;
  try {
    docs = loadAll(src, null, opts);
  } catch (e) {
    const cut = firstDocumentEnd(src);
    const pos = e && e.mark && typeof e.mark.position === 'number' ? e.mark.position : -1;
    if (cut >= src.length || pos < cut) throw e;
    docs = loadAll(src.slice(0, cut), null, opts);
  }
  return { doc: docs.length ? docs[0] : null, anchorMaps };
}

// First object/array that is its own ancestor (a self-referencing alias),
// or null. Shared, non-cyclic aliases (the same anchor used twice) are fine.
// Iterative (explicit stack) so a deeply nested but acyclic document cannot
// overflow the call stack here.
function findCycle(root) {
  const onPath = new Set();
  const stack = [{ node: root, exit: false }];
  while (stack.length) {
    const { node, exit } = stack.pop();
    if (exit) { onPath.delete(node); continue; }
    if (node === null || typeof node !== 'object') continue;
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
function stripUnsafeKeys(v) {
  if (Array.isArray(v)) return v.map(stripUnsafeKeys);
  if (isPlainMap(v)) {
    const out = {};
    for (const k of Object.keys(v)) {
      if (UNSAFE_KEYS.has(k)) continue;
      out[k] = stripUnsafeKeys(v[k]);
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
