---
title: "ADR-014: Tech-Debt Category and REG Budget Isolation"
tags: [adr, governance, tech-debt, regressions, budget, v2.7.0]
audience: [platform-engineers, tech-leads]
version: v2.7.0
lang: en
---

# ADR-014: Tech-Debt Category and REG Budget Isolation

> **Language / 語言：** **English (Current)** | [中文](./014-tech-debt-category-budget-isolation.md)

> Originally recorded as **DEC-N** in `docs/internal/v2.7.0-planning.md §19`.
> This ADR supplements **governance guardrails** to prevent TECH-DEBT from becoming a loophole for the REG Budget.

## Status

✅ **Accepted** (v2.7.0 Day 4, 2026-04-16) — Already incorporated into `docs/internal/known-regressions.md`, with TECH-DEBT-001/002 serving as the first examples.

## Background

### REG Budget Mechanism (Existing)

`docs/internal/dev-rules.md` mandates that the number of active REGs ≤ 4% of test count (enforced by `make pr-preflight`). When exceeded, a "Budget Overrun" is triggered, PRs cannot merge, and existing REGs must be addressed or new requirements postponed.

### Motivation for the New Category

The v2.7.0 Phase .a A-3 batch3 survey discovered that cicd-setup-wizard and config-lint have zero aria labels. These are **not regressions** (never worked before), but rather **tech debt from inception lacking a11y coverage**.

If forced into the REG registry:
- Violates the REG definition (something that once worked but now is broken)
- Consumes REG Budget, squeezing space for genuine regressions
- Pollutes regression test history (appears as a new regression but is actually old debt)

If not tracked at all:
- Knowledge gets lost in commit messages
- No way to display debt backlog in Dashboard / CHANGELOG
- No quantified observability of a11y coverage progress in Phase .a

## Decision Drivers

- **Traceable** (has id + severity + reproduction steps like REGs)
- **No Budget Impact** (calculated independently from REGs)
- **Upgrade Path Available** (can become REG if necessary; not a permanent exemption)
- **Time Bound** (cannot remain open across multiple minor versions without action)

## Decision

### Category Definition

Add a "Tech-Debt" section to `docs/internal/known-regressions.md` with the following specification:

| Attribute | REG | TECH-DEBT |
|---|---|---|
| `id` prefix | `REG-XXX` | `TECH-DEBT-XXX` |
| `first_observed` criterion | Broke in specific version | Never worked before |
| Budget impact | ✅ 4% limit | ❌ No impact |
| Requires regression_test | ✅ Mandatory | ❌ Optional (should add when fixed) |
| Blocks merge in `make pr-preflight` | ✅ | ❌ |

### Governance Guardrails (New Rules)

To prevent TECH-DEBT from becoming a loophole:

1. **Escalation Rule A — Impact Spread**: If a TECH-DEBT is reported by users 3+ times at the same severity level, it **must** be evaluated for escalation to REG. The evaluation is decided jointly by the Phase owner and maintainers, recorded in `docs/internal/dev-rules.md`.

2. **Escalation Rule B — Time Limit (Annealing)**: TECH-DEBT that remains untouched across **1 minor version** → forced triage (review meeting); across **2 minor versions** → **automatic escalation to REG** or marked as `wontfix` and archived. Implement via extending `make playbook-freshness` to `make tech-debt-freshness` (new Makefile target, added in v2.7.0 wrap-up).

3. **No Reverse Reclassification**: Existing REGs cannot be **downgraded** to TECH-DEBT to circumvent Budget. If incorrect classification is discovered, open a correction PR rather than reclassifying.

4. **Priority Ranking**: P1 TECH-DEBT fix priority equals P2 REG; neither higher nor lower.

### First Examples

- `TECH-DEBT-001`: cicd-setup-wizard 0 aria (P1, resolved Day 4)
- `TECH-DEBT-002`: config-lint 0 aria + no role=alert in error area (P2, open, planned v2.7.0)

## Rejected Alternatives

| Alternative | Rejection Reason |
|---|---|
| Expand REG Budget cap (4% → 6%) | Treats symptom, not root cause; blurs category definitions |
| Use GitHub Issues labels instead | Separates governance from repo-internal tracking; CI/Makefile cannot read |
| Put everything in REG + tag `is_original_debt: true` | Complicates schema; requires split Budget logic |
| Create separate `tech-debt.md` file | Overlaps governance logic with known-regressions.md; dual maintenance overhead |

## Consequences

### Positive

- REG Budget returns to its original purpose: tracking "genuine regressions"
- Debt backlog can be aggregated (e.g., "v2.7.0 closed 3 TECH-DEBTs")
- TECH-DEBT-001's same-day resolution pattern proves this category enables "rapid discovery → same-day fix" cycles

### Negative / Risks

1. **Budget Escape Hatch**: If governance rules loosen, REGs could be miscategorized as TECH-DEBT. **Mitigation**: Time limit in Escalation Rule B + no reverse reclassification rule.
2. **Learning Curve for New Contributors**: Must understand the difference between two categories. **Mitigation**: Add REG vs TECH-DEBT decision flow diagram at the top of `known-regressions.md` (added Day 5).
3. **Governance Rules Not Yet Automated**: Escalation Rule B currently relies on manual triage; `make tech-debt-freshness` not yet implemented. **Mitigation**: Implement the Makefile target before v2.7.0 release; if delayed, at least add a manual checklist to `docs/internal/dev-rules.md`.

## Related

- `docs/internal/known-regressions.md` (host definitions + examples)
- `docs/internal/dev-rules.md` §12 Branch + PR + future §13 TECH-DEBT treatment
- `docs/internal/v2.7.0-planning.md` §19 DEC-N + §20 Day 5 patch
