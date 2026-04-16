---
title: "ADR-012: threshold-heatmap Colorblind Accessibility Hotfix — Structured Severity Return Value"
tags: [adr, accessibility, wcag, portal, v2.7.0]
audience: [frontend-developers, design-system-maintainers]
version: v2.6.0
lang: en
---

# ADR-012: threshold-heatmap Colorblind Accessibility Hotfix — Structured Severity Return Value

> Originally recorded as **DEC-L (Sprint 0)** in `docs/internal/v2.7.0-planning.md §19`.
> The hotfix was deployed on Day 4 AM; this ADR preserves the decision context for future
> generalization to other color-dependent tools (platform-demo mode badges, health dashboard tier badges, etc.).

## Status

✅ **Accepted** (v2.7.0 Day 4 Sprint 0, 2026-04-16) — hotfix landed; subsequent runtime WCAG verification CI-gated.

## Background

### Defect in Original Implementation

In the `threshold-heatmap.jsx` v2.6.0 implementation, cell severity was expressed through a three-color palette
(green-200 / yellow-200 / orange-200 / red-500). This violated **WCAG 1.4.1 Use of Color**: information was conveyed
only through color, making it impossible for users with red-green color blindness to distinguish "medium" from "anomalous" values.

### Why Sprint 0 Rather Than Full Migration

1. Full palette-to-token migration (87 tailwind palette → 0) requires ~3 hours and would block the Phase .a0 batch 4 schedule
2. WCAG 1.4.1 is a legal compliance risk (AA requirement), so we prioritize **remedying semantics** while **deferring color token migration**
3. After the hotfix lands, threshold-heatmap's formal token migration remains a Phase .a0 closure task (§19 Day 5 candidate #5)

## Decision Drivers

- Must simultaneously satisfy both "visual symbols" and "screen reader" accessibility channels
- A single callsite (cell render) must be able to retrieve color class / symbol / tier label in one call, avoiding 3 parallel if-else trees
- Future tools (platform-demo badges) can directly re-import this helper

## Decision

Extract the cell severity determination logic into a `getCellSeverity(value, stats)` function that **returns a structured object**:

```jsx
function getCellSeverity(value, stats) {
  if (value > stats.p95) {
    return {
      colorClass: 'bg-red-500 text-white',
      symbol: '❌',
      tier: 'outlier',
      ariaLabel: t('Outlier', 'Outlier'),
    };
  }
  if (value > stats.mean + stats.stddev * 2) {
    return { colorClass: 'bg-orange-200 text-orange-900', symbol: '⚠⚠', tier: 'high', ariaLabel: t('High', 'High') };
  }
  if (value > stats.mean) {
    return { colorClass: 'bg-yellow-200 text-yellow-900', symbol: '⚠', tier: 'medium', ariaLabel: t('Medium', 'Medium') };
  }
  return { colorClass: 'bg-green-200 text-green-900', symbol: '✓', tier: 'low', ariaLabel: t('Low', 'Low') };
}
```

### Rendering Side

- cell outer: `<td aria-label={ariaLabel} className={colorClass}>`
- cell inner: `<span aria-hidden="true">{symbol}</span>{value}`

This design ensures:
- **Screen readers** announce the `ariaLabel` ("Outlier 45.2"), preventing double-announcement of the symbol
- **Colorblind users** see a Unicode symbol instead of relying solely on color
- **Sighted users** see familiar traffic-light colors + symbol redundancy

## Rejected Alternatives

| Option | Rejection Reason |
|---|---|
| Add `aria-label` only, no symbol | Colorblind users cannot distinguish on screen |
| Add symbol only, no `aria-label` | Screen readers announce "Warning Sign Warning Sign" (⚠⚠ dual-character) — poor UX |
| Use CSS `::before` to insert symbol | Some screen readers don't announce ::before content; CSS pseudo-elements cannot follow React state |
| Use shapes (square / triangle / circle) | Requires SVG or icon library; will break during future theming |

## Consequences

### Positive

- WCAG 1.4.1 **semantic compliance** achieved (hotfix level; runtime verification CI-gated)
- `getCellSeverity` helper reusable across other severity-badge tools
- Symbols and colors decoupled, can be swapped independently (e.g., use ▲ ● ■ instead)

### Negative / Risks

1. **Cross-platform font inconsistency**: ⚠⚠ width varies between Windows Consolas and macOS SF Mono, causing table misalignment. **Mitigation**: During formal Phase .a0 migration on Day 5, fix monospace font stack or switch to fixed-width Unicode blocks.
2. **Screen reader announcement of `⚠⚠`**: NVDA / VoiceOver announces "Warning Sign Warning Sign". **Mitigation**: This hotfix relies on `ariaLabel` to override, but future callsites placing symbols directly will need reminding.
3. **Dark mode contrast unverified**: Hotfix uses Tailwind palette (not migrated to tokens), which may fail contrast checks under `[data-theme="dark"]`. **Mitigation**: Sync contrast calculations during formal Phase .a0 token migration.

### Further Tracking

- `docs/internal/v2.7.0-day5-verification-triage.md` §3 lists CI gate for runtime verification
- Phase .a0 Day 5 candidate #5: threshold-heatmap palette → token formal migration

## Related

- WCAG 2.1 — Success Criterion 1.4.1 Use of Color (Level A)
- `docs/interactive/tools/threshold-heatmap.jsx`
- `docs/internal/v2.7.0-planning.md` §19 DEC-L
- `docs/internal/v2.7.0-day5-verification-triage.md` §3
