---
title: "Architecture Decision Records (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.7.0
lang: en
---

# Architecture Decision Records (ADR)

> **Language / 語言：** **English (Current)** | [中文](README.md)

This directory contains Architecture Decision Records (ADRs) for the Multi-Tenant Dynamic Alerting platform. Each ADR documents the background, option evaluation, and long-term impact of a specific design decision.

## Quick Guide

New here? Pick based on your needs:

- **Understand core design**: [001 Severity Dedup](./001-severity-dedup-via-inhibit.en.md) + [005 Projected Volume](./005-projected-volume-for-rule-packs.en.md) — two foundations of the rule engine
- **Preparing to deploy**: [008 Operator Integration](./008-operator-native-integration-path.en.md) — ConfigMap vs Operator CRD dual-path
- **Multi-cluster needs**: [004 Federation](./004-federation-central-exporter-first.en.md) + [006 Tenant Mapping](./006-tenant-mapping-topologies.en.md) — Federation architecture and topologies
- **Management plane**: [009 Tenant API](./009-tenant-manager-crud-api.en.md) + [011 PR Write-back](./011-pr-based-write-back.en.md) — UI/API management and compliance workflows
- **Scale / Config management (v2.7.0)**: [010 Multi-Tenant Grouping](./010-multi-tenant-grouping.en.md) + [017 conf.d/ directory hierarchy](./017-conf-d-directory-hierarchy-mixed-mode.en.md) + [018 inheritance engine + dual-hash](./018-defaults-yaml-inheritance-dual-hash.en.md) — thousand-tenant config organization and hot-reload
- **Frontend quality governance (v2.7.0)**: [013 Component health + Token Density](./013-component-health-token-density-metric.en.md) + [014 TECH-DEBT budget isolation](./014-tech-debt-category-budget-isolation.en.md) + [015 Wizard token migration](./015-wizard-arbitrary-value-token-migration.en.md) + [016 data-theme single-track dark mode](./016-data-theme-single-track-dark-mode.en.md)
- **Accessibility hotfix (v2.7.0)**: [012 threshold-heatmap colorblind patch](./012-colorblind-hotfix-structured-severity-return.en.md)

## ADR Index

| ID | Title | Status | Summary |
|:---|:-----|:-----|:-----|
| [001](#001-severity-dedup-via-inhibit-rules) | Severity Dedup via Inhibit Rules | ✅ Accepted | Use Alertmanager inhibit_rules instead of PromQL for severity deduplication, preserving TSDB completeness |
| [002](#002-oci-registry-over-chartmuseum) | OCI Registry over ChartMuseum | ✅ Accepted | Consolidate Helm charts and Docker images distribution via ghcr.io OCI registry, eliminating ChartMuseum dependency |
| [003](#003-sentinel-alert-pattern) | Sentinel Alert Pattern | ✅ Accepted | Implement tri-state control via sentinel alerts + inhibit, replacing direct PromQL suppression |
| [004](#004-federation-architecture-central-exporter-first) | Federation Architecture — Central Exporter First | ✅ Accepted → Extended | Prioritize central exporter + edge Prometheus federation (v2.1.0+: both architectures implemented) |
| [005](#005-projected-volume-for-rule-packs) | Projected Volume for Rule Packs | ✅ Accepted | Use Projected Volume with optional:true to implement selectable Rule Pack unloading |
| [006](#006-tenant-mapping-topologies) | Tenant Mapping Topologies (1:1, N:1, 1:N) | ✅ Accepted | Data plane Recording Rules resolve three instance-tenant mapping topologies; exporter zero changes |
| [007](#007-cross-domain-routing-profiles-and-domain-policies) | Cross-Domain Routing Profiles and Domain Policies | ✅ Accepted | Routing Profiles (reuse) + Domain Policies (constraints) two-layer architecture |
| [008](#008-operator-native-integration-path) | Operator-Native Integration Path | ✅ Accepted | Dual-path toolchain for Prometheus Operator CRD conversion; core exporter architecture unchanged |
| [009](#009-tenant-manager-crud-api) | Tenant Manager CRUD API | ✅ Accepted | Go HTTP server with oauth2-proxy, commit-on-write Git audit, async batch operations and SSE push |
| [010](#010-multi-tenant-grouping-architecture) | Multi-Tenant Grouping Architecture | ✅ Accepted | Custom tenant groups with static members, multi-dimensional filtering via extended metadata schema |
| [011](#011-pr-based-write-back-mode) | PR-based Write-back Mode | ✅ Accepted | Dual-mode architecture (direct commit / pull request), supporting GitHub PR and GitLab MR |
| [012](#012-threshold-heatmap-colorblind-patch) | threshold-heatmap Colorblind Patch — Structured Severity Return | ✅ Accepted | Fix WCAG 1.4.1 violation: replace color-only output with `{severity, color, ariaLabel}` structure to support colorblind readability |
| [013](#013-component-health-and-token-density-metric) | Component Health and Token Density Metric | ✅ Accepted | 5-dimension weighted scoring (LOC+Audience+Phase+Writer+Recency) with automatic Tier 1/2/3 classification; introduce `token_density` metric quantifying token migration progress |
| [014](#014-tech-debt-vs-regression-budget-isolation) | TECH-DEBT vs Regression Budget Isolation | ✅ Accepted | Split technical debt and user-visible regressions into two separate budgets to prevent TECH-DEBT from eroding REG budget; LL crossing 2 minor versions requires tri-choice (codify / automate / archive) |
| [015](#015-wizard-token-arbitrary-value-migration-strategy) | Wizard Token Arbitrary-Value Migration Strategy (Option A) | ✅ Accepted | Use `bg-[color:var(--da-color-*)]` arbitrary-value rewrite for legacy `bg-slate-200`, avoiding Tailwind config expansion + completing full replacement in one commit |
| [016](#016-data-theme-single-track-dark-mode) | `[data-theme]` Single-track Dark Mode (removing `dark:` variant) | ✅ Accepted | Unify dark mode under `[data-theme="dark"]` attribute, disabling Tailwind `dark:` variant to eliminate token/class dual-track issues |
| [017](#017-confd-directory-hierarchy-mixed-mode) | conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy | 🟡 Proposed | Directory Scanner supports both flat and domain/region/env 3-level hierarchy; zero-downtime upgrade + optional `migrate-conf-d` tool |
| [018](#018-defaultsyaml-inheritance-semantics-dual-hash-hot-reload) | `_defaults.yaml` Inheritance Semantics + dual-hash hot-reload | 🟡 Proposed | Deep merge with override (array replace, null-as-delete) + dual hash (source_hash + merged_hash) for precise reload trigger determination, paired with 300ms debounce |

---

## 001: Severity Dedup via Inhibit Rules

**Document**: [`001-severity-dedup-via-inhibit.en.md`](./001-severity-dedup-via-inhibit.en.md)

Use Alertmanager inhibit_rules instead of PromQL `absent()`/`unless()` for severity deduplication. Key consideration: preserve TSDB integrity where all severity levels of the same metric are recorded, with intelligent suppression performed at the Alertmanager layer.

---

## 002: OCI Registry over ChartMuseum

**Document**: [`002-oci-registry-over-chartmuseum.en.md`](./002-oci-registry-over-chartmuseum.en.md)

Consolidate Helm charts and Docker images distribution via ghcr.io OCI registry, eliminating dependency on a standalone ChartMuseum. Requires Helm 3.8+, but significantly simplifies operational overhead.

---

## 003: Sentinel Alert Pattern

**Document**: [`003-sentinel-alert-pattern.en.md`](./003-sentinel-alert-pattern.en.md)

Implement tri-state mode (Normal/Silent/Maintenance) via exporter flag metric → recording rule → sentinel alert → inhibit flow. Compared to direct PromQL suppression, this pattern provides strong composability and easier debugging.

---

## 004: Federation Architecture — Central Exporter First

**Document**: [`004-federation-central-exporter-first.en.md`](./004-federation-central-exporter-first.en.md)

Prioritize "Central Exporter + Edge Prometheus" architecture (80-20 principle). v1.12.0 core implementation complete; v2.1.0 Edge Exporter architecture also implemented (`rule-pack-split`); v2.6.0 extends multi-cluster CRD deployment and drift detection.

---

## 005: Projected Volume for Rule Packs

**Document**: [`005-projected-volume-for-rule-packs.en.md`](./005-projected-volume-for-rule-packs.en.md)

Use Projected Volume with `optional: true` to implement selective Rule Pack unloading for 15 Rule Packs. Tenants can delete individual ConfigMaps to disable specific Rule Packs; Prometheus does not fail when packs are missing.

---

## 006: Tenant Mapping Topologies

**Document**: [`006-tenant-mapping-topologies.en.md`](./006-tenant-mapping-topologies.en.md)

Resolve three instance-tenant mapping topologies (1:1, N:1, 1:N) at the data plane via Prometheus Recording Rules. The 1:N topology (Oracle multi-schema, DB2 multi-tablespace) uses config-driven `instance_tenant_mapping` to auto-generate Recording Rules; threshold-exporter requires zero changes.

---

## 007: Cross-Domain Routing Profiles and Domain Policies

**Document**: [`007-cross-domain-routing-profiles.en.md`](./007-cross-domain-routing-profiles.en.md)

Two-layer architecture: Routing Profiles (named routing configs shared by multiple tenants) + Domain Policies (business domain compliance constraints, validation not inheritance). Configuration duplication reduced from O(N) to O(1); domain policies provide machine-verifiable compliance constraints.

---

## 008: Operator-Native Integration Path

**Document**: [`008-operator-native-integration-path.en.md`](./008-operator-native-integration-path.en.md)

Core platform (threshold-exporter + Rule Packs) remains path-agnostic. New `operator-generate` / `operator-check` toolchain handles Prometheus Operator CRD conversion and validation. v2.6.0 establishes architectural boundary: exporter does not watch any CRD; external tools manage CRD transformations.

---

## 009: Tenant Manager CRUD API

**Document**: [`009-tenant-manager-crud-api.en.md`](./009-tenant-manager-crud-api.en.md)

Standalone Go HTTP server (`tenant-api`) serves as management plane backend for da-portal. Authentication via oauth2-proxy, commit-on-write ensures Git audit trail, `_rbac.yaml` provides fine-grained permissions. v2.6.0 extends with async batch operations (goroutine pool + task_id polling), SSE server-sent events (replacing WebSocket), and PR-based write-back (ADR-011, GitHub + GitLab).

---

## 010: Multi-Tenant Grouping Architecture

**Document**: [`010-multi-tenant-grouping.en.md`](./010-multi-tenant-grouping.en.md)

`_groups.yaml` stores custom group definitions with static `members[]` lists. Extended `_metadata` schema (environment, region, domain, db_type, tags) enables multi-dimensional filtering and group batch operations. v2.5.0 completed static membership; v2.7.0+ candidates include filter-based auto-membership and group member lint hooks.

---

## 011: PR-based Write-back Mode

**Document**: [`011-pr-based-write-back.en.md`](./011-pr-based-write-back.en.md)

Extends commit-on-write with `_write_mode: pr` option: UI operations generate GitHub PR or GitLab MR instead of direct commits, satisfying four-eyes review requirements. Platform Abstraction Layer supports GitHub and GitLab dual platforms.

---

## 012: threshold-heatmap Colorblind Patch

**Document**: [`012-colorblind-hotfix-structured-severity-return.en.md`](./012-colorblind-hotfix-structured-severity-return.en.md)

Fix WCAG 1.4.1 violation in v2.6.0 `threshold-heatmap.jsx` where severity was conveyed via color only. `getSeverityColorClass()` is replaced by `getSeverityInfo()` returning `{severity, color, ariaLabel}` structure; cells additionally expose `aria-label` and icon for dual-channel presentation, enabling colorblind users to distinguish severities. Runtime WCAG validation is consolidated into CI.

---

## 013: Component Health and Token Density Metric

**Document**: [`013-component-health-token-density-metric.en.md`](./013-component-health-token-density-metric.en.md)

v2.7.0 Phase .a new baseline: 5-dimension weighted scoring (LOC 0-3 + Audience 0-2 + Phase 0-2 + Writer 0-2 + Recency -1~+1) with automatic Tier 1/2/3 classification. Introduce the `token_density = tokens / (tokens + palette_hits)` metric quantifying design-token migration progress across JSX tools (Group A/B/C). Consolidated from early DEC-08 and DEC-M planning decisions.

---

## 014: TECH-DEBT vs Regression Budget Isolation

**Document**: [`014-tech-debt-category-budget-isolation.en.md`](./014-tech-debt-category-budget-isolation.en.md)

On top of the v2.6.x Regression Budget (P2/P3 fixes ≤ 15% of release effort), add a "TECH-DEBT" category with its own independent budget (4%), preventing technical debt from consuming user-visible regression-fix time. LLs crossing 2 minor versions must take one of three paths: codify into formal rules, mark 🛡️ automated, or archive under `archive/`. Provides a mechanism for Playbook knowledge annealing.

---

## 015: Wizard Token Arbitrary-Value Migration Strategy

**Document**: [`015-wizard-arbitrary-value-token-migration.en.md`](./015-wizard-arbitrary-value-token-migration.en.md)

v2.7.0 Phase .a0 migrates `deployment-wizard.jsx` from legacy `bg-slate-200 / text-gray-700` palette to design tokens. **Option A** selected: `bg-[color:var(--da-color-*)]` arbitrary-value rewrite instead of expanding `tailwind.config`. Preserves the Tailwind utility style + token SSOT; subsequent rbac / cicd / threshold-heatmap batch 4 follow the same rule.

---

## 016: `[data-theme]` Single-track Dark Mode

**Document**: [`016-data-theme-single-track-dark-mode.en.md`](./016-data-theme-single-track-dark-mode.en.md)

Fully remove the Tailwind `dark:` variant and unify dark mode under the `[data-theme="dark"]` attribute. The previous coexistence of class-based and attribute-based tracks caused tooltip/palette color drift and double maintenance cost. `jsx-loader` sets `data-theme` instead of toggling `class="dark"`; `tailwind.config.darkMode` is removed. A prerequisite for all subsequent Phase .a0 token migrations.

---

## 017: conf.d/ Directory Hierarchy + Mixed Mode

**Document**: [`017-conf-d-directory-hierarchy-mixed-mode.en.md`](./017-conf-d-directory-hierarchy-mixed-mode.en.md)

v2.7.0 Phase .b B-1. Directory Scanner supports both flat and `{domain}/{region}/{env}/` three-level structures, **without forcing migration**. Directory paths can infer default `_metadata.domain/region/environment` values; explicit fields in the file override. The `migrate-conf-d` tool is optional, supports `--dry-run` + `git mv` to preserve history. Resolves readability and blast-radius blind spots at 200+ tenants.

---

## 018: `_defaults.yaml` Inheritance Semantics + dual-hash hot-reload

**Document**: [`018-defaults-yaml-inheritance-dual-hash.en.md`](./018-defaults-yaml-inheritance-dual-hash.en.md)

v2.7.0 Phase .b B-1. Define multi-level `_defaults.yaml` inheritance semantics (L0 global → L1 domain → L2 region → L3 env → tenant) with deep merge with override (array replace, null-as-delete, `_metadata` not inherited). Dual hash: `source_hash` (tenant YAML file itself) + `merged_hash` (effective config canonical JSON) precisely determines reload trigger, avoiding reload storms when `_defaults.yaml` changes; 300ms debounce handles batch git pulls.

---

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.en.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — Platform engineer quick start guide
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — AI development context guide
