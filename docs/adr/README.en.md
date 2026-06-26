---
title: "Architecture Decision Records (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
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
- **Thousand-tenant Scale / Config management**: [010 Multi-Tenant Grouping](./010-multi-tenant-grouping.en.md) + [016 conf.d/ directory hierarchy](./016-conf-d-directory-hierarchy-mixed-mode.en.md) + [017 inheritance engine + dual-hash](./017-defaults-yaml-inheritance-dual-hash.en.md) — thousand-tenant config organization and hot-reload
- **Frontend quality governance**: [013 Component health + Token Density](./013-component-health-token-density-metric.en.md) + [014 Wizard token migration](./014-wizard-arbitrary-value-token-migration.en.md) + [015 data-theme single-track dark mode](./015-data-theme-single-track-dark-mode.en.md)
- **Accessibility patches**: [012 threshold-heatmap colorblind patch](./012-colorblind-hotfix-structured-severity-return.en.md)
- **Customer-migration pipeline**: [018 Profile-as-Directory-Default](./018-profile-as-directory-default.en.md) — Profile Builder's default-vs-override rule when emitting into conf.d/

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
| [014](#014-wizard-token-arbitrary-value-migration-strategy) | Wizard Token Arbitrary-Value Migration Strategy (Option A) | ✅ Accepted | Use `bg-[color:var(--da-color-*)]` arbitrary-value rewrite for legacy `bg-slate-200`, avoiding Tailwind config expansion + completing full replacement in one commit |
| [015](#015-data-theme-single-track-dark-mode) | `[data-theme]` Single-track Dark Mode (removing `dark:` variant) | ✅ Accepted | Unify dark mode under `[data-theme="dark"]` attribute, disabling Tailwind `dark:` variant to eliminate token/class dual-track issues |
| [016](#016-confd-directory-hierarchy-mixed-mode) | conf.d/ Directory Hierarchy + Mixed Mode + Migration Strategy | ✅ Accepted | Directory Scanner supports both flat and domain/region/env 3-level hierarchy; zero-downtime upgrade + optional `migrate-conf-d` tool |
| [017](#017-defaultsyaml-inheritance-semantics-dual-hash-hot-reload) | `_defaults.yaml` Inheritance Semantics + dual-hash hot-reload | ✅ Accepted | Deep merge with override (array replace, null-as-delete) + dual hash (source_hash + merged_hash) for precise reload trigger determination, paired with 300ms debounce |
| [018](#018-profile-as-directory-default) | Profile-as-Directory-Default | ✅ Accepted | Cluster-wide thresholds in `_defaults.yaml`; only divergent tenants write `<id>.yaml` overrides (median + sparse override). The cross-component "default vs override boundary" rule consumed by Profile Builder, batch PR pipeline, and the Dangling Defaults Guard. Translator heuristic details live in `translate.go`'s package header (single source of truth, no drift) |
| [019](#019-planning-ssot-frontmatter-contract-discovery-based-index) | Planning SSOT — Frontmatter Contract + Discovery-based Index | ✅ Accepted | **(For contributors: internal planning-tracking governance, not a platform feature.)** Unify scattered planning tracking (tech-debt / dx-backlog / known-regression / roadmap / sprint) under a per-entry frontmatter contract + discovery-based index generator + active CI status-sync check. Consolidates TD/HA/REG into the TRK namespace; ADR and S# kept as separate namespaces |
| [020](#020-tenant-federation-label-injection-proxy-over-self-built-endpoint) | Tenant Federation — Label-Injection Proxy over Self-Built Endpoint | 🟡 Proposed | Tenant pulls own metrics subset back to tenant-side infra for self-managed federation. Adopts vmauth (VM customers) / prom-label-proxy (Prom customers) as label-enforced read proxy; platform does NOT self-build endpoint. 2-tier policy (platform whitelist + tenant subset) + 4h TTL token (no server-side revocation, **compensating control**: gateway rate limit mandatory) + **3-layer blast radius** (storage backend series/sample cap + gateway per-token rate limit + proxy label injection) + data-layer prerequisite (whitelisted metrics must natively carry `tenant_id` label, admission validator enforces) |
| [021](#021-tenant-log-query-authorization-plane-only-ingestion-decoupled) | Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled | ✅ Accepted | Tenant queries their own logs **in-place** on the platform (query-in-place, not pull-back). Platform owns the authorization plane only, reusing the existing federation-gateway with a new `victorialogs` mode; isolation comes from VictoriaLogs native `(AccountID, ProjectID)` tenancy + JWT claim→header injection (**NOT** prom-label-proxy — LogsQL ≠ PromQL). Ingestion stamping is decoupled as an explicit, verifiable contract (zero-trust payload + node-edge stamping). Phase 1 (b) platform-ops-logs-about-tenant → targets v2.10.0; Phase 2 (a) tenant application logs → defer-with-trigger. 3-layer blast radius recalibrated for LogsQL (no sample cap; time-range limit instead). |
| [022](#022-tenant-api-dev-auth-bypass-four-layer-containment) | tenant-api Dev-Auth Bypass — Local-Dev Identity Substitute, Four-Layer Containment | ✅ Accepted | try-local Mode 0 (no oauth2-proxy) lacks the X-Forwarded-* headers tenant-api trusts. A default-off `--dev-bypass-auth` flag injects a dev identity when `X-Forwarded-Email` is absent (identity-only — RBAC still enforced, never overrides a real identity). Risk contained in four layers: L1 default-off, L2 observable tripwire (response header + `/metrics` gauge + loud WARN), L3 runtime poison pill (panics if it detects Kubernetes), L4 deploy-time SAST (`check_dev_bypass_manifest.py` hard-blocks the flag in helm/k8s/operator manifests). |
| [023](#023-tenant-api-write-plane-single-writer-invariant-resilience-containment) | tenant-api Write Plane — Single-Writer Invariant & Resilience Containment | ✅ Accepted | Promotes the implicit single-writer assumption in tenant-api's write path to an explicit invariant (read plane horizontally scalable, write plane MUST be single writer). Three-layer enforcement: Helm static guard → rolling-update overlap (phantom replica) via `strategy: Recreate` interim → read/write split deployment target → K8s Lease watertight (deferred). Single-writer resilience: in-lock fetch + independent `TA_GIT_FETCH_TIMEOUT` (option A, rejecting option B's TOCTOU), queue-stage-only context-bound load-shedding, secondary-rate-limit 403 circuit-breaker recognition. |
| [024](#024-version-aware-threshold-declarative-cutover-via-the-existing-dimensional-version-label) | Version-Aware Threshold — Declarative Cutover via the Existing Dimensional `version` Label | 🟡 Proposed | Tenant pre-stages rules that activate on app version bump + "know which version is running" when troubleshooting. **Current-state correction**: the existing dimensional-label mechanism (`container_cpu{version="v1"}: "80"`) already emits the target `user_threshold{...,version="v1"}` shape with **zero exporter parse/emit change**, so Phase 1 is a **rule-pack normalize layer** (`:vlabeled` + `by(tenant, version)` join + `label_replace(..,"version","default","version","^$")` fallback, **not** `or vector(0)`), not a new config schema — **Option A reuse-over-build**, `versioned:` sugar demoted to defer-with-trigger. Cutover is emergent; auto-immune to K8s rolling/rollback/propagation lag. Orthogonal to §2.6 scheduled thresholds (time axis vs state axis); #423 R1 rejected absolute dates in `ScheduledValue`. Former top risk observed-but-not-declared is architecturally resolved by dynamic fallback (to default); `version_unknown` is demoted to a buffered visibility signal. Draft; #423 epic SOT; finalized at GA. |

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

Use Projected Volume with `optional: true` to implement selective Rule Pack unloading for 16 Rule Packs. Tenants can delete individual ConfigMaps to disable specific Rule Packs; Prometheus does not fail when packs are missing.

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

v2.7.0 baseline: 5-dimension weighted scoring (LOC 0-3 + Audience 0-2 + Phase 0-2 + Writer 0-2 + Recency -1~+1) with automatic Tier 1/2/3 classification. Introduces the `token_density = tokens / (tokens + palette_hits)` metric quantifying design-token migration progress across JSX tools (Group A/B/C).

---

## 014: Wizard Token Arbitrary-Value Migration Strategy

**Document**: [`014-wizard-arbitrary-value-token-migration.en.md`](./014-wizard-arbitrary-value-token-migration.en.md)

v2.7.0 migrates `deployment-wizard.jsx` from legacy `bg-slate-200 / text-gray-700` palette to design tokens. **Option A** selected: `bg-[color:var(--da-color-*)]` arbitrary-value rewrite instead of expanding `tailwind.config`. Preserves the Tailwind utility style + token SSOT; subsequent rbac / cicd / threshold-heatmap migrations follow the same rule.

---

## 015: `[data-theme]` Single-track Dark Mode

**Document**: [`015-data-theme-single-track-dark-mode.en.md`](./015-data-theme-single-track-dark-mode.en.md)

Fully remove the Tailwind `dark:` variant and unify dark mode under the `[data-theme="dark"]` attribute. The previous coexistence of class-based and attribute-based tracks caused tooltip/palette color drift and double maintenance cost. `jsx-loader` sets `data-theme` instead of toggling `class="dark"`; `tailwind.config.darkMode` is removed. A prerequisite for all subsequent v2.7.0 token migrations.

---

## 016: conf.d/ Directory Hierarchy + Mixed Mode

**Document**: [`016-conf-d-directory-hierarchy-mixed-mode.en.md`](./016-conf-d-directory-hierarchy-mixed-mode.en.md)

First building block of v2.7.0 Scale Foundation. Directory Scanner supports both flat and `{domain}/{region}/{env}/` three-level structures, **without forcing migration**. Directory paths can infer default `_metadata.domain/region/environment` values; explicit fields in the file override. The `migrate-conf-d` tool is optional, supports `--dry-run` + `git mv` to preserve history. Resolves readability and blast-radius blind spots at 200+ tenants.

---

## 017: `_defaults.yaml` Inheritance Semantics + dual-hash hot-reload

**Document**: [`017-defaults-yaml-inheritance-dual-hash.en.md`](./017-defaults-yaml-inheritance-dual-hash.en.md)

Second building block of v2.7.0 Scale Foundation. Defines multi-level `_defaults.yaml` inheritance semantics (L0 global → L1 domain → L2 region → L3 env → tenant) with deep merge with override (array replace, null-as-delete, `_metadata` not inherited). Dual hash: `source_hash` (tenant YAML file itself) + `merged_hash` (effective config canonical JSON) precisely determines reload trigger, avoiding reload storms when `_defaults.yaml` changes; 300ms debounce handles batch git pulls.

---

## 018: Profile-as-Directory-Default

**Document**: [`018-profile-as-directory-default.en.md`](./018-profile-as-directory-default.en.md)

v2.8.0 customer-migration pipeline — Profile Builder writing back to conf.d/. Pins the cross-component design principle: cluster-wide thresholds live in `_defaults.yaml`; only tenants whose value diverges from the default write a `<id>.yaml` override (median + sparse override). The shape this principle dictates is consumed by Profile Builder emission, the batch PR pipeline's directory placement, release packaging, and the Dangling Defaults Guard — getting this right at the ADR layer keeps all four components consistent. Translator heuristic details (metric_key 5-step ladder, median, cluster aggregation, operator handling) live in `internal/profile/translate.go`'s package header — single source of truth, no drift. Non-goals: directory inference (deferred to the batch PR pipeline), dimensional/regex labels emission, auto-rewriting source PromRules, two-tier severity translation.

---

## 019: Planning SSOT — Frontmatter Contract + Discovery-based Index

**Document**: [`019-planning-ssot.md`](./019-planning-ssot.md) (ZH-primary; EN mirror tracked under issue [#409](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/409))

> **👥 Audience:** platform **contributors / maintainers / AI agents** — this ADR governs *internal* planning tracking, not a platform feature. If you deploy the platform, configure thresholds, or are evaluating adoption, you likely want [Architecture & Design](../architecture-and-design.md) or [Getting Started](../getting-started/README.md) instead.

Unify the scattered "future plans / known issues / in-flight work" (previously spread across 8+ sources: CHANGELOG `[Unreleased]`, dx-tooling-backlog, frontend-quality-backlog, sprint planning ledger, roadmap-future, per-ADR Future Work, code comments, flaky-tests registry) under a three-layer design: a per-entry frontmatter contract, a discovery-based index generator (`generate_planning_index.py`), and an active CI status-sync check. Consolidates the TD/HA/REG namespaces into a single `TRK-NNN` namespace; `ADR-NNN` and `S#NNN` are kept as separate namespaces (ADR is permanent design history, not backlog tracking).

---

## 020: Tenant Federation — Label-Injection Proxy over Self-Built Endpoint

**Document**: [`020-tenant-federation.md`](./020-tenant-federation.md) (ZH-primary; EN mirror deferred to `Accepted` state per ADR-019 pattern)

v2.8.0 draft, targets v2.9.0 epic. Covers the cross-boundary federation scenario (complementary to ADR-004's platform-internal multi-cluster federation): tenants pull a subset of their own metrics back to tenant-side infra for self-managed federation. Adopts **vmauth** (VM customers) / **prom-label-proxy** (Prom customers) as a label-enforced read proxy; the platform does NOT self-build an endpoint (label-sanitization in a self-built impl is a multi-tenant breach landmine — production-hardened proxies have years of corner-case coverage). MVP 2-tier policy (platform whitelist + tenant subset) — domain layer drops to Future Work. Token: 4h TTL + no server-side revocation list (explicit trade-off: simpler impl in exchange for a 4h exposure window; **compensating control is mandatory** — gateway rate limit must be in place or 4h becomes a DoS playground). Blast radius defense is **3-layer** (adversarial review surfaced that a thin proxy cannot enforce series caps or per-token concurrency alone): storage backend handles series/sample limits (Prom `--query.max-samples` / VM `-search.maxUniqueTimeseries`), API gateway handles per-token rate limit + timeout (Nginx/Envoy with JWT claim extraction), proxy handles label injection + audit only. Data-layer prerequisite: whitelisted metrics must natively carry `tenant_id` label, enforced by admission validator. Implementation epic (~68h after adversarial review revision) tracked at issue [#380](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/380) IV-2.

---

## 021: Tenant Log Query — Authorization-Plane-Only, Ingestion-Decoupled

**Document**: [`021-tenant-log-query-federation.md`](./021-tenant-log-query-federation.md) (ZH-primary; no EN mirror per the platform language policy — same as ADR-019 / ADR-020)

ADR-020's sister decision, in the opposite direction: ADR-020 lets a tenant **pull** metrics back to its own infra (pull-back), while this ADR lets a tenant query its own logs **in-place** on the platform (query-in-place, not pull-back). The platform **owns the authorization plane only** (tenant identity resolution + query-path isolation enforcement + visibility governance), reusing the existing `helm/federation-gateway` with a third `victorialogs` mode (verify JWT → inject tenant header). Cross-tenant isolation comes 100% from VictoriaLogs' native `(AccountID, ProjectID)` tenancy model — **NOT** prom-label-proxy (LogsQL ≠ PromQL; naively reusing it is the easiest premise mistake to make), **NOT** a self-built LogsQL injector, **NOT** vmauth. Ingestion stamping (how logs get centrally shipped into the platform with a trustworthy tenant identity) is decoupled as "a separate design," constrained here by an **explicit, verifiable contract** (zero-trust payload + node-edge stamping + monotonic, never-recycled AccountID allocation). The 3-layer blast radius is recalibrated for LogsQL (no metrics-style sample cap; a time-range limit is the primary guardrail instead). **Phase 1 — (b) platform operational logs** (a tenant querying the platform's operational observability about itself; the data already exists in the #539 audit stream) targets v2.10.0; **Phase 2 — (a) tenant application logs** is defer-with-trigger.

---

## 022: tenant-api Dev-Auth Bypass — Four-Layer Containment

**Document**: [`022-dev-auth-bypass-four-layer-containment.md`](./022-dev-auth-bypass-four-layer-containment.md) (ZH-primary; no EN mirror per the platform language policy)

v2.8.1 (Accepted). tenant-api itself does no login — it trusts the `X-Forwarded-Email` / `X-Forwarded-Groups` headers injected by an upstream oauth2-proxy. try-local Mode 0 (`docker compose up da-portal tenant-api`, no oauth2-proxy) lacks them, so `/me` returns 401 and the flagship Tenant Manager cannot open. A default-off `--dev-bypass-auth` (`TA_DEV_BYPASS_AUTH`) injects a dev identity (`dev@local` / `demo-admins`) when `X-Forwarded-Email` is absent, and never overrides a real forwarded identity. It is **identity-only — RBAC is still enforced** (no god-mode). The risk of shipping an auth-bypass in the production binary is contained in four layers (matching the project's four-layer-defense culture): **L1** default-off; **L2** observable tripwire (`X-Dev-Auth-Bypass: active` response header + `tenant_api_dev_auth_bypass_active` gauge + loud startup WARN); **L3** runtime poison pill (panics, fail-closed, if it detects `KUBERNETES_SERVICE_HOST` / a serviceaccount token); **L4** deploy-time SAST (`check_dev_bypass_manifest.py` hard-blocks the flag in `helm/`, `k8s/`, operator manifests via pre-commit + CI). L1–L2 stop honest accidents; L3–L4 stop misdeployment. Tracker #464.

---

## 023: tenant-api Write Plane — Single-Writer Invariant & Resilience Containment

**Document**: [`023-write-plane-single-writer-invariant.md`](./023-write-plane-single-writer-invariant.md) (ZH-primary; no EN mirror per the platform language policy)

v2.9.0 (Accepted, accepted 2026-06-06). Promotes the implicit "one writer per process" assumption in `gitops.Writer` to an **explicit invariant**: the read plane is stateless and horizontally scalable, the write plane MUST be a single writer. Clarifies the boundary against ADR-020's stateless-multi-replica federation goal. Three-layer enforcement: Helm static guard (`replicaCount>1` fails) → rolling-update overlap (the "phantom replica" static checks miss) mitigated by `strategy: Recreate` as a zero-code interim → read/write split deployment (the CQRS landing target; needs a read-only enforcement mode + method routing) → K8s Lease leader-election (the watertight, deferred option). Single-writer resilience patterns: (1) fresh anchor via in-lock `git fetch --prune` + an independent `TA_GIT_FETCH_TIMEOUT` (option A; option B's fetch-outside-lock is rejected for its TOCTOU window); (2) load-shedding semaphore whose context binding covers only the queueing stage (work inside the critical section runs to completion); (3) `isForgeDegradation` recognizing GitHub secondary-rate-limit 403 and honoring `Retry-After`. The sub-items span stale-anchor refresh, 403 circuit-breaking, load-shedding, the phantom-replica fix, and the read/write split. From a chaos-engineering external review, fact-checked line-by-line + adversarial subagent re-verification.

---

## 024: Version-Aware Threshold — Declarative Cutover via the Existing Dimensional version Label

**Document**: [`024-version-aware-threshold-via-dimensional-label.en.md`](./024-version-aware-threshold-via-dimensional-label.en.md)

v2.9.0 (Proposed). Answers the tenant ask to "pre-stage rules that activate on the app version bump" + "know which version is running when troubleshooting". **Current-state correction**: the existing dimensional-label mechanism (`container_cpu{version="v1"}: "80"`) already emits the target `user_threshold{...,version="v1"}` shape with **zero threshold-exporter parse/emit change** (verified against `config/resolve.go` dimensional path + `collector.go` CustomLabels emit), so Phase 1's core is a **rule-pack normalize layer** (`:vlabeled` recording rule + `by(tenant, version)` join + `label_replace(..,"version","default","version","^$")` fallback, **never** `or vector(0)`), not a new config schema — i.e. **Option A reuse-over-build**, with the `versioned:` dedicated block demoted to defer-with-trigger. Cutover is **emergent**: whichever `version` the metric carries after a deploy joins the matching threshold, auto-immunizing K8s rolling/rollback/GitOps-propagation-lag failure modes. **Orthogonal to and coexisting with** [§2.6 scheduled thresholds](../design/config-driven.en.md) (recurring time window vs state-based version label); #423 §4 R1 rejected pushing absolute dates into `ScheduledValue`. Top risk: **observed-but-not-declared = silent alerting gap** (a false negative, mitigated by the `version_unknown` sentinel emitting immediately). Two independently-deployable halves (threshold-side ships zero-change first; metric-side `kube_pod_labels` relabel follows, 100% backward-compatible in between). Draft; #423 is the epic SOT; status → accepted at v2.9.0 GA. Open-question to-dos: OQ-1 pipeline contract needs tenant-team sign-off; OQ-6 guard scopes to piloted components only.

---

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.en.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — Platform engineer quick start guide
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — AI development context guide
