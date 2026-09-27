---
title: "Config Template Gallery — load-time placeholders"
purpose: |
  docs/assets/template-data.json is static, so a timestamp written into it is
  either already past or silences for decades (the maintenance template once
  shipped `expires: 2099-…`). Templates instead carry `{{expires:+24h}}` /
  `{{expires:+7d}}`, and the gallery resolves them against `now` when the data
  loads, before anything is shown, copied or sent anywhere (#2033).

  Syntax: `{{<name>:<arg>}}`. Only `expires` exists; its arg is `+N` followed
  by `h` (hours) or `d` (days). Any other `{{name:…}}` token, or a malformed
  arg, THROWS — a placeholder is never passed through verbatim. Tokens without
  the `name:` shape (e.g. `{{tenant}}`, Alertmanager `{{ .Labels }}`) are not
  placeholders and are left alone.

  Public API:
    resolvePlaceholders(text, now)        -> text with every placeholder resolved
    resolveTemplateData(data, now)        -> data with every template.yaml resolved
---

import { rfc3339After } from '../tenant-manager/utils/yaml-generators.js';

const TOKEN = /\{\{\s*([A-Za-z_][\w-]*):([^}]*)\}\}/g;
const UNIT_MS = { h: 60 * 60 * 1000, d: 24 * 60 * 60 * 1000 };

function resolvePlaceholders(text, now = new Date()) {
  return text.replace(TOKEN, (whole, name, arg) => {
    if (name !== 'expires') throw new Error(`Unknown template placeholder ${whole}`);
    const m = /^\s*\+(\d+)([hd])\s*$/.exec(arg);
    if (!m) throw new Error(`Malformed template placeholder ${whole} (expected {{expires:+N<h|d>}})`);
    return rfc3339After(now, Number(m[1]) * UNIT_MS[m[2]]);
  });
}

function resolveTemplateData(data, now = new Date()) {
  if (!data || !Array.isArray(data.templates)) return data;
  return {
    ...data,
    templates: data.templates.map(tpl => {
      try {
        return { ...tpl, yaml: resolvePlaceholders(tpl.yaml || '', now) };
      } catch (err) {
        throw new Error(`template "${tpl.id}": ${err.message}`);
      }
    }),
  };
}

export { resolvePlaceholders, resolveTemplateData };
