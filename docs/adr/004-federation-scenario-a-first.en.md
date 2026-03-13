---
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.0.0-preview.2
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
- Documentation recorded ([`docs/federation-integration.en.md`](../federation-integration.en.md))
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

Expected smooth upgrade when implementing Scenario B in v2.0:
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

No direct architectural dependencies. This decision is purely a prioritization choice.

## Roadmap Plan

- **v1.13.0**: Scenario A documentation refinement, real customer validation
- **v1.14.0** (P2): Early prototype and technical evaluation for Scenario B
- **v2.0**: Complete Scenario B implementation, dual-stack compatibility mode

## References

- [`docs/federation-integration.en.md`](../federation-integration.en.md) — Scenario A detailed integration guide
- [`docs/scenarios/multi-cluster-federation.en.md`](../scenarios/multi-cluster-federation.en.md) — Multi-cluster scenario examples
- [`CHANGELOG.md`](../../CHANGELOG.md) — v1.12.0 Federation initial implementation notes

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](adr/001-severity-dedup-via-inhibit.en.md) | ★★★ |
| [002-oci-registry-over-chartmuseum.en](adr/002-oci-registry-over-chartmuseum.en.md) | ★★★ |
| [003-sentinel-alert-pattern.en](adr/003-sentinel-alert-pattern.en.md) | ★★★ |
| [004-federation-scenario-a-first.en](adr/004-federation-scenario-a-first.en.md) | ★★★ |
| [005-projected-volume-for-rule-packs.en](adr/005-projected-volume-for-rule-packs.en.md) | ★★★ |
| [README.en](adr/README.en.md) | ★★★ |
| ["Architecture and Design — Multi-Tenant Dynamic Alerting Platform Technical Whitepaper"](./architecture-and-design.en.md) | ★★ |
| ["Project Context Diagram: Roles, Tools, and Product Interactions"](./context-diagram.en.md) | ★★ |
