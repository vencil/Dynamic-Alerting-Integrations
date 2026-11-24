---
title: "ADR-015: Migrate comprehensively to `[data-theme]` single-track dark mode, remove Tailwind `dark:` variant"
tags: [adr, design-tokens, dark-mode, v2.7.0]
audience: [frontend-developers, designers, maintainers]
version: v2.9.0
lang: en
---

# ADR-015: Migrate comprehensively to `[data-theme]` single-track dark mode, remove Tailwind `dark:` variant

> **Language / 語言：** **English (Current)** | [中文](./015-data-theme-single-track-dark-mode.md)

## Status

✅ **Accepted** (v2.7.0, 2026-04-16)

Revised 2026-05-13: the JSX source moved to `tools/portal/src/`.

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

The portal's JSX tools have exactly one dark mode mechanism: the `data-theme` attribute on `<html>`. All light/dark color switching is done by the tokens (CSS variables) in `docs/assets/design-tokens.css`. **JSX does not use Tailwind's `dark:` variant** (for example `dark:bg-slate-900`).

The rules:

1. JSX does not use the `dark:` prefix.
2. Colors are written as Tailwind arbitrary values that reference a token (a class with a raw CSS value in square brackets, such as `bg-[color:var(--da-color-surface)]`). When migrating an old component, its `dark:xxx` classes are deleted outright, not rewritten into something else.
3. Switching the theme means changing `<html data-theme="…">`; components never check whether the current theme is light or dark.
4. The Tailwind configuration does not enable `darkMode`.
5. When switching themes, the loader sets only `data-theme` and does not add the class `dark`.

The current loader does not yet meet rules 4 and 5; see "Known limitations".

### Example: one class, recolored by `data-theme`

The component writes its background once, referencing `--da-color-surface`:

```jsx {.quote src="tools/portal/src/interactive/tools/master-onboarding.jsx"}
<div className="border border-[color:var(--da-color-surface-border)] rounded-lg p-5 bg-[color:var(--da-color-surface)]">
```

That token has a light and a dark value in `design-tokens.css`; the dark value sits under `[data-theme="dark"]`:

```css {.quote src="docs/assets/design-tokens.css"}
:root {
// ...
  --da-color-surface:         #ffffff;
// ...
[data-theme="dark"] {
// ...
  --da-color-surface:         #1e293b;
```

Result: with `<html data-theme="light">` the card's background is `#ffffff`; with `<html data-theme="dark">` it is `#1e293b`. The JSX does not change at all, and no second class is needed.

The theme is set by `docs/assets/jsx-loader.html`: the user cycles between Light / Dark / System, the choice is stored in `localStorage` under `__da_theme_pref`, and when nothing is stored the OS `prefers-color-scheme` setting is followed.

## Background

Before this decision, portal JSX had two dark mode mechanisms at once:

1. **Tailwind's `dark:` variant**: driven by whether `<html>` carries the class `dark`, e.g. `<div class="bg-white dark:bg-slate-900">`.
2. **`[data-theme="dark"]` in `design-tokens.css`**: driven by an attribute on `<html>`; tokens switch values automatically.

There was no bridge between them: toggling the class did not toggle the attribute, and vice versa. The problems this caused:

- In a half-migrated component, token-based nodes and `dark:` nodes did not match, e.g. a white tooltip with dark text.
- Every new color had to be written twice, once as a Tailwind class and once as a token.
- When dark mode switching broke, it was hard to tell which mechanism was at fault.

Why a single track:

1. `design-tokens.css` is the one place colors are defined (Single Source of Truth); a second, parallel switching mechanism would make the tokens pointless.
2. [ADR-014](014-wizard-arbitrary-value-token-migration.en.md) decided to rewrite colors as token-referencing arbitrary values; once colors live in tokens, `dark:` is only leftover.
3. The only benefit of keeping two tracks is "everyone knows the Tailwind idiom", which is worth less than overall consistency.

## Consequences and Known Limitations

### Positive

- On screens that use tokens, dark mode depends on `data-theme` alone, and the whole screen stays consistent.
- A new component writes each color once; there is no second copy for dark.

### Known limitations

- **Colors without a token do not switch.** Tailwind's built-in palette classes (such as `bg-red-500 text-white`) bypass the tokens and keep the same color in the dark theme. Many such classes remain in the JSX under `tools/portal/src/`, e.g. status colors and step-completed colors. This decision only solves "two mechanisms side by side on screens that have tokens", not "colors that never entered the tokens"; the latter is tracked separately. The `design-token-usage` lint looks only at added lines and catches hex color codes, px values in styles, the error/warning tokens used as foreground color, and `slate-*` classes; other palette classes are not caught.
- **The loader does not yet meet rules 4 and 5.** The current `jsx-loader.html` sets `tailwind.config = { darkMode: 'class' }` and, when switching to dark, adds both the class `dark` and `data-theme="dark"`, because the loader's own chrome (back link, theme button, loading spinner) is styled with `html.dark` selectors.

  ```html {.quote src="docs/assets/jsx-loader.html"}
      document.documentElement.classList.toggle('dark', isDark);
      document.documentElement.setAttribute('data-theme', isDark ? 'dark' : 'light');
  ```

  This has no effect on the JSX tools, since the JSX contains no `dark:` variants. It does mean that if someone writes `dark:` into JSX again, it will take effect on screen and the problem will not show.
- **Nothing enforces "no `dark:`".** No lint blocks `dark:`; review is the only guard. To check the current state, run from the repo root:

  ```bash
  grep -rnE '\bdark:[a-z]' tools/portal/src --include='*.jsx'
  ```

  It should print nothing. Comments that mention `dark:` (e.g. noting that a component removed its `dark:` classes) do not count as violations; the pattern above only matches `dark:` immediately followed by a class name.

## Considered Alternatives

| Option | Description | Outcome |
|---|---|---|
| Both active at once | Tailwind config `darkMode: ['class', '[data-theme="dark"]']`, so `dark:` also follows the attribute | Rejected: both mechanisms still coexist; complexity does not drop |
| Loader keeps both in sync | The loader sets `<html class="dark">` and `data-theme="dark"` together when switching | Rejected: a patch; tokens and classes remain two sources |
| **`[data-theme]` only** | Remove every `dark:` from JSX; tokens do the switching | **Chosen**: a single source |

## References

- [ADR-014: Wizard Token Arbitrary-Value Migration Strategy](014-wizard-arbitrary-value-token-migration.en.md) — how colors are rewritten as tokens; used together with this ADR
- Implementation: `docs/assets/design-tokens.css` (light and dark token values), `docs/assets/jsx-loader.html` (theme switching), `tools/portal/src/` (JSX source)
