---
title: "ADR-015: wizard.jsx design token migration adopts Option A (full Tailwind arbitrary value rewrite)"
tags: [adr, design-tokens, phase-a0, v2.7.0]
audience: [frontend-developers, maintainers]
version: v2.7.0
lang: en
---

# ADR-015: wizard.jsx design token migration adopts Option A (full Tailwind arbitrary value rewrite)

> **Language / 語言：** **English (Current)** | [中文](./015-wizard-arbitrary-value-token-migration.md)

> Originally recorded as **DEC-A** in `docs/internal/v2.7.0-planning.md §15`
> (Day 2, 2026-04-16 AM). Back-filled during Day 5 retrospective review —
> DEC-A shaped the token-migration pattern used by every subsequent Phase .a0
> batch (including Day 4 batch 4), so it deserved an ADR from day one.

## Status

✅ **Accepted** (v2.7.0 Day 2, 2026-04-16) — Implementation in commit `ec07914`
`refactor(jsx): migrate wizard.jsx core palette to design tokens (Phase .a0 DEC-A)`;
landed 69 occurrences rewritten, 19 state-specific colors explicitly retained.

## Background

wizard.jsx (`docs/getting-started/wizard.jsx`, ~900 LOC at the time) was the
"largest design system decoupling case" discovered during Day 1 critique:
**0% token adoption**, 100% Tailwind core palette (slate/blue/emerald/amber/red)
hardcoded, flagship onboarding tool but completely ignoring `--da-color-*` tokens.

Phase .a0 needed to bring it into the token system, but three migration paths
involved different trade-offs:

- **A**. Full Tailwind → `bg-[color:var(--da-color-*)]` arbitrary value rewrite
       → **Keep Tailwind syntax, but actually consume tokens**.
- **B**. Migrate key elements (Role card / Option card / Primary button / Progress bar / GlossaryTip),
       retain Tailwind core palette for others.
- **C**. Defer to v2.8.0 Master Onboarding rewrite.

## Decision Drivers

1. wizard is the flagship; **using it as a "new pattern" example** has the highest value
2. The true power of `var(--da-color-*)` is automatic token swapping on `[data-theme="dark"]`,
   while Tailwind `dark:` variant requires double-ups for every class → Option A naturally wins
   after DEC-F selects single-track `[data-theme]` routing
3. Option B leaving half Tailwind results in "looks migrated but dark mode still broken" →
   quality debt becomes more hidden
4. Option C defers the entire onboarding core work; risk unacceptable

## Decision

**Adopt Option A**: full rewrite with `bg-[color:var(--da-color-*)]` / `text-[color:var(--da-color-*)]` /
`border-[color:var(--da-color-*)]` arbitrary values. Explicitly allowed retention of
**19 state-specific colors** (e.g., `bg-blue-600` for active selection) must be annotated
with reasons, serving as a waiver list for future audits.

## Alternative Options Considered

| Option | Pros | Cons | Result |
|--------|------|------|--------|
| A | Single-track dark mode; full tokenization; reusable as template | Large initial rewrite; longer classNames | ✅ Selected |
| B | Quick | Hybrid Tailwind → dark mode/contrast drift hard to track | ❌ |
| C | Zero immediate work | wizard flagship loses template for downstream batches; version-deferral risk | ❌ |

## Consequences

### Positive
- **Establishes Phase .a0 standard migration pattern**: Day 3 deployment-wizard, Day 4 batch 4
  (rbac / cicd / threshold-heatmap) all directly follow Option A rewrite style and waiver annotations
- Dark mode cleanly aligns with DEC-F (`[data-theme]` single-track routing)
- New developers only need to inspect wizard.jsx diff to understand how to migrate the next tool

### Negative / Risks
- className strings average ~2x longer → readability degradation
- Arbitrary value CSS outputs bypass Tailwind tree-shake consolidation; **bundle size grows ~2–4%**
  (v2.7.0 defers optimization; Phase .e to evaluate postcss preset)
- **Day 5 runtime axe-core discovery: `--da-color-tag-bg` + `--da-color-muted` insufficient contrast
  not caused by Option A → token definition layer issue (TECH-DEBT-003), but Option A
  amplified impact surface (every step indicator using this token pair absorbs the same bug)**
- **Retrospective lesson**: Option A lets "token definition layer flaws" be absorbed by multiple tools,
  appearing as benefit of "complete UI fix at once", but in practice means "when tokens fail AA,
  systemic breakage scope also expands". Next version should run runtime contrast audit as
  token definition acceptance criterion.

## Effective Scope

- v2.7.0 Phase .a0 all JSX token migrations uniformly adopt Option A style (Option B not allowed)
- Waiver list documented in PR description; exceeding 20 waivers flagged as "incomplete rewrite" requiring review

## Cross-References

- `docs/getting-started/wizard.jsx` (implementation)
- Commit `ec07914` (Day 2 landed)
- DEC-F (ADR-016, paired dark mode single-track decision)
- DEC-G (`docs/internal/dev-rules.md` §S1, paired gray neutral color style rule)
- TECH-DEBT-003 (Day 5 discovered token-pair contrast issue, not directly caused by this ADR)
