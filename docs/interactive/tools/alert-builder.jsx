---
title: "Alert Builder Wizard"
tags: [alert, prometheus-rule, builder, wizard, c-3]
audience: [platform-engineer, sre, tenant]
version: v2.7.0
lang: en
related: [alert-simulator, threshold-calculator, master-onboarding, cicd-setup-wizard, deployment-wizard]
---

import React, { useState, useMemo } from 'react';

/* ── i18n + repo helpers ───────────────────────────────────────────── */
const t = window.__t || ((zh, en) => en);

/* ── Step definitions (4 steps) ──────────────────────────────────────
 *
 * 1. Identity:    alert name + group + summary annotation
 * 2. Expression:  PromQL + comparison op + threshold value + for duration
 * 3. Severity:    severity + extra labels + description annotation
 * 4. Review:      generated PrometheusRule YAML + copy + integration hint
 *
 * Honest scope (S#91 / C-3 PR-2):
 *   - Form-driven only: no metric autocomplete, no live PromQL parse
 *   - Output: PrometheusRule YAML snippet (paste into rule-packs/<x>.yaml
 *     or feed `da-tools alert-create`)
 *   - Validation is shape-only (non-empty fields + identifier syntax)
 * ─────────────────────────────────────────────────────────────────── */

const STEPS = [
  { id: 'identity', label: () => t('1. 基本資訊', '1. Identity') },
  { id: 'expression', label: () => t('2. 條件運算式', '2. Expression') },
  { id: 'severity', label: () => t('3. 嚴重程度與 Labels', '3. Severity & Labels') },
  { id: 'review', label: () => t('4. 檢視與輸出 YAML', '4. Review & YAML') },
];

const COMPARISON_OPS = [
  { id: '>', label: '> (greater than)' },
  { id: '>=', label: '>= (greater or equal)' },
  { id: '<', label: '< (less than)' },
  { id: '<=', label: '<= (less or equal)' },
  { id: '==', label: '== (equal)' },
  { id: '!=', label: '!= (not equal)' },
];

const SEVERITY_OPTIONS = [
  {
    id: 'warning',
    label: 'warning',
    icon: '⚠️',
    desc: () => t('需要關注但不需要立即動作', 'Needs attention but no immediate action'),
  },
  {
    id: 'critical',
    label: 'critical',
    icon: '🚨',
    desc: () => t('需要立即動作（pager / on-call）', 'Requires immediate action (pager / on-call)'),
  },
  {
    id: 'info',
    label: 'info',
    icon: 'ℹ️',
    desc: () => t('資訊性記錄，不觸發通知', 'Informational only, no notification'),
  },
];

const FOR_DURATION_PRESETS = [
  { id: '1m', label: '1m' },
  { id: '5m', label: '5m' },
  { id: '10m', label: '10m' },
  { id: '15m', label: '15m' },
  { id: '30m', label: '30m' },
  { id: '1h', label: '1h' },
];

/* ── Validation helpers ───────────────────────────────────────────── */
const ALERT_NAME_RE = /^[A-Za-z][A-Za-z0-9_]*$/;
const GROUP_NAME_RE = /^[A-Za-z][A-Za-z0-9_-]*$/;
const LABEL_KEY_RE = /^[a-z][a-z0-9_]*$/;

function isValidAlertName(s) {
  return typeof s === 'string' && ALERT_NAME_RE.test(s);
}
function isValidGroupName(s) {
  return typeof s === 'string' && GROUP_NAME_RE.test(s);
}
function isValidLabelKey(s) {
  return typeof s === 'string' && LABEL_KEY_RE.test(s);
}
function isValidThreshold(s) {
  if (typeof s !== 'string' || s.trim() === '') return false;
  return !Number.isNaN(Number(s));
}

/* ── YAML generator ───────────────────────────────────────────────── */
function buildYaml(config) {
  const {
    alertName,
    groupName,
    summary,
    expression,
    op,
    threshold,
    forDuration,
    severity,
    description,
    labels,
  } = config;

  const yamlIndent = (n) => ' '.repeat(n);
  const lines = [];

  lines.push(`groups:`);
  lines.push(`  - name: ${groupName || 'my-alerts'}`);
  lines.push(`    rules:`);
  lines.push(`      - alert: ${alertName || 'MyAlert'}`);
  // Expr — the wizard composes "<promql_expr> <op> <threshold>". If user
  // already wrote `> 0.8` inside expression they get a malformed rule;
  // surfaced in review-step note.
  lines.push(`        expr: ${expression || 'rate(metric[5m])'} ${op || '>'} ${threshold || '0'}`);
  lines.push(`        for: ${forDuration || '5m'}`);
  lines.push(`        labels:`);
  lines.push(`${yamlIndent(10)}severity: ${severity || 'warning'}`);
  for (const [k, v] of Object.entries(labels || {})) {
    if (k && v) lines.push(`${yamlIndent(10)}${k}: ${v}`);
  }
  lines.push(`        annotations:`);
  lines.push(`${yamlIndent(10)}summary: ${JSON.stringify(summary || `${alertName || 'MyAlert'} fired`)}`);
  if (description) {
    lines.push(`${yamlIndent(10)}description: ${JSON.stringify(description)}`);
  }
  return lines.join('\n');
}

/* ── Step navigation gate ─────────────────────────────────────────── */
function canAdvance(step, config) {
  switch (step) {
    case 0:
      return (
        isValidAlertName(config.alertName) &&
        isValidGroupName(config.groupName) &&
        config.summary.trim() !== ''
      );
    case 1:
      return (
        config.expression.trim() !== '' &&
        config.op !== '' &&
        isValidThreshold(config.threshold) &&
        config.forDuration.trim() !== ''
      );
    case 2:
      return config.severity !== '';
    default:
      return true;
  }
}

/* ── Reusable input field ─────────────────────────────────────────── */
function Field({ label, hint, error, children }) {
  return (
    <label className="block">
      <span className="block text-sm font-medium text-[color:var(--da-color-fg)] mb-1">
        {label}
      </span>
      {children}
      {hint && !error && (
        <span className="block text-xs text-[color:var(--da-color-muted)] mt-1">
          {hint}
        </span>
      )}
      {error && (
        <span className="block text-xs text-[color:var(--da-color-error)] mt-1">
          {error}
        </span>
      )}
    </label>
  );
}

/* ── Main component ───────────────────────────────────────────────── */
export default function AlertBuilder() {
  const [step, setStep] = useState(0);
  const [config, setConfig] = useState({
    alertName: '',
    groupName: 'my-alerts',
    summary: '',
    expression: '',
    op: '>',
    threshold: '',
    forDuration: '5m',
    severity: 'warning',
    description: '',
    labels: { team: '' },
  });
  const [copied, setCopied] = useState(false);

  const yaml = useMemo(() => buildYaml(config), [config]);

  const setField = (key, value) =>
    setConfig((c) => ({ ...c, [key]: value }));

  const setLabel = (key, value) =>
    setConfig((c) => ({ ...c, labels: { ...c.labels, [key]: value } }));

  const removeLabel = (key) =>
    setConfig((c) => {
      const next = { ...c.labels };
      delete next[key];
      return { ...c, labels: next };
    });

  const addLabelRow = () => {
    setConfig((c) => {
      const nextKey = `label_${Object.keys(c.labels).length + 1}`;
      return { ...c, labels: { ...c.labels, [nextKey]: '' } };
    });
  };

  const advance = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const retreat = () => setStep((s) => Math.max(s - 1, 0));

  const copyYaml = async () => {
    try {
      await navigator.clipboard.writeText(yaml);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('clipboard copy failed', err);
    }
  };

  const inputClass =
    'w-full px-3 py-2 text-sm border border-[color:var(--da-color-surface-border)] rounded-md bg-[color:var(--da-color-surface)] text-[color:var(--da-color-fg)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]';

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-1">
          {t('告警規則建構精靈', 'Alert Builder Wizard')}
        </h1>
        <p className="text-sm text-[color:var(--da-color-muted)]">
          {t(
            '4 步建立 PrometheusRule alert：identity / expression / severity / review YAML。',
            '4-step PrometheusRule builder: identity / expression / severity / review YAML.'
          )}
        </p>
      </div>

      {/* Step indicator */}
      <nav
        aria-label={t('精靈進度', 'Wizard progress')}
        className="flex flex-wrap gap-2"
      >
        {STEPS.map((s, i) => {
          const active = i === step;
          const done = i < step;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => i <= step && setStep(i)}
              disabled={i > step}
              data-testid={`alert-builder-step-${s.id}`}
              className={`px-3 py-1.5 text-xs font-medium rounded-full border transition-colors ${
                active
                  ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]'
                  : done
                  ? 'border-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)] text-[color:var(--da-color-success)]'
                  : 'border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)] text-[color:var(--da-color-muted)]'
              } disabled:opacity-60`}
            >
              {s.label()}
            </button>
          );
        })}
      </nav>

      {/* Step 0: Identity */}
      {step === 0 && (
        <div className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
          <Field
            label={t('Alert 名稱', 'Alert Name')}
            hint={t('PascalCase；只能 [A-Za-z][A-Za-z0-9_]*', 'PascalCase; pattern [A-Za-z][A-Za-z0-9_]*')}
            error={
              config.alertName && !isValidAlertName(config.alertName)
                ? t('名稱含非法字元', 'Invalid identifier')
                : null
            }
          >
            <input
              type="text"
              value={config.alertName}
              onChange={(e) => setField('alertName', e.target.value)}
              placeholder="HighCPUUsage"
              data-testid="alert-builder-name"
              className={inputClass}
            />
          </Field>
          <Field
            label={t('Group 名稱', 'Group Name')}
            hint={t('PrometheusRule group key', 'PrometheusRule group key')}
            error={
              config.groupName && !isValidGroupName(config.groupName)
                ? t('Group 名稱含非法字元', 'Invalid group name')
                : null
            }
          >
            <input
              type="text"
              value={config.groupName}
              onChange={(e) => setField('groupName', e.target.value)}
              placeholder="my-alerts"
              className={inputClass}
            />
          </Field>
          <Field
            label={t('Summary（單行說明）', 'Summary (one-liner)')}
            hint={t(
              '出現在告警通知標題；annotations.summary',
              'Shown as alert notification title; annotations.summary'
            )}
          >
            <input
              type="text"
              value={config.summary}
              onChange={(e) => setField('summary', e.target.value)}
              placeholder={t('CPU 使用率超過 80%', 'CPU usage above 80%')}
              className={inputClass}
            />
          </Field>
        </div>
      )}

      {/* Step 1: Expression */}
      {step === 1 && (
        <div className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
          <Field
            label={t('PromQL 運算式', 'PromQL Expression')}
            hint={t(
              '只填左側部分；wizard 會接 op + threshold（例：rate(node_cpu_seconds_total[5m]) → > 0.8）',
              'Left-hand side only; wizard appends op + threshold (e.g. rate(node_cpu_seconds_total[5m]) → > 0.8)'
            )}
          >
            <textarea
              value={config.expression}
              onChange={(e) => setField('expression', e.target.value)}
              placeholder="rate(node_cpu_seconds_total[5m])"
              rows={3}
              data-testid="alert-builder-expr"
              className={`${inputClass} font-mono`}
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label={t('比較運算子', 'Comparison Operator')}>
              <select
                value={config.op}
                onChange={(e) => setField('op', e.target.value)}
                className={inputClass}
              >
                {COMPARISON_OPS.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label={t('閾值', 'Threshold')}
              error={
                config.threshold && !isValidThreshold(config.threshold)
                  ? t('需為數字', 'Must be a number')
                  : null
              }
            >
              <input
                type="text"
                value={config.threshold}
                onChange={(e) => setField('threshold', e.target.value)}
                placeholder="0.8"
                data-testid="alert-builder-threshold"
                className={inputClass}
              />
            </Field>
          </div>
          <Field
            label={t('持續時間（for）', 'For Duration')}
            hint={t(
              '條件需持續 N 時間才觸發告警，避免毛刺',
              'Condition must hold for N before alert fires (debounce spikes)'
            )}
          >
            <div className="flex gap-2 flex-wrap">
              {FOR_DURATION_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => setField('forDuration', p.id)}
                  className={`px-3 py-1.5 text-xs rounded border ${
                    config.forDuration === p.id
                      ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]'
                      : 'border-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]'
                  }`}
                >
                  {p.label}
                </button>
              ))}
              <input
                type="text"
                value={config.forDuration}
                onChange={(e) => setField('forDuration', e.target.value)}
                placeholder="5m"
                className={`${inputClass} w-24`}
              />
            </div>
          </Field>
        </div>
      )}

      {/* Step 2: Severity & Labels */}
      {step === 2 && (
        <div className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
          <Field label={t('嚴重程度', 'Severity')}>
            <div className="grid grid-cols-1 gap-2">
              {SEVERITY_OPTIONS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setField('severity', s.id)}
                  data-testid={`alert-builder-severity-${s.id}`}
                  className={`text-left px-4 py-3 rounded border ${
                    config.severity === s.id
                      ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)]'
                      : 'border-[color:var(--da-color-surface-border)] bg-[color:var(--da-color-surface)]'
                  }`}
                >
                  <span className="font-medium text-[color:var(--da-color-fg)]">
                    {s.icon} {s.label}
                  </span>
                  <span className="block text-xs text-[color:var(--da-color-muted)] mt-1">
                    {s.desc()}
                  </span>
                </button>
              ))}
            </div>
          </Field>
          <Field
            label={t('額外 Labels（key=value）', 'Extra Labels (key=value)')}
            hint={t(
              '常用：team / service / environment；用於 Alertmanager routing。',
              'Common: team / service / environment; used by Alertmanager routing.'
            )}
          >
            <div className="space-y-2">
              {Object.entries(config.labels).map(([k, v], idx) => (
                <div key={idx} className="flex gap-2">
                  <input
                    type="text"
                    value={k}
                    onChange={(e) => {
                      const newKey = e.target.value;
                      setConfig((c) => {
                        const next = { ...c.labels };
                        delete next[k];
                        next[newKey] = v;
                        return { ...c, labels: next };
                      });
                    }}
                    placeholder="key"
                    className={`${inputClass} flex-1`}
                  />
                  <input
                    type="text"
                    value={v}
                    onChange={(e) => setLabel(k, e.target.value)}
                    placeholder="value"
                    className={`${inputClass} flex-1`}
                  />
                  <button
                    type="button"
                    onClick={() => removeLabel(k)}
                    aria-label={t('移除', 'Remove')}
                    className="px-2 text-sm text-[color:var(--da-color-error)]"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addLabelRow}
                className="text-sm text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] underline decoration-dotted underline-offset-4"
              >
                + {t('新增 Label', 'Add Label')}
              </button>
              {Object.keys(config.labels).some(
                (k) => k && !isValidLabelKey(k)
              ) && (
                <p className="text-xs text-[color:var(--da-color-warning)]">
                  {t(
                    '⚠ Label key 慣例：[a-z][a-z0-9_]*（小寫 + 底線）',
                    '⚠ Label key convention: [a-z][a-z0-9_]* (lowercase + underscore)'
                  )}
                </p>
              )}
            </div>
          </Field>
          <Field label={t('描述（選填）', 'Description (optional)')}>
            <textarea
              value={config.description}
              onChange={(e) => setField('description', e.target.value)}
              placeholder={t('可包含 {{ $labels.* }} 模板變數', 'Supports {{ $labels.* }} template vars')}
              rows={2}
              className={`${inputClass} font-mono`}
            />
          </Field>
        </div>
      )}

      {/* Step 3: Review */}
      {step === 3 && (
        <div className="space-y-4">
          <div className="p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-base font-semibold text-[color:var(--da-color-fg)]">
                {t('PrometheusRule YAML', 'PrometheusRule YAML')}
              </h3>
              <button
                type="button"
                onClick={copyYaml}
                data-testid="alert-builder-copy"
                className="px-3 py-1.5 text-xs font-medium rounded bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] hover:bg-[color:var(--da-color-accent-hover)]"
              >
                {copied ? t('✓ 已複製', '✓ Copied') : t('複製 YAML', 'Copy YAML')}
              </button>
            </div>
            <pre
              data-testid="alert-builder-yaml"
              className="text-xs bg-[color:var(--da-color-toast-bg)] text-[color:var(--da-color-toast-fg)] p-4 rounded overflow-x-auto whitespace-pre"
            >
{yaml}
            </pre>
          </div>
          <div className="p-4 border-l-4 border-[color:var(--da-color-info)] bg-[color:var(--da-color-info-soft)] rounded">
            <h4 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-1">
              {t('整合提示', 'Integration tips')}
            </h4>
            <ul className="text-xs text-[color:var(--da-color-muted)] space-y-1 list-disc pl-5">
              <li>
                {t(
                  '貼到 rule-packs/<your-pack>.yaml 的 groups 區塊；da-tools lint 會驗證 schema。',
                  'Paste into rule-packs/<your-pack>.yaml groups block; da-tools lint validates schema.'
                )}
              </li>
              <li>
                {t(
                  '預覽通知格式：alert-simulator 工具吃這份 YAML 模擬 fire / inhibit 行為。',
                  'Preview notification: alert-simulator consumes this YAML to simulate fire / inhibit behaviour.'
                )}
              </li>
              <li>
                {t(
                  '若需多 tenant 共用，把 expr 中具體值移到 _defaults.yaml + tenant.yaml override。',
                  'For multi-tenant, lift literal values from expr into _defaults.yaml + tenant.yaml overrides.'
                )}
              </li>
            </ul>
          </div>
        </div>
      )}

      {/* Navigation buttons */}
      <div className="flex justify-between items-center pt-2">
        <button
          type="button"
          onClick={retreat}
          disabled={step === 0}
          className="px-4 py-2 text-sm text-[color:var(--da-color-muted)] hover:text-[color:var(--da-color-fg)] disabled:opacity-40"
        >
          ← {t('上一步', 'Back')}
        </button>
        {step < STEPS.length - 1 ? (
          <button
            type="button"
            onClick={advance}
            disabled={!canAdvance(step, config)}
            data-testid="alert-builder-next"
            className="px-4 py-2 text-sm font-medium rounded bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {t('下一步', 'Next')} →
          </button>
        ) : (
          <span className="text-sm text-[color:var(--da-color-muted)]">
            {t('複製 YAML 後即可貼回 rule-packs/', 'Copy YAML, then paste into rule-packs/')}
          </span>
        )}
      </div>
    </div>
  );
}
