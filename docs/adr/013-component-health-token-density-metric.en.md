---
title: "ADR-013: Component Health Scanner — Tier Scoring Algorithm and token_density Auxiliary Metric"
tags: [adr, metrics, component-health, design-tokens, tier-scoring, v2.7.0]
audience: [frontend-developers, platform-engineers, maintainers]
version: v2.9.0
lang: en
---

# ADR-013: Component Health Scanner — Tier Scoring Algorithm and token_density Auxiliary Metric

> **Language / 語言：** **English (Current)** | [中文](./013-component-health-token-density-metric.md)

## Status

✅ **Accepted** (v2.7.0, 2026-04-16)

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged.

## Decision

`scripts/tools/dx/scan_component_health.py` scans every portal interactive tool (a JSX file) registered in `docs/assets/tool-registry.yaml` and makes two kinds of judgement. Both live in the same tool, so they share one ADR.

1. **Tier is the sum of five signals, not "how many documents reference the tool"** (the registry's `appears_in`). `appears_in` is still copied into the output but is not scored.
2. **Design-token migration progress looks at both tokens and leftover palette classes, not the token count alone.** A design token here is a CSS variable such as `var(--da-…)`; a palette class is a hard-coded Tailwind color class such as `bg-slate-100`. The ratio metric `token_density` that this ADR originally defined is **not implemented**; see below.

### Tier classification

The score is the five signals added together (every weight is effectively 1; the code has no weight parameters):

| Signal | Points | How it is judged |
|---|---|---|
| LOC (lines) | 0–3 | ≥800 → 3, ≥400 → 2, ≥200 → 1, otherwise 0 |
| Audience | 0–2 | registry `audience` contains `tenant` plus another audience → 2; two or more audiences without `tenant` → 1; otherwise 0 |
| Phase (journey phase) | 0–2 | registry `journey_phase` is `deploy`/`configure` → 2; `monitor`/`troubleshoot` → 1; otherwise 0 |
| Writer (does it write) | 0 or 2 | the tool key contains one of wizard, manager, editor, setup, generator, heatmap, playground, or the source shows a write (`onSubmit=`, a `fetch(` with POST/PUT/PATCH/DELETE, `apiCall`, `window.confirm(`) → 2; otherwise 0 |
| Recency | -1 to +1 | last commit to the file ≤90 days ago → +1; ≤180 days → 0; older → -1; no date found → 0 |

A score of ≥7 is Tier 1, 4–6 is Tier 2, ≤3 is Tier 3. Two cases are labelled `Tier 3 (deprecation_candidate)` (a candidate for retirement):

- LOC < 100 and Recency is -1: checked **before** the score, so a high score does not help.
- Score ≤3 with both Writer and Audience at 0: applied only once the tool has already landed in Tier 3.

```python {.quote src="scripts/tools/dx/scan_component_health.py"}
    is_stub_stale = loc < 100 and breakdown["recency"] < 0
    if is_stub_stale:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    if score >= 7:
        return "Tier 1", score, breakdown
    if score >= 4:
        return "Tier 2", score, breakdown
    if breakdown["writer"] == 0 and breakdown["audience"] == 0:
        return "Tier 3 (deprecation_candidate)", score, breakdown
    return "Tier 3", score, breakdown
```

Tools marked `status: archived` in the registry are not scored: they get `tier: "Archived"` and are left out of the tier, token-group, coverage and i18n aggregates (they still count in `total_registered_tools`). The archiving process and the automatic `archive_candidates` suggestions are in [`dev-rules.md`](../internal/dev-rules.md), §T (tool lifecycle).

### Example: no document references it, and it is still Tier 1

In the registry, `threshold-heatmap` has an empty `appears_in`; under the old single signal it would sit in the lowest tier:

```yaml {.quote src="docs/assets/tool-registry.yaml"}
- key: threshold-heatmap
# ...
  file: interactive/tools/threshold-heatmap.jsx
  audience:
  - platform
  - domain
  - sre
# ...
  journey_phase: monitor
  hub_section: advanced
  appears_in: []
```

Running `python3 scripts/tools/dx/scan_component_health.py --stdout --today 2026-11-24` gives, for this tool (excerpt):

```json
"tier": "Tier 1",
"tier_score": 7,
"tier_breakdown": {"loc": 2, "audience": 1, "phase": 1, "writer": 2, "recency": 1, "_total": 7},
"design_tokens": 99,
"tailwind_palette": 0,
"token_density_per_100_loc": 19.6,
"token_group": "A"
```

504 lines → LOC 2; three audiences without `tenant` → Audience 1; `monitor` → Phase 1; the key contains heatmap → Writer 2; the file changed within 90 days before `--today` → Recency 1. Total 7, Tier 1. Recency depends on the git history and `--today`, so another clone may produce a different value for it.

### Token migration progress

Each tool's output carries these fields:

| Field | Meaning |
|---|---|
| `design_tokens` | occurrences of `var(--da-…)` |
| `tailwind_palette` | occurrences of hard-coded Tailwind color classes (`bg-`/`text-`/`border-` etc. with shades 50–950 of slate, gray, blue, red and similar families) |
| `hex_colors_hardcoded` | occurrences of `#` followed by 3–8 hex digits on lines that are not comments (starting with `//` or `*`) |
| `token_density_per_100_loc` | tokens per 100 lines |
| `token_group` | migration group, rules below |

The group looks at the token count together with leftover palette and hex:

```python {.quote src="scripts/tools/dx/scan_component_health.py"}
    if tokens >= 80 and tailwind_palette <= 5 and hex_hardcoded <= 5:
        return "A"  # mature
    if tokens >= 20:
        return "B"  # in-progress
    if tailwind_palette == 0 and hex_hardcoded == 0 and tokens == 0:
        return "N/A"  # stylistically trivial
    return "C"  # unmigrated
```

So "80 tokens with 20 palette classes left" lands in B, not A.

**What is not implemented.** The ratio this ADR defines, `token_density = tokens / (tokens + palette_hits)` (0.0–1.0; closer to 1 means fewer palette classes left), and the three output fields `token_count`, `palette_count` and `token_density` are not produced by the scanner today; the existing `token_density_per_100_loc` is tokens per hundred lines, not this ratio. The usage rules from the original decision are kept, for when the ratio is implemented:

- **Appropriate**: finding tools on a dashboard that are "almost migrated" (ratio ≥0.9 and palette > 0); comparing migration completeness across tools.
- **Inappropriate**: replacing the token count as the primary metric (a tool with 3 tokens and 0 palette has a ratio of 1.0 but is not mature); setting a hard gate on it (penalizes tools that never had many palette classes); using it as the sole basis for grouping.

## Background

**Ranking by references alone systematically underrates standalone tools.** The early scanner judged Tier solely by `appears_in` (how many documents reference a tool): a tool referenced by many documents went to Tier 1, while an equally complex portal tool mentioned in only one document fell to Tier 3. Tier decides the order of token migration and how the regression-test budget is spent, so a wrong ranking misdirects the effort.

**Grouping by token count alone hides how much is left to migrate.** The early grouping cut on token count only (≥80 → A, ≥20 → B, otherwise C): 80 tokens with 0 palette and 80 tokens with 20 palette were both A, although the latter still had a fair amount of palette left.

## Consequences and Known Limitations

- **The weights are guesses, not calibrated.** Adding the five signals equally and every tier threshold are estimates that have not been checked against actual migration results; the group thresholds (80/20, palette and hex each ≤5) are likewise hard-coded.
- **Recency needs the full git history.** Dates come from `git log`; in a shallow clone (one that fetched only the latest commits), many files' last-modified date falls on the clone's boundary commit, which distorts Recency and `first_commit` and, through them, `archive_candidates`.
- **Writer is only 0 or 2.** It relies on matching the key name and source strings, with no middle value; a name that happens to contain heatmap, setup and the like also gets 2.
- **It advises; it blocks nothing.** The scanner only reads, exits 0 on success, and is not wired into pre-commit or CI. Its output file `docs/internal/component-health-snapshot.json` is listed in `.gitignore` and is regenerated locally.
- **The portal's component-health page is not live data.** `tools/portal/src/interactive/tools/component-health.jsx` embeds a hard-coded snapshot (the page states a snapshot date of 2026-04-17) that does not follow the scanner.
- **Tests hold only part of this.** `tests/dx/test_scan_component_health.py` holds the exclusion of archived tools, the `archive_candidates` criteria, and the field list of a normal tool's output (including `token_group` and `token_density_per_100_loc`). The Tier score thresholds, the per-signal scoring rules and the `token_group` grouping rule have no direct unit test.

## Alternatives Considered

| Option | Why not chosen |
|---|---|
| Keep the single `appears_in` signal | Systematically underrates standalone portal tools |
| 10 or more signals | High maintenance cost; calibration needs regression data |
| Machine-learning classifier | Only a few dozen tools, and no labelled training set |
| Token ratio as the main axis for grouping | Small tools (3 tokens, 0 palette) would be misclassified as mature |
| Sorting by `palette_count` descending | Large tools always come first; does not reflect completeness |
| A separate composite `migration_score` | One more metric to maintain; the token ratio is enough |

## Related

- `scripts/tools/dx/scan_component_health.py` (implementation)
- `tests/dx/test_scan_component_health.py` (tests)
- [`dev-rules.md`](../internal/dev-rules.md) §T tool lifecycle (archived and `archive_candidates`)
- [`component-health-snapshot.md`](../internal/component-health-snapshot.md)
