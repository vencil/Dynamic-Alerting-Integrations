---
title: "ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.6.0
lang: en
---

# ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)

> **Language / 語言：** **English (Current)** | [中文](006-tenant-mapping-topologies.md)

## Status

✅ **Accepted** (v2.1.0) — Toolchain completed, 1:N end-to-end integration pending production validation

## Background

In enterprise environments, the mapping between database instances and tenants is not always one-to-one. As the platform scales, three common topology patterns emerge:

### Topology Classification

| Topology | Description | Typical Scenarios |
|:---------|:------------|:------------------|
| **1:1** | One namespace/instance maps to one tenant | Independently deployed microservices, dedicated DB |
| **N:1** | Multiple namespaces/instances aggregate to one tenant | Multi-cluster same tenant, DR pairs, read-write splitting |
| **1:N** | Single instance contains multiple logical tenants | Oracle multi-schema, DB2 multi-tablespace, shared RDS |

### Problem Statement

The platform's current 1:1 mapping is implicit (`namespace` label = tenant ID), and N:1 is already supported via `scaffold_tenant.py --namespaces` with `relabel_configs` regex. However, **1:N topology** lacks native support:

- Metrics from a single DB instance contain only an `instance` label, without tenant dimensions
- The exporter cannot perceive internal schema/tablespace partitioning within an instance
- Alert rules cannot differentiate thresholds for different tenants on the same instance

A general-purpose solution is needed to split instance-level metrics into tenant-level metrics without modifying the threshold-exporter core logic.

## Decision

**Resolve mapping at the Data Plane via Prometheus Recording Rules; the Control Plane (threshold-exporter) requires zero changes.**

Specific mechanisms:

1. **1:1 (Default)**: Maintain current implicit mapping of `namespace` label = tenant ID
2. **N:1 (Already Implemented)**: Prometheus `relabel_configs` regex aggregates multiple namespaces to a single tenant label
3. **1:N (New)**: Config-driven `instance_tenant_mapping` → auto-generated Rule Pack Part 1 Recording Rules

### 1:N Implementation Architecture

```yaml
# _instance_mapping.yaml — new config file (in config-dir)
instance_tenant_mapping:
  oracle-prod-01:
    - tenant: db-a
      filter: 'schema=~"app_a_.*"'
    - tenant: db-b
      filter: 'schema=~"app_b_.*"'
  db2-shared-01:
    - tenant: db-c
      filter: 'tablespace="ts_client_c"'
    - tenant: db-d
      filter: 'tablespace="ts_client_d"'
```

```yaml
# Auto-generated Recording Rule (Rule Pack Part 1: Data Normalization)
groups:
  - name: tenant_mapping_oracle-prod-01
    rules:
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_a_.*"}
        labels:
          tenant: db-a
      - record: tenant_mapped:oracle_sessions:current
        expr: oracle_sessions{instance="oracle-prod-01", schema=~"app_b_.*"}
        labels:
          tenant: db-b
```

## Rationale

### Why Data Plane Mapping

**Zero Exporter Changes**: The threshold-exporter's responsibility is "YAML → Metrics"; it should not bear instance-to-tenant mapping logic. Mapping is data normalization and belongs at the Prometheus layer.

**Natural Advantages of Recording Rules**:
- Generated time series carry the `tenant` label natively; downstream alert rules and Dashboard queries require no modification
- TSDB retains both original instance metrics and mapped tenant metrics, supporting dual-perspective analysis
- Recording Rule computation completes within the Prometheus evaluation cycle, with no additional latency

**Unified Management**: All three topologies converge to the same representation — all alert rules can assume the `tenant` label exists.

### Why Reject Solving in the Exporter

- **Scope Creep**: The exporter would become a "data normalization engine", violating single responsibility
- **Restart Cost**: Mapping changes require exporter restart, affecting metric collection for all tenants
- **Multi-Exporter Sync**: Under HA deployment, two exporters need consistent mappings, introducing distributed consistency challenges

### Why Reject Solving in Alertmanager

- **Too Late**: Alertmanager only processes already-triggered alerts; it cannot split at the metric level
- **Dashboard Blind Spot**: Tools like Grafana query Prometheus directly, bypassing Alertmanager

## Consequences

### Positive Impact

✅ threshold-exporter remains completely unaware of topology complexity, staying simple
✅ All three topologies converge via a unified `tenant` label; downstream rules and Dashboards require zero changes
✅ Recording Rules support hot reload (configmap-reload), no Prometheus restart needed
✅ TSDB retains both original and mapped metrics, supporting multi-dimensional retrospective analysis
✅ Integrates seamlessly with existing Rule Pack architecture (ADR-005 Projected Volume)

### Negative Impact

⚠️ Recording Rules generate additional time series, increasing TSDB storage (~2× per mapped metric)
⚠️ Need to develop `generate_tenant_mapping_rules.py` tool to auto-generate Recording Rules
⚠️ 1:N mapping filter syntax requires tenants to understand underlying DB schema/tablespace structure

### Operational Considerations

- `generate_tenant_mapping_rules.py` outputs as Rule Pack Part 1 ConfigMap, distributed via Projected Volume
- CI validation: tenant IDs referenced in mappings must have corresponding tenant YAML in `config-dir`
- Cardinality assessment: each mapping entry generates M recording rules (M = number of mapped metrics); evaluate TSDB impact
- Recommend setting `_cardinality_multiplier` marker for 1:N tenants for capacity planning reference

## Alternative Approaches Considered

### Approach A: Exporter Built-in Mapping (Rejected)
- Pros: Single component handles everything
- Cons: Scope creep, high restart impact, HA consistency issues

### Approach B: Alertmanager-Level Mapping (Rejected)
- Pros: Only affects notification path
- Cons: Cannot split at metric level, Dashboard blind spot

### Approach C: External Proxy (Considered)
- Pros: Fully decoupled
- Cons: Introduces new component, increased latency, high operational complexity

## v2.1.0 Implementation Summary

- `generate_tenant_mapping_rules.py` — auto-generates Recording Rules from `_instance_mapping.yaml`, supporting Oracle/DB2/generic filter syntax (36 tests)
- `discover_instance_mappings.py` — auto-detects instance topology in Prometheus (1:1/N:1/1:N) and outputs suggested mapping configuration
- `scaffold_tenant.py --topology=1:N` — Onboarding integration (with `--mapping-instance`, `--mapping-filter`)
- Example config `conf.d/examples/_instance_mapping.yaml`
- Go/Python dual-side reserved key sync

## Evolution Status

- **v2.1.0** (completed): Core toolchain (`generate_tenant_mapping_rules.py` / `discover_instance_mappings.py` / `scaffold_tenant.py --topology=1:N`), Federation Scenario B support via `rule-pack-split` with edge/central layered mapping behavior
- **v2.1.0 & v2.6.0** (completed): Federated topology validation across central and edge clusters

**Remaining**:
- End-to-end validation in real multi-schema Oracle environments (pending production feedback)
- Schema validation (`_instance_mapping.yaml` JSON Schema) deferred to next cycle

## Related Decisions

- [ADR-005: Projected Volume for Rule Packs](./005-projected-volume-for-rule-packs.md) — Recording Rules distributed via same Projected Volume mechanism
- [ADR-004: Federation Central Exporter First](./004-federation-central-exporter-first.en.md) — Mapping consistency under federation
- [ADR-001: Severity Dedup via Inhibit Rules](./001-severity-dedup-via-inhibit.md) — Mapped metrics still subject to inhibit dedup

## References

- [`docs/architecture-and-design.en.md`](../architecture-and-design.md) §2.3 — Tenant-Namespace Mapping
- [`scaffold_tenant.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/scaffold_tenant.py) — Current `--namespaces` N:1 support
- [`generate_alertmanager_routes.py`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/scripts/tools/ops/generate_alertmanager_routes.py) — Route generator (ADR-007 related)
- [Prometheus Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/) — Official documentation

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [001-severity-dedup-via-inhibit.en](001-severity-dedup-via-inhibit.en.md) | ⭐⭐ |
| [002-oci-registry-over-chartmuseum.en](002-oci-registry-over-chartmuseum.en.md) | ⭐ |
| [003-sentinel-alert-pattern.en](003-sentinel-alert-pattern.en.md) | ⭐⭐ |
| [004-federation-central-exporter-first.en](004-federation-central-exporter-first.en.md) | ⭐⭐⭐ |
| [005-projected-volume-for-rule-packs.en](005-projected-volume-for-rule-packs.en.md) | ⭐⭐⭐ |
| [006-tenant-mapping-topologies.en](006-tenant-mapping-topologies.en.md) | ⭐⭐⭐ |
| [README.en](README.en.md) | ⭐⭐⭐ |
| ["Architecture and Design"](../architecture-and-design.md) | ⭐⭐⭐ |
| ["Architecture & Design — Appendix A"](../architecture-and-design.en.md#appendix-a-role-tool-quick-reference) | ⭐⭐ |
