---
title: "Config Diff — engine (tenant-split parser + diff)"
purpose: |
  Pure config-diff engine, extracted from config-diff.jsx (portal ROI
  wave 2) so the parsing/diff logic can be exercised without React.

  Previously config-diff.jsx carried its own hand-rolled YAML mini-parser
  (`extractTenants`) that lacked the prototype-pollution guard and size
  guard the shared parser already has. This engine converges onto the
  shared `_common/validation/yaml-parser.js` `parseYaml()` and keeps only
  the one thing that parser does not do: split a multi-tenant `tenants:`
  block into per-tenant configs.

  Public API:
    extractTenants(yaml)          -> { tenants, errors }
      tenants: { <tenant>: { <key>: <stringValue> } }, one comparable
               string per top-level key. HYBRID value strategy:
               - scalars + _routing / _metadata → flattenValue(parseYaml),
                 i.e. quote-stripped, nested blocks flattened. Diff by
                 VALUE (not object identity — a raw object would always
                 compare unequal and report a phantom change).
               - every OTHER key whose parseYaml value flattens to '' but
                 that has an indented raw body (list-valued _custom_alerts,
                 or the _routing_defaults / _domain_policy / _instance_mapping
                 / _namespaces reserved blocks) → fall back to the key's RAW
                 dedented child text. parseYaml only nests the exact strings
                 _routing / _metadata (its :97), so without this fallback a
                 change inside any other nested/list block would be silently
                 undetected — the core blast-radius diff this tool exists for.
      errors:  string[] surfaced from parseYaml plus a top-level size
               guard on the whole document.
    computeDiff(oldYaml, newYaml) -> { changes, errors }

  Security: tenant NAMES are assigned by this wrapper (not by parseYaml),
  so `__proto__` / `constructor` / `prototype` tenant ids are dropped here
  with the same UNSAFE_KEYS set; per-tenant key-level pollution + the size
  guard are inherited from parseYaml. The raw-child fallback stores child
  text as an opaque STRING only — it is never re-parsed into object keys,
  so it introduces no new pollution vector; its own top-level key is also
  UNSAFE_KEYS-guarded before use.

  Closure deps (all read at call time with `|| fallback`, dev-rules §S6):
    window.__t, window.__UNSAFE_KEYS, window.__MAX_YAML_SIZE.
---

import { parseYaml } from '../_common/validation/yaml-parser.js';

// Collapse a parsed value into a single comparable string. Scalars pass
// through; inline arrays join with ", "; nested objects (_routing /
// _metadata) serialize to a stable "key: value" multi-line block so the
// diff detects change-by-value instead of object identity.
function flattenValue(v) {
  if (Array.isArray(v)) return v.join(', ');
  if (v && typeof v === 'object') {
    return Object.keys(v)
      .map((k) => `${k}: ${flattenValue(v[k])}`)
      .join('\n');
  }
  return v;
}

// Collect the RAW indented body of each top-level key in an already-dedented
// tenant block, as an opaque string (never re-parsed). This is the fallback
// for nested/list keys parseYaml does not model (anything that is not the
// literal _routing / _metadata) so a change inside e.g. _custom_alerts is
// still detected. UNSAFE_KEYS top-level keys are skipped so the returned map
// can never carry a prototype-pollution key.
function collectRawChildBlocks(dedentedLines, UNSAFE_KEYS) {
  const raw = {};
  let curKey = null;
  for (const line of dedentedLines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const indent = line.search(/\S/);
    if (indent === 0) {
      const m = trimmed.match(/^([^:]+?):/);
      curKey = m && !UNSAFE_KEYS.has(m[1].trim()) ? m[1].trim() : null;
    } else if (indent > 0 && curKey) {
      if (!raw[curKey]) raw[curKey] = [];
      raw[curKey].push(line);
    }
  }
  const out = {};
  for (const k of Object.keys(raw)) out[k] = raw[k].join('\n');
  return out;
}

// Split a `tenants:` YAML block into per-tenant configs, delegating each
// tenant's (dedented) body to the shared parseYaml so the pollution + size
// guards are inherited. Returns { tenants, errors }.
function extractTenants(yaml) {
  const t = window.__t || ((zh, en) => en);
  const UNSAFE_KEYS = window.__UNSAFE_KEYS || new Set(['__proto__', 'constructor', 'prototype']);
  const MAX_YAML_SIZE = window.__MAX_YAML_SIZE || 100000;

  const tenants = {};
  const errors = [];

  if (typeof yaml !== 'string') return { tenants, errors };
  if (yaml.length > MAX_YAML_SIZE) {
    errors.push(t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)'));
    return { tenants, errors };
  }

  // Tenant-splitting layer: collect each tenant's body (indent >= 4),
  // dedented by 4 so its keys sit at indent 0 and _routing sub-keys at
  // indent 2 — exactly the shape parseYaml expects.
  const blocks = {};
  const order = [];
  let currentTenant = null;
  for (const line of yaml.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const indent = line.search(/\S/);
    if (indent === 0) {
      // Root wrapper (`tenants:`) or stray top-level line — not a tenant.
      currentTenant = null;
      continue;
    }
    if (indent === 2 && trimmed.endsWith(':') && !trimmed.includes(': ')) {
      const name = trimmed.slice(0, -1).trim();
      if (UNSAFE_KEYS.has(name)) {
        currentTenant = null; // drop prototype-pollution tenant ids
        continue;
      }
      currentTenant = name;
      if (!blocks[name]) {
        blocks[name] = [];
        order.push(name);
      }
      continue;
    }
    if (indent >= 4 && currentTenant) {
      blocks[currentTenant].push(line.slice(4));
    }
  }

  for (const name of order) {
    const dedented = blocks[name];
    const { config, errors: parseErrors } = parseYaml(dedented.join('\n'));
    if (parseErrors && parseErrors.length) errors.push(...parseErrors);
    const rawChild = collectRawChildBlocks(dedented, UNSAFE_KEYS);
    const flat = {};
    // Union of parsed keys + keys that only surface via a raw body. Both
    // sources are UNSAFE_KEYS-guarded already; the extra guard here is
    // belt-and-suspenders so `flat` can never gain a pollution key.
    for (const key of new Set([...Object.keys(config), ...Object.keys(rawChild)])) {
      if (UNSAFE_KEYS.has(key)) continue;
      const parsed = Object.prototype.hasOwnProperty.call(config, key)
        ? flattenValue(config[key])
        : undefined;
      // Non-empty parsed value wins (scalars + _routing/_metadata); otherwise
      // fall back to the raw child text (nested/list keys parseYaml drops).
      if (parsed !== undefined && parsed !== '') {
        flat[key] = parsed;
      } else if (Object.prototype.hasOwnProperty.call(rawChild, key)) {
        flat[key] = rawChild[key];
      } else {
        flat[key] = parsed !== undefined ? parsed : '';
      }
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
