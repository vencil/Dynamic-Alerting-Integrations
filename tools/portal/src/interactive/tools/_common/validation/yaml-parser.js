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
  would turn them into Date objects). Known remaining difference: a
  leading-zero integer such as `010` is 10 here (YAML 1.2) but 8 in
  yaml.v3; `0o10` is 8 in both.

  Public API:
    parseDuration(str)     parse '30s' / '5m' / '2h' / '1d' to seconds (or null)
    loadYamlDocument(text) -> { doc, error } — raw js-yaml load with the
                           schema above + size guard. `error` is
                           { message, line, column } (1-based, from the
                           js-yaml mark) or null. `doc` keeps a
                           `__proto__` key as an own property (js-yaml
                           defines it, never assigns it) — no pollution.
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

import { load, CORE_SCHEMA, types } from 'js-yaml';
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
  try {
    return { doc: load(src, { schema: YAML_SCHEMA }), error: null };
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
