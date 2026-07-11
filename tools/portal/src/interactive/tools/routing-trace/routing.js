---
title: "Routing Trace — Alertmanager route resolution"
purpose: |
  Pure routing-resolution algorithm + validators behind the routing trace tool.
  computeTrace walks child routes top-to-bottom; the first whose match labels are
  ALL satisfied by the alert labels wins, else the alert falls through to the
  default route's receiver. Returns { matchedRoute, receiver, reasons[] }.

  Pre-PR-portal-18 this lived inline in routing-trace.jsx (727 LOC) with 0%
  coverage — despite being the correctness-critical core (a wrong trace means an
  alert is shown routing to the wrong receiver). Extracted here for direct
  unit testing.

  Public API:
    computeTrace({ alert, defaultRoute, childRoutes }) -> { matchedRoute, receiver, reasons }
    canAdvance(step, state)        -> bool (wizard step gate)
    isValidAlertName(s)            -> bool
    isValidLabelKey(s)             -> bool

  Closure deps: window.__t for bilingual reason lines (falls back to English).
---

// i18n fallback (moved with the cluster from routing-trace.jsx).
const t = window.__t || ((zh, en) => en);

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

// Legacy jsx-loader path: expose as window globals (see PR-portal-12 / TD-030z).

export { isValidAlertName, isValidLabelKey, computeTrace, canAdvance };
