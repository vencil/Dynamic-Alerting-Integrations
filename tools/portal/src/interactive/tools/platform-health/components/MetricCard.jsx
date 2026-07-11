---
title: "Platform Health — MetricCard"
purpose: |
  Compact KPI card with label / value / optional subtitle and an
  optional status tint (warning | error). Extracted from
  platform-health.jsx (da-portal ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): Tailwind palette classes →
  `--da-color-*` arbitrary values (threshold-heatmap pattern). Tint
  mapping mirrors component-health.jsx TIER_COLORS: warning →
  warning-soft bg + warning border; error → error-soft bg + error
  border; default → card-bg + card-border. Text: label/subtitle →
  muted, value → fg.

  NAMING: this `MetricCard` is namespaced to platform-health only. A
  same-named `MetricCard` also exists in multi-tenant-comparison and
  component-health; cross-tool convergence is out of scope for this
  wave (epic-level). Behavior contract: identical to the inline card.
---

function MetricCard({ label, value, subtitle, status }) {
  return (
    <div className={`p-3 rounded-lg border ${
      status === 'warning' ? 'bg-[color:var(--da-color-warning-soft)] border-[color:var(--da-color-warning)]' :
      status === 'error' ? 'bg-[color:var(--da-color-error-soft)] border-[color:var(--da-color-error)]' :
      'bg-[color:var(--da-color-card-bg)] border-[color:var(--da-color-card-border)]'
    }`}>
      <div className="text-xs text-[color:var(--da-color-muted)]">{label}</div>
      <div className="text-xl font-bold text-[color:var(--da-color-fg)]">{value}</div>
      {subtitle && <div className="text-xs text-[color:var(--da-color-muted)]">{subtitle}</div>}
    </div>
  );
}

export { MetricCard };
