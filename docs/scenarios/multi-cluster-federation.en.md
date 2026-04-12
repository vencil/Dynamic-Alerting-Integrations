---
title: "Scenario: Multi-Cluster Federation — Central Threshold + Edge Metrics"
tags: [scenario, federation, multi-cluster]
audience: [platform-engineer]
version: v2.6.0
lang: en
---
# Scenario: Multi-Cluster Federation — Central Threshold + Edge Metrics

> **Quick Guide** — Full architecture design and rationale: [Federation Integration Guide](../integration/federation-integration.en.md). This document focuses on deployment steps.

## Problem

Organizations operating multiple Kubernetes clusters face scattered thresholds, monitoring silos, and chaotic notification routing. A unified multi-cluster alert management solution is needed.

## Architecture Choice

| Aspect | Central Evaluation | Edge Evaluation |
|--------|-------------------|-----------------|
| Suitable Scale | < 20 edge clusters | 20+ edge or high-latency cross-region |
| Latency | ~60–90s (federation) / ~30s (remote-write) | ~5–15s |
| Complexity | Low (single-point deployment) | High (Rule Packs need partitioning) |

Detailed comparison: [Federation Guide §1.2](../integration/federation-integration.en.md#12-architecture-choice-central-evaluation-vs-edge-evaluation).

## Deployment Steps (Central Evaluation)

### Step 1: Edge Cluster Configuration

**1.1 Set external_labels**

```yaml
# prometheus.yml (edge)
global:
  scrape_interval: 15s
  external_labels:
    cluster: "edge-asia-prod"    # unique identifier
```

**1.2 Tenant Label Injection** (choose one mode)

```yaml
# Namespace-to-Tenant 1:1
relabel_configs:
  - source_labels: [__meta_kubernetes_namespace]
    target_label: tenant

# Or use scaffold tool to auto-generate
da-tools scaffold --tenant db-a --db postgresql --namespaces ns-prod,ns-staging
```

**1.3 Verify Edge**

```bash
da-tools federation-check edge --prometheus http://edge-prometheus:9090
```

### Step 2: Central Cluster Configuration

**2.1 Choose Transport**

Federation (< 10 edges) or Remote Write (10+ edges). Config examples: [Federation Guide §4](../integration/federation-integration.en.md#4-central-cluster-configuration).

**2.2 Deploy threshold-exporter HA**

```bash
helm upgrade --install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.6.0 \
  -n monitoring --create-namespace -f values-override.yaml
```

**2.3 Configure Tenant Thresholds**

Create tenant YAML files in `conf.d/` (same format as single-cluster).

**2.4 Deploy Rule Packs + Verify**

```bash
da-tools federation-check central --prometheus http://central-prometheus:9090
```

### Step 3: End-to-End Verification

```bash
da-tools federation-check e2e \
  --prometheus http://central-prometheus:9090 \
  --edge-urls http://edge-asia:9090,http://edge-europe:9090

da-tools diagnose db-a --prometheus http://central-prometheus:9090
```

## Checklist

**Edge clusters**: external_labels set · Tenant relabel correct · DB exporter running · Federation/remote-write enabled

**Central cluster**: threshold-exporter ×2 HA · Rule Packs fully deployed · Alertmanager routes configured · All edge metrics visible

**End-to-end**: Cross-cluster metric queries · Alerts route to correct channels · Grafana global view

## Troubleshooting

| Symptom | Diagnostic | Common Cause |
|---------|-----------|-------------|
| Edge metrics not reaching central | `federation-check edge` | tenant label not injected, match[] too strict, network unreachable |
| Alert not firing | `federation-check central` | tenant in silent/maintenance, route missing matcher |
| Recording rule no output | `federation-check central` | Rule Pack not mounted, metric name mismatch |

## Interactive Tools

- [Capacity Planner](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/capacity-planner.jsx) — Estimate multi-cluster resource requirements
- [Dependency Graph](https://vencil.github.io/Dynamic-Alerting-Integrations/assets/jsx-loader.html?component=../interactive/tools/dependency-graph.jsx) — Rule Pack dependency visualization

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [Federation Integration Guide](../integration/federation-integration.en.md) | ⭐⭐⭐ |
| [ADR-004 Federation Central Exporter First](../adr/004-federation-central-exporter-first.en.md) | ⭐⭐ |
| [Advanced Scenarios & Test Coverage](../internal/test-coverage-matrix.md) | ⭐⭐ |
| [Shadow Monitoring Cutover Workflow](shadow-monitoring-cutover.en.md) | ⭐⭐ |
