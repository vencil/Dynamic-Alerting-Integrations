---
title: "ADR-012: threshold-heatmap Colorblind Accessibility Hotfix — Structured Severity Return Value"
tags: [adr, accessibility, wcag, portal, v2.7.0]
audience: [frontend-developers, design-system-maintainers]
version: v2.9.0
lang: en
---

# ADR-012: threshold-heatmap Colorblind Accessibility Hotfix — Structured Severity Return Value

> **Language / 語言：** **English (Current)** | [中文](./012-colorblind-hotfix-structured-severity-return.md)

## Status

✅ **Accepted** (v2.7.0, 2026-04-16)

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged. Cell colors have moved from the Tailwind palette to design tokens (`--da-color-*`); the structure returned by the severity function is unaffected.

## Decision

**Each threshold-heatmap cell expresses its severity through three channels at once: color, a Unicode symbol, and a text label for screen readers.** All three are decided by one function, `getCellSeverity`, which returns a structured object, instead of three parallel if-else trees on the render side.

[WCAG 1.4.1 "Use of Color"](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html) (Level A) requires that information not be conveyed by color alone. The symbol lets colorblind users tell the tiers apart on screen; the text label lets screen readers announce the tier; color is reinforcement only.

### Example: cell value → tier, symbol, announced text

`getCellSeverity(value, min, max, percentile95)` first checks whether the value is above P95 (the 95th percentile of all cell values); otherwise it tiers by the value's ratio within min–max, and returns `{ colorClass, symbol, tier }`:

```jsx {.quote src="tools/portal/src/interactive/tools/threshold-heatmap.jsx"}
function getCellSeverity(value, min, max, percentile95) {
  if (value === null || value === undefined) {
    return { colorClass: 'bg-[color:var(--da-color-surface)]', symbol: '', tier: 'none' };
  }

  // Flag outliers first (>P95)
  if (value > percentile95) {
    return {
      colorClass: 'bg-[color:var(--da-color-error)] text-[color:var(--da-color-card-bg)] font-bold',
      symbol: '\u274C', // ❌
      tier: 'outlier',
    };
  }

  const ratio = max === min ? 0 : (value - min) / (max - min);

  if (ratio < 0.33) {
// ...
```

Running the function with min = 10, max = 90, P95 = 80, together with `tierLabel(tier)`, which turns `tier` into Chinese/English text, gives:

| Cell value | Ratio | `tier` | Symbol | Announced tier text |
|---|---|---|---|---|
| 20 | 0.125 | `low` | ✓ | 低 / Low |
| 45 | 0.44 | `medium` | ⚠ | 中 / Medium |
| 70 | 0.75 | `high` | ⚠⚠ | 高 / High |
| 85 | — | `outlier` | ❌ | 異常值 / Outlier |
| no data | — | `none` | (none) | (none) |

The render side puts the text label on the `<td>` and hides the symbol with `aria-hidden="true"`, so screen readers announce the label only and do not read the symbol as well:

```jsx {.quote src="tools/portal/src/interactive/tools/threshold-heatmap.jsx"}
                        const { colorClass, symbol, tier } = getCellSeverity(
                          value, stats.min, stats.max, stats.p95
                        );
                        const valueText = value ? value.toFixed(0) : '—';
                        const tierText = tierLabel(tier);
                        const ariaLabel = value
                          ? `${tenant} ${metric}: ${value.toFixed(2)}${tierText ? ', ' + tierText : ''}`
                          : `${tenant} ${metric}: ${t('無資料', 'no data')}`;
// ...
                            aria-label={ariaLabel}
                            role="gridcell"
                          >
                            {symbol && (
                              <span aria-hidden="true" className="mr-1 opacity-90">{symbol}</span>
                            )}
                            {valueText}
```

So a cell with value 70 shows `⚠⚠70` on screen, and a screen reader announces "`<tenant> <metric>: 70.00, High`".

min, max and P95 are computed over **all** cells on screen together (the selected tenants × every metric of the selected Rule Packs), not per metric; the tool shows sample data generated from Rule Pack defaults plus random variance.

## Background

In v2.6.0, threshold-heatmap expressed severity only through cell background color (green / yellow / orange / red). That violated WCAG 1.4.1: users with red-green color blindness could not tell "medium" from "outlier", and screen-reader users got no tier information at all.

The fix was deliberately split in two: first remedy the semantics (add symbols and text labels), and move colors to design tokens later as separate work, because the latter was much larger in scope while the former was a Level A compliance gap. The token migration has since been completed; the decision in this ADR is unaffected.

Requirements at design time:

- Both the visual (symbol) and screen-reader (text) channels must be served.
- The render side gets color, symbol and tier in one call, without maintaining three parallel decision trees.
- Symbols and colors are decoupled, so the symbols can later be swapped (for example ▲ ● ■) without touching colors.

## Consequences and Known Limitations

### What we get

- Every cell with a value has a non-color representation of its tier: the on-screen symbol and the announced tier text.
- The "symbol + text, never color alone" pattern has been extended elsewhere, each with its own test:
  - Grafana dashboard `k8s/03-monitoring/fleet-threshold-distribution.json`, its sample-adequacy and Outliers panels; `tests/dx/test_fleet_threshold_dashboard.py` checks that every color tier has a symbol.
  - Grafana dashboard `k8s/03-monitoring/federation-revocation-dashboard.json`, its stat panels; `tests/dx/test_federation_revocation_dashboard.py` makes the same check.
  - The portal's `recipe-builder.jsx` shows its verdicts as a shape glyph (`aria-hidden`) + word + color.
- What was extended is the pattern, not the function: `getCellSeverity` is not exported and is currently used only inside `threshold-heatmap.jsx`.

### Acceptance and its tests

- `colorblind-safe severity symbols are rendered (ADR-012)` in `tests/e2e/threshold-heatmap.spec.ts`: after the tool loads, at least one severity symbol (✓ / ⚠ / ❌) must be on screen. It checks only that symbols exist, not the mapping from value to tier; the legend itself contains ✓, so it passes even if no cell renders a symbol.
- The same spec's smoke check runs axe-core's WCAG 2 A / AA rules: zero Critical violations, at most one non-Critical violation tolerated.
- These e2e tests run on every PR in `Smoke Tests (Chromium)`.
- The tier boundaries of `getCellSeverity` have no unit test.

### Known limitations

1. **Some cells have insufficient text contrast in the light theme.** Computed from the token values in `docs/assets/design-tokens.css`: the text of medium and high cells (`--da-color-warning` on `--da-color-warning-soft`) is about 2.1:1, and outlier cells (white on `--da-color-error`) about 3.8:1, both below the 4.5:1 AA requires for normal text. The same file already has `--da-color-warning-text` designed for this (annotated 6.1:1), which the cells do not use yet. In the dark theme the same pairs are about 9.0:1 and 5.3:1 (low cells are above 5:1 in both themes). The e2e axe check runs only the default theme and does not cover the dark theme.
2. **Medium and high share the same background** (both the warning token); they are told apart only by the symbols ⚠ and ⚠⚠ and by the announced text.
3. **The legend and the actual tiers disagree.** The legend says "High (66–85%)", but cells with a ratio of 0.85 or more that are not above P95 are also `high`.
4. **A cell whose value is 0 displays inconsistently.** The render side tests `value ? … : …` for "has data", so 0 shows as `—` and is announced as "no data", yet `getCellSeverity` still treats it as a value and draws a ✓; 0 is also skipped when computing min / max / P95.
5. **A new call site must uphold two things itself**: put the symbol in an element with `aria-hidden="true"`, and put the tier text where it will be announced. Without the former, a screen reader reads ⚠⚠ as "warning sign" twice; without the latter, the screen reader gets no tier.

## Alternatives Considered

| Option | Why not chosen |
|---|---|
| Add `aria-label` only, no symbol | Colorblind users still cannot tell the tiers apart on screen |
| Add symbol only, no `aria-label` | Screen readers read the symbol literally (⚠⚠ as "warning sign" twice) and cannot announce the tier name |
| Insert the symbol with CSS `::before` | Some screen readers do not announce `::before` content; CSS pseudo-elements also cannot follow React state directly |
| Use shapes (square / triangle / circle) | Needs SVG or an icon library, and theming then has to handle the shapes' colors separately |

## Related

- [WCAG 2.1 — Understanding 1.4.1 Use of Color](https://www.w3.org/WAI/WCAG21/Understanding/use-of-color.html)
- Implementation: `tools/portal/src/interactive/tools/threshold-heatmap.jsx` (`getCellSeverity`, `tierLabel`)
- Tests: `tests/e2e/threshold-heatmap.spec.ts`
- [ADR-014: wizard.jsx design token migration](014-wizard-arbitrary-value-token-migration.en.md) — the migration approach for the tokens the cell colors use
- [ADR-015: `[data-theme]` Single-track Dark Mode](015-data-theme-single-track-dark-mode.en.md) — how the dark theme is switched
