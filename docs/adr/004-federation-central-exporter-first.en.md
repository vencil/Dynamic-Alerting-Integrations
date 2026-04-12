---
title: "ADR-004: Federation Architecture — Central Exporter First"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: en
---

# ADR-004: Federation Architecture — Central Exporter First

> **Language / 語言：** **English (Current)** | [中文](004-federation-central-exporter-first.md)

## Status

✅ **Accepted** (v1.12.0) → **Extended** (v2.1.0+: both architectures now implemented)

## Background

The Multi-Tenant Dynamic Alerting platform faces multi-cluster monitoring requirements. Enterprises typically run workloads across multiple Kubernetes clusters and need unified alert management.

Federation has two main architectural approaches:

**Central Exporter + Edge Prometheus**
- Single threshold-exporter deployed centrally, serving all edge clusters
- Edge Prometheus reads threshold metrics from the central exporter via `remote_read`
- Edge Prometheus only needs local rules; alerts handled by local or central Alertmanager

**Edge Exporter + Central Aggregation**
- Each edge cluster deploys independent threshold-exporter instances
- Central Prometheus aggregates edge data via federation scrape or remote_write
- Complexity: N exporter instances, N configurations, central coordination logic

### Decision Criteria

| Criterion | Central Exporter | Edge Exporter |
|:-----|:-----:|:-----:|
| Exporter Deployments | 1 | N |
| Configuration Management Complexity | Low | High |
| Use Case Coverage % | ~80% | ~20% |
| Implementation Time | Short | Long |

## Decision

**Prioritize the "Central Exporter + Edge Prometheus" architecture.**

Based on the 80-20 principle: most enterprises adopt centralized monitoring architectures (unified alert policy, single exporter sufficient for multi-cluster). This approach covers the majority of use cases and enables fast delivery.

## Rationale

### Architecture Simplicity

**Central Exporter**: Configuration managed centrally, all Prometheus instances sync pulling. Single exporter deployed with HA (multiple replicas), low cost. Edge Prometheus instances have no interdependencies or coordination logic.

**Edge Exporter**: Each edge requires independent configuration; central must track N instances. N exporter versions require coordinated upgrades. Central must aggregate edge data, risking duplication or loss.

### Time and Resource Considerations

Core development for Central Exporter was completed in v1.12.0: `remote_read` integration testing, documentation ([federation-integration.md](../integration/federation-integration.md)), typical deployment time 2-3 hours. In contrast, the Edge Exporter architecture requires an additional 6-8 weeks (instance management framework, aggregation logic, multi-tier configuration validation).

## Consequences

### Positive Impact

- Rapidly launch Federation support, satisfying most use cases
- Simplify initial operational burden
- Establish API/tool foundation for subsequent Edge Exporter architecture
- Customers can adopt progressively — start with central, upgrade as needed

### Negative Impact

- Edge autonomy use cases unsupported in v1.x
- Partial redesign risk if Edge Exporter demand becomes critical

### Migration Path

Smooth upgrade from central to edge architecture: API compatibility guaranteed (no modification to existing deployments), `scaffold_tenant.py` extended for edge configuration, documentation provides clear switching steps.

## Alternative Approaches Considered

| Approach | Verdict | Reason |
|----------|---------|--------|
| Implement both simultaneously | Rejected | Timeline delay, excessive initial complexity, difficult testing |
| Implement only Edge architecture | Rejected | Violates MVP principle, delays customer timelines |

## Related Decisions

- [ADR-006: Tenant Mapping Topologies](./006-tenant-mapping-topologies.en.md) — Builds on Central Exporter's data-plane Recording Rules for 1:N mapping
- [ADR-005: Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.en.md) — Rule Pack mounting mechanism in Federation scenarios

## Evolution Log

| Version | Status | Change |
|---------|--------|--------|
| v1.12.0 | ✅ Done | Central Exporter core implementation, `remote_read` integration tests, documentation |
| v2.1.0 | ✅ Done | `federation_check.py` supports edge/central dual-mode validation. **Edge Exporter architecture also implemented** — `da-tools rule-pack-split` supports edge normalization + central aggregation + Operator CRD output |
| v2.6.0 | ✅ Done | `operator-generate --kustomize` supports multi-cluster CRD deployment; `drift_detect.py --mode operator` detects cross-cluster CRD drift |

## References

- [`docs/federation-integration.en.md`](../integration/federation-integration.en.md) — Federation detailed integration guide
- [`docs/scenarios/multi-cluster-federation.en.md`](../scenarios/multi-cluster-federation.en.md) — Multi-cluster scenario examples
- `CHANGELOG.md` — v1.12.0 Federation initial implementation notes

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| [Architecture and Design](../architecture-and-design.en.md) | ⭐⭐ |
