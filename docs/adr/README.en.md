---
tags: [adr, architecture]
audience: [platform-engineers]
version: v1.13.0
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

## 004: Federation Scenario A First

**Document**: [`004-federation-scenario-a-first.en.md`](./004-federation-scenario-a-first.en.md)

Prioritize Federation Scenario A implementation: central exporter + edge Prometheus. This approach is simple (single exporter deployment), covering 80% of federation use cases; Scenario B (edge exporter) deferred to P2.

---

## 005: Projected Volume for Rule Packs

**Document**: [`005-projected-volume-for-rule-packs.en.md`](./005-projected-volume-for-rule-packs.en.md)

Use Projected Volume with `optional: true` to implement selective Rule Pack unloading for 15 Rule Packs. Tenants can delete individual ConfigMaps to disable specific Rule Packs; Prometheus does not fail when packs are missing.

---

## Related Documents

- [`docs/architecture-and-design.en.md`](../architecture-and-design.en.md) — Complete architecture design
- [`docs/getting-started/for-platform-engineers.en.md`](../getting-started/for-platform-engineers.en.md) — Platform engineer quick start guide
- [`CLAUDE.md`](../../CLAUDE.md) — AI development context guide

