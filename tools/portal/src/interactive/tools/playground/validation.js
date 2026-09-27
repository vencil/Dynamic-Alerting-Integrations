---
title: "YAML Playground — tenant config syntax + structure check"
purpose: |
  Playground-local parse-layer check for a multi-tenant `tenants:` document.
  Parsing is js-yaml via the shared loadYamlDocument (same schema as the
  rest of the portal: CORE + merge key). js-yaml and the exporter's yaml.v3
  read some edge cases differently, in both directions.

  Scope (#2033, owner decision R′): this module judges only the parse
  layer —
    - YAML syntax errors (incl. duplicate keys, tab indentation), with the
      line/column from the js-yaml mark
    - the document root is a mapping with a `tenants:` key
    - `tenants` is a mapping
    - every tenant value is a mapping or null (a scalar/list tenant body
      is an unmarshal error in the exporter's ThresholdConfig)
  It deliberately makes NO semantic verdict (threshold format, known
  metric keys, receiver types, duration ranges, reserved-key shapes): the
  previous hand-written rules were stricter than, or contrary to, the
  schema and the exporter. Semantics belong to the exporter.

  Public API:
    parseYAML(text)                 -> { success, data } | { success:false, error, line, column }
    validateTenantConfig(yamlText)  -> { valid, errors, summary: { tenants } }

  Closure deps: window.__t for bilingual messages (falls back to English).
---

import { loadYamlDocument, isPlainMap } from '../_common/validation/yaml-parser.js';

const t = window.__t || ((zh, en) => en);

function parseYAML(text) {
  const { doc, error } = loadYamlDocument(text);
  if (error) {
    return { success: false, error: error.message, line: error.line, column: error.column };
  }
  return { success: true, data: doc };
}

function validateTenantConfig(yamlText) {
  const fail = (rule, message) => ({
    valid: false,
    errors: [{ rule, message }],
    summary: { tenants: 0 },
  });

  const parsed = parseYAML(yamlText);
  if (!parsed.success) {
    return fail(t('YAML 語法', 'YAML Syntax'), t(`解析錯誤：${parsed.error}`, `Parse error: ${parsed.error}`));
  }

  const doc = parsed.data;
  if (!isPlainMap(doc) || !Object.prototype.hasOwnProperty.call(doc, 'tenants')) {
    return fail(t('結構', 'Structure'), t('根節點必須是含 "tenants:" 鍵的 mapping', 'Root must be a mapping with a "tenants:" key'));
  }

  const tenants = doc.tenants;
  if (!isPlainMap(tenants)) {
    return fail(t('結構', 'Structure'), t('"tenants" 必須是 mapping（租戶名稱: 設定）', '"tenants" must be a mapping (tenant name: config)'));
  }

  const errors = [];
  const names = Object.keys(tenants);
  for (const name of names) {
    const body = tenants[name];
    if (body !== null && !isPlainMap(body)) {
      errors.push({
        rule: t('租戶結構', 'Tenant Structure'),
        message: t(`租戶 "${name}" 的值必須是 mapping`, `Tenant "${name}" must be a mapping`),
      });
    }
  }

  return {
    valid: errors.length === 0,
    errors,
    summary: { tenants: names.length },
  };
}

export { validateTenantConfig, parseYAML };
