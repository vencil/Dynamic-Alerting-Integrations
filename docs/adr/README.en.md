---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.5.0
lang: en
---

# Architecture Decision Records (ADR)

> **Language / 語言：** **English (Current)** | [中文](README.md)

This directory contains Architecture Decision Records (ADRs) for the Multi-Tenant Dynamic Alerting platform. Each ADR documents the background, option evaluation, and long-term impact of a specific design decision.

## ADR Index

| ID | Title | Status | Summary |
|:---|:-----|:-----|:-----|
| [001](#001-severity-dedup-via-inhibit-rules) | Severity Dedup via Inhibit Rules | ✅ Accepted | Use Alertmanager inhibit_rules instead of PromQL for severity deduplication, preserving TSDB completeness |
| [002](#002-oci-registry-over-chartmuseum) | OCI Registry over ChartMuseum | ✅ Accepted | Consolidate Helm charts and Docker images distribution via ghcr.io OCI registry, eliminating ChartMuseum dependency |
| [003](#003-sentinel-alert-pattern) | Sentinel Alert Pattern | ✅ Accepted | Implement tri-state control via sentinel alerts + inhibit, replacing direct PromQL suppression |
| [004](#004-federation-scenario-a-first) | Federation Scenario A First | ✅ Accepted | Prioritize central exporter + edge Prometheus federation pattern |
| [005](#005-projected-volume-for-rule-packs) | Projected Volume for Rule Packs | ✅ Accepted | Use Projected Volume with optional:true to implement selectable Rule Pack unloading |
| [006](#006-tenant-mapping-topologies) | Tenant Mapping Topologies (1:1, N:1, 1:N) | ✅ Accepted | Data plane Recording Rules resolve three instance-tenant mapping topologies; exporter zero changes |
| [007](#007-cross-domain-routing-profiles-and-domain-policies) | Cross-Domain Routing Profiles and Domain Policies | ✅ Accepted | Routing Profiles (reuse) + Domain Policies (constraints) two-layer architecture |

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

## 004: Federation Scenario A First

**Document**: [`004-federation-scenario-a-first.en.md`]

Prioritize Federation Scenario A implementation: central exporter + edge Prometheus. This approach is simple (single exporter deployment), covering 80% of federation use cases; Scenario B (edge exporter) deferred to P2.

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

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.md) — Platform engineer quick start guide
- [`CLAUDE.md`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/CLAUDE.md) — AI development context guide

