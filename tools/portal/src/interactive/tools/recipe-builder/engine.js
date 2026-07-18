---
title: "Custom Alert Recipe Builder — pure recipe/validation/YAML engine"
purpose: |
  Pure functions extracted from recipe-builder.jsx (ADR-024 §S6b, #741):
  recipe lifecycle status, field-layout model, per-field + whole-recipe
  validation, plain-English summary, and the `_custom_alerts` recipe object
  + copy-paste YAML snippet (the "Dumb Handoff" payload).

  Pre-this-split these ~14 functions were inline at the top of
  recipe-builder.jsx (~200 LOC of the 720-LOC monolith), covered only via
  the slower .tsx lifecycle/UI tests. Splitting matches the
  cost-estimator/calc.js pattern and lets the recipe/validation/YAML logic
  be unit-tested directly. Behaviour is preserved verbatim — the functions
  are moved, not rewritten.

  All enums derive from recipe-enums.json (schema-extracted; a Vitest
  drift-guard keeps it honest) — NO hardcoded enums here. `recipeSummary`
  and `yamlSnippet` read window.__t for their user-facing strings (same
  live-global-with-fallback idiom as the rest of the portal).

  Public API:
    recipeStatus(r)                 ADR-024 §8 lifecycle status (advisory)
    formSupported(recipe)           does the recipe have a form (vs YAML-only)
    isValidName/Metric/Window(s)    per-field structural validators
    parseThresholdValue(s)          leading numeric of a "value[:sev]" string
    composeThreshold(f)             fold value + severity → "value:severity"
    requiredFields(recipe)          required-field list for the state gate
    isFieldValid(field, value)      per-field validity
    allRequiredValid(recipe, f)     summary/submit gate
    recipeSummary(recipe, f)        plain-English summary (null until valid)
    buildRecipeObject(recipe, f)    the RecipeObject handoff payload
    yamlValue(v)                    quote YAML-ambiguous scalars
    yamlSnippet(tenantId, obj)      copy-paste _custom_alerts YAML block
    FIELDS_BY_RECIPE / METRIC_FIELDS  render-order field model
---

import ENUMS from '../_common/data/recipe-enums.json';
import RECIPE_STATUS from '../_common/data/recipe-status.json';

/* ── i18n helper ───────────────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* Recipe lifecycle status (ADR-024 §8), derived from shape.py RECIPE_STATUS via
 * recipe-status.json. Unknown → active. This is ADVISORY UX only: the tenant-api
 * writer is the eol-expansion authority (B2-wide). */
function recipeStatus(r) {
  return (RECIPE_STATUS.statuses && RECIPE_STATUS.statuses[r]) || 'active';
}

/* Per-recipe field LAYOUT map (UI concern, not the validation authority;
 * the schema's if/then only covers horizon-vs-window). Order = render
 * order. The metric-typed fields each get their own autocomplete.
 *
 * DEFERRED (S6b-1 scope): `selectors` / `selectors_re` (label filters) are
 * NOT yet surfaced — the MVP authors whole-metric recipes. Recipes that
 * commonly want a label filter (rate/ratio status=~"5..", etc.) still work
 * on the bare metric; a dynamic key→value selectors editor is a fast-follow
 * (the schema + compiler already accept them). */
const FIELDS_BY_RECIPE = {
  threshold: ['metric', 'op', 'window', 'threshold', 'severity', 'mode', 'for'],
  rate: ['metric', 'op', 'window', 'threshold', 'severity', 'mode', 'for'],
  ratio: ['metric', 'denominator_metric', 'op', 'window', 'threshold', 'severity', 'mode', 'for'],
  absence: ['metric', 'window', 'threshold', 'severity', 'mode', 'for'],
  p99_latency: ['metric', 'quantile', 'op', 'window', 'threshold', 'severity', 'mode', 'for'],
  forecast: ['metric', 'capacity_metric', 'op', 'horizon', 'threshold', 'severity', 'mode', 'for'],
};

const METRIC_FIELDS = ['metric', 'denominator_metric', 'capacity_metric'];

/* A recipe in the schema enum but WITHOUT a FIELDS_BY_RECIPE entry (e.g.
 * slo_burn_rate, ADR-031) has no form support yet. Rendering the generic
 * name/threshold/window skeleton for it would be a dead end — threshold is
 * REJECTED by that recipe and window is ignored, so `ready` could never go
 * true (or worse, would emit an invalid recipe). Instead the builder disables
 * the option for NEW alerts and shows a YAML-only callout (an existing
 * declaration loaded via initialValue keeps its option selectable so the
 * callout — not a wrong form — renders). */
function formSupported(recipe) {
  return Object.prototype.hasOwnProperty.call(FIELDS_BY_RECIPE, recipe);
}

const NAME_RE = new RegExp(ENUMS.patterns.name);
const METRIC_RE = new RegExp(ENUMS.patterns.metric);
const WINDOW_RE = new RegExp(ENUMS.patterns.window);

/* ── validation (pure UX fast-feedback; the S5 Go preflight + CI are the
 * authority — this never re-implements the cross-language contract) ─── */
function isValidName(s) { return typeof s === 'string' && NAME_RE.test(s); }
function isValidMetric(s) { return typeof s === 'string' && METRIC_RE.test(s); }
function isValidWindow(s) { return typeof s === 'string' && WINDOW_RE.test(s); }

function parseThresholdValue(s) {
  if (typeof s !== 'string' || s.trim() === '') return NaN;
  return Number(s.split(':')[0].trim());
}

/* The threshold field holds the VALUE only; the severity dropdown is the
 * single source of severity (no dual-entry). composeThreshold folds them
 * back into the schema's `value:severity` string for the emitted recipe +
 * summary, so the dropdown is never a no-op. A stray ':sev' typed into the
 * value field is stripped (the dropdown wins). */
function composeThreshold(f) {
  const value = (f.threshold || '').split(':')[0].trim();
  const sev = f.severity || ENUMS.severityDefault;
  return `${value}:${sev}`;
}

function requiredFields(recipe) {
  const base = ['name', 'metric', 'threshold'];
  if (recipe === 'forecast') base.push('horizon');
  else base.push('window');
  if (recipe === 'ratio') base.push('denominator_metric');
  return base;
}

function isFieldValid(field, value) {
  switch (field) {
    case 'name': return isValidName(value);
    case 'metric':
    case 'denominator_metric':
    case 'capacity_metric': return isValidMetric(value);
    case 'window': return isValidWindow(value);
    case 'horizon': return ENUMS.horizon.includes(value);
    case 'quantile': { const n = Number(value); return Number.isFinite(n) && n > 0 && n < 1; }
    case 'threshold': return !Number.isNaN(parseThresholdValue(value));
    default: return true;
  }
}

/* allRequiredValid — the state-machine gate for the summary + submit.
 * False until every required field is present and structurally valid, so
 * the plain-English summary never renders [undefined] (review Reef 4). */
function allRequiredValid(recipe, f) {
  if (!formSupported(recipe)) return false; // YAML-only recipe: never "ready"
  const required = requiredFields(recipe);
  // Required fields: must be present AND valid.
  for (const field of required) {
    if (!f[field] || !isFieldValid(field, f[field])) return false;
  }
  // Optional fields shown on THIS recipe's form: if filled, must also be
  // valid — else a garbage capacity_metric / quantile would slip through to
  // a YAML the S5 Go preflight then rejects, breaking the "validate up
  // front" promise. (name is checked via `required` above; it is rendered
  // outside FIELDS_BY_RECIPE, so iterating only the map would skip it.)
  for (const field of (FIELDS_BY_RECIPE[recipe] || [])) {
    if (!required.includes(field) && f[field] && !isFieldValid(field, f[field])) return false;
  }
  // forecast ratio mode: capacity_metric set → floor ∈ (0,1).
  if (recipe === 'forecast' && f.capacity_metric) {
    const floor = parseThresholdValue(f.threshold);
    if (!(floor > 0 && floor < 1)) return false;
  }
  return true;
}

/* ── plain-English summary (dynamic, ZH-primary; NEVER exposes PromQL /
 * series / labels). null until allRequiredValid. ───────────────────── */
function recipeSummary(recipe, f) {
  if (!allRequiredValid(recipe, f)) return null;
  const value = (f.threshold || '').split(':')[0].trim();
  const sev = f.severity || ENUMS.severityDefault; // dropdown is the SoT
  const op = f.op || ENUMS.opDefault;
  switch (recipe) {
    case 'threshold':
      return t(`當 ${f.metric} 在 ${f.window} 內 ${op} ${value} 時，觸發 ${sev} 告警`,
        `fires ${sev} when ${f.metric} ${op} ${value} over ${f.window}`);
    case 'rate':
      return t(`當 ${f.metric} 的每秒速率在 ${f.window} 內 ${op} ${value} 時，觸發 ${sev} 告警`,
        `fires ${sev} when the per-second rate of ${f.metric} ${op} ${value} over ${f.window}`);
    case 'ratio':
      return t(`當 ${f.metric} / ${f.denominator_metric} 的比例在 ${f.window} 內 ${op} ${value} 時，觸發 ${sev} 告警`,
        `fires ${sev} when ${f.metric} / ${f.denominator_metric} ${op} ${value} over ${f.window}`);
    case 'absence':
      return t(`當 ${f.metric} 連續 ${f.window} 無數據時，觸發 ${sev} 告警`,
        `fires ${sev} when ${f.metric} has no data for ${f.window}`);
    case 'p99_latency': {
      const q = f.quantile || '0.99';
      return t(`當 ${f.metric} 的 p${q} 延遲在 ${f.window} 內 ${op} ${value} 秒時，觸發 ${sev} 告警`,
        `fires ${sev} when the p${q} latency of ${f.metric} ${op} ${value}s over ${f.window}`);
    }
    case 'forecast':
      if (f.capacity_metric) {
        return t(`當 ${f.metric} / ${f.capacity_metric} 的比例預測在 ${f.horizon} 內 ${op} ${value} 時，觸發 ${sev} 告警`,
          `fires ${sev} when ${f.metric} / ${f.capacity_metric} is predicted to ${op} ${value} within ${f.horizon}`);
      }
      return t(`當 ${f.metric} 預測在 ${f.horizon} 內 ${op} ${value} 時，觸發 ${sev} 告警`,
        `fires ${sev} when ${f.metric} is predicted to ${op} ${value} within ${f.horizon}`);
    default: return null;
  }
}

/* ── recipe object + YAML snippet (the Dumb Handoff payload) ────────── */
function buildRecipeObject(recipe, f) {
  const obj = { recipe, name: f.name, metric: f.metric, threshold: composeThreshold(f) };
  const fields = FIELDS_BY_RECIPE[recipe] || [];
  for (const k of ['op', 'window', 'horizon', 'quantile', 'denominator_metric', 'capacity_metric', 'mode', 'for']) {
    if (fields.includes(k) && f[k]) obj[k] = f[k];
  }
  return obj;
}

/* yamlValue — quote anything a YAML reader could take as a NON-string scalar.
 * #1017: a number-looking value (quantile "0.99", incl. dotless exponents like
 * "95e-2") must be emitted QUOTED — bare, the Go (yaml.v3) and Python (PyYAML
 * 1.1) readers can disagree on its type/text, silently splitting the
 * cross-language recipe_id join; the conf.d schema now rejects a bare-number
 * quantile outright (tenant-config.schema.json `type: string`). Number(v)
 * covers every form the quantile field validator accepts. Other fields are
 * never number-like (metric/name are identifiers, window/horizon carry a unit
 * suffix, threshold is composed as "value:severity" → quoted by the charset
 * test below). */
function yamlValue(v) {
  if (/[^a-zA-Z0-9_.]/.test(v) || (v !== '' && !Number.isNaN(Number(v)))) return JSON.stringify(v);
  return v;
}

function yamlSnippet(tenantId, obj) {
  const id = tenantId || 'YOUR_TENANT_ID';
  const lines = [
    `# ${t('加入你的 conf.d 對應租戶檔', 'add under your conf.d tenant file')}`,
    'tenants:', `  ${id}:`, '    _custom_alerts:',
  ];
  Object.keys(obj).forEach((k, i) => {
    lines.push(`${i === 0 ? '      - ' : '        '}${k}: ${yamlValue(String(obj[k]))}`);
  });
  return lines.join('\n');
}

export {
  recipeStatus,
  formSupported,
  isValidName,
  isValidMetric,
  isValidWindow,
  parseThresholdValue,
  composeThreshold,
  requiredFields,
  isFieldValid,
  allRequiredValid,
  recipeSummary,
  buildRecipeObject,
  yamlValue,
  yamlSnippet,
  FIELDS_BY_RECIPE,
  METRIC_FIELDS,
};
