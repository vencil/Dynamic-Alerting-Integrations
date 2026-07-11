---
title: "Alert Builder — PrometheusRule generation + validators"
purpose: |
  Pure helpers behind the alert builder wizard: identifier/threshold validators
  and buildYaml, which composes a PrometheusRule snippet from the wizard config
  (group, alert, expr = promql<op><threshold>, for, labels, annotations).

  Pre-PR-portal-19 these were inline in alert-builder.jsx (616 LOC) with 0%
  coverage. buildYaml is pure string assembly (no i18n, no globals), so this
  module needs no window.__t fallback.

  Public API:
    isValidAlertName(s)  isValidGroupName(s)
    isValidLabelKey(s)   isValidThreshold(s)
    buildYaml(config)    -> PrometheusRule YAML string
    canAdvance(step, config) -> bool (wizard step gate)

  Closure deps: none. Pure functions; receive config as args.
---

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

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).

export { isValidAlertName, isValidGroupName, isValidLabelKey, isValidThreshold, buildYaml, canAdvance };
