---
title: "_common — YAML parser + duration helper"
purpose: |
  Lightweight YAML subset parser tailored for the tenant config shape
  (top-level scalars + one or two levels of nested objects under
  _routing / _metadata). Plus a parseDuration helper that turns
  "30s" / "5m" / "2h" / "1d" into seconds.

  Why a custom parser instead of js-yaml: portal is zero-build /
  Babel-standalone-in-browser; pulling js-yaml would either inflate
  the vendor bundle (~70KB) or require an additional CDN dep that
  air-gapped deployments cannot fetch. The tenant subset we accept
  is small enough to roll by hand; everything more complex (anchors,
  multi-doc, complex flows) belongs server-side.

  Public API:
    window.__parseDuration(str)   parse '30s' / '5m' / '2h' / '1d' to seconds (or null)
    window.__parseYaml(text)      parse tenant YAML to {config, errors}

  Behaviour notes:
    parseYaml hard-rejects > MAX_YAML_SIZE chars and returns errors
    array. UNSAFE_KEYS (__proto__, constructor, prototype) are
    silently dropped to mitigate prototype pollution. Inline values
    in [a, b, c] form parse to a JS array of trimmed strings.
    Indented children of _routing / _metadata flatten one extra
    level — enough for `_routing.webhook_url` etc.

  Closure deps:
    Reads window.__t (i18n thunk), window.__UNSAFE_KEYS,
    window.__MAX_YAML_SIZE. Pulled at call time so test harnesses
    that swap globals between renders see updated values.

  Backward compatibility: portal-shared.jsx re-exports parseDuration
  + parseYaml on window.__portalShared unchanged.
---

function parseDuration(str) {
  if (!str) return null;
  const m = String(str).match(/^(\d+\.?\d*)([smhd])$/);
  if (!m) return null;
  const multi = { s: 1, m: 60, h: 3600, d: 86400 };
  return parseFloat(m[1]) * (multi[m[2]] || 1);
}

function parseYaml(text) {
  const t = window.__t || ((zh, en) => en);
  const UNSAFE_KEYS = window.__UNSAFE_KEYS || new Set(['__proto__', 'constructor', 'prototype']);
  const MAX_YAML_SIZE = window.__MAX_YAML_SIZE || 100000;

  const errors = [];
  if (text.length > MAX_YAML_SIZE) {
    return { config: {}, errors: [t('YAML 超過大小限制（100KB）', 'YAML exceeds size limit (100KB)')] };
  }
  const config = {};
  let currentKey = null;
  let currentObj = null;

  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.replace(/\s+#(?![^"']*["'][^"']*$).*$/, '').trimEnd();
    if (!trimmed || trimmed.trim() === '') continue;

    const lineIndent = line.search(/\S/);
    const content = trimmed.trim();

    const kvMatch = content.match(/^([^:]+?):\s+(.+)$/);
    const objMatch = content.match(/^([^:]+?):\s*$/);

    if (lineIndent === 0 && kvMatch) {
      const key = kvMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }
      config[key] = val;
      currentKey = null;
      currentObj = null;
    } else if (lineIndent === 0 && objMatch) {
      const key = objMatch[1].trim();
      if (UNSAFE_KEYS.has(key)) continue;
      config[key] = {};
      currentKey = key;
      currentObj = config[key];
    } else if (currentKey && lineIndent > 0 && kvMatch) {
      const key = kvMatch[1].trim();
      let val = kvMatch[2].trim();
      if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
      if (val.startsWith("'") && val.endsWith("'")) val = val.slice(1, -1);
      if (val.startsWith('[') && val.endsWith(']')) {
        val = val.slice(1, -1).split(',').map(s => s.trim().replace(/"/g, '').replace(/'/g, ''));
      }

      if (currentKey === '_routing' || currentKey === '_metadata') {
        const depth = Math.floor(lineIndent / 2) - 1;
        if (depth === 0) {
          currentObj[key] = val;
        } else if (depth === 1 && typeof currentObj[Object.keys(currentObj).pop()] === 'object') {
          const parentKey = Object.keys(currentObj).pop();
          if (typeof currentObj[parentKey] === 'object') {
            currentObj[parentKey][key] = val;
          }
        }
      }
    } else if (currentKey && lineIndent > 0 && objMatch) {
      const key = objMatch[1].trim();
      if (currentObj) {
        currentObj[key] = {};
      }
    }
  }
  return { config, errors };
}

window.__parseDuration = parseDuration;
window.__parseYaml = parseYaml;
