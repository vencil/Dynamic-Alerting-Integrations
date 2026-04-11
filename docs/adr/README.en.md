---
title: "Architecture Decision Records (ADR)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
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

---

## 001: Severity Dedup via Inhibit Rules

**Document**: [`001-severity-dedup-via-inhibit.en.md`]

Use Alertmanager inhibit_rules instead of PromQL `absent()`/`unless()` for severity deduplication. Key consideration: preserve TSDB integrity where all severity levels of the same metric are recorded, with intelligent suppression performed at the Alertmanager layer.

---

## 002: OCI Registry over ChartMuseum

**Document**: [`002-oci-registry-over-chartmuseum.en.md`]

Consolidate Helm charts and Docker images distribution via ghcr.io OCI registry, eliminating dependency on a standalone ChartMuseum. Requires Helm 3.8+, but significantly simplifies operational overhead.

---

## 003: Sentinel Alert Pattern

**Document**: [`003-sentinel-alert-pattern.en.md`]

Implement tri-state mode (Normal/Silent/Maintenance) via exporter flag metric → recording rule → sentinel alert → inhibit flow. Compared to direct PromQL suppression, this pattern provides strong composability and easier debugging.

---

## 004: Federation Architecture — Central Exporter First

**Document**: [`004-federation-central-exporter-first.en.md`](./004-federation-central-exporter-first.en.md)

Prioritize "Central Exporter + Edge Prometheus" architecture (80-20 principle). v1.12.0 core implementation complete; v2.1.0 Edge Exporter architecture also implemented (`rule-pack-split`); v2.6.0 extends multi-cluster CRD deployment and drift detection.

---

## 005: Projected Volume for Rule Packs

**Document**: [`005-projected-volume-for-rule-packs.en.md`]

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

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.md) — Platform engineer quick start guide
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — AI development context gui
