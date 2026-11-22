---
title: "ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)"
tags: [adr, architecture]
audience: [platform-engineers]
version: v2.9.0
lang: en
---

# ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)

> **Language / 語言：** **English (Current)** | [中文](006-tenant-mapping-topologies.md)

## Status

✅ **Accepted** (v2.1.0) — Toolchain completed, 1:N end-to-end integration pending production validation
📎 **Addendum** (v2.10.0) — Tenant-attribution policy for infrastructure metrics (disk/PVC/node), see §Addendum below
🧭 **Revision** (2026-11-21) — cluster identity `da_cluster` (one external label per Prometheus) and per-cluster alert granularity for a tenant in several clusters, see "One tenant in several clusters" below; implementation in progress. 2026-11-22: notification grouping is not split per cluster by default

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
✅ Recording Rules support hot reload (config-reloader sidecar — actually deployed only in #1246; before that a manual reload was required), no Prometheus restart needed
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

## One tenant in several clusters: cluster identity and alert granularity

**Decision: one tenant may appear in several clusters. Each Prometheus says which cluster it is in with one external label, `da_cluster`; every rule whose input is tenant-exporter series aggregates and matches per (tenant, da_cluster), and alerts fire once per cluster.**

This section is the contract for the N:1 row above ("multi-cluster same tenant, DR pairs" — a primary site and a standby site, one set each). **It describes the behaviour once the work is complete, and only part of it is in place**: the rule packs already evaluate per cluster, and Alertmanager's warning/critical dedup is per cluster too (point 5); notification grouping stays as it is by decision and is not split per cluster (point 5); per-cluster presence (point 6) is not done yet. It is being implemented in stages, tracked in [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097).

### Why it is needed

Two shapes of rule go wrong when a tenant spans clusters:

- **Masking.** Liveness is written as "instances expected to exist, minus instances that reported success", the two sides paired with `on(tenant, instance)`. When two clusters each have an instance at the same address (overlapping internal ranges are common), the healthy one cancels the failed one and the failed cluster raises no alert.
- **Silent merging.** `sum by(tenant)` adds the two clusters together and `max by(tenant)` keeps only the larger. The on-call engineer cannot tell which cluster it is, and the value no longer corresponds to any one database.

### Contract

1. **Where cluster identity comes from.** Each Prometheus sets `da_cluster: <cluster id>` under `global.external_labels` — one line per Prometheus, and nothing in any scrape job. Prometheus attaches external labels to data as it leaves: alerts sent to Alertmanager, `/federate` and remote write all carry them. So wherever two clusters' series or alerts meet (a central Prometheus, Alertmanager) the data already tells the clusters apart; a Prometheus that evaluates only its own cluster has no such label on its series, and the rules' grouping then reads as per tenant. A single-cluster deployment sets it as well, with one fixed value.
2. **The cluster id.** Unique across the platform, one per cluster, and stable over time; the Prometheus replicas of one cluster (an HA pair) share one value. A deployment that already sets `external_labels.cluster` as the [Federation integration guide](../integration/federation-integration.en.md) describes gives `da_cluster` the same value, next to it.
3. **Thresholds stay one per tenant.** Thresholds, maintenance state and tenant metadata come from threshold-exporter: one set per tenant, not per cluster. When these series arrive federated from another cluster they carry `da_cluster` too; the rules always collapse them `by(tenant)` first and then apply them to each of the tenant's clusters with `on(tenant) group_left` (`group_left` lets one series on the right match several on the left), so the result is the same either way — provided every cluster sends the same thresholds.
4. **How rules are written.** In an aggregation over tenant-exporter series, a `by(...)` that contains any of `tenant`, `instance`, `pod`, `namespace`, `node` must also contain `da_cluster`. The same holds for a pairing (`on(...)`) whose two sides are both such series.
5. **Alerts carry `da_cluster`.** Every alert that reaches Alertmanager has the label: an alert the rules computed with it (the cluster its series came from) keeps that value; one without gets the external label of the Prometheus that sends it — "the cluster of the Prometheus that evaluated this rule". Alertmanager's warning/critical dedup ([ADR-001](./001-severity-dedup-via-inhibit.en.md)) includes it, so the same alert in two clusters does not inhibit its counterpart. Notification grouping (`group_by`, which decides which alerts are combined into one notification) is unchanged by default: the same alert firing in several clusters at once is one notification that lists each cluster's alert with its `da_cluster`. A deployment or tenant that wants one notification per cluster adds `da_cluster` to its own `group_by`. An alert that reads only threshold-exporter series (for example "tenant has not configured a threshold") is of the second kind: it is about the whole tenant, and the cluster on its label only says which Prometheus computed it.
6. **"Expected but absent" is judged per cluster.** A tenant may declare in its configuration which clusters it is expected in; when it does, any declared cluster that reports nothing raises an alert. Without a declaration, appearing in at least one cluster counts as present.
7. **A Prometheus without `da_cluster` is pointed out when it goes live.** `da-tools federation-check edge` reads the running Prometheus' configuration and fails when `external_labels.da_cluster` has no value; the repo's own configuration is held to the same by the scrape-configuration lint. Both look in one place, with no relabelling to work out.

### Example

Each Prometheus' configuration (its own value):

```yaml
global:
  external_labels:
    da_cluster: edge-a
```

Tenant `t1` has one database at `10.0.0.5:50000` in each of `edge-a` and `edge-b`, and the one in `edge-a` is down. The series a central Prometheus receives from the two clusters:

```text
db_expected{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}  1
db_expected{tenant="t1", da_cluster="edge-b", instance="10.0.0.5:50000"}  1
db_up{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}        0
db_up{tenant="t1", da_cluster="edge-b", instance="10.0.0.5:50000"}        1
```

Pairing without cluster identity — no result, no alert (the success in `edge-b` masks `edge-a`):

```promql
max by(tenant, instance) (db_expected)
  unless on(tenant, instance)
max by(tenant, instance) (db_up == 1)
```

Pairing with cluster identity — one result, `{tenant="t1", da_cluster="edge-a", instance="10.0.0.5:50000"}`:

```promql
max by(tenant, da_cluster, instance) (db_expected)
  unless on(tenant, da_cluster, instance)
max by(tenant, da_cluster, instance) (db_up == 1)
```

`db_expected` and `db_up` are names made up for the illustration, not platform metrics.

### Consequences and known limits

- **Upgrade order does not matter.** The rules work on series without `da_cluster` exactly as they did before clusters were told apart; once the external label is set, data leaving that Prometheus starts to carry it.
- **Alerts gain a label on the Alertmanager side.** Once the external label is set, Alertmanager treats an alert carrying it as a different alert: the new one appears and notifies at once, the old one stays until it expires, and the two coexist in between.
- **Existing silences still apply.** A silence matches on the labels it lists; one more label on the alert does not affect it.
- **The cluster is not visible locally in an edge cluster.** Queries against an edge Prometheus, its `ALERTS` series and `$labels.da_cluster` in alert annotations are all empty; the value exists once the data has reached Alertmanager or the central Prometheus.
- **It does not cover one Prometheus that scrapes several clusters directly.** An external label has one value. Such a deployment writes `da_cluster` on each cluster's scrape jobs itself (a value already on a series wins over the external label), and the lint does not check that.
- **Thresholds must agree across clusters.** When each cluster runs its own threshold-exporter and federates it to the centre, the centre collapses the copies `by(tenant)` and takes the larger value; if they differ, the centre uses a set that is no single cluster's own.
- **Some scrape agents add external labels at scrape time** (Prometheus itself does not). Series then carry `da_cluster` locally as well, and the rules give the same result.
- **Sums are per cluster.** Rules that sum "all of the tenant's instances" (for example lock wait and deadlock counts) sum within each cluster and compare each against the threshold.
- **Maintenance mode is the whole tenant.** One cluster cannot be put into maintenance alone.
- **Clusters cannot have different thresholds.** A tenant uses one set of thresholds in all its clusters.
- **Notifications are not split per cluster by default.** The platform ships no notification template, so what people see is the output of Alertmanager's built-in templates. When two clusters fire at once, the email body, the PagerDuty details and the webhook payload list each alert with its `da_cluster`; the one-line title is only `[FIRING:2] <alert name> <tenant>`, without the cluster (when a single cluster fires, the title does show the cluster, because it is then a label common to the whole group). The built-in Slack template has a title and no body, so with two clusters firing it does not show which two; that needs a custom template that lists the alerts. In PagerDuty one group is one incident, resolved once every cluster has recovered. The default is left alone because every tenant route carries its own explicit `group_by` (from the deployment's `_routing_defaults`, a routing profile, or the tenant itself), so changing the platform default would reach only some tenants; the other reason is the first cost below.
- **Adding `da_cluster` to `group_by` has two costs.** First, an alert about the whole tenant (second half of point 5) carries the cluster of the Prometheus that evaluated it, so with several Prometheus evaluating the same rules it becomes one notification per Prometheus. Second, changing a route's `group_by` on a running Alertmanager makes every currently firing group of that route notify once more (the group key changes), and receivers keyed on the group (PagerDuty) open new incidents. The configuration is shown in the [BYO Alertmanager integration guide](../integration/byo-alertmanager-integration.en.md).

### Alternatives considered

- **Reuse the name `cluster`.** The RabbitMQ and Elasticsearch exporters emit their own `cluster` (that service's own cluster name), and Prometheus has two behaviours for a label of the same name, both of which go wrong. By default it keeps the value the scrape configuration writes and renames the exporter's to `exported_cluster`, so a rule that reads the exporter's `cluster` reads the platform's cluster id. With `honor_labels: true` (which the central job that federates from the edges sets) it keeps the exporter's value, and cluster identity becomes the service's own cluster name. Hence a platform-specific name.
- **Write `da_cluster` with a relabel step on every scrape job.** This was the first form of this section; its appeal is that series carry the value at rule-evaluation time. But there are four places to write it (tenant exporters, container metrics, volume metrics, Pod state metrics), one of them limited to a single Service; with only some of them written, the container alerts that match two of those sources find no match and go silent. Stopping such a configuration before deployment means a checker has to work out statically what the relabelling leaves — redoing Prometheus' own behaviour, with no end to the cases. And the only place the rules need cluster identity is where two clusters' data meet, where the data already carries the external label.
- **Add `da_cluster` only where a label like `instance` is already present, and keep tenant-level aggregates one per tenant across clusters.** Far fewer places to change. But when rules are deployed in layers ([ADR-004](./004-federation-central-exporter-first.en.md), and the "edge evaluation" section of the [Federation integration guide](../integration/federation-integration.en.md)) the tenant-level recording rules are computed in each edge cluster: two edges each send a series of the same name to the centre, and the result differs from "everything evaluated centrally". Per cluster is the coarsest granularity at which both deployment modes agree. Ratio rules also cannot have per-edge ratios recombined into a whole-tenant ratio.
- **Do not support it, and reject tenants that span clusters.** Rejecting them needs cluster identity first; and DR pairs and multi-site deployments are exactly what the N:1 row lists.

## Addendum: Tenant Attribution for Infrastructure Metrics (v2.10.0)

**Decision: disk, PVC, and node metrics support 1:1 tenant attribution only (one namespace = one tenant).**

ADR-024's self-service alerts now let tenants set conditions on platform infrastructure metrics (e.g. kubelet's `kubelet_volume_stats_*`). These metrics natively carry only `namespace`, not `tenant`, so at scrape time the `namespace` is used directly as the `tenant` — the 1:1 approach this ADR already uses.

**Why N:1 and 1:N are out of scope here.** This ADR's N:1 (several namespaces aggregated into one tenant) and 1:N (one instance split into several logical tenants) rely on an extra mapping layer; infrastructure metrics are attributed by a plain rename at scrape time and never reach that layer:

- **N:1 would silently miss alerts.** The metric is labeled `tenant="db-a-read"`, but the tenant's config uses the name `db-a` — they don't match, so the alert never fires.
- **1:N is structurally impossible.** A single PVC cannot be split per schema / tablespace across tenants.

**Keeping "unsupported" from becoming "silently broken."** ADR-024 ships a per-tenant watchdog alert, `CustomRecipeDiskInert`: if a tenant declared a disk alert and its pods are running but the attributed metric never appears, it fires. So an N:1 customer gets a clear "this alert isn't working" signal on migration day instead of a silent miss. When an N:1 customer (e.g. a read/write-split database) actually needs infrastructure alerts, the mapping can be added then.

Related: [ADR-024](024-version-aware-threshold-via-dimensional-label.md), node-health alerting (#809, which uses the same 1:1 attribution), the epic (#692).

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
| ["Architecture & Design — Appendix A"](../architecture-and-design.en.md#appendix-a-role--tool-quick-reference) | ⭐⭐ |
