---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.3.0
lang: en
---

# ADR-004: Federation Scenario A First

> **Language / 語言：** **English (Current)** | [中文](004-federation-scenario-a-first.md)

## Status

✅ **Accepted** (v1.12.0)

## Background

The Multi-Tenant Dynamic Alerting platform faces multi-cluster monitoring requirements. Enterprises typically run workloads across multiple Kubernetes clusters and need unified alert management.

### Two Main Federation Scenarios

**Scenario A: Central Exporter + Edge Prometheus**
- Single threshold-exporter deployed centrally, serving all edge clusters
- Edge Prometheus reads metrics from the central exporter via `remote_read` in `prometheus.yml`
- Edge Prometheus only needs local rules; alerts handled by local or central Alertmanager

**Scenario B: Edge Exporter + Central Aggregation**
- Each edge cluster deploys independent threshold-exporter instances
- Central Prometheus aggregates edge data via federation scrape or remote_write
- Complexity: N exporter instances, N configurations, central coordination logic

### Decision Criteria

| Criterion | Scenario A | Scenario B |
|:-----|:-----:|:-----:|
| Exporter Deployments | 1 | N |
| Configuration Management Complexity | Low | High |
| Use Case Coverage % | ~80% | ~20% |
| Implementation Time | Short | Long |

## Decision

**Prioritize Federation Scenario A (Central Exporter + Edge Prometheus). Defer Scenario B to P2 (Roadmap).**

## Rationale

### 80-20 Principle

Based on customer research, most enterprises adopt centralized monitoring architectures:

- 80%: Unified alert policy, single exporter instance sufficient for multi-cluster
- 20%: Edge autonomy, requiring edge exporter instances (cloud-edge collaboration, high autonomy)

Scenario A covers the majority of use cases, enabling faster go-to-market.

### Architecture Simplicity

**Scenario A**:
- Configuration Centralization: Tenant configuration managed centrally, all Prometheus instances sync pulling
- Exporter HA: Single exporter deployed with HA (multiple replicas), low cost
- No Coordination Logic: Edge Prometheus instances have no interdependencies

**Scenario B**:
- Distributed Configuration: Each edge requires independent configuration; central must track N instances
- Exporter Management: N exporter versions, patches, upgrades requiring coordination
- Synchronization Complexity: Central must aggregate edge data, risk of duplication or loss

### Time and Resource Considerations

Core development work for Scenario A is already complete (v1.12.0):
- `remote_read` integration testing complete
- Documentation recorded ([`docs/federation-integration.en.md`](../federation-integration.md))
- Typical deployment time: 2-3 hours

Scenario B requires additional development:
- Edge exporter instance management framework
- Central aggregation logic (dedup, ordering)
- Multi-tier configuration validation
- Estimated development time: 6-8 weeks

## Consequences

### Positive Impact

✅ Rapidly launch Federation support, satisfying 80% of use cases
✅ Simplify initial operational burden
✅ Establish API/tool foundation for subsequent Scenario B
✅ Customers can adopt progressively, upgrade from A to B later

### Negative Impact

⚠️ Edge autonomy use cases delayed
⚠️ Risk of redesign if Scenario B demand becomes critical
⚠️ Some "edge exporter autonomous configuration" scenarios unsupported in v1.x

### Migration Path

Smooth upgrade path when Scenario B is implemented in the future:
- API Compatibility guarantee: No modification needed for existing Scenario A deployments
- Tool Support: `scaffold_tenant.py` extended to support edge exporter configuration
- Documentation Guide: Clear steps for switching to Scenario B

## Alternative Approaches Considered

### Approach A: Implement Both A and B Simultaneously (Rejected)
- Pros: Comprehensive coverage
- Cons: Timeline delay, excessive initial complexity, difficult testing

### Approach B: Implement Only Scenario B (Rejected)
- Pros: More powerful
- Cons: Violates Minimum Viable Product (MVP) principle, delays customer timelines

## Related Decisions

- [ADR-006: 1:N Tenant Mapping Topologies](./006-tenant-mapping-topologies.en.md) — Builds on Scenario A's data-plane Recording Rules for 1:N mapping
- [ADR-005: Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.en.md) — Rule Pack mounting mechanism used in Federation scenarios

## Current Status & Next Steps

- **v1.12.0** (completed): Scenario A core implementation, `remote_read` integration tests, documentation
- **v2.1.0** (completed): `federation_check.py` migrated to shared `query_prometheus_instant`, edge/central dual-mode validation
- **Future**: Scenario B (Rule Pack layering) remains a future direction, see [`architecture-and-design.md` §5.1](../architecture-and-design.md)

## References

- [`docs/federation-integration.en.md`](../federation-integration.md) — Scenario A detailed integration guide
- [`docs/scenarios/multi-cluster-federation.en.md`](../scenarios/multi-cluster-federation.md) — Multi-cluster scenario examples
- `CHANGELOG.md` — v1.12.0 Federation initial implementation notes

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐⭐⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐⭐ |
| [004-federation-scenario-a-first.en](004-federation-scenario-a-first.en.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| ["Architecture and Design"](../architecture-and-design.md) | ⭐⭐ |
| ["Architecture & Design — Appendix A"](../architecture-and-design.en.md#appendix-a-role--tool-quick-reference) | ⭐⭐ |
