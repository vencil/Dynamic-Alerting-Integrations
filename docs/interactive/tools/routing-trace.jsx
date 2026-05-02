---
title: "Routing Trace Wizard"
tags: [routing, alertmanager, trace, wizard, c-3]
audience: [platform-engineer, sre]
version: v2.7.0
lang: en
related: [alert-builder, alert-simulator, master-onboarding, cicd-setup-wizard, deployment-wizard]
---

import React, { useState, useMemo } from 'react';

const t = window.__t || ((zh, en) => en);

/* ── Step definitions ─────────────────────────────────────────────────
 *
 * 4-step routing trace simulator (S#92 / C-3 PR-3):
 *
 *   1. Alert input — alertname + labels + severity
 *   2. Default route — top-level receiver + group_by
 *   3. Child routes — list of {match, receiver} (first-match-wins)
 *   4. Trace result — matched route hierarchy + final receiver
 *
 * Honest scope:
 *   - Form-driven, NO arbitrary YAML parsing (avoid vendor dep)
 *   - Equality matchers only — NO regex (`match_re` / `matchers`)
 *   - Single-level child routes — NO nested route trees
 *   - First-match-wins — NO Alertmanager `continue: true` semantics
 *   - NO timing simulation (group_wait / group_interval / repeat_interval)
 *   - NO inhibit rules (orthogonal; alert-simulator already covers
 *     inhibit preview per master-onboarding step 4 fallback link)
 *
 * The wizard teaches the routing CONCEPT (label matchers → tree walk →
 * receiver) without trying to be a complete Alertmanager simulator.
 * For full simulation use alertmanager itself + amtool.
 * ─────────────────────────────────────────────────────────────────── */

const STEPS = [
  { id: 'alert', label: () => t('1. 樣本告警', '1. Sample Alert') },
  { id: 'default', label: () => t('2. 預設路由', '2. Default Route') },
  { id: 'children', label: () => t('3. 子路由', '3. Child Routes') },
  { id: 'trace', label: () => t('4. 追蹤結果', '4. Trace Result') },
];

const SEVERITY_OPTIONS = [
  { id: 'warning', label: 'warning', icon: '⚠️' },
  { id: 'critical', label: 'critical', icon: '🚨' },
  { id: 'info', label: 'info', icon: 'ℹ️' },
];

const PRESET_RECEIVERS = ['default-pager', 'team-platform', 'team-database', 'team-frontend', 'noisy-channel'];

/* ── Validation helpers ────────────────────────────────────────────── */
const ALERT_NAME_RE = /^[A-Za-z][A-Za-z0-9_]*$/;
const LABEL_KEY_RE = /^[a-z][a-z0-9_]*$/;

function isValidAlertName(s) {
  return typeof s === 'string' && ALERT_NAME_RE.test(s);
}
function isValidLabelKey(s) {
  return typeof s === 'string' && LABEL_KEY_RE.test(s);
}

/* ── Routing trace algorithm ───────────────────────────────────────
 *
 * Walks child routes top-to-bottom; first whose match labels are ALL
 * satisfied by the alert labels wins. If none match, the default
 * route's receiver is used.
 *
 * Returns trace: { matchedRoute, receiver, reasons[] }
 *   - matchedRoute: child route index (0-based) that won, or null if
 *     fell through to default
 *   - receiver: the receiver string
 *   - reasons: ordered list of human-readable trace lines
 * ────────────────────────────────────────────────────────────────── */
// Note: NOT exported — Babel-standalone in jsx-loader only supports
// `export default`; named exports compile to `exports.x = ...` which
// fails at runtime with `exports is not defined` (caught locally
// reproducing PR #182 first-CI-run failure). Kept module-scope so
// the component closure can reach it.
function computeTrace({ alert, defaultRoute, childRoutes }) {
  const alertLabels = {
    alertname: alert.alertname || '(blank)',
    severity: alert.severity || 'warning',
    ...(alert.labels || {}),
  };
  const reasons = [];
  reasons.push(
    t(
      `Alert: { alertname=${alertLabels.alertname}, severity=${alertLabels.severity}${
        Object.entries(alert.labels || {})
          .filter(([k, v]) => k && v)
          .map(([k, v]) => `, ${k}=${v}`)
          .join('')
      } }`,
      `Alert: { alertname=${alertLabels.alertname}, severity=${alertLabels.severity}${
        Object.entries(alert.labels || {})
          .filter(([k, v]) => k && v)
          .map(([k, v]) => `, ${k}=${v}`)
          .join('')
      } }`
    )
  );

  for (let i = 0; i < (childRoutes || []).length; i++) {
    const r = childRoutes[i];
    const match = r.match || {};
    const matchEntries = Object.entries(match).filter(([k, v]) => k && v);
    if (matchEntries.length === 0) {
      reasons.push(
        t(
          `Child route ${i + 1}: skipped (no match labels)`,
          `Child route ${i + 1}: skipped (no match labels)`
        )
      );
      continue;
    }
    const failed = matchEntries.find(([k, v]) => alertLabels[k] !== v);
    if (failed) {
      const [k, v] = failed;
      reasons.push(
        t(
          `Child route ${i + 1}: NO MATCH (${k}=${v} ≠ ${alertLabels[k] ?? '(missing)'})`,
          `Child route ${i + 1}: NO MATCH (${k}=${v} ≠ ${alertLabels[k] ?? '(missing)'})`
        )
      );
      continue;
    }
    reasons.push(
      t(
        `Child route ${i + 1}: MATCH — all of { ${matchEntries.map(([k, v]) => `${k}=${v}`).join(', ')} } satisfied → receiver: ${r.receiver || '(blank)'}`,
        `Child route ${i + 1}: MATCH — all of { ${matchEntries.map(([k, v]) => `${k}=${v}`).join(', ')} } satisfied → receiver: ${r.receiver || '(blank)'}`
      )
    );
    return {
      matchedRoute: i,
      receiver: r.receiver || '(blank)',
      reasons,
    };
  }

  reasons.push(
    t(
      `No child route matched → fall through to default receiver: ${defaultRoute.receiver || '(blank)'}`,
      `No child route matched → fall through to default receiver: ${defaultRoute.receiver || '(blank)'}`
    )
  );
  return {
    matchedRoute: null,
    receiver: defaultRoute.receiver || '(blank)',
    reasons,
  };
}

/* ── Step navigation gate ─────────────────────────────────────────── */
function canAdvance(step, state) {
  switch (step) {
    case 0:
      return isValidAlertName(state.alert.alertname);
    case 1:
      return state.defaultRoute.receiver.trim() !== '';
    case 2:
      // Child routes are optional — fall-through to default is valid.
      // Just check that any added route has both match + receiver.
      return (state.childRoutes || []).every(
        (r) =>
          r.receiver.trim() !== '' &&
          Object.entries(r.match || {}).filter(([k, v]) => k && v).length > 0
      );
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

const inputClass =
  'w-full px-3 py-2 text-sm border border-[color:var(--da-color-surface-border)] rounded-md bg-[color:var(--da-color-surface)] text-[color:var(--da-color-fg)] focus:outline-none focus:ring-2 focus:ring-[color:var(--da-color-focus-ring)]';

/* ── Main component ───────────────────────────────────────────────── */
export default function RoutingTrace() {
  const [step, setStep] = useState(0);
  const [state, setState] = useState({
    alert: {
      alertname: 'HighCPUUsage',
      severity: 'critical',
      labels: { team: 'platform', env: 'prod' },
    },
    defaultRoute: {
      receiver: 'default-pager',
      groupBy: 'alertname,cluster',
    },
    childRoutes: [
      { match: { severity: 'critical' }, receiver: 'team-platform' },
      { match: { team: 'database' }, receiver: 'team-database' },
    ],
  });
  const [copied, setCopied] = useState(false);

  const trace = useMemo(() => computeTrace(state), [state]);

  const setAlertField = (key, value) =>
    setState((s) => ({ ...s, alert: { ...s.alert, [key]: value } }));
  const setAlertLabel = (key, value) =>
    setState((s) => ({
      ...s,
      alert: { ...s.alert, labels: { ...(s.alert.labels || {}), [key]: value } },
    }));
  const removeAlertLabel = (key) =>
    setState((s) => {
      const next = { ...(s.alert.labels || {}) };
      delete next[key];
      return { ...s, alert: { ...s.alert, labels: next } };
    });
  const addAlertLabelRow = () =>
    setState((s) => {
      const labels = { ...(s.alert.labels || {}) };
      labels[`label_${Object.keys(labels).length + 1}`] = '';
      return { ...s, alert: { ...s.alert, labels } };
    });

  const setDefaultField = (key, value) =>
    setState((s) => ({ ...s, defaultRoute: { ...s.defaultRoute, [key]: value } }));

  const setChildRoute = (index, updater) =>
    setState((s) => {
      const next = [...s.childRoutes];
      next[index] = updater(next[index]);
      return { ...s, childRoutes: next };
    });
  const setChildMatch = (index, key, value) =>
    setChildRoute(index, (r) => ({
      ...r,
      match: { ...(r.match || {}), [key]: value },
    }));
  const removeChildMatch = (index, key) =>
    setChildRoute(index, (r) => {
      const next = { ...(r.match || {}) };
      delete next[key];
      return { ...r, match: next };
    });
  const addChildMatchRow = (index) =>
    setChildRoute(index, (r) => {
      const next = { ...(r.match || {}) };
      next[`label_${Object.keys(next).length + 1}`] = '';
      return { ...r, match: next };
    });
  const addChildRoute = () =>
    setState((s) => ({
      ...s,
      childRoutes: [...s.childRoutes, { match: {}, receiver: '' }],
    }));
  const removeChildRoute = (index) =>
    setState((s) => ({
      ...s,
      childRoutes: s.childRoutes.filter((_, i) => i !== index),
    }));

  const advance = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const retreat = () => setStep((s) => Math.max(s - 1, 0));

  const traceText = useMemo(() => {
    const lines = [];
    lines.push(t('Routing Trace Result', 'Routing Trace Result'));
    lines.push('═'.repeat(40));
    for (const reason of trace.reasons) lines.push(reason);
    lines.push('');
    lines.push(
      t(
        `Final Receiver: ${trace.receiver}`,
        `Final Receiver: ${trace.receiver}`
      )
    );
    lines.push(
      t(
        `Matched Child Route: ${trace.matchedRoute === null ? '(default fall-through)' : `#${trace.matchedRoute + 1}`}`,
        `Matched Child Route: ${trace.matchedRoute === null ? '(default fall-through)' : `#${trace.matchedRoute + 1}`}`
      )
    );
    return lines.join('\n');
  }, [trace]);

  const copyTrace = async () => {
    try {
      await navigator.clipboard.writeText(traceText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('clipboard copy failed', err);
    }
  };

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-[color:var(--da-color-fg)] mb-1">
          {t('告警路由追蹤精靈', 'Routing Trace Wizard')}
        </h1>
        <p className="text-sm text-[color:var(--da-color-muted)]">
          {t(
            '4 步模擬 Alertmanager 路由樹：定義樣本告警 + 預設路由 + 子路由，查看哪個 receiver 收到。',
            '4-step Alertmanager routing tree simulator: define sample alert + default route + child routes, see which receiver gets it.'
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
              data-testid={`routing-trace-step-${s.id}`}
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

      {/* Step 0: Alert */}
      {step === 0 && (
        <div className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
          <Field
            label={t('Alert 名稱', 'Alert Name')}
            hint={t('PascalCase；alertname label 值', 'PascalCase; alertname label value')}
            error={
              state.alert.alertname && !isValidAlertName(state.alert.alertname)
                ? t('名稱含非法字元', 'Invalid identifier')
                : null
            }
          >
            <input
              type="text"
              value={state.alert.alertname}
              onChange={(e) => setAlertField('alertname', e.target.value)}
              placeholder="HighCPUUsage"
              data-testid="routing-trace-alertname"
              className={inputClass}
            />
          </Field>
          <Field label={t('嚴重程度', 'Severity')}>
            <div className="flex gap-2 flex-wrap">
              {SEVERITY_OPTIONS.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setAlertField('severity', s.id)}
                  data-testid={`routing-trace-severity-${s.id}`}
                  className={`px-3 py-1.5 text-xs rounded border ${
                    state.alert.severity === s.id
                      ? 'border-[color:var(--da-color-accent)] bg-[color:var(--da-color-accent-soft)] text-[color:var(--da-color-accent)]'
                      : 'border-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]'
                  }`}
                >
                  {s.icon} {s.label}
                </button>
              ))}
            </div>
          </Field>
          <Field
            label={t('額外 Labels（key=value）', 'Extra Labels (key=value)')}
            hint={t(
              '除了 alertname / severity 之外，可加 team / service / env 等標籤幫助路由匹配。',
              'Beyond alertname / severity, add team / service / env labels to help routing match.'
            )}
          >
            <div className="space-y-2">
              {Object.entries(state.alert.labels || {}).map(([k, v]) => (
                <div key={k} className="flex gap-2">
                  <input
                    type="text"
                    value={k}
                    onChange={(e) => {
                      const newKey = e.target.value;
                      setState((s) => {
                        const labels = { ...(s.alert.labels || {}) };
                        delete labels[k];
                        labels[newKey] = v;
                        return { ...s, alert: { ...s.alert, labels } };
                      });
                    }}
                    placeholder="key"
                    className={`${inputClass} flex-1`}
                  />
                  <input
                    type="text"
                    value={v}
                    onChange={(e) => setAlertLabel(k, e.target.value)}
                    placeholder="value"
                    className={`${inputClass} flex-1`}
                  />
                  <button
                    type="button"
                    onClick={() => removeAlertLabel(k)}
                    aria-label={t('移除', 'Remove')}
                    className="px-2 text-sm text-[color:var(--da-color-error)]"
                  >
                    ✕
                  </button>
                </div>
              ))}
              <button
                type="button"
                onClick={addAlertLabelRow}
                className="text-sm text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] underline decoration-dotted underline-offset-4"
              >
                + {t('新增 Label', 'Add Label')}
              </button>
            </div>
          </Field>
        </div>
      )}

      {/* Step 1: Default route */}
      {step === 1 && (
        <div className="space-y-4 p-5 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
          <Field
            label={t('預設 Receiver', 'Default Receiver')}
            hint={t(
              '當沒有任何子路由匹配時，告警送往這個 receiver。',
              'Used when no child route matches the alert.'
            )}
          >
            <input
              type="text"
              list="receivers"
              value={state.defaultRoute.receiver}
              onChange={(e) => setDefaultField('receiver', e.target.value)}
              placeholder="default-pager"
              data-testid="routing-trace-default-receiver"
              className={inputClass}
            />
            <datalist id="receivers">
              {PRESET_RECEIVERS.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
          </Field>
          <Field
            label={t('Group By（資訊用）', 'Group By (informational)')}
            hint={t(
              '逗號分隔的標籤名；本 wizard 不模擬 grouping，僅展示。',
              'Comma-separated label names; this wizard does not simulate grouping, displayed for completeness.'
            )}
          >
            <input
              type="text"
              value={state.defaultRoute.groupBy}
              onChange={(e) => setDefaultField('groupBy', e.target.value)}
              placeholder="alertname,cluster"
              className={inputClass}
            />
          </Field>
          <div className="p-3 border-l-4 border-[color:var(--da-color-info)] bg-[color:var(--da-color-info-soft)] rounded">
            <p className="text-xs text-[color:var(--da-color-muted)]">
              {t(
                '⚠ Honest scope：本 wizard 不模擬 group_wait / group_interval / repeat_interval timing；僅模擬「哪個 receiver 收到」。',
                '⚠ Honest scope: this wizard does not simulate group_wait / group_interval / repeat_interval timing; only "which receiver receives".'
              )}
            </p>
          </div>
        </div>
      )}

      {/* Step 2: Child routes */}
      {step === 2 && (
        <div className="space-y-4">
          <p className="text-sm text-[color:var(--da-color-muted)]">
            {t(
              '由上至下評估；第一個 match 全滿足者 wins（first-match-wins，不模擬 continue: true）。',
              'Evaluated top-to-bottom; first whose match labels are ALL satisfied wins (first-match-wins; does not simulate continue: true).'
            )}
          </p>
          {state.childRoutes.map((r, i) => (
            <div
              key={i}
              className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]"
              data-testid={`routing-trace-child-route-${i}`}
            >
              <div className="flex items-center justify-between mb-3">
                <span className="text-sm font-semibold text-[color:var(--da-color-fg)]">
                  {t(`子路由 #${i + 1}`, `Child Route #${i + 1}`)}
                </span>
                <button
                  type="button"
                  onClick={() => removeChildRoute(i)}
                  aria-label={t('移除', 'Remove')}
                  className="text-xs text-[color:var(--da-color-error)] hover:underline"
                >
                  ✕ {t('移除', 'Remove')}
                </button>
              </div>
              <Field label={t('Receiver', 'Receiver')}>
                <input
                  type="text"
                  list="receivers"
                  value={r.receiver}
                  onChange={(e) =>
                    setChildRoute(i, (cr) => ({ ...cr, receiver: e.target.value }))
                  }
                  placeholder="team-platform"
                  className={inputClass}
                />
              </Field>
              <Field
                label={t('Match Labels (key=value)', 'Match Labels (key=value)')}
                hint={t(
                  '所有條件必須同時成立才視為 match（AND 語意）。',
                  'All conditions must hold simultaneously to match (AND semantics).'
                )}
              >
                <div className="space-y-2">
                  {Object.entries(r.match || {}).map(([k, v]) => (
                    <div key={k} className="flex gap-2">
                      <input
                        type="text"
                        value={k}
                        onChange={(e) => {
                          const newKey = e.target.value;
                          setChildRoute(i, (cr) => {
                            const next = { ...(cr.match || {}) };
                            delete next[k];
                            next[newKey] = v;
                            return { ...cr, match: next };
                          });
                        }}
                        placeholder="key"
                        className={`${inputClass} flex-1`}
                      />
                      <input
                        type="text"
                        value={v}
                        onChange={(e) => setChildMatch(i, k, e.target.value)}
                        placeholder="value"
                        className={`${inputClass} flex-1`}
                      />
                      <button
                        type="button"
                        onClick={() => removeChildMatch(i, k)}
                        aria-label={t('移除', 'Remove')}
                        className="px-2 text-sm text-[color:var(--da-color-error)]"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    onClick={() => addChildMatchRow(i)}
                    className="text-xs text-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent-hover)] underline decoration-dotted underline-offset-4"
                  >
                    + {t('新增 Match', 'Add Match')}
                  </button>
                </div>
              </Field>
            </div>
          ))}
          <button
            type="button"
            onClick={addChildRoute}
            data-testid="routing-trace-add-child-route"
            className="px-4 py-2 text-sm font-medium border border-dashed border-[color:var(--da-color-surface-border)] rounded-lg w-full hover:border-[color:var(--da-color-accent)] hover:text-[color:var(--da-color-accent)]"
          >
            + {t('新增子路由', 'Add Child Route')}
          </button>
        </div>
      )}

      {/* Step 3: Trace result */}
      {step === 3 && (
        <div className="space-y-4">
          <div
            className={`p-4 rounded-lg border-l-4 ${
              trace.matchedRoute === null
                ? 'border-[color:var(--da-color-warning)] bg-[color:var(--da-color-warning-soft)]'
                : 'border-[color:var(--da-color-success)] bg-[color:var(--da-color-success-soft)]'
            }`}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="text-sm font-semibold text-[color:var(--da-color-fg)]">
                {trace.matchedRoute === null
                  ? t('預設路由 Fall-through', 'Default Route Fall-through')
                  : t(
                      `匹配子路由 #${trace.matchedRoute + 1}`,
                      `Matched Child Route #${trace.matchedRoute + 1}`
                    )}
              </span>
              <button
                type="button"
                onClick={copyTrace}
                data-testid="routing-trace-copy"
                className="px-3 py-1.5 text-xs font-medium rounded bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] hover:bg-[color:var(--da-color-accent-hover)]"
              >
                {copied ? t('✓ 已複製', '✓ Copied') : t('複製追蹤結果', 'Copy Trace')}
              </button>
            </div>
            <p className="text-base font-bold text-[color:var(--da-color-fg)]">
              <span className="text-xs font-normal text-[color:var(--da-color-muted)]">
                {t('最終 Receiver:', 'Final Receiver:')}
              </span>{' '}
              <span data-testid="routing-trace-receiver">{trace.receiver}</span>
            </p>
          </div>
          <div className="p-4 border border-[color:var(--da-color-surface-border)] rounded-lg bg-[color:var(--da-color-surface)]">
            <h3 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-2">
              {t('追蹤步驟', 'Trace Steps')}
            </h3>
            <ol
              data-testid="routing-trace-reasons"
              className="text-xs font-mono text-[color:var(--da-color-fg)] space-y-1"
            >
              {trace.reasons.map((r, i) => (
                <li key={i} className="leading-relaxed">
                  {i + 1}. {r}
                </li>
              ))}
            </ol>
          </div>
          <div className="p-4 border-l-4 border-[color:var(--da-color-info)] bg-[color:var(--da-color-info-soft)] rounded">
            <h4 className="text-sm font-semibold text-[color:var(--da-color-fg)] mb-1">
              {t('整合提示', 'Integration tips')}
            </h4>
            <ul className="text-xs text-[color:var(--da-color-muted)] space-y-1 list-disc pl-5">
              <li>
                {t(
                  '完整 Alertmanager 模擬：用 amtool 對 alertmanager.yml 跑 routes test。',
                  'Full Alertmanager simulation: use amtool routes test against alertmanager.yml.'
                )}
              </li>
              <li>
                {t(
                  'inhibit rules 模擬：見 alert-simulator 工具的 inhibit preview 功能。',
                  'Inhibit rules simulation: see alert-simulator tool inhibit preview.'
                )}
              </li>
              <li>
                {t(
                  '本 wizard 教 routing 概念（label match + tree walk + receiver），不教 timing / grouping。',
                  'This wizard teaches the routing concept (label match + tree walk + receiver), not timing / grouping.'
                )}
              </li>
            </ul>
          </div>
        </div>
      )}

      {/* Navigation */}
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
            disabled={!canAdvance(step, state)}
            data-testid="routing-trace-next"
            className="px-4 py-2 text-sm font-medium rounded bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] hover:bg-[color:var(--da-color-accent-hover)] disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {t('下一步', 'Next')} →
          </button>
        ) : (
          <span className="text-sm text-[color:var(--da-color-muted)]">
            {t('修改任何設定，trace 即時重算', 'Edit any field — trace recomputes live')}
          </span>
        )}
      </div>
    </div>
  );
}
