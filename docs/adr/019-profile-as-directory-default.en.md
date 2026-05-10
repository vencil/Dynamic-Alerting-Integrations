---
title: "ADR-019: Profile-as-Directory-Default"
tags: [adr, profile-builder, conf-d, v2.8.0]
audience: [platform-engineers, sre, contributors]
version: v2.8.0
lang: en
---

# ADR-019: Profile-as-Directory-Default

> **Language / 語言：** **English (Current)** | [中文](./019-profile-as-directory-default.md)

> Profile Builder component of the v2.8.0 customer-migration pipeline.
> Paired with [ADR-017](017-conf-d-directory-hierarchy-mixed-mode.en.md) (Directory Hierarchy) + [ADR-018](018-defaults-yaml-inheritance-dual-hash.en.md) (Inheritance Semantics).

## Status

🟢 **Accepted** (v2.8.0, 2026-04-27) — The Profile Builder's PromRule→threshold translator and emission ship with v2.8.0.

## Context

The Profile Builder clusters a customer's PromRule corpus into "structurally similar" groups. Each cluster is expected to land in the conf.d/ tree — the question is **how**:

1. **One full tenant.yaml per tenant** — N structurally similar rules become N files; even with `_defaults.yaml` inheritance, every tenant repeats every key. Classic GitOps anti-pattern.
2. **Pour everything into `_defaults.yaml`, no tenant.yaml** — loses per-tenant fine-tuning.
3. **Cluster-wide values in `_defaults.yaml`, only divergent tenants get a `<id>.yaml` override** — ADR-018's deepMerge already supports sparse override; the question is "what value goes in default?" and "which tenants count as 'truly different'?". Without an explicit rule each operator interprets it differently.

The Profile Builder ships both a translator (extracts threshold scalars from PromRule expressions) and emission (writes the cluster's decisions into a conf.d tree). The translator's heuristics are internal-package territory (see the `internal/profile/translate.go` package header); but "what shape does emission produce" is a cross-component decision affecting the batch PR pipeline's directory placement, the redundant-override guard's semantics, and release packaging — ADR territory.

## Decision

### Profile-as-Directory-Default

**Cluster-wide thresholds live in `_defaults.yaml`; only tenants whose value genuinely differs get a `<id>.yaml` override file.**

Concrete rules (implemented by `emit_translated.go`; the translator package header carries metric_key / median / cluster-aggregation heuristic details):

- The `_defaults.yaml` `defaults: {<metric_key>: <threshold>}` carries the cluster **median** (not mean — single-outlier resilience).
- Member threshold == cluster default → **no tenant file** (rely on ADR-018 inheritance).
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

tenant-a and tenant-b have no files (runtime deepMerge picks up the 80 from `_defaults.yaml`).

### Why this principle warrants an ADR

- **Cross-component**: Profile Builder emission shape, the batch PR pipeline's directory placement, release packaging, and the redundant-override guard ALL must agree on this default-vs-override boundary. A divergent interpretation anywhere creates GitOps smell (duplicate overrides; values silently shadowed by defaults).
- **Customer-visible**: the conf.d/ shape customers see is directly determined by this principle.
- **Long-term stable**: translator heuristics may evolve as customer corpora reveal new shapes, but the default-vs-override boundary should not shift for years.

Translator-internal algorithms (the metric_key 5-step ladder, majority vote, median outlier resistance, operator handling, status fallback) are implementation details and live in `components/threshold-exporter/app/internal/profile/translate.go`'s package header. This ADR does NOT duplicate them — single source of truth, no drift.

## Known cross-component non-goals

| Non-goal | Why | Plan |
|---|---|---|
| Profile Builder auto-inferring directory placement (which cluster lands at L1 / L2 / L3) | This is the batch PR pipeline's job; needs cross-domain/region corpus view | batch PR pipeline |
| Emitting dimensional / regex labels (`{queue=~"q.*"}`) | Needs expression rewrite + label expansion, cross-component | batch PR pipeline dimensional support |
| Auto-rewriting customer PromRule expressions to `> on(tenant) user_threshold{}` form | Rule rewrite is a separate toolkit, decoupled from conf.d emission | batch PR pipeline / customer manual |
| Two-tier severity translation (`metric_key_critical` derived from one cluster) | Clusters are semantically single-tier; two-tier needs re-clustering at PromRule-pair level | later fuzzier matcher / customer manual |

## Interactions

### With ADR-018 (deepMerge)

This ADR's emission relies on ADR-018's:

- **null-as-delete**: tenants who want to explicitly clear a value can still do so (emission uses explicit numbers, never null).
- **map deep-merge**: each tenant file lists ONLY keys that differ from `_defaults.yaml`; runtime ResolveAt fills the rest from defaults.
- **scalar override**: tenant string values (e.g. `"1500"`) override the default numeric; runtime uses strconv to convert back to float at ResolveAt time.

### With ADR-017 (Directory Hierarchy)

Emission's `<RootPrefix>/<ProposalDir>/` maps to ADR-017's directory levels. **The caller** (the batch PR pipeline is the primary user) decides whether `ProposalDirs[i]` lands at L1 / L2 / L3. **The Profile Builder does NOT infer directory placement**; that's the batch PR pipeline's job.

### With the Dangling Defaults Guard

Emission produces native ADR-018 deepMerge shapes, so the guard applies naturally:

- Schema validation: metric_key required-fields check.
- Cardinality guard: predicted-metric-count includes emission's metric_key entries.
- Redundant-override warn: tenant override matching `_defaults` median → guard suggests removal (this check is effectively the post-merge enforcement of Profile-as-Directory-Default).

Once emission ships, customer PRs automatically run the guard's GH Actions wrapper for validation. The loop closes.

## Implementation locations

| File | Role |
|---|---|
| `internal/profile/translate.go` | Translator + heuristic details (metric_key ladder / cluster aggregation / median / operator handling) — full inline doc in package header |
| `internal/profile/emit.go` | `EmissionInput.Translate` flag + dispatch into `emitTranslatedProposal`; conf.d-shape template implementation |
| `internal/profile/translate_test.go` | Table tests covering translator + cluster aggregation (`-race -count=2` stable) |

(These files live under `components/threshold-exporter/app/`, outside the MkDocs site — open from GitHub.)

## Changelog

- v2.8.0: this ADR ships with the PromRule→threshold translator + emit dispatch.
- v2.8.0 review: after a "do we need this ADR?" check from the user, the original translator heuristic details moved into `translate.go`'s package header. This ADR was then slimmed to the single cross-component design principle, eliminating double-writing drift risk.
