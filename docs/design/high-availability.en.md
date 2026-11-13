---
title: "High Availability (HA) Design — Replicas, PDB, Preventing Double-Counting"
tags: [architecture, high-availability, design]
audience: [platform-engineer, devops]
version: v2.9.0
lang: en
parent: architecture-and-design.en.md
---
# High Availability (HA) Design

> **Language / 語言：** **English (Current)** | [中文](high-availability.md)
>
> ← [Back to Main Document](../architecture-and-design.en.md)

## 4. High Availability Design

### 4.1 Deployment Strategy

```yaml
replicas: 2
strategy:
  type: RollingUpdate
  rollingUpdate:
    maxUnavailable: 0    # Zero-downtime rolling update
    maxSurge: 1

affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
```

**Features:**
- 2 replicas spread across different nodes
- During rolling update, always 1 replica available
- Kind single-node cluster: soft affinity allows bin-packing

### 4.2 Pod Disruption Budget

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: threshold-exporter-pdb
spec:
  minAvailable: 1
  selector:
    matchLabels:
      app: threshold-exporter
```

**Effect:** during voluntary disruptions such as draining a node, at least 1 selected Pod stays available (the PDB does not guarantee that Prometheus can scrape it). The PDB is created only when `replicaCount` is greater than 1 (`helm/threshold-exporter/templates/pdb.yaml`), and it does not cover involuntary disruptions such as a node failure.

### 4.3 Critical: `max by(tenant)` vs `sum`

#### ❌ Wrong: Using `sum`
```yaml
- record: tenant:alert_threshold:mysql_connection_usage_percent
  expr: |
    sum by(tenant)
      user_threshold{tenant=~".*", component="mysql", metric="connection_usage_percent"}
```

**Problem:**
- Prometheus scrapes the same metric from two replicas → double value
- `sum by(tenant)` adds values from both replicas → **threshold doubled**
- Alerts fire incorrectly

#### ✓ Correct: Using `max`
```yaml
- record: tenant:alert_threshold:mysql_connection_usage_percent
  expr: |
    max by(tenant)
      user_threshold{tenant=~".*", component="mysql", metric="connection_usage_percent"}
```

**Advantage:**
- Takes the maximum value from both replicas (logically identical)
- Avoids double-counting
- Alert threshold accurate under HA

#### `tenant_metadata_info`: not joined, queried in annotations

Every replica also serves its own copy of `tenant_metadata_info{tenant, runbook_url, owner, tier}`. The platform's Prometheus scrapes per pod (the raw config's `platform-components` job uses `role: endpointslice`, and a Prometheus Operator ServiceMonitor also discovers endpoints, so each pod is its own target), and the same tenant arrives twice, differing only in `instance`/`pod`.

Rules **do not join** this metric, and alerts carry no runbook_url/owner/tier labels. An annotation that shows metadata reads it with one query template (owner and tier change only the final field name):

```yaml
annotations:
  runbook_url: "{{ with query (printf `topk(1, tenant_metadata_info{tenant=%q})` $labels.tenant) }}{{ with index . 0 }}{{ .Labels.runbook_url }}{{ end }}{{ end }}"
```

- `topk(1, …)` takes one series when the two replicas each serve a copy; for a tenant without metadata the query returns nothing, `with` does not expand, the annotation is an empty string and the alert still fires.
- Why not a join: a join makes metadata part of the alert's identity. A raw join hits many-to-many with several replicas and the whole rule stops evaluating; collapsing the right-hand side to one series per tenant fixes that, but which series the collapse picks changes during the reload window (see the known limitation below), and when the alert's labels change so does its identity and `for:` restarts. VictoriaMetrics' `topk` may pick a different one of several equal-valued series on each evaluation, so that tenant would not fire during the window.
- `check_leftouterjoin_enrichment.py` refuses any expression that joins `tenant_metadata_info` (it may appear only on the right of `and`/`unless`; `or` is refused too, since it passes right-hand series through with their labels), and refuses `$labels.runbook_url`/`owner`/`tier` and any metadata query other than this template. `tests/rulepacks/rule-pack-metadata-replicas_test.yaml` uses two replica series, with one tenant's replicas serving different `runbook_url` values, and checks on both promtool and vmalert-tool that every tenant's alert fires exactly once, carries no metadata label, and that annotations show the metadata.

**Known limitation: during the reload window, annotations may show either replica's metadata.** The window can last a minute or two: each replica checks its config only every `reloadInterval` (30s by default in helm/threshold-exporter), and an update to the mounted ConfigMap reaches each replica through its own node's kubelet sync (typically tens of seconds, sometimes over a minute); the two delays add up, so the replicas can switch to the new version that far apart. The window only affects the tenant whose metadata changed: its notifications may show the old or the new `runbook_url`/`owner`/`tier`. The alert's identity and firing are unaffected, and so are other tenants.

### 4.4 Self-Monitoring (Platform Rule Pack)

4 dedicated alerts monitor threshold-exporter itself:

| Alert | Condition | Action |
|-------|-----------|--------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PagerDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | Warning → Platform team |
| TooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | Warning → SRE |
| HighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | Investigation |

