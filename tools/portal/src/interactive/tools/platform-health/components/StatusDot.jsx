---
title: "Platform Health — StatusDot"
purpose: |
  Small colored dot indicating a component / tenant status. Extracted
  from platform-health.jsx (da-portal ROI refactor Wave 5b).

  Design-token migration (ADR-014 / DEC-A): hardcoded Tailwind palette
  classes replaced with arbitrary-value `--da-color-*` references, per
  the threshold-heatmap.jsx pattern. Semantic mapping by VISUAL intent:
  healthy/normal → success (green), degraded/maintenance → warning
  (yellow), down → error (red), silent → muted (neutral), unknown →
  surface-border (light neutral). Note: the mode-* tokens are NOT used
  here — they encode maintenance=red / silent=amber, which would flip
  this dashboard's benign yellow/gray intent for those states.

  Behavior contract: identical to the inline StatusDot; same status→dot
  color semantics preserved.
---

function StatusDot({ status }) {
  const colors = {
    healthy: 'bg-[color:var(--da-color-success)]',
    degraded: 'bg-[color:var(--da-color-warning)]',
    down: 'bg-[color:var(--da-color-error)]',
    normal: 'bg-[color:var(--da-color-success)]',
    maintenance: 'bg-[color:var(--da-color-warning)]',
    silent: 'bg-[color:var(--da-color-muted)]',
  };
  return (
    <span className={`inline-block w-2.5 h-2.5 rounded-full ${colors[status] || 'bg-[color:var(--da-color-surface-border)]'}`} />
  );
}

export { StatusDot };
