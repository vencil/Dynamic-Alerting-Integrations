---
title: "ADR-004: Federation Architecture — Central Exporter First"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---

# ADR-004: Federation Architecture — Central Exporter First

> **Language / 語言：** **English (Current)** | [中文](004-federation-central-exporter-first.md)

**Decision in brief**: multi-cluster deployments are supported first as "central exporter + edge Prometheus": one threshold-exporter runs in the central cluster and serves every edge cluster. The "one exporter per edge cluster" architecture is deferred and added later.

## Status

✅ **Accepted** (v1.12.0) → **Extended** (v2.1.0+: both architectures now implemented)

## Terms

- **Federation**: bringing data from several Prometheus servers together in one place. Here it means the platform's own cross-cluster deployment: edge clusters collect metrics, and the central cluster manages thresholds and alerting for all of them.
- **Central cluster / edge cluster**: the central cluster handles unified monitoring and alerting; edge clusters are the Kubernetes clusters that each run their own workloads and databases.

## Background

Enterprises usually spread workloads across several Kubernetes clusters and need unified alert management. There are two main architectures for a multi-cluster deployment:

**Central Exporter + Edge Prometheus**

- A single threshold-exporter is deployed centrally and serves all edge clusters.

**Edge Exporter + Central Aggregation**

- Each edge cluster deploys its own threshold-exporter.
- Central Prometheus aggregates edge data via federation scrape or `remote_write`.
- Complexity: N exporter instances, N configurations, plus central coordination logic.

### Decision Criteria

| Criterion | Central Exporter | Edge Exporter |
|:-----|:-----:|:-----:|
| Exporter deployments | 1 | N |
| Configuration complexity | Low | High |
| Use cases covered (estimate at decision time) | ~80% | ~20% |
| Implementation time | Short | Long |

## Decision

**Implement the "central exporter + edge Prometheus" architecture first.**

The estimate at decision time was that about four in five enterprises run centrally managed monitoring (one alerting policy, a single exporter handling several clusters). Building this architecture first covers most cases in less time.

## Rationale

### Architectural Simplicity

**Central exporter**: configuration is managed in one place. There is a single exporter deployment, made highly available with multiple replicas, at low cost. Edge Prometheus servers don't depend on each other and need no coordination logic.

**Edge exporter**: every edge needs its own configuration, and the centre has to track N instances; upgrading N exporters requires coordination; and aggregating edge data centrally can produce duplicated or missing data.

### Time and Resources

The estimate at decision time was that the edge exporter architecture needed an extra 6–8 weeks of development (instance management framework, aggregation logic, multi-layer configuration validation).

## Consequences

### Positive Impact

- Multi-cluster support ships sooner and covers most use cases.
- Lower operational burden early on.
- Lays the API and tooling groundwork for the later edge exporter architecture.
- Customers can adopt gradually: start with the central architecture and upgrade later as needed.

### Negative Impact

- While only the central architecture was supported (v1.x), use cases needing edge clusters to operate autonomously were not supported.
- If demand for edge exporters is high, part of the design has to be reworked.

### Migration Path

For the steps to move from the central to the edge architecture, see [Federation Integration Guide §8.5](../integration/federation-integration.en.md#85-migrating-from-central-evaluation-to-edge-evaluation).

### Current Tooling

- `da-tools federation-check` verifies the edge cluster, the central cluster, or end to end (`edge` / `central` / `e2e`).
- `da-tools rule-pack-split` splits Rule Packs into an edge normalisation part and a central part, and can output the PrometheusRule CRDs (Kubernetes custom resources) used by the Prometheus Operator.
- `da-tools operator-generate --kustomize` generates a `kustomization.yaml` listing all CRD files; `da-tools drift-detect --mode operator` compares the PrometheusRule CRDs on a cluster with local files.

## Alternatives Considered

| Approach | Verdict | Reason |
|------|------|------|
| Implement both architectures at once | Rejected | Delays the schedule, too much complexity up front, hard to test |
| Implement only the edge architecture | Rejected | Goes against shipping the smallest usable version first, and holds up customer timelines |

## Related

- [ADR-006: Tenant Mapping Topologies](./006-tenant-mapping-topologies.en.md) — 1:N mapping via data-plane recording rules on top of the central exporter
- [ADR-005: Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.en.md) — how Rule Packs are mounted in a multi-cluster deployment
- [Federation Integration Guide](../integration/federation-integration.en.md) — detailed integration steps
- [Scenario: Multi-Cluster Federation](../scenarios/multi-cluster-federation.en.md) — a multi-cluster deployment walkthrough
