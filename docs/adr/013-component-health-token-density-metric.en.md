---
title: "ADR-013: Component Health Scanner — Tier Scoring Algorithm and token_density Auxiliary Metric"
tags: [adr, metrics, component-health, design-tokens, tier-scoring, v2.7.0]
audience: [frontend-developers, platform-engineers, maintainers]
version: v2.7.0
lang: en
---

# ADR-013: Component Health Scanner — Tier Scoring Algorithm and token_density Auxiliary Metric

> Consolidates two decisions:
> - **DEC-08** (Day 1, planning §10): Tier classification refactored from single `appears_in` signal to five-dimensional weighted scoring
> - **DEC-M** (Day 4, planning §19): New `token_density` auxiliary metric introduced
>
> Both changes apply to `scripts/tools/dx/scan_component_health.py`, merged into a single ADR to avoid fragmentation.

## Status

✅ **Accepted** (DEC-08 Day 1 + DEC-M Day 4, v2.7.0, 2026-04-16)

## Background

### Problem 1: Single-Signal Tier Classification Inaccuracy (DEC-08)

The v2.6.x scanner relied solely on `appears_in` (number of documents referencing a tool) for Tier determination:
- tenant-manager referenced in 6 documents → Tier 1
- A portal tool with similar complexity but documented in only 1 file → Tier 3

This resulted in **independent, high-value portal tools being systematically underestimated**. Tier classification directly impacts Phase .a0 migration ordering and regression budget allocation.

### Problem 2: Group A/B/C Lacks Migration Completion Precision (DEC-M)

During Phase .a0 batches 3/4, the `token_group` classification (A/B/C = ≥80 / ≥20 / <20 tokens) proved too coarse:
- 80 tokens + 0 palette → Group A
- 80 tokens + 20 palette → Still Group A, but residual palette is significant

## Decision

### Part 1: Five-Dimensional Weighted Tier Scoring (DEC-08)

```python
score = (
    w_loc * loc_signal       # LOC 0–3 (≥800=3, ≥400=2, ≥150=1, <150=0)
  + w_aud * audience_signal  # Audience 0–2 (multi-persona=2, team-internal=1, single=0)
  + w_jp  * journey_signal   # Journey Phase 0–2 (onboarding=2, operate=1, explore=0)
  + w_wr  * writer_signal    # Writer capability 0–2 (domain-expert=2, agent=1, unknown=0)
  + w_rec * recency_signal   # Recency -1~+1 (last_touched ≤6mo=+1, ≤12mo=0, >12mo=-1)
)
```

All `w_*` weights are currently set to **1** (equal weighting).

**Tier thresholds**: ≥7 → Tier 1, 4–6 → Tier 2, ≤3 → Tier 3

**Deprecation override**: `LOC < 100 AND recency < 0` or `writer = 0 AND audience = 0` → Force mark as deprecation candidate, bypass normal Tier classification.

#### Rejected Alternatives

| Option | Rejection Reason |
|---|---|
| Retain `appears_in` single signal | Systematically underestimates independent portal tools |
| 10+ dimensions | Excessive maintenance cost; calibration requires regression data |
| ML classifier | Insufficient data (38 tools); no labeled training set |

### Part 2: token_density Auxiliary Metric (DEC-M)

```python
token_density = tokens / (tokens + palette_hits)   # Range [0.0, 1.0]
```

Output example:

```json
{
  "threshold-heatmap": {
    "token_count": 12, "palette_count": 87,
    "token_density": 0.121, "token_group": "C"
  }
}
```

#### Usage Guidelines

**✅ Appropriate**: Display in pre-commit / dashboard showing "nearly complete" status (density ≥ 0.9 and palette > 0); cross-tool comparison of migration completion.

**⛔ Inappropriate**: Replace `token_count` as primary metric (a 3-token tool with density=1.0 is not mature); enforce hard gates (penalizes tools with inherently fewer palettes); serve as sole basis for Group A/B/C classification.

#### Rejected Alternatives

| Option | Rejection Reason |
|---|---|
| Density as primary axis for Group classification | Small tools (3 tokens, 0 palette) incorrectly marked as "mature" |
| Reverse-order `palette_count` sorting | Not scalable; large tools always appear first |
| Composite `migration_score` | Additional metric to maintain; density is sufficient |

## Consequences

### Positive

- Tier classification no longer systematically underestimates independent portal tools (corrects Day 1 ordering bias for tools like tenant-manager)
- Dashboard precisely shows "which tools need 1-2 more palettes"
- Per-tool JSON output changes are additive; no breaking changes
- Day 4 batch 4 migration ordering is more reasonable than Day 1

### Negative / Risks

1. **Five-dimensional weights are heuristic** — Equal weighting `w=1` lacks empirical calibration. **Mitigation**: After Phase .a concludes, retrospectively validate with actual migration outcomes; adjust if necessary.
2. **Density misinterpretation** — Risk of treating density=1.0 as "complete". **Mitigation**: `scan_component_health.py` docstring clarifies "density is a secondary signal".
3. **Group thresholds (≥80/≥20/<20) remain hardcoded** — Sourced from Day 1 heuristic estimation. **Mitigation**: Recalibrate based on actual distribution after Phase .a concludes.

## Related

- `scripts/tools/dx/scan_component_health.py` (implementation)
- `docs/internal/v2.7.0-planning.md` §10 DEC-08 + §19 DEC-M
- `docs/internal/v2.7.0-day1to3-retrospective-review.md` §3.1 (DEC-08 retrospective)
