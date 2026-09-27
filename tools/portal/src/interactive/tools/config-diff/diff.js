---
title: "Config Diff — engine (tenant-split parser + diff)"
purpose: |
  Pure config-diff engine, extracted from config-diff.jsx (portal ROI
  wave 2) so the parsing/diff logic can be exercised without React.

  Parsing is the shared `_common/validation/yaml-parser.js` `parseYaml()`
  (js-yaml, #2033) over the WHOLE document; this engine only reads the
  `tenants:` mapping out of it and turns each tenant's values into
  comparable strings.

  Public API:
    extractTenants(yaml)          -> { tenants, errors }
      tenants: { <tenant>: { <key>: <string> } }, one comparable string
               per top-level tenant key via flattenValue: scalars as
               String(v) (null -> ''), scalar lists joined with ", ",
               nested maps / lists of maps as a stable indented
               "key: value" block. Diff by VALUE (not object identity —
               a raw object would always compare unequal and report a
               phantom change). Nested and list-valued keys
               (_custom_alerts, _routing.receiver.*, ...) are fully
               represented, so a change inside them is detected.
               A tenant whose body is not a mapping maps to {}.
      errors:  string[] from parseYaml (syntax errors carry the line)
               plus a top-level size guard on the whole document.
    computeDiff(oldYaml, newYaml) -> { changes, errors }

  Security: parseYaml drops `__proto__` / `constructor` / `prototype`
  keys at every depth (tenant names included), so neither `tenants` nor
  any per-tenant map can gain a prototype-pollution key.

  Closure deps: window.__t (host-page i18n, per-call). MAX_YAML_SIZE is
  ESM-imported from the shared validation constants.
---

import { parseYaml, isPlainMap } from '../_common/validation/yaml-parser.js';
import { MAX_YAML_SIZE } from '../_common/validation/constants.js';

function indentBlock(s) {
  return s.split('\n').map((l) => `  ${l}`).join('\n');
}

// Collapse a parsed value into a single comparable string. Deterministic
// for a given parsed value (key order is document order).
function flattenValue(v) {
  if (v === null || v === undefined) return '';
  if (Array.isArray(v)) {
    if (v.every((x) => x === null || typeof x !== 'object')) {
      return v.map(flattenValue).join(', ');
    }
    return v.map((x) => `-\n${indentBlock(flattenValue(x))}`).join('\n');
  }
  if (typeof v === 'object') {
    return Object.keys(v)
      .map((k) => {
        const inner = flattenValue(v[k]);
        return inner.includes('\n') ? `${k}:\n${indentBlock(inner)}` : `${k}: ${inner}`;
      })
      .join('\n');
  }
  return String(v);
}

// Read the `tenants:` mapping of a multi-tenant YAML document into
// per-tenant { key: comparableString } maps. Returns { tenants, errors }.
function extractTenants(yaml) {
  const t = window.__t || ((zh, en) => en);

  const tenants = {};
  const errors = [];

  if (typeof yaml !== 'string') return { tenants, errors };
  if (yaml.length > MAX_YAML_SIZE) {
    errors.push(t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)'));
    return { tenants, errors };
  }

  const { config, errors: parseErrors } = parseYaml(yaml);
  if (parseErrors && parseErrors.length) errors.push(...parseErrors);

  const root = config.tenants;
  if (!isPlainMap(root)) return { tenants, errors };

  for (const name of Object.keys(root)) {
    const body = root[name];
    const flat = {};
    if (isPlainMap(body)) {
      for (const key of Object.keys(body)) flat[key] = flattenValue(body[key]);
    }
    tenants[name] = flat;
  }

  return { tenants, errors };
}

// Diff two multi-tenant YAML documents into a flat change list.
function computeDiff(oldYaml, newYaml) {
  const oldParsed = extractTenants(oldYaml);
  const newParsed = extractTenants(newYaml);
  const oldTenants = oldParsed.tenants;
  const newTenants = newParsed.tenants;
  const errors = [...new Set([...oldParsed.errors, ...newParsed.errors])];
  const allTenants = new Set([...Object.keys(oldTenants), ...Object.keys(newTenants)]);
  const changes = [];

  allTenants.forEach((tenant) => {
    const oldT = oldTenants[tenant];
    const newT = newTenants[tenant];
    if (!oldT && newT) {
      changes.push({ type: 'tenant-added', tenant, keys: Object.keys(newT) });
      return;
    }
    if (oldT && !newT) {
      changes.push({ type: 'tenant-removed', tenant, keys: Object.keys(oldT) });
      return;
    }
    const allKeys = new Set([...Object.keys(oldT), ...Object.keys(newT)]);
    allKeys.forEach((key) => {
      const oldVal = oldT[key];
      const newVal = newT[key];
      if (oldVal === undefined && newVal !== undefined) {
        changes.push({ type: 'key-added', tenant, key, newVal });
      } else if (oldVal !== undefined && newVal === undefined) {
        changes.push({ type: 'key-removed', tenant, key, oldVal });
      } else if (oldVal !== newVal) {
        changes.push({ type: 'key-changed', tenant, key, oldVal, newVal });
      }
    });
  });

  return { changes, errors };
}

export { extractTenants, computeDiff, flattenValue };
