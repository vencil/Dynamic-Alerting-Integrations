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
- record: tenant:alert_threshold:mysql_connections
  expr: |
    sum by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**Problem:**
- Prometheus scrapes the same metric from two replicas → double value
- `sum by(tenant)` adds values from both replicas → **threshold doubled**
- Alerts fire incorrectly

#### ✓ Correct: Using `max`
```yaml
- record: tenant:alert_threshold:mysql_connections
  expr: |
    max by(tenant)
      user_threshold{tenant=~".*", metric="connections"}
```

**Advantage:**
- Takes the maximum value from both replicas (logically identical)
- Avoids double-counting
- Alert threshold accurate under HA

#### The same rule applies to `tenant_metadata_info` joins

Every replica also serves its own copy of `tenant_metadata_info`. Scraped per pod (a Prometheus Operator ServiceMonitor uses endpoints, so each pod is its own target), the same tenant arrives twice, differing only in `instance`/`pod`. If the join that copies metadata into an alert uses the raw metric:

```promql
* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info
```

the right-hand side has two series per tenant, Prometheus reports `many-to-many matching not allowed`, and **the whole alert stops evaluating** (it does not just lose labels; it does not fire). Collapse the right-hand side first:

```promql
* on(tenant) group_left(runbook_url, owner, tier)
  max by(tenant, runbook_url, owner, tier) (tenant_metadata_info)
```

- `by` lists the labels the exporter serves, rather than `without` listing the labels the scrape adds: a scrape config can add more per-replica labels (a relabelled `node`, say), and any one `without` misses brings the error back. A test compares the `by` list with the label list in `collector.go`, so a label added to the exporter turns it red instead of being collapsed away silently.
- `check_leftouterjoin_enrichment.py` rejects any join whose right-hand side is not this form; `tests/rulepacks/rule-pack-metadata-replicas_test.yaml` checks a representative alert from every pack against two replica series.
- The void branch `unless on(tenant) tenant_metadata_info` keeps the raw metric: `unless` only tests presence, and duplicate right-hand series do not make it fail.

**Known limitation: the reload window.** Each replica hot-reloads on its own, so they switch versions at slightly different times. If a tenant's `owner`/`runbook_url`/`tier` changes, the two replicas briefly serve different values, `max by(...)` still leaves two series, and that tenant's alerts hit many-to-many and fail to evaluate for those cycles (`prometheus_rule_evaluation_failures_total` goes up; during those cycles the alerts neither newly fire nor get re-sent). Evaluation recovers once both replicas run the new version. This is not handled for now: picking one series with `topk` would make the labels flip between the two values and change the alert's identity.

### 4.4 Self-Monitoring (Platform Rule Pack)

4 dedicated alerts monitor threshold-exporter itself:

| Alert | Condition | Action |
|-------|-----------|--------|
| ThresholdExporterDown | `up{job="threshold-exporter"} == 0` for 2m | PagerDuty → SRE |
| ThresholdExporterAbsent | Metrics absent > 5m | Warning → Platform team |
| TooFewReplicas | `count(up{job="threshold-exporter"}) < 2` | Warning → SRE |
| HighRestarts | `rate(container_last_terminated_reason[5m]) > 0.1` | Investigation |

