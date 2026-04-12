---
title: "Operator Prometheus Integration Guide"
tags: [operator, prometheus, integration]
audience: [platform-engineer]
version: v2.6.0
lang: en
---
# Operator Prometheus Integration Guide

> **Audience**: Platform Engineers, SREs
> **Version**: v2.6.0
> **Related Document**: [BYO Prometheus Integration Guide](byo-prometheus-integration.en.md) (ConfigMap Path)
> **Related ADR**: [ADR-008 — Operator CRD Path](../adr/008-operator-native-integration-path.en.md)

---

## Overview

This guide covers how to integrate the Dynamic Alerting **Prometheus component** (ServiceMonitor + PrometheusRule) with your cluster in a Prometheus Operator (kube-prometheus-stack) environment.

If you have not yet decided between the ConfigMap or Operator path, please refer to [Deployment Decision Matrix](../getting-started/decision-matrix.md).

---

## Prerequisites

### Verify CRDs Are Installed

```bash
kubectl get crd prometheusrules.monitoring.coreos.com
kubectl get crd servicemonitors.monitoring.coreos.com

# Single-line verification
kubectl get crd | grep -E 'prometheus|servicemonitor'
```

### Verify Operator Version

Dynamic Alerting v2.6.0 requires Prometheus Operator **≥ 0.65.0**:

```bash
kubectl get deployment -n monitoring prometheus-operator \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

kubectl api-versions | grep monitoring.coreos.com
# Expected to include: monitoring.coreos.com/v1beta1
```

### Install kube-prometheus-stack (if not already installed)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values values.yaml
```

**Key values.yaml Configuration**:

```yaml
prometheus:
  prometheusSpec:
    ruleSelectorNilUsesHelmValues: false   # ★ Must be false to allow external PrometheusRule
    ruleSelector: {}                       # Empty selector = include all PrometheusRule
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelector: {}
```

> The combination of `ruleSelectorNilUsesHelmValues: false` and `ruleSelector: {}` tells Prometheus Operator to accept all PrometheusRules, including those deployed outside of kube-prometheus-stack.

---

## Step 1: ServiceMonitor — Inject tenant Labels + Scrape threshold-exporter

### 1a. Create ServiceMonitor for Database Exporters

Inject the `tenant` label into your existing database exporters (MySQL, PostgreSQL, Redis, etc.):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: tenant-db-exporters
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames:
      - db-a
      - db-b
  selector:
    matchLabels:
      prometheus.io/scrape: "true"
  endpoints:
    - port: metrics
      interval: 10s
      relabelings:
        # ★ Core: inject namespace as tenant label
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
```

### 1b. Create ServiceMonitor for threshold-exporter

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: threshold-exporter
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: ["monitoring"]
  selector:
    matchLabels:
      app: threshold-exporter
  endpoints:
    - port: http
      interval: 15s
```

> **Helm Shortcut**: When `rules.mode: operator` is configured, the threshold-exporter chart automatically generates ServiceMonitor (see `rules.operator.serviceMonitor` in `values.yaml`).

### Verify ServiceMonitor

```bash
kubectl get servicemonitor -n monitoring
curl -s 'http://localhost:9090/api/v1/query?query=up{tenant!=""}' | jq '.data.result | length'
```

---

## Step 2: PrometheusRule — Auto-convert Rule Pack

### Recommended Method: da-tools operator-generate

```bash
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops

kubectl apply -f rules-crd/
```

### Manual Method

Extract rule content from ConfigMap and convert to PrometheusRule CRD:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: dynamic-alerts-mariadb
  namespace: monitoring
  labels:
    prometheus: kube-prometheus         # ★ da-tools operator-generate default
    release: kube-prometheus-stack      # ★ kube-prometheus-stack default ruleSelector
    app.kubernetes.io/part-of: dynamic-alerting
spec:
  groups:
    - name: mariadb-normalization
      interval: 30s
      rules:
        - record: tenant:mysql_global_status_connections:max
          expr: max by (tenant) (mysql_global_status_connections)
    - name: mariadb-threshold-alerts
      interval: 30s
      rules:
        - alert: MariaDBHighConnections
          expr: |
            tenant:mysql_threads_connected:max
              > on(tenant) group_left()
            tenant:alert_threshold:connections
          for: 5m
          labels:
            severity: warning
            metric_group: mariadb-connections
```

### Verify PrometheusRule

```bash
kubectl get prometheusrule -n monitoring
curl -s 'http://localhost:9090/api/v1/rules' | jq '.data.groups | length'
# Verify no evaluation errors
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '[.data.groups[].rules[] | select(.lastError != "")] | length'
```

---

## Helm Chart: rules.mode Toggle

The threshold-exporter Helm chart adds a `rules.mode` toggle in v2.6.0:

```yaml
# values.yaml
rules:
  mode: operator    # Switch to Operator mode
  operator:
    ruleLabels:
      prometheus: kube-prometheus
    serviceMonitor:
      enabled: true
      interval: 15s
```

When `mode: operator` is set: the chart automatically generates ServiceMonitor; Rule Pack is loaded via PrometheusRule CRD instead of ConfigMap projected volume. The threshold-exporter itself remains unchanged—it still reads tenant thresholds from `conf.d/` ConfigMap.

---

## Troubleshooting

### PrometheusRule Not Being Loaded

Check if Prometheus's ruleSelector matches:

```bash
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'
```

If empty or non-matching, modify kube-prometheus-stack values and run `helm upgrade`.

### ruleSelector Dual-Label Matching

`operator-generate` outputs PrometheusRule with **two** selector labels:

```yaml
metadata:
  labels:
    release: kube-prometheus-stack    # Helm chart default selector
    prometheus: kube-prometheus        # Prometheus CRD spec.ruleSelector
```

**Why two labels?** Depending on how `kube-prometheus-stack` was installed, the Prometheus CRD's `spec.ruleSelector.matchLabels` may use `release`, `prometheus`, or both. Including both labels ensures PrometheusRules are picked up across all common installation configurations.

**Diagnostic steps**:

```bash
# 1. Check actual ruleSelector on Prometheus CRD
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# 2. Check labels on your PrometheusRule
kubectl get prometheusrule -n monitoring --show-labels

# 3. If labels don't match, regenerate using operator-generate (auto dual-label)
da-tools operator-generate --components rules --namespace monitoring
```

> **Common pitfall**: Manually-written PrometheusRules often only carry `release: kube-prometheus-stack`, but some environments require `prometheus: kube-prometheus` in the ruleSelector. Using `operator-generate` avoids this issue.

### Cross-namespace Selector Fails

Verify Prometheus ServiceAccount has RBAC permissions for cross-namespace access:

```bash
kubectl get clusterrolebinding | grep prometheus
```

---

## Related Documents

| Document | Description |
|----------|-------------|
| [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) | AlertmanagerConfig CRD routing configuration |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | ArgoCD / Flux integration |
| [Operator Shadow Monitoring](operator-shadow-monitoring.en.md) | Dual-track observation strategy |
| [BYO Prometheus Integration](byo-prometheus-integration.en.md) | ConfigMap path (Path A) |
| [ADR-008](../adr/008-operator-native-integration-path.en.md) | Architecture Decision Record |
