---
title: "ADR-016: Migrate comprehensively to `[data-theme]` single-track dark mode, remove Tailwind `dark:` variant"
tags: [adr, design-tokens, dark-mode, phase-a0, v2.7.0]
audience: [frontend-developers, designers, maintainers]
version: v2.7.0
lang: en
---

# ADR-016: Migrate comprehensively to `[data-theme]` single-track dark mode, remove Tailwind `dark:` variant

> Originally recorded as **DEC-F = Option C** in `docs/internal/v2.7.0-planning.md §17`
> (Day 3, 2026-04-16, finalized after two rounds of user discussion). Back-filled during Day 5
> retrospective review — DEC-F is a prerequisite upon which all subsequent Phase .a0 token
> migrations depend, warranting an independent ADR.

## Status

✅ **Accepted** (v2.7.0 Day 3, 2026-04-16) — implemented in `deployment-wizard.jsx`
migration (commit `8634ea2`), all 83 `dark:` variants removed. Subsequent batch 4
(rbac / cicd / threshold-heatmap) follows the same rule.

## Background

Before v2.6.x, portal JSX had two concurrent dark mode mechanisms:

1. **Tailwind `dark:` variant** (class-based): `<div class="bg-white dark:bg-slate-900">`
2. **design-tokens.css `[data-theme="dark"]` attribute**: tokens automatically swap colors
   `:root { --da-color-bg: #fff } [data-theme="dark"] { --da-color-bg: #0b1220 }`

The two mechanisms **lacked integration**: toggling the class did not toggle the attribute, and vice versa. During Day 2 critique
of `deployment-wizard.jsx`, we confirmed this causes:

- Partially migrated components: token-based nodes clash with `dark:` nodes (white background tooltip paired with dark text)
- Systemic maintenance burden: every new color must be synchronized in both Tailwind classes and tokens
- Root causes of dark mode toggle bugs are nearly impossible to bisect

## Decision Drivers

1. `[data-theme]` is the SSOT (Single Source of Truth) for design-tokens.css; if we want tokens to have
   substantive value, we cannot have a second parallel system
2. DEC-A (Option A arbitrary-value rewrite) ingests all colors into tokens, making `dark:` a vestigial remnant
3. The only justification for maintaining dual tracks is "familiarity with existing tailwind patterns" — this is
   outweighed by system consistency

## Decision

**Choose Option C**: Migrate comprehensively to `[data-theme="dark"]` attribute-based dark mode,
**remove all Tailwind `dark:` variants**.

Implementation requirements:
1. Any new JSX must not use the `dark:` prefix (pre-commit lint to be added later)
2. During Phase .a0 token migration, delete `dark:xxx` directly (color swapping is handled by tokens)
3. Remove `darkMode` configuration from `tailwind.config` (if enabled)
4. `jsx-loader` theme switch: `document.documentElement.setAttribute('data-theme', 'dark')`
   (do not toggle class `dark`)

## Considered Alternatives

| Option | Description | Decision |
|---|---|---|
| A | Tailwind config `darkMode: ['class', '[data-theme="dark"]']`: enable both systems simultaneously | ❌ Parallel complexity does not decrease |
| B | `jsx-loader` simultaneously toggles `<html class="dark">` and `data-theme="dark"` | ❌ Patch-like; does not solve dual-source problem between tokens and classes |
| **C** | Migrate entirely to `[data-theme]`, remove `dark:` | ✅ Chosen; cleanest solution |

In the first round of user inquiry, they requested careful comparison of A/B/C pros and cons → I provided
3-way pros/cons analysis → user selected C.

## Consequences

### Positive
- **After Phase .a0, all migration tool dark mode behavior becomes predictable**: switch `data-theme` in one place,
  entire UI remains consistent
- New developers will not accidentally use `dark:`
- Day 4 batch 4 (rbac/cicd/threshold-heatmap) saves approximately 30–40% per-component rewrite time
  (no need to dual-write colors)

### Negative / Risks
- **Existing tools not yet migrated will have dark mode gaps**: for example, config-lint still retains
  some `dark:`, so theme switching presents partial UI → listed as Phase .a0 closure acceptance criterion
- **Retrospective discovery**: Day 5 runtime axe scan of threshold-heatmap revealed
  `bg-red-500 text-white` hard-coded palette **without tokens, hence without dark mode color swap**,
  which DEC-F cannot help with (TECH-DEBT-005). That is: DEC-F solved the **dual-track problem for screens with tokens**,
  but did not solve "palette remnants that never enter the dark pipeline in the first place"
- Must add `grep 'dark:' docs/**/*.jsx` verification at Phase .a0 closure, otherwise `dark:` remnants
  will be overlooked in subsequent code reviews

## Scope of Effect

- From v2.7.0 Phase .a0 onward, new JSX must not use `dark:` variant
- Existing tools must remove `dark:` during their respective Phase .a0 migration PR
- Before Phase .a0 closure, must pass `grep -r 'dark:' docs/getting-started docs/interactive/tools` returning empty

## References

- Commit `8634ea2` (Day 3 deployment-wizard migration, first landing)
- ADR-015 (DEC-A / Option A) — together they form the Phase .a0 standard migration toolkit
- DEC-G (`docs/internal/dev-rules.md` §S1, gray neutral color style rule) — paired convention finalized the same day
- TECH-DEBT-005 (palette remnants causing dark mode gap)
- Retrospective: `docs/internal/v2.7.0-day1to3-retrospective-review.md §3.3`
