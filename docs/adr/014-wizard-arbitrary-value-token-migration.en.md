---
title: "ADR-014: wizard.jsx design token migration adopts Option A (full Tailwind arbitrary value rewrite)"
tags: [adr, design-tokens, v2.7.0]
audience: [frontend-developers, maintainers]
version: v2.9.0
lang: en
---

# ADR-014: wizard.jsx design token migration adopts Option A (full Tailwind arbitrary value rewrite)

> **Language / 語言：** **English (Current)** | [中文](./014-wizard-arbitrary-value-token-migration.md)

## Status

✅ **Accepted** (v2.7.0, 2026-04-16).

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

**When a portal JSX tool moves to design tokens, the whole tool is migrated at once: every color class is rewritten as a Tailwind arbitrary value (a class whose brackets hold a raw CSS value) that points at a `--da-color-*` token.** The forms are `bg-[color:var(--da-color-*)]`, `text-[color:var(--da-color-*)]` and `border-[color:var(--da-color-*)]`; `ring-` and other color classes follow the same pattern. This is the title's Option A (the three options are under "Alternatives considered").

- No hardcoded Tailwind palette classes (`bg-blue-600`, `text-slate-700` and the like), and no Tailwind `dark:` variants. The dark theme is handled by tokens changing value under `[data-theme="dark"]`; see [ADR-015](015-data-theme-single-track-dark-mode.en.md).
- Migrating only key elements while leaving the rest on palette classes is not adopted: every tool is rewritten in full.
- A state color with no matching semantic token yet (for example the green for "done" or the amber for "priority") may stay as a Tailwind class for now, but the reason must be written in a JSX comment, so the comments double as a to-do list for when the token is added.

`tools/portal/src/getting-started/wizard.jsx` (the getting-started wizard) was the first tool migrated this way and is the template for later ones. It now contains no hardcoded Tailwind palette classes; the state colors that were temporarily kept now use semantic tokens (`--da-color-success`, `--da-color-warning`, `--da-color-semantic-other` and others).

### Example: the step indicator dot

Before the rewrite, the "current step" dot was `bg-blue-600`, fixed at `#2563eb` and unchanged in the dark theme. After the rewrite, wizard.jsx's step indicator reads:

```jsx {.quote src="tools/portal/src/getting-started/wizard.jsx"}
        className={`flex-shrink-0 flex items-center justify-center w-10 h-10 rounded-full font-bold transition-all ${
          i < step
            ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)]"
            : i === step
              ? "bg-[color:var(--da-color-accent)] text-[color:var(--da-color-accent-fg)] ring-4 ring-[color:var(--da-color-focus-ring)]"
              : "bg-[color:var(--da-color-surface-border)] text-[color:var(--da-color-muted)]"
```

`--da-color-accent` has one value per theme in `docs/assets/design-tokens.css`:

```css {.quote src="docs/assets/design-tokens.css"}
:root {
// ...
  --da-color-accent:          #2563eb;
// ...
[data-theme="dark"] {
// ...
  --da-color-accent:          #60a5fa;
```

Result: in the light theme the dot is still `#2563eb`, the same as before the rewrite; when the page switches to `[data-theme="dark"]` the dot becomes `#60a5fa` automatically, and the JSX needs no extra classes for the dark theme.

## Background

wizard.jsx is the portal's getting-started wizard, the first tool a new user sees. Before the migration it used no `--da-color-*` tokens at all; every color was a hardcoded Tailwind palette class (slate / blue / emerald / amber / red), so its colors did not change in the dark theme.

There are two ways to make it follow the theme: add a `dark:` variant next to every color class (writing each class twice), or have the classes consume tokens that change value with the theme. [ADR-015](015-data-theme-single-track-dark-mode.en.md) chose to switch themes through `[data-theme]` alone, which makes the `dark:` route unnecessary.

What remained was the scope of the migration. There were three options:

- **A**: rewrite every color class as an arbitrary value pointing at a token. Keeps the Tailwind style but actually consumes tokens.
- **B**: migrate only key elements (role cards, option cards, primary buttons, progress bar, glossary tooltips) and leave the rest on palette classes.
- **C**: defer to the next release's rewrite of the whole onboarding flow.

The wizard is the front door of onboarding, so using it to demonstrate the new style was worth the most; A, the most complete option, was chosen.

## Consequences and known limitations

- **Longer classNames.** Each color class goes from `bg-blue-600` to `bg-[color:var(--da-color-accent)]`, which is harder to read.
- **A defect in a token definition spreads further.** Every tool consumes the same tokens, so one low-contrast token pair shows up everywhere that pair is used. Example: `--da-color-muted` text on a `--da-color-tag-bg` background once had insufficient contrast, and every place using that pair failed together. That was a token-definition problem; the current definitions meet WCAG AA in both the light and dark themes, but it shows the shape of the risk. The upside scales the same way: fix the token once and every tool is fixed.
- **No contrast acceptance check on token pairs.** Contrast is currently measured per tool on the rendered page: `tests/e2e/wizard.spec.ts` runs axe-core (automated accessibility checks) with the WCAG 2 A / AA rules, and any violation fails the test. It only measures the wizard's first step in the default theme; later steps and the dark theme are not measured. Separately, the `design-token-usage` lint blocks new uses of the saturated `--da-color-error` / `--da-color-warning` tokens as text color.
- **Mechanical enforcement is partial.** The pre-commit `design-token-usage` hook (which only looks at lines added in the current diff) blocks new hardcoded hex colors and new `slate-*` classes (rule in [dev-rules S1](../internal/dev-rules.md)); other palettes (`blue-*`, `emerald-*`, `amber-*`, `red-*`, `gray-*` and so on) have no lint and rely on review. A good number of older tools still contain hardcoded palette classes, as not-yet-migrated backlog.
- **A misspelled token name is not caught.** When `var(--da-color-…)` names a token that does not exist, that color simply is not applied on screen, with no error. The `undefined-tokens-check` hook that looks for undefined tokens currently scans only files under `docs/`, while the JSX sources live in `tools/portal/src/`, so a misspelling in JSX is outside its reach.

## Alternatives considered

| Option | Pros | Cons | Result |
|---|---|---|---|
| A: rewrite everything as arbitrary values pointing at tokens | Dark theme through tokens alone; the whole tool consumes tokens; reusable as a template for other tools | Large initial rewrite; longer classNames | ✅ Adopted |
| B: migrate only key elements | Quick | Half the classes stay hardcoded: it looks migrated but the dark theme is still broken, contrast differences are hard to track, and the quality debt is better hidden | ❌ |
| C: defer to the next release's rewrite | No work now | The core onboarding work slips a whole release; later tools lose their template | ❌ |

## Related

- `tools/portal/src/getting-started/wizard.jsx` — the first tool migrated under this ADR
- `docs/assets/design-tokens.css` — the `--da-color-*` token definitions (light `:root`, dark `[data-theme="dark"]`)
- [ADR-015: `[data-theme]` Single-track Dark Mode](015-data-theme-single-track-dark-mode.en.md) — the paired dark-theme decision
- [dev-rules S1](../internal/dev-rules.md) — no `slate-*` classes for neutrals, and its exception
