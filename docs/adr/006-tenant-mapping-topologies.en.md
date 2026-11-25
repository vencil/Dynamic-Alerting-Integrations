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

✅ **Accepted** (v2.1.0).

Revised 2026-06-14: infrastructure metrics such as disk, PVC and node support 1:1 attribution only (see "Tenant attribution of infrastructure metrics (Addendum)").

Revised 2026-11-21: when one tenant runs in several clusters, each Prometheus names its cluster with the external label `da_cluster`, and rules and alerts are computed per cluster (see "One tenant in several clusters").

Revised 2026-11-22: notification grouping is not split per cluster by default; a tenant can declare the exporters it expects per cluster (`kind@cluster`); the two Kubernetes "configured but no matching data" checks are judged per cluster.

Revised 2026-11-23: tenant self-service alerts (`Custom_*`) are judged per cluster, except the `absence` recipe, which stays tenant-wide; `da-tools byo-check prometheus` and `da-tools onboard` also point out a Prometheus without `da_cluster`.

Revised 2026-11-24: rewritten to describe current behavior; the decision is unchanged. Two limitations added: the generated 1:N mapping rules are not in the Prometheus deployment the platform ships, and the platform's rule packs do not read the mapped series.

Revised 2026-11-25: the warning and the critical of one `metric_group` must have the same cluster scope, checked in CI; what is per cluster and what is not in "One tenant in several clusters" now describes the current state, with two limitations added for `MariaDBNoPrimary` and `da-tools migrate`.

## Decision

**The mapping between tenants and instances is handled in the data plane: scrape configuration and Prometheus recording rules (queries computed ahead of time and stored as new series). threshold-exporter does not handle mapping.** Each of the three topologies has one mechanism:

1. **1:1 (default)**: one namespace is one tenant. The scrape writes `namespace` into `tenant`; no extra configuration.
2. **N:1**: several namespaces belong to one tenant. A `relabel_configs` regex in the scrape configuration labels all of them with the same `tenant`; `scaffold_tenant.py --namespaces ns1,ns2` produces that snippet.
3. **1:N**: one database instance holds several logical tenants (for example several Oracle schemas). An `_instance_mapping.yaml` at the root of the config directory drives `generate_tenant_mapping_rules.py`, which produces recording rules that split the instance-level series by filter and add `tenant` to each part.

A common case of N:1 is one tenant running in several clusters. Each Prometheus then also says which cluster it is in, and rules are computed per (tenant, cluster); see "One tenant in several clusters" below.

### Example: a 1:N mapping

`_instance_mapping.yaml` at the root of the config directory (the example file in the repository):

```yaml {.quote src="components/threshold-exporter/config/conf.d/examples/_instance_mapping.yaml"}
instance_tenant_mapping:
  # Oracle: single instance, multiple schemas → multiple tenants
  oracle-prod-01:
    - tenant: db-a
      filter: 'schema=~"app_a_.*"'
    - tenant: db-b
      filter: 'schema=~"app_b_.*"'

  # DB2: single instance, multiple tablespaces → multiple tenants
  db2-shared-01:
    - tenant: db-c
      filter: 'tablespace="ts_client_c"'
    - tenant: db-d
      filter: 'tablespace="ts_client_d"'
```

Output of `generate_tenant_mapping_rules.py --config-dir <dir> --metrics oracle_sessions` (exit code 0):

```yaml
groups:
- name: tenant_mapping_db2-shared-01
  interval: 30s
  rules:
  - record: tenant_mapped:oracle_sessions:current
    expr: oracle_sessions{instance="db2-shared-01", tablespace="ts_client_c"}
    labels:
      tenant: db-c
  - record: tenant_mapped:oracle_sessions:current
    expr: oracle_sessions{instance="db2-shared-01", tablespace="ts_client_d"}
    labels:
      tenant: db-d
- name: tenant_mapping_oracle-prod-01
  interval: 30s
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

One rule group per instance, one rule per (entry, metric). `filter` is a comma-separated list of `label=`, `label=~` and `label!=` matchers; anything else goes into the selector as written. Without `--metrics`, every metric of `metric-dictionary.yaml` is used. `--format configmap` wraps the same rules in a ConfigMap named `rules-tenant-mapping` (key `tenant-mapping-rules.yaml`).

With `--validate`, the tool checks that every entry's tenant has a tenant file in the config directory. If the directory above holds tenant files for `db-a` and `db-b` only, it prints these three lines, exits with code 1 and produces no rules:

```text
ERROR: instance=db2-shared-01: tenant 'db-c' not found in config-dir
ERROR: instance=db2-shared-01: tenant 'db-d' not found in config-dir
ERROR: validation failed — fix errors above before generating rules
```

Related tools: `da-tools discover-mappings` (`discover_instance_mappings.py`) reads an exporter's `/metrics` or queries Prometheus, lists the values of labels such as schema and tablespace in an instance, and writes an `_instance_mapping.yaml` draft whose tenant names you fill in; `scaffold_tenant.py --topology=1:N --mapping-instance <instance> --mapping-filter <filter>` adds to its draft config directory an `_instance_mapping.yaml` holding only this tenant, which you merge into the real one yourself.

### One tenant in several clusters: cluster identity and alert granularity

**One tenant may appear in several clusters. Each Prometheus says which cluster it is in with one external label, `da_cluster`; every rule whose input is tenant-exporter series aggregates and matches per (tenant, da_cluster), and alerts fire once per cluster.**

This is the contract for the N:1 scenarios "one tenant in several clusters" and "DR pairs (a primary and a standby site, one stack each)". Per cluster are: the rule packs, Alertmanager's warning/critical dedup, the exporter-absence alerts (when the tenant declares per cluster, clause 6), the two Kubernetes "configured but no matching data" checks and tenant self-service alerts (`Custom_*`, except the `absence` recipe). Among the tools, `da-tools federation-check`, `byo-check` and `onboard` point out a Prometheus without `da_cluster` (clause 7); `diagnose`, `backtest` and `threshold-recommend` judge per cluster, and the rules `migrate` generates aggregate per cluster. What is not per cluster is listed under "Consequences and known limitations".

#### Contract

1. **Where the cluster identity comes from.** Each Prometheus sets `da_cluster: <cluster id>` under `global.external_labels`, one line per Prometheus; scrape jobs write nothing. Prometheus attaches external labels to data as it leaves: alerts sent to Alertmanager, `/federate` and remote write all carry them. So wherever two clusters' series or alerts meet (a central Prometheus, Alertmanager) they are already told apart; a Prometheus that evaluates only its own cluster has no such label on its series, and the rules' groupings read as per tenant. A single-cluster deployment sets it too, to a fixed value.
2. **Cluster id.** Unique across the platform, one per cluster, stable over time; Prometheus replicas of the same cluster (an HA pair) use the same value. A deployment that already sets `external_labels.cluster` per the [Federation integration guide](../integration/federation-integration.en.md) uses the same value for `da_cluster`, next to it.
3. **Thresholds stay one per tenant.** Thresholds, maintenance state and tenant metadata come from threshold-exporter, one set per tenant, not per cluster. Federated from another cluster, these series also carry `da_cluster`; rules always collapse them with `by(tenant)` first and apply them to each of the tenant's clusters with `on(tenant) group_left` (`group_left` lets one series on the right match many on the left), so the result is the same either way. This assumes every cluster sends the same thresholds.
4. **How rules are written.** When the input is an aggregation of tenant-exporter series, a `by(...)` that contains any of `tenant`, `instance`, `pod`, `namespace`, `node` must also contain `da_cluster`. The same holds for a match (`on(...)`) whose two sides are both such series. `check_cluster_identity.py` (pre-commit) checks this on each rule's syntax tree; a rule that deliberately looks across clusters declares so directly above the rule with `# da_cluster: tenant-wide <finding kind> (<labels>) — <reason>`, as `TenantExporterReplicaDown` does.
5. **Alerts carry `da_cluster`.** Every alert sent to Alertmanager has this label: an alert that already carries it from the series it read has that value; one that does not gets the external label of the Prometheus that sends it, i.e. "the cluster of the Prometheus that evaluated this rule". Alertmanager's warning/critical dedup ([ADR-001](./001-severity-dedup-via-inhibit.en.md)) includes it, so same-named alerts in two clusters do not suppress each other. Dedup pairs two alerts only when both carry `da_cluster` or neither does, so the warning and the critical of one `metric_group` must have the same cluster scope: the results their rules compute both carry `da_cluster`, or neither does. When one is per cluster and the other is about the whole tenant, on a Prometheus whose series carry `da_cluster` the two do not pair, and the warning is still notified while the critical fires. `check_metric_group_pairs.py` (pre-commit) checks this for the warning and critical alerts of every `metric_group` in the rule packs, with the same judgement of cluster scope as `check_cluster_identity.py`; both without the label is allowed — evaluated by one Prometheus, they leave it with the same value (or none) and still pair. Notification grouping (`group_by`, which decides which alerts are combined into one notification) is unchanged by default: when the same alert fires in several clusters at once it is one notification holding each cluster's alert; whether the alerts are listed one by one with their `da_cluster` depends on the receiver's template (see known limitations). A deployment or tenant that wants one notification per cluster adds `da_cluster` to its own `group_by`. Tenant self-service alerts (`Custom_*`) are the exception: they go through a separate route whose grouping is fixed at `[tenant, alertname]`, and the tenant's `group_by` does not apply to them. An alert that reads only threshold-exporter series (for example "tenant has no thresholds configured") carries the second kind of value: it is about the whole tenant, and the cluster on the label only says which Prometheus computed it.
6. **"Expected but absent" is judged per cluster.** A tenant can declare in its configuration which clusters it expects to appear in; with a declaration, any declared cluster that does not report raises an alert. Without one, appearing in at least one cluster counts as present. The declaration goes in the tenant's top-level `_expected_exporters` (see [ADR-010](./010-multi-tenant-grouping.en.md)), which lists the kinds of exporter the tenant runs: an entry with only a kind (`mariadb`) declares no cluster; an entry written `kind@cluster` (`mariadb@edge-a`) declares "this cluster must have an exporter of this kind reporting", the cluster being the `da_cluster` value of that cluster's Prometheus. One kind is written one way only; writing it both ways is refused. A cluster id may use only letters, digits, `.`, `_` and `-` (starting and ending with a letter or digit, at most 63 characters, case-sensitive); a cluster whose `da_cluster` value contains other characters (for example `/` or `:`) cannot be declared per cluster today. The per-cluster absence alert (`TenantExporterAbsent`) carries the declared cluster as `da_cluster`. The platform has no list of clusters and does not check that a declared cluster exists: a misspelt cluster id results in an absence alert for that non-existent cluster.
7. **A Prometheus without `da_cluster` is caught at onboarding.** `da-tools federation-check edge` reads the configuration of the actual Prometheus, and its `edge_cluster_identity` check fails when `external_labels.da_cluster` has no value; `da-tools byo-check prometheus` also reads the loaded configuration, and its `cluster_identity` check fails with exit code 1 when there is no value (2 if the same run has a caller error); against a query layer that answers 404 (Thanos Querier, VictoriaMetrics) the check is skipped, and each Prometheus is checked with `federation-check edge` instead. `da-tools onboard --scrape-config`, analysing an existing `prometheus.yml`, prints a WARN on stderr when there is no value and writes the verdict to `cluster_identity` in `scrape-analysis.yaml` (`set`, `missing`, or `unknown` when the file has no `global` section). This repository's own Prometheus configuration is checked for the same thing by `check_scrape_reachability.py` (`CLUSTER-IDENTITY`). These checks all look at one place and need not work out what relabeling would produce.

These clauses are held by the following tests: `tests/rulepacks/rule-pack-cluster-masking_test.yaml` (with one cluster healthy and another broken, only the broken one alerts), `tests/rulepacks/test_cluster_metamorphic.py` (every rule-pack test replayed with a cluster label) and `tests/dx/fixtures/custom_alerts_promtool/per_cluster.yaml` (every self-service alert recipe over two clusters) hold clause 4; `tests/alertmanager-inhibit/inhibit_semantics_test.go` holds the dedup of clause 5, and `tests/lint/test_check_metric_group_pairs.py` holds the one cluster scope within a `metric_group`; `tests/rulepacks/rule-pack-liveness-per-cluster_test.yaml` and `components/threshold-exporter/app/pkg/config/expected_exporters_test.go` hold clause 6; `tests/ops/test_federation_check.py`, `tests/ops/test_byo_check.py`, `tests/ops/test_onboard_platform.py` and `tests/lint/test_check_scrape_reachability.py` hold clause 7.

#### Example

This repository's Prometheus configuration (each Prometheus fills in its own value):

```yaml {.quote src="k8s/03-monitoring/configmap-prometheus.yaml"}
global:
# ...
  external_labels:
    da_cluster: lab
```

Tenant `db-a` has one database in each of `edge-a` and `edge-b`, both at address `10.0.0.5:50000`, and the one in `edge-a` fails. The series the central Prometheus receives from the two clusters:

```text
db_expected{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}  1
db_expected{tenant="db-a", da_cluster="edge-b", instance="10.0.0.5:50000"}  1
db_up{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}        0
db_up{tenant="db-a", da_cluster="edge-b", instance="10.0.0.5:50000"}        1
```

Matching without the cluster identity: no result, no alert (`edge-b`'s success masks `edge-a`).

```promql
max by(tenant, instance) (db_expected)
  unless on(tenant, instance)
max by(tenant, instance) (db_up == 1)
```

Matching with the cluster identity: one result, `{tenant="db-a", da_cluster="edge-a", instance="10.0.0.5:50000"}`.

```promql
max by(tenant, da_cluster, instance) (db_expected)
  unless on(tenant, da_cluster, instance)
max by(tenant, da_cluster, instance) (db_up == 1)
```

`db_expected` and `db_up` are names chosen for illustration, not platform metrics. A real case of the same shape in the rule packs (`DB2DatabaseDown`, with the same instance address in two clusters) is in `tests/rulepacks/rule-pack-cluster-masking_test.yaml`.

What a per-cluster declaration looks like (clause 6). Tenant `db-a`'s configuration:

```yaml
tenants:
  db-a:
    _expected_exporters: [mariadb@edge-a, mariadb@edge-b, redis]
```

`edge-a`'s MariaDB exporter disappears, `edge-b`'s is still there, and the Redis exporter runs only in `edge-b`. The series on the central Prometheus:

```text
tenant_expected_exporter{tenant="db-a", pack="mariadb", expected_cluster="edge-a"}  1
tenant_expected_exporter{tenant="db-a", pack="mariadb", expected_cluster="edge-b"}  1
tenant_expected_exporter{tenant="db-a", pack="redis"}                               1
mysql_up{job="tenant-exporters", tenant="db-a", da_cluster="edge-b"}                1
redis_up{job="tenant-exporters", tenant="db-a", da_cluster="edge-b"}                1
```

Three minutes later there is one alert: `TenantExporterAbsent{tenant="db-a", pack="mariadb", da_cluster="edge-a"}`. Neither `edge-b`'s MariaDB nor Redis (declared by kind only) alerts. With the same series, if the tenant writes only `_expected_exporters: [mariadb, redis]`, nothing alerts: one exporter reporting in `edge-b` counts as present. These two cases are L1 and L2 of `tests/rulepacks/rule-pack-liveness-per-cluster_test.yaml` (the test names its clusters `east` and `west`).

### Tenant attribution of infrastructure metrics (Addendum)

**Infrastructure metrics such as disk, PVC and node support only 1:1 tenant attribution (one namespace is one tenant).**

The self-service alerts of [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) let tenants set conditions on the platform's infrastructure metrics (for example kubelet's `kubelet_volume_stats_*`). These metrics natively carry only `namespace`, not `tenant`, so `namespace` is copied into `tenant` at scrape time. The Prometheus configuration the platform ships (`k8s/03-monitoring/configmap-prometheus.yaml`) does this in the `metric_relabel_configs` of the kubelet scrape job, keeping only namespaces that start with `db-`.

## Background

In enterprise environments, the mapping between database instances and tenants is not always one-to-one:

| Topology | Description | Typical scenarios |
|:---------|:------------|:------------------|
| **1:1** | One namespace/instance maps to one tenant | Independently deployed microservices, dedicated DB |
| **N:1** | Several namespaces/instances aggregate to one tenant | One tenant in several clusters, DR pairs, read-write splitting |
| **1:N** | One instance holds several logical tenants | Oracle multi-schema, DB2 multi-tablespace, shared RDS |

**1:N.** A shared instance's metrics carry only `instance`, not a tenant. The exporter does not know how the instance is divided among tenants by schema or tablespace, so alert rules cannot apply different thresholds to different tenants on the same instance. A general way is needed to split instance-level metrics into tenant-level ones without changing threshold-exporter.

**One tenant in several clusters.** Two kinds of rule expression go wrong when one tenant spans clusters:

- **Masking.** A liveness check written as "instances expected to exist, minus instances that report success" matches the two sides with `on(tenant, instance)`. When each of two clusters has an instance at the same address (overlapping internal address ranges across clusters are common), the healthy one cancels the failed one and the failed cluster does not alert.
- **Silent merging.** `sum by(tenant)` adds the two clusters together, and `max by(tenant)` keeps only the larger one. The on-call engineer cannot tell which cluster it is, and the value no longer corresponds to any single database.

**Infrastructure metrics.** N:1 and 1:N in this ADR rely on extra mapping rules, while infrastructure metrics are renamed directly at scrape time and never see that mapping:

- **N:1 would silently miss alerts.** The metric is labelled `tenant="db-a-read"` while the tenant's configuration uses the name `db-a`; the two never match and the alert never fires.
- **1:N is impossible in principle.** A PVC cannot be split between tenants by schema or tablespace.

## Consequences and known limitations

### 1:N mapping

- **threshold-exporter is unaware of topology.** It does not read `_instance_mapping.yaml`; all three topologies converge on the `tenant` label of the series.
- **Original and mapped series coexist.** A recording rule produces new series and keeps the original instance series, so both views can be queried. The cost is that the mapped part doubles in series count: each rule copies every original series it selects and adds `tenant`. The estimate `--dry-run` prints is "entries × metrics", assuming each rule selects exactly one series.
- **Mapped series lag by up to one evaluation interval.** The generated rule groups have `interval: 30s`.
- **The platform's rule packs do not read the mapped series.** The generated series are named `tenant_mapped:<original metric>:current`, and no rule in `rule-packs/` reads that name; to alert on them, you write the rules yourself.
- **The generated rules are not in the Prometheus deployment the platform ships.** The rules projected volume in `k8s/03-monitoring/deployment-prometheus.yaml` has no `rules-tenant-mapping` source, and Prometheus loads only `/etc/prometheus/rules/*.yml`. To use them, add the ConfigMap to that projected volume and project it to a `.yml` file name; once added, changes are reloaded by the config-reloader sidecar without restarting Prometheus ([ADR-005](./005-projected-volume-for-rule-packs.en.md)).
- **The tenant-existence check has to be wired into your pipeline.** `--validate` checks that every tenant in the mapping has a tenant file in the config directory, but the repository's CI does not run this tool.
- **No schema validation.** `_instance_mapping.yaml` has no JSON Schema; an entry missing `tenant` or with an empty `filter` is skipped with a WARN and does not fail the tool.
- **Filters require knowing the database's structure.** Whoever writes a filter has to know how schemas/tablespaces map onto the exporter's labels.
- **No end-to-end validation.** The repository has only unit tests for the generator (`tests/ops/test_generate_tenant_mapping_rules.py`); no test loads the generated rules into Prometheus, and it has not been run against a real multi-schema Oracle environment.

### Known limitations of one tenant in several clusters

- **Upgrade order does not matter.** Rules work on series without `da_cluster`, with the same results as without clusters; data starts carrying the label once the external label is set on the Prometheus it leaves.
- **Alerts gain a label at the Alertmanager end.** Once the external label is set, Alertmanager treats the alert with the new label as a different alert: the new one appears and notifies once right away, the old one disappears only when it expires, and the two coexist in between.
- **Existing silences still work.** A silence matches on the labels it lists; an extra label on the alert does not affect it.
- **The cluster is not visible locally in an edge cluster.** In an edge Prometheus's queries, the `ALERTS` series and `$labels.da_cluster` in alert annotations, it is empty; the value appears only once the data reaches Alertmanager or the centre.
- **Not applicable when one Prometheus directly scrapes several clusters.** An external label has only one value. Such a deployment writes `da_cluster` on each cluster's scrape jobs itself (a value already on a series takes precedence over the external label); the lint does not check that form.
- **Thresholds must be the same in every cluster.** When each cluster has its own threshold-exporter federated to the centre, the centre collapses them with `by(tenant)`, taking the larger value; if they differ, the centre uses a set that is not any single cluster's own.
- **Sums are per cluster.** A rule that sums "all instances of the tenant" (for example lock waits or deadlock counts) sums each cluster separately and compares each with the threshold.
- **Tenant self-service alerts (`Custom_*`) are judged per cluster.** Except `absence`, every recipe computes and compares per (tenant, cluster): one that takes the maximum (`threshold`) uses that cluster's own value; one that sums (`rate`) adds only that cluster's series; one that computes a ratio (`ratio`, the ratio mode of `forecast`, `slo_burn_rate`) takes numerator and denominator from that cluster only; quantiles (`p99_latency`) and trend prediction (`forecast`) are also computed per cluster. The long window, short window and bad-event floor of `slo_burn_rate` must hold in the same cluster; one cluster's long window does not combine with another cluster's short window into an alert. The "equals a code" form (`op: "=="`) fires once per matching cluster. The alert carries `da_cluster` with that cluster's own value; thresholds and maintenance state stay one per tenant (clause 3). So on a Prometheus that collects several clusters' data, an alert that crosses the threshold only when two clusters are added does not fire (40/s in each cluster against 50/s: no alert), and a ratio diluted by another cluster's traffic still fires (`east` has no errors and a hundred times `west`'s traffic, `west`'s error ratio is 10% against 5%: combined about 0.1%, per cluster `west` alerts with value 10%). On a single Prometheus whose series carry no `da_cluster`, results are the same as without clusters. These cases are held by `tests/dx/fixtures/custom_alerts_promtool/per_cluster.yaml`.
- **The `absence` recipe keeps "present in any cluster counts".** A recipe has nowhere to declare which clusters the metric should appear in, and per clause 6, without a declaration appearing in at least one cluster counts as present. So it alerts only when none of the tenant's clusters has the metric; a cluster missing it while another still has it does not alert. The alert is about the whole tenant and carries no cluster as computed (the cluster added on sending is that of the Prometheus that evaluated it, as in the second half of clause 5). The compiled rule file writes `# da_cluster: tenant-wide by-drops-cluster (tenant) — …` directly above this rule, saying it deliberately judges across clusters.
- **A custom alert cannot target one cluster.** A recipe's `selectors` and `selectors_re` do not accept `da_cluster`; that custom-alert entry is refused at load, and the tenant's other custom alerts still load. The reason is that series carry this label only where clusters' data meet: the same configuration would look at only the named cluster on a central Prometheus, but match no series at all in a deployment where each cluster evaluates its own rules, so the alert would never fire and nothing would say so.
- **Per-cluster presence must be evaluated where series carry `da_cluster`.** That is the central Prometheus that collects every cluster's data; in a layered rule deployment the alert rules live there anyway. In a deployment where each cluster evaluates alert rules itself with a shared tenant configuration, local series carry no `da_cluster`, and `kind@cluster` there reads as the kind alone: if an exporter of that kind is present locally, every declared cluster counts as present; if not, one alert fires per declared cluster of that kind, naming clusters the local Prometheus cannot see. Such deployments should write kinds only, with each cluster having its own tenant configuration. Worse, if an edge Prometheus writes `da_cluster` on its scrape jobs itself and evaluates with a shared tenant configuration, local series carry only the local cluster, and every other declared cluster alerts permanently even when all are healthy.
- **A presence series without `da_cluster` counts as present for every declared cluster.** The other side of the previous item: if the central Prometheus also scrapes tenant exporters directly, those local series silence every declared cluster. For per-cluster judgment, write `da_cluster` on that scrape job.
- **The two Kubernetes "configured but no matching data" checks are judged per cluster and need no declaration.** "A custom-recipe disk alert is configured but there is no matching data" (`CustomRecipeDiskInert`) is per (tenant, cluster): if the tenant has running pods in a cluster and that cluster has none of its disk data, the alert fires for that cluster, and data in another cluster does not count; a cluster where the tenant has no running pods does not alert. "A version-specific threshold is configured but there is no version data" (`VersionAwareThresholdInert`) is per cluster: it fires once for each cluster where a tenant with such a threshold has pods but there is no pod-label data at all. It looks only at container CPU version thresholds; a tenant that set only memory version thresholds is outside this alert's scope (its thresholds equally have no effect without pod labels, but this alert does not point that out). A cluster that runs only other tenants does not alert; there are no version thresholds to apply there. Both alerts carry `da_cluster`, and their annotations name the cluster. On a single Prometheus where no series carries `da_cluster`, `VersionAwareThresholdInert` does not fire while every tenant with a version threshold has no pods, even if other tenants have pods and there is no pod-label data; it fires once that tenant's first pod appears. The cases with `da_cluster` are held by `tests/rulepacks/rule-pack-kubernetes-sentinels-per-cluster_test.yaml`; the single-Prometheus case by cases F and F2 of `tests/rulepacks/rule-pack-kubernetes-sentinel_test.yaml`.
- **These two checks assume both kinds of data from one cluster carry the same `da_cluster`.** The two kinds are "has running pods" and the data compared against it (disk data, pod-label data); they come through the same Prometheus, so either both carry it or neither does. When the assumption breaks, the result can be a false alert or a missed one. False alert: one kind carries `da_cluster` and the other does not, or one cluster arrives both through a labelled federation path and an unlabelled local scrape with the compared data on only one path; the alert then fires for a cluster that is actually fine (it would not without clusters). Missed alert: compared data arriving under the identity of "another cluster" can cancel the alert. For example, cluster A's pod data carries no label and really has no pod-label data, while cluster B's pod-label data also carries no label; B's data is then taken as A's. The lenient rule "data without `da_cluster` counts for every cluster" (which the exporter-absence alert has) is deliberately not used here: with it, data from the cluster the central Prometheus scrapes itself would mask another cluster that really lacks data. When the central Prometheus's own cluster (unlabelled) coexists with federated clusters (labelled), each side is judged separately and correctly.
- **What is not per cluster.** The aggregate alert "exporters of many tenants absent at once" (`MassExporterOutage`) counts per (tenant, kind); one kind counts as one however many clusters it is declared for, the alert the rule computes carries no `da_cluster` (at Alertmanager it carries only the cluster of the Prometheus that evaluated it), and it does not say which cluster has the mass absence. The `absence` recipe of tenant self-service alerts is not per cluster (see above). The alert "the tenant still has a healthy scrape target but one of them is down" (`TenantExporterReplicaDown`) deliberately keeps matching across clusters: when one cluster of a kind-only tenant is entirely down, as long as that cluster's scrape targets are still discovered and read `up=0`, this is the only signal. If the targets themselves are gone (for example the Service was removed), this alert does not fire either, and for a kind-only tenant the exporter-absence alert (`TenantExporterAbsent`) also stays silent because another cluster answers: that case has no signal at all unless the tenant declares per cluster. The alert "the tenant has no writable primary" (`MariaDBNoPrimary`) fires per cluster, but its question "does this tenant have a writable node in another cluster" deliberately looks across clusters: when one replication set is stretched over two clusters (the primary in one, replicas in the other), the replica-only cluster need not alert. The cost: with an independent replication set in each of two clusters, a cluster that lost its primary while one of its replicas' IO threads is still connected to its source stays quiet because of the other cluster's primary. `da-tools migrate` writes only the outer aggregation it generates per cluster; a `by(...)` / `on(...)` the original rule wrote itself is copied as is, and one that names `tenant`, `instance`, `pod`, `namespace` or `node` without `da_cluster` is marked in the report and in the generated rule file for a person to decide whether to add it.
- **Maintenance mode covers the whole tenant.** One cluster cannot be put into maintenance alone.
- **Clusters cannot have different thresholds.** A tenant uses one set of thresholds in all its clusters.
- **Notifications are not split per cluster by default.** The platform ships no notification templates; what you see is Alertmanager's built-in template output. When two clusters fire at once, the email body, PagerDuty details and webhook payload list each alert with its `da_cluster`; the one-line title is `[FIRING:2] <alert name> <tenant> (<other common labels>)`, without the cluster (when only one cluster fires the title does show the cluster, because it is then a label common to the whole group). The built-in Slack template has only a title and no body, so when two clusters fire at once you cannot tell which two; use a custom template that lists the alerts one by one. In PagerDuty one group is one incident, resolved only after every cluster recovers. Why the default is unchanged: whether a tenant's route carries `group_by` depends on whether the deployment's `_routing_defaults`, the routing profile and the tenant's own `_routing` end up with a value (the `_defaults.yaml` that `da-tools init` generates has one); only tenants left without a value (no layer gave one, or the tenant overrode it with `null` or an empty list) fall back to the root route's grouping, so changing the platform default would affect only those tenants, not all; the other reason is the first cost below.
- **Adding `da_cluster` to `group_by` has two costs.** First, alerts about the whole tenant (second half of clause 5) carry the cluster of the Prometheus that evaluated them, so when several Prometheus servers evaluate the same rules there is one notification per Prometheus. Second, changing a route's `group_by` on a running Alertmanager re-notifies every group currently firing on that route (the group key changes), and a receiver keyed by group (PagerDuty) opens new incidents; Alertmanager does not resolve the old incidents (recovery notifications go to the new key), so they must be closed by hand. For configuration, see the [BYO Alertmanager integration guide](../integration/byo-alertmanager-integration.en.md).

### Infrastructure metrics

- **An N:1 customer gets a signal on migration day.** The watchdog alert `CustomRecipeDiskInert` of [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) fires when a tenant has configured a disk alert and its pods are running, but the attributed disk metrics never appear. An N:1 customer therefore receives an explicit "this alert is not in effect" signal instead of a silent miss.
- **N:1 and 1:N are not supported.** When an N:1 customer (for example a read-write split database) actually needs infrastructure alerts, the mapping will be added.

## Alternatives considered

### Which layer does 1:N mapping

- **Built into the exporter**: one component does it all, but the exporter's job is turning YAML configuration into metrics; mapping is data normalization and does not belong there. A mapping change would require restarting the exporter, affecting every tenant's metrics, and in an HA deployment two exporters would have to keep identical mappings.
- **In Alertmanager**: affects only the notification path, but it is too late: Alertmanager handles only alerts that have already fired and cannot split at the metric level; tools such as Grafana query Prometheus directly and never see the mapping.
- **An external proxy**: fully decoupled, but adds a component, latency and operational complexity.

### Cluster identity for several clusters

- **Reuse the name `cluster`.** The RabbitMQ and Elasticsearch exporters emit `cluster` themselves (the service's own cluster name), and Prometheus has two behaviors for a clashing label, both wrong here. By default it keeps the value from the scrape configuration and renames the exporter's to `exported_cluster`, so rules reading the exporter's `cluster` read the platform's cluster id instead. With `honor_labels: true` (as on the federation job by which the centre pulls from the edges) it keeps the exporter's value, and the cluster identity is replaced by the service's own cluster name. Hence a platform-specific name.
- **Write `da_cluster` with relabeling on every scrape job.** The advantage is that the series already carry the value when rules are evaluated. But it has to be written in four places (tenant exporters, container metrics, volume metrics, pod-state metrics), one of them restricted to a single Service; when only some are written, container alerts that match two sources fail to match and go silent. Catching such a configuration before deployment would require the checker to work out relabeling results statically, re-implementing Prometheus's behavior, which never ends. And the only place rules actually need the cluster identity is where two clusters' data meet, where the data already carries the external label.
- **Add `da_cluster` only where labels such as `instance` already exist, keeping tenant-level aggregations one across clusters.** Far fewer places to change. But in a layered rule deployment ([ADR-004](./004-federation-central-exporter-first.en.md), §8 edge evaluation of the [Federation integration guide](../integration/federation-integration.en.md)), tenant-level recording rules are computed at each edge: two edges each send a same-named series to the centre, giving a result different from "everything evaluated centrally". Per cluster is the coarsest granularity that gives the same result under both deployment styles. Ratio rules also cannot combine the edges' ratios back into the whole tenant's ratio.
- **No support, and block tenants that span clusters.** Blocking would need a cluster identity first; and DR pairs and multi-site deployments are exactly the typical N:1 scenarios.

## Related

- [ADR-001](./001-severity-dedup-via-inhibit.en.md) — severity dedup inhibit rules, matched per cluster
- [ADR-004](./004-federation-central-exporter-first.en.md) — federation architecture
- [ADR-005](./005-projected-volume-for-rule-packs.en.md) — rule packs distributed by projected volume
- [ADR-010](./010-multi-tenant-grouping.en.md) — `_expected_exporters`
- [ADR-024](024-version-aware-threshold-via-dimensional-label.en.md) — self-service alerts and infrastructure metrics
- [Federation integration guide](../integration/federation-integration.en.md)
- [BYO Alertmanager integration guide](../integration/byo-alertmanager-integration.en.md)
- [#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097) — discussion and implementation record of one tenant in several clusters
- [Prometheus Recording Rules](https://prometheus.io/docs/prometheus/latest/configuration/recording_rules/)
