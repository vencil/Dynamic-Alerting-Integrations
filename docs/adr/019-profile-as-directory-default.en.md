---
title: "ADR-019: Profile-as-Directory-Default + PromRule→threshold translator"
tags: [adr, profile-builder, translator, conf-d, phase-c, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.7.0
lang: en
---

# ADR-019: Profile-as-Directory-Default + PromRule→threshold translator

> **Language / 語言：** **English (Current)** | [中文](./019-profile-as-directory-default.md)

> Phase .c C-9 (v2.8.0 Customer-Migration Pipeline).
> Paired with [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.en.md) (Directory Hierarchy) + [ADR-018](018-defaults-yaml-inheritance-dual-hash.en.md) (Inheritance Semantics).

## Status

🟢 **Accepted** (v2.8.0 Phase .c, 2026-04-27, lands with C-9 PR-3)

## Context

The C-9 Profile Builder clusters a customer's PromRule corpus into "structurally similar" groups. The goal: turn each cluster into ONE shared `_defaults.yaml` (cluster-wide structure) plus thin per-tenant overrides for the values that genuinely differ — instead of N copies of nearly-identical tenant.yaml files (the GitOps anti-pattern Phase .c is built to fight).

PR-1 shipped the cluster engine. PR-2 shipped an *intermediate artifact* emission carrying metadata like `shared_expr_template`. The intermediate format is NOT consumable by the threshold-exporter's ADR-018 deepMerge runtime:

```yaml
# Intermediate (PR-2 shape) — NOT consumed by exporter runtime
shared_expr_template: 'rate(node_cpu_seconds_total[<NUM>m]) > <NUM>'
dialect: prom
member_count: 5
```

```yaml
# Conf.d-ready (what exporter ResolveAt actually loads)
defaults:
  cpu_rate_5m: 0.85
```

Bridging the two requires:

1. Pulling the **scalar threshold** out of the PromQL expression (`> 0.85` → `0.85`).
2. Picking a stable **`metric_key`** for each cluster (the conf.d field name that publishes the threshold).

PR-3 ships:
- `internal/profile/translate.go` — pure Go function performing AST walk + metric_key resolution.
- ADR-019 (this doc) — pinning the "Profile-as-Directory-Default" principle, the metric_key resolution order, and the explicit non-goals.

## Decision

### 1. Profile-as-Directory-Default

**Cluster-wide thresholds live in `_defaults.yaml`; only tenants whose value genuinely differs get a `<id>.yaml` override file.**

Concrete rules (implemented by `emitTranslatedProposal`):

- The `_defaults.yaml` `defaults: {<metric_key>: <threshold>}` carries the cluster **median** (not mean — single-outlier resilience).
- Member threshold == cluster default → no tenant file (rely on ADR-018 inheritance).
- Member threshold != cluster default → write `<id>.yaml` carrying ONLY the override for this `metric_key`.

Example input (3 PromRules, thresholds 80 / 80 / 1500):

```yaml
# _defaults.yaml (cluster median = 80)
defaults:
  mysql_connections: 80

# tenant-c.yaml (only c diverges from default)
tenants:
  tenant-c:
    mysql_connections: "1500"
```

tenant-a and tenant-b have no files (runtime deepMerge picks up the 80 from `_defaults.yaml`). **The line-savings PR-2's intermediate format only estimated, PR-3 actually realises.**

### 2. `metric_key` resolution order (resolveMetricKey)

`metric_key` is the conf.d-side "threshold field name". PromRule has no native concept of one, so the translator must choose. **ADR-019 pins this resolution order, top-down, first match wins**:

| Order | Source | Behaviour | Confidence |
|-------|--------|-----------|------------|
| 1 | `rule.Labels["metric_key"]` | Use verbatim | OK (explicit) |
| 2 | `rule.Alert` snake-cased | Derive from alert name | Partial (warning) |
| 3 | `rule.Record` snake-cased | Recording-rule fallback | Partial (warning) |
| 4 | First `MetricExpr.__name__` in expr | Last-resort heuristic | Partial (warning) |
| 5 | Nothing of the above | translation status = `skipped` | — |

**Rationale**:

- Explicit label (order 1) emits NO warning — the customer chose this on purpose. No uncertainty.
- Alert/record name (orders 2/3) emits a warning because "rule rename → silent metric_key drift" is a real risk. Reviewers see the warning and can decide whether to add an explicit label.
- Inner metric name (order 4) is the weakest fallback; reserved for rules with no other anchor.
- Empty (order 5): the translator REFUSES to invent a key. Skipped status falls back to PR-2's intermediate artifact so a human can intervene.

### 3. Cluster-level aggregation (TranslateProposal)

When a cluster has N members, the translator decides cluster-level facts:

| Axis | Rule | When inconsistent |
|------|------|-------------------|
| `metric_key` | Majority vote | Status = Partial + warning listing dissent |
| `operator` (`>` / `>=` / `<` / `<=`) | Majority vote | Same |
| `severity` | Majority vote | Same |
| `default_threshold` | **median** | N/A (numeric) |

- **Majority vote rather than hard error**: PR-1 already certified the cluster as "structurally similar"; these axes match 99% of the time. When they don't, a human should see the dissent — but the translator should NOT hard-fail (which would block the whole batch). Partial status + dissent warning written into PROPOSAL.md and the `_defaults.yaml` header comment is the right balance.
- **Median rather than mean**: thresholds frequently contain outliers (one tenant set their limit 10× above norm). Median is outlier-immune; mean is not.

### 4. Comparison operator handling

PR-3 recognises four operators: `>`, `>=`, `<`, `<=`.
- `==` / `!=`: **ADR-019 §non-goals**. Equality on a numeric metric vs scalar is rarely "threshold" semantics; leave for human review.
- Inverted form `0.85 < metric`: translator auto-flips to `metric > 0.85`. Downstream consumers always see "metric op threshold".
- Multiple comparisons (`a > 1 and b > 2`): pre-order tree walk picks the first comparison; status is Partial with a warning. Which one is the "primary" threshold is a human decision.

### 5. Translation status + fallback ladder

The translator emits one of three statuses per rule:

| Status | Conditions | Cluster-level handling |
|--------|-----------|-----------|
| `ok` | Explicit metric_key + single numeric comparison + severity label | Counted in cluster median |
| `partial` | metric_key from heuristic / non-unanimous axis / missing severity | Counted in median; Warnings explain the soft spots |
| `skipped` | Parse error / no numeric comparison / vector comparison / equality / no metric_key source | **Excluded from cluster median**; PROPOSAL.md surfaces for human review |

**Cluster fallback**: if every member is skipped, the cluster status is also skipped → **emission falls back to PR-2 intermediate format** (NOT conf.d-ready). Reviewers see the intermediate shape and immediately understand "this cluster wasn't translatable" without mistaking it for a successful conf.d landing.

### 6. Emission mode dispatch

`EmissionInput.Translate bool` is the caller's flag:

- `false` (PR-2 default): emit intermediate format. Backwards-compat for tooling already integrated against PR-2.
- `true`: attempt translation; per-proposal dispatch — TranslationOK / Partial → conf.d-shape, TranslationSkipped → intermediate-shape.

**Why per-proposal rather than batch all-or-nothing**: customer corpora are usually mixed (easy + hard rules). Dropping the whole batch back to intermediate when one rule resists translation hides PR-3's value. Per-proposal dispatch ships "translate as many as possible, surface the rest for review".

## Known non-goals (PR-3 does NOT do these)

| Non-goal | Why | Plan |
|---|---|---|
| Auto-rewrite the source PromRule expression (`expr > N` → `expr > on(tenant) user_threshold{...}`) | Rule rewrite is a separate toolkit, out of conf.d-emission scope | Defer to C-10 PR-3 / customer manual |
| `==` / `!=` operator translation | No threshold semantics | Defer to ADR-020 if customer use case demands |
| Histogram quantile bucketing | Non-scalar comparison | v2.9.0 |
| Translating dimensional / regex labels (`{queue=~"q.*"}`) | Needs expr rewrite + label expansion | Defer to C-10 dimensional support |
| Two-tier severity (warning + critical from one cluster) | If members disagree, PR-3 picks majority | Defer to PR-4 (fuzzier matcher) |
| Smarter root-pick on multi-comparison expressions | Pre-order first-hit is the "good enough" policy | Revisit if customer corpora demand |

## Interactions

### With ADR-018 (deepMerge)

PR-3 emission relies on ADR-018's:
- **null-as-delete**: tenants who want to explicitly clear a value can still do so (PR-3 emission uses explicit numbers, never null).
- **map deep-merge**: each tenant file lists ONLY keys that differ from `_defaults.yaml`; runtime ResolveAt fills the rest from defaults.
- **scalar override**: tenant string values (e.g. `"1500"`) override the default numeric; runtime uses strconv to convert back to float at ResolveAt time.

### With ADR-017 (Directory Hierarchy)

PR-3 emission's `<RootPrefix>/<ProposalDir>/` maps to ADR-017's directory levels. The caller (C-10 batch PR pipeline is the primary user) decides whether `ProposalDirs[i]` lands at L1 / L2 / L3. **PR-3 does NOT infer directory placement**; that's C-10 PR-3's job (per planning §C-10).

### With C-12 Dangling Defaults Guard

PR-3 emission produces native ADR-018 deepMerge shapes, so C-12 guards apply naturally:
- Schema validation: metric_key required-fields check.
- Cardinality guard: predicted-metric-count includes PR-3 emission's metric_key entries.
- Redundant-override warn: tenant override matching `_defaults` median → guard suggests removal.

After PR-3 lands, customer PRs automatically run the C-12 PR-5 GH Actions wrapper for validation. The loop closes.

## Implementation locations

The files below live under `components/threshold-exporter/app/` (outside the MkDocs site — open from GitHub):

| File | Role |
|---|---|
| `internal/profile/translate.go` | `TranslateRule` (per-rule) + `TranslateProposal` (cluster aggregation) |
| `internal/profile/emit.go` | `EmissionInput.Translate` flag + dispatch into `emitTranslatedProposal` |
| `internal/profile/translate_test.go` | Table tests covering ADR-019 §metric-key-resolution / §cluster-aggregation / §non-goals |

## Validation

PR-3 land requires `-race -count=2` clean across:
- `go test ./internal/profile/...`
- `go test ./...` (threshold-exporter full-module sweep)
- `go test ./...` (tenant-api: confirm `EffectiveConfig` field additions from C-12 PR-5 don't conflict)

PR-3 does NOT touch:
- C-12 PR-5 GH Actions wrapper (ADR-019 emission is conf.d-native, the guard works without changes).
- PR-2 intermediate emission path (still available as fallback).
- tenant-api `/effective` JSON contract (PR-3 doesn't modify `EffectiveConfig`).

## Changelog

- v2.8.0 Phase .c C-9 PR-3: this ADR ships with the translator + emit dispatch.
