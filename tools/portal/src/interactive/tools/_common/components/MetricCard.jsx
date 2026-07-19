---
title: "_common — MetricCard (shared KPI card)"
purpose: |
  Canonical compact KPI card: label / value / optional unit / optional
  subtitle, with an optional status tint (warning | error). Token-based
  (`--da-color-*` arbitrary values) so it re-themes in dark mode.

  Promoted to _common from platform-health/components/MetricCard.jsx
  (portal deferred-followups Phase B) as the convergence base for the
  MetricCard family. Tint mapping mirrors component-health TIER_COLORS:
  warning → warning-soft bg + warning border; error → error-soft bg +
  error border; default → card-bg + card-border. Text: label/subtitle
  → muted, value → fg, unit → muted (rendered only when passed, so a
  caller that omits `unit` gets byte-identical output to the pre-promotion
  platform-health card).

  Props: { label, value, unit?, subtitle?, status? }.
---

import React from 'react';

function MetricCard({ label, value, unit, subtitle, status }) {
  return (
    <div className={`p-3 rounded-lg border ${
      status === 'warning' ? 'bg-[color:var(--da-color-warning-soft)] border-[color:var(--da-color-warning)]' :
      status === 'error' ? 'bg-[color:var(--da-color-error-soft)] border-[color:var(--da-color-error)]' :
      'bg-[color:var(--da-color-card-bg)] border-[color:var(--da-color-card-border)]'
    }`}>
      <div className="text-xs text-[color:var(--da-color-muted)]">{label}</div>
      <div className="text-xl font-bold text-[color:var(--da-color-fg)]">
        {value}{unit ? <span className="text-sm font-normal text-[color:var(--da-color-muted)] ml-1">{unit}</span> : null}
      </div>
      {subtitle && <div className="text-xs text-[color:var(--da-color-muted)]">{subtitle}</div>}
    </div>
  );
}

export { MetricCard };
