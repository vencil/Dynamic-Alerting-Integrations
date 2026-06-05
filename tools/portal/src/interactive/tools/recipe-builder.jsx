---
title: "Custom Alert Recipe Builder"
tags: [custom-alert, recipe, builder, adr-024, s6b]
audience: [tenant, sre, platform-engineer]
version: v2.9.0
lang: en
related: [alert-builder, tenant-manager, threshold-calculator, simulate-preview]
---

import React, { useState, useEffect, useMemo, useRef } from 'react';
import { useDebouncedValue } from './_common/hooks/useDebouncedValue.js';
import ENUMS from './_common/data/recipe-enums.json';

/* ── i18n helper ───────────────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── Custom Alert Recipe Builder (ADR-024 §S6b, #741) ─────────────────
 *
 * A no-PromQL "Smart Form, Dumb Handoff" component: it produces a valid
 * `_custom_alerts` recipe object and hands it off — it does NOT own the
 * write. Two exits:
 *   - onSubmit absent (S6b-1 standalone, GitOps persona): renders a
 *     copy-paste YAML snippet WITH the full tenants/<id>/_custom_alerts
 *     wrapper (review OQ2 fix — no "where do I paste it" friction).
 *   - onSubmit present (S6b-2, folded into tenant-manager): hands the
 *     RecipeObject to the write authority (name-based mutation + PUT+S5).
 *
 * All enums derive from recipe-enums.json (extracted from the schema;
 * a Vitest drift-guard keeps it honest) — NO hardcoded enums here.
 * ─────────────────────────────────────────────────────────────────── */

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
    case 'threshold': return !Number.isNaN(parseThresholdValue(value));
    default: return true;
  }
}

/* allRequiredValid — the state-machine gate for the summary + submit.
 * False until every required field is present and structurally valid, so
 * the plain-English summary never renders [undefined] (review Reef 4). */
function allRequiredValid(recipe, f) {
  for (const field of requiredFields(recipe)) {
    if (!f[field] || !isFieldValid(field, f[field])) return false;
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

function yamlValue(v) {
  return /[^a-zA-Z0-9_.]/.test(v) ? JSON.stringify(v) : v;
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

/* ── default live S6a fetcher (overridable for tests / tenant-manager) ─ */
function defaultFetchMetrics(tenantId, q, signal) {
  const qs = q ? `?q=${encodeURIComponent(q)}` : '';
  return fetch(`/api/v1/tenants/${encodeURIComponent(tenantId)}/metrics${qs}`, { signal })
    .then((r) => { if (!r.ok) throw new Error(`discovery HTTP ${r.status}`); return r.json(); })
    .then((j) => (Array.isArray(j.metrics) ? j.metrics : []));
}

/* ── MetricField — an INDEPENDENT autocomplete fetcher (review Reef 4):
 * own debounce + AbortController; ghost validation DECOUPLED from the
 * suggestion list (review Reef 3). Module-scope (jsx-loader-compat). ── */
function MetricField({ label, value, onChange, tenantId, fetchMetrics, inputClass, required, testid }) {
  // Fully controlled: the input value IS the parent's field value (no local
  // mirror state). onChange commits every keystroke, so a derived-state copy
  // would only risk going stale on an external value change (S6b-2 cheap-edit
  // / programmatic reset). Ghost status is the only local state.
  const [suggestions, setSuggestions] = useState([]);
  const [ghost, setGhost] = useState('idle'); // idle|validating|ok|ghost|unavailable
  const debounced = useDebouncedValue(value || '', 300);
  const abortRef = useRef(null);
  const listId = `dl-${testid || label}`;

  useEffect(() => {
    if (!tenantId || !debounced) { setSuggestions([]); return undefined; }
    if (abortRef.current) abortRef.current.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let live = true;
    fetchMetrics(tenantId, debounced, ctrl.signal)
      .then((names) => { if (live) setSuggestions(names.slice(0, 20)); })
      .catch((err) => { if (live && err && err.name !== 'AbortError') setSuggestions([]); });
    return () => { live = false; ctrl.abort(); };
  }, [debounced, tenantId, fetchMetrics]);

  function validateExact(name) {
    // Blur-time precise check (decoupled from the autocomplete list). A check
    // in flight when the component unmounts resolves into a no-op setState
    // (React warns, harmless); blur is infrequent so a tracked abort isn't
    // worth the complexity here.
    onChange(name);
    if (!name || !tenantId) { setGhost('idle'); return; }
    setGhost('validating');
    const ctrl = new AbortController();
    fetchMetrics(tenantId, name, ctrl.signal)
      .then((names) => setGhost(names.includes(name) ? 'ok' : 'ghost'))
      .catch((err) => { if (err && err.name !== 'AbortError') setGhost('unavailable'); });
  }

  return (
    <div className="mb-3">
      <label className="block text-sm font-medium mb-1">{label}{required ? ' *' : ''}</label>
      <input
        className={inputClass}
        value={value || ''}
        list={listId}
        aria-label={label}
        data-testid={testid}
        onChange={(e) => { setGhost('idle'); onChange(e.target.value); }}
        onBlur={(e) => validateExact(e.target.value.trim())}
      />
      <datalist id={listId}>
        {suggestions.map((s) => <option key={s} value={s} />)}
      </datalist>
      {ghost === 'validating' && (
        <p className="text-xs mt-1 text-[color:var(--da-color-muted)]">{t('驗證中…', 'Validating...')}</p>
      )}
      {ghost === 'ghost' && (
        <p className="text-xs mt-1 pl-2 border-l-2 border-[color:var(--da-color-warning)] text-[color:var(--da-color-warning)]" data-testid={`${testid}-ghost`}>
          {t('此 metric 目前無數據，確認名稱無誤？', 'This metric has no data right now — confirm the name?')}
        </p>
      )}
      {ghost === 'unavailable' && (
        <p className="text-xs mt-1 text-[color:var(--da-color-muted)]">
          {t('指標發現暫不可用，可手動輸入', 'Discovery unavailable — manual entry is fine')}
        </p>
      )}
    </div>
  );
}

/* ── main component ───────────────────────────────────────────────── */
export default function RecipeBuilder(props) {
  const {
    tenantId = (typeof window !== 'undefined'
      ? new URLSearchParams(window.location.search).get('tenant_id') || ''
      : ''),
    fetchMetrics = defaultFetchMetrics,
    onSubmit = null,
    initialValue = null,
  } = props || {};

  const [recipe, setRecipe] = useState((initialValue && initialValue.recipe) || 'threshold');
  const [f, setF] = useState(() => ({
    name: '', metric: '', denominator_metric: '', capacity_metric: '',
    op: ENUMS.opDefault, window: '5m', horizon: '4h', quantile: '0.99',
    threshold: '', severity: ENUMS.severityDefault,
    mode: ENUMS.modeDefault, for: ENUMS.forDefault,
    ...(initialValue || {}),
  }));
  const set = (k, v) => setF((prev) => ({ ...prev, [k]: v }));

  const fields = FIELDS_BY_RECIPE[recipe] || [];
  const summary = useMemo(() => recipeSummary(recipe, f), [recipe, f]);
  const ready = allRequiredValid(recipe, f);
  const recipeObj = useMemo(() => (ready ? buildRecipeObject(recipe, f) : null), [ready, recipe, f]);

  const inputClass =
    'w-full px-3 py-2 text-sm border border-[color:var(--da-color-surface-border)] rounded-md '
    + 'bg-[color:var(--da-color-surface)] text-[color:var(--da-color-fg)] '
    + 'focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]';
  const labelClass = 'block text-sm font-medium mb-1';

  const select = (key, label, options) => (
    <div className="mb-3" key={key}>
      <label className={labelClass}>{label}</label>
      <select className={inputClass} value={f[key]} aria-label={label}
        data-testid={`field-${key}`} onChange={(e) => set(key, e.target.value)}>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );

  const text = (key, label) => (
    <div className="mb-3" key={key}>
      <label className={labelClass}>{label}</label>
      <input className={inputClass} value={f[key]} aria-label={label}
        data-testid={`field-${key}`} onChange={(e) => set(key, e.target.value)} />
    </div>
  );

  const LABELS = {
    op: t('比較運算子', 'Operator'), severity: t('嚴重程度', 'Severity'),
    mode: t('模式', 'Mode'), for: t('持續 (for)', 'For'),
    horizon: t('預測範圍 (horizon)', 'Horizon'), window: t('視窗 (window)', 'Window'),
    quantile: t('分位數 (quantile)', 'Quantile'),
    threshold: t('閾值（數值，嚴重程度由下拉選）', 'Threshold value (severity from the dropdown)'),
    metric: t('指標', 'Metric'), denominator_metric: t('分母指標', 'Denominator metric'),
    capacity_metric: t('容量指標（選填，比例模式）', 'Capacity metric (optional, ratio mode)'),
  };

  function renderField(field) {
    if (['op', 'severity', 'mode', 'for', 'horizon'].includes(field)) {
      return select(field, LABELS[field], ENUMS[field === 'severity' ? 'severity' : field]);
    }
    if (METRIC_FIELDS.includes(field)) {
      return (
        <MetricField key={field} label={LABELS[field]} value={f[field]}
          onChange={(v) => set(field, v)} tenantId={tenantId} fetchMetrics={fetchMetrics}
          inputClass={inputClass} required={field !== 'capacity_metric'} testid={`field-${field}`} />
      );
    }
    return text(field, LABELS[field]);
  }

  return (
    <div className="max-w-2xl" data-testid="recipe-builder">
      <h2 className="text-lg font-semibold mb-1">
        {t('自訂告警 Recipe Builder', 'Custom Alert Recipe Builder')}
      </h2>
      <p className="text-sm text-[color:var(--da-color-muted)] mb-4">
        {t('選指標、選 recipe、填參數 —— 不需寫 PromQL。', 'Pick a metric, a recipe, fill the params — no PromQL.')}
      </p>

      {/* recipe select has its own handler (recipe is top-level state, not in `f`) */}
      <div className="mb-3">
        <label className={labelClass}>{t('Recipe 類型', 'Recipe type')}</label>
        <select className={inputClass} value={recipe} aria-label="recipe" data-testid="field-recipe"
          onChange={(e) => setRecipe(e.target.value)}>
          {ENUMS.recipe.map((r) => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>

      <div className="mb-3">
        <label className={labelClass}>{t('告警名稱', 'Alert name')} *</label>
        <input className={inputClass} value={f.name} aria-label="name" data-testid="field-name"
          onChange={(e) => set('name', e.target.value)} placeholder="queue_depth_high" />
        {f.name && !isValidName(f.name) && (
          <p className="text-xs mt-1 pl-2 border-l-2 border-[color:var(--da-color-error)] text-[color:var(--da-color-error)]">
            {t('名稱須符合 ^[a-z][a-z0-9_]*$', 'name must match ^[a-z][a-z0-9_]*$')}
          </p>
        )}
      </div>

      {fields.map((field) => renderField(field))}

      {/* plain-English summary state machine */}
      <div className="my-4 p-3 rounded-md bg-[color:var(--da-color-accent-soft)]" data-testid="summary">
        {summary
          ? <span className="text-sm text-[color:var(--da-color-fg)]">{summary}</span>
          : <span className="text-sm text-[color:var(--da-color-muted)]">
              {t('等待填寫必填參數以生成規則摘要…', 'Waiting for required fields to generate the rule summary...')}
            </span>}
      </div>

      {/* exit: onSubmit (tenant-manager) or YAML snippet (GitOps persona) */}
      {onSubmit ? (
        <button type="button" className="px-4 py-2 rounded-md text-sm font-medium disabled:opacity-50"
          data-testid="submit" disabled={!ready}
          onClick={() => recipeObj && onSubmit(recipeObj)}>
          {t('加入此 recipe', 'Add this recipe')}
        </button>
      ) : (
        <div data-testid="yaml-output">
          <label className={labelClass}>{t('複製進你的 conf.d 租戶檔', 'Copy into your conf.d tenant file')}</label>
          <pre className="text-xs p-3 rounded-md overflow-x-auto bg-[color:var(--da-color-surface)] border border-[color:var(--da-color-surface-border)]">
            {ready ? yamlSnippet(tenantId, recipeObj)
              : t('填妥必填參數後在此產生 YAML。', 'Fill the required fields to generate the YAML here.')}
          </pre>
        </div>
      )}
    </div>
  );
}
