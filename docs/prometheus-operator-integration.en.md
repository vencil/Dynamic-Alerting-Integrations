---
title: "Prometheus Operator Integration Guide"
tags: [operator, integration, kube-prometheus-stack]
audience: [platform-engineer]
version: v2.5.0
lang: en
---
# Prometheus Operator Integration Guide

> **Audience**: Platform Engineers, SREs
> **Version**: v2.3.0
> **Prerequisites**: [BYO Prometheus Integration Guide](byo-prometheus-integration.en.md), [Architecture & Design](architecture-and-design.en.md)
> **Related ADR**: [ADR-008 — Operator CRD Path](adr/008-operator-native-integration-path.en.md)

---

## Overview

This guide covers the complete integration of the Dynamic Alerting platform in Prometheus Operator (kube-prometheus-stack) environments. Compared to the native ConfigMap approach in [BYO Prometheus](byo-prometheus-integration.en.md), the **Operator path uses declarative CRDs** (ServiceMonitor, PrometheusRule, AlertmanagerConfig) instead of manual ConfigMap mounts, with higher automation.

### When to Choose the Operator Path?

| Feature | Native ConfigMap | Operator CRD | Recommended |
|---------|------------------|--------------|-------------|
| kube-prometheus-stack already installed | ❌ | ✅ | **Operator** |
| Prometheus config via Helm values | ✅ | ❌ | ConfigMap |
| Multiple Prometheus instances | ⚠️ (complex) | ✅ | **Operator** |
| GitOps (ArgoCD/Flux) | ✅ | ✅ | **Operator** better |
| Simple deployment (1 Prometheus) | ✅ | ✅ | Either, team preference |

---

## Prerequisites: Verify Operator Installation

### Check CRDs Exist

```bash
# Verify PrometheusRule, ServiceMonitor, AlertmanagerConfig CRDs
kubectl get crd prometheusrules.monitoring.coreos.com
kubectl get crd servicemonitors.monitoring.coreos.com
kubectl get crd alertmanagerconfigs.monitoring.coreos.com

# Check all in one line
kubectl get crd | grep -E 'prometheus|alertmanager'
```

### Verify Operator Version

Dynamic Alerting v2.3.0 requires Prometheus Operator **≥ 0.65.0** (supports PrometheusRule `v1beta1`, AlertmanagerConfig `v1beta1`):

```bash
# Query Operator version
kubectl get deployment -n monitoring prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}'
# Expected format: ghcr.io/prometheus-operator/prometheus-operator:v0.x.y

# Check available API versions
kubectl api-versions | grep monitoring.coreos.com
# Expected output includes: monitoring.coreos.com/v1beta1
```

### If Operator is Not Installed

Install kube-prometheus-stack via Helm:

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values values.yaml                       # See next section
```

Reference values configuration (`values.yaml`):

```yaml
prometheus:
  prometheusSpec:
    ruleSelectorNilUsesHelmValues: false     # ★ Must be false to allow external PrometheusRule
    ruleSelector: {}                         # Empty selector = include all PrometheusRule
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelector: {}
    # Other configurations...

alertmanager:
  alertmanagerSpec:
    alertmanagerConfigSelector: {}           # Include all AlertmanagerConfig
```

> **Critical**: The combination of `ruleSelectorNilUsesHelmValues: false` and `ruleSelector: {}` tells Prometheus Operator to accept all PrometheusRule (including those not deployed by kube-prometheus-stack).

---

## Step 1: ServiceMonitor — Inject tenant Label + Scrape threshold-exporter

### Step 1a. Create ServiceMonitor for Database Exporters

Inject the `tenant` label for your existing database exporters (MySQL, PostgreSQL, Redis, etc.):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: tenant-db-exporters
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ← Must match Prometheus serviceMonitorSelector
spec:
  namespaceSelector:
    matchNames:
      - db-a
      - db-b
      # Add other tenant namespaces...
  selector:
    matchLabels:
      prometheus.io/scrape: "true"       # ← Matches your Service annotation
  endpoints:
    - port: metrics                      # ← Service port name, not number
      interval: 10s
      relabelings:
        # ★ Core: Inject namespace name as tenant label
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
        # (Optional) Drop debug pods
        - action: drop
          sourceLabels: [__meta_kubernetes_pod_name]
          regex: ".*-debug.*"
```

**Apply**:

```bash
kubectl apply -f servicemonitor-tenant-exporters.yaml
```

### Step 1b. Create ServiceMonitor for threshold-exporter

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
      app: threshold-exporter             # ← Matches threshold-exporter Service label
  endpoints:
    - port: http                          # ← threshold-exporter Service port name
      interval: 15s
```

**Apply**:

```bash
kubectl apply -f servicemonitor-threshold-exporter.yaml
```

### Verify ServiceMonitor

```bash
# Confirm ServiceMonitor is discovered by Prometheus
kubectl get servicemonitor -n monitoring

# Check Prometheus-generated scrape config (should include __meta_kubernetes_namespace → tenant relabel)
kubectl exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -- \
  cat /etc/prometheus/config_out/prometheus.env.yaml | \
  grep -A 20 "tenant-db-exporters"

# Query whether tenant label is correctly injected
curl -s 'http://localhost:9090/api/v1/query?query=up{tenant!=""}' | jq '.data.result | length'
```

---

## Step 2: PrometheusRule — Auto-convert Rule Packs

### Recommended: Use da-tools operator-generate

**Recommended method**: Use the `da-tools` built-in `operator-generate` command to automatically convert platform rule packs into PrometheusRule CRDs.

```bash
# Install/update da-tools
docker pull ghcr.io/vencil/da-tools:v2.4.0

# Specify config-dir, auto-scan and generate PrometheusRule
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops                                # (Optional) Output GitOps-ready YAML

# View generated CRDs
ls -la rules-crd/
```

**Output structure**:

```
rules-crd/
├── prometheusrule-mariadb.yaml
├── prometheusrule-postgresql.yaml
├── prometheusrule-kubernetes.yaml
├── prometheusrule-redis.yaml
├── prometheusrule-mongodb.yaml
├── prometheusrule-elasticsearch.yaml
├── prometheusrule-oracle.yaml
├── prometheusrule-db2.yaml
├── prometheusrule-clickhouse.yaml
├── prometheusrule-kafka.yaml
├── prometheusrule-rabbitmq.yaml
├── prometheusrule-operational.yaml
└── prometheusrule-platform.yaml
```

**Apply to cluster**:

```bash
# Apply all at once
kubectl apply -f rules-crd/

# Or use kubectl kustomize / ArgoCD / Flux
kustomize build rules-crd/ | kubectl apply -f -
```

### Manual Method (if not using da-tools)

#### 2a. Extract Rule Pack Contents

Extract rules from existing ConfigMap:

```bash
# Check existing ConfigMap
kubectl get configmap -n monitoring | grep prometheus-rules

# Extract specific rule pack (MariaDB example)
kubectl get configmap prometheus-rules-mariadb -n monitoring -o yaml > mariadb-cm.yaml

# View ConfigMap contents
cat mariadb-cm.yaml
```

#### 2b. Convert to PrometheusRule

Using the example below, convert ConfigMap `data` fields to PrometheusRule `spec.groups[]`:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: dynamic-alerts-mariadb
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ★ Prometheus ruleSelector must include this label
spec:
  groups:
    # Following groups from original ConfigMap mariadb-recording.yml and mariadb-alert.yml
    - name: mariadb-normalization
      interval: 30s
      rules:
        - record: tenant:mysql_global_status_connections:max
          expr: max by (tenant) (mysql_global_status_connections)
        - record: tenant:mysql_global_status_threads_connected:max
          expr: max by (tenant) (mysql_global_status_threads_connected)
        # ... more recording rules

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
          annotations:
            summary: "MariaDB high connection count for {{ $labels.tenant }}"
            description: "Current: {{ $value | humanize }}"
        # ... more alert rules
```

#### 2c. Apply to Cluster

```bash
kubectl apply -f prometheusrule-mariadb.yaml
```

### Verify PrometheusRule

```bash
# Confirm PrometheusRule is discovered by Operator
kubectl get prometheusrule -n monitoring

# Detailed check on specific rule
kubectl describe prometheusrule dynamic-alerts-mariadb -n monitoring

# Check whether Prometheus loaded correctly (view rule evaluation status)
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '.data.groups[] | select(.name | contains("mariadb"))'

# Verify no evaluation errors
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '[.data.groups[].rules[] | select(.lastError != "")] | length'
# Expected: 0
```

---

## Step 3: AlertmanagerConfig — Dynamic Routing Configuration

### Prerequisite: Alertmanager Operator Configuration

Ensure Prometheus Operator Helm Chart has Alertmanager enabled and configured to accept AlertmanagerConfig:

```yaml
# values.yaml
alertmanager:
  enabled: true
  alertmanagerSpec:
    alertmanagerConfigSelector: {}        # Empty selector = include all AlertmanagerConfig
    # Or specify specific labels
    # alertmanagerConfigSelector:
    #   matchLabels:
    #     release: kube-prometheus-stack
```

### 3a. Use da-tools operator-generate to Output AlertmanagerConfig

```bash
# Generated alongside PrometheusRule
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops

# Output includes
ls rules-crd/alertmanagerconfig-*.yaml
```

### 3b. Manually Create AlertmanagerConfig

Based on tenant YAML `_routing` configuration, generate per-tenant AlertmanagerConfig:

```yaml
apiVersion: monitoring.coreos.com/v1beta1
kind: AlertmanagerConfig
metadata:
  name: tenant-db-a
  namespace: monitoring
  labels:
    tenant: db-a
    release: kube-prometheus-stack
spec:
  route:
    # Match all alerts for this tenant
    matchers:
      - name: tenant
        value: db-a
    groupBy: ["alertname", "instance"]
    groupWait: 30s
    groupInterval: 5m
    repeatInterval: 12h
    receiver: db-a-pagerduty

  receivers:
    - name: db-a-pagerduty
      pagerdutyConfigs:
        - serviceKey: <secret:db-a-pagerduty-key>
          description: "{{ .GroupLabels.alertname }} on {{ .GroupLabels.tenant }}"

  inhibitRules:
    # Severity dedup rule: Critical suppresses Warning
    - sourceMatchers:
        - name: severity
          value: critical
      targetMatchers:
        - name: severity
          value: warning
        - name: tenant
          value: db-a

    # Silent Mode: maintenance label suppresses all alerts
    - sourceMatchers:
        - name: alertstate
          value: silent
      targetMatchers:
        - name: tenant
          value: db-a
```

**Apply**:

```bash
kubectl apply -f alertmanagerconfig-db-a.yaml
```

### Verify AlertmanagerConfig

```bash
# Confirm AlertmanagerConfig is discovered by Operator
kubectl get alertmanagerconfig -n monitoring

# Check whether Alertmanager config is correct (requires port-forward)
kubectl port-forward svc/alertmanager -n monitoring 9093:9093 &
curl -s 'http://localhost:9093/api/v1/status' | jq '.config'

# Query tenant-specific routes
curl -s 'http://localhost:9093/api/v1/alerts?silenced=false&inhibited=false' | jq '.'
```

---

## Migration Path: From ConfigMap to Operator CRD

If you already have ConfigMap-based integration, here's the step-by-step migration process:

### Phase 1: Parallel Run (1 week)

Deploy both ConfigMap and CRD, monitor whether alert output is consistent on both ends:

```bash
# Keep old ConfigMap deployment
kubectl get configmap -n monitoring | grep prometheus-rules

# Add PrometheusRule CRD
kubectl apply -f prometheusrule-*.yaml

# Prometheus will load both rule sets; same rule names will error
# Check for conflicts
kubectl logs -n monitoring prometheus-kube-prometheus-stack-prometheus-0 | grep -i "duplicate\|conflict"
```

### Phase 2: Switch Alert Routes (1 day)

Once Alertmanager alert count stabilizes, gradually migrate routing config to AlertmanagerConfig:

```bash
# Apply AlertmanagerConfig
kubectl apply -f alertmanagerconfig-*.yaml

# Check Alertmanager config changes (should be identical)
curl -s http://localhost:9093/api/v1/alerts | jq '.alerts | length'
```

### Phase 3: Clean Up ConfigMap (1 day later)

After confirming CRD stability, delete old ConfigMap:

```bash
# Backup old config
kubectl get configmap -n monitoring | grep prometheus-rules | while read -r cm; do
  kubectl get configmap "$cm" -n monitoring -o yaml > "backup-${cm}.yaml"
done

# Delete old ConfigMap
kubectl delete configmap prometheus-rules-mariadb prometheus-rules-redis ... -n monitoring
```

### Rollback Plan

If issues occur, quick rollback:

```bash
# Restore ConfigMap
kubectl apply -f backup-*.yaml

# Delete CRD (automatically restores old config)
kubectl delete -f prometheusrule-*.yaml
```

---

## Namespace Strategy: Cluster-wide vs Namespace-scoped

### Cluster-wide Deployment (Recommended)

All ServiceMonitor, PrometheusRule, AlertmanagerConfig deployed in `monitoring` namespace:

**Advantages**:
- Centralized management, GitOps-friendly
- Single Prometheus instance covers all tenants

**Applicable scenarios**:
- Single Prometheus instance
- Tenants in separate namespaces

**Configuration**:

```yaml
# ServiceMonitor in monitoring namespace, but namespaceSelector spans multiple tenant ns
namespaceSelector:
  matchNames:
    - db-a
    - db-b
    - db-c
```

### Namespace-scoped Deployment

Each tenant namespace deploys its own ServiceMonitor and PrometheusRule (using aggregating Prometheus):

**Advantages**:
- Tenants manage their own rules
- Suitable for multiple Prometheus instances (per-tenant or per-region)

**Disadvantages**:
- Higher management complexity
- Rule duplication

**Configuration example**:

```yaml
# ServiceMonitor in db-a namespace
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: db-a-exporters
  namespace: db-a                           # ← In tenant namespace
spec:
  namespaceSelector:
    matchNames: ["db-a"]
  selector:
    matchLabels:
      app: mysql-exporter
```

**Recommendation**: For most deployments, use **Cluster-wide**.

---

## GitOps Integration

### ArgoCD

Create ArgoCD Application pointing to the CRD directory:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dynamic-alerting-operator
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/your-org/monitoring-config
    targetRevision: main
    path: prometheus-operator-crds/          # ← CRD directory
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### Flux

Create Flux Kustomization:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: dynamic-alerting-operator
  namespace: flux-system
spec:
  targetNamespace: monitoring
  sourceRef:
    kind: GitRepository
    name: monitoring-config
  path: ./prometheus-operator-crds/
  prune: true
  interval: 5m
```

### Using da-tools with --gitops Flag

```bash
# Generate GitOps-ready YAML
da-tools operator-generate \
  --config-dir ./conf.d \
  --output-dir ./gitops/monitoring \
  --gitops                                  # Includes kustomization.yaml

# Push to Git
git add gitops/
git commit -m "Update Prometheus Operator CRDs"
git push
```

---

## Verification & Troubleshooting

### Complete Checklist

```bash
# 1. CRDs installed
kubectl get crd | grep monitoring.coreos.com

# 2. ServiceMonitor discovered
kubectl get servicemonitor -n monitoring

# 3. PrometheusRule loaded (no conflicts)
kubectl get prometheusrule -n monitoring
curl -s http://localhost:9090/api/v1/rules | jq '.data.groups | length'

# 4. AlertmanagerConfig applied
kubectl get alertmanagerconfig -n monitoring

# 5. Metrics and threshold vector matching work correctly
curl -s 'http://localhost:9090/api/v1/query?query=tenant:mysql_threads_connected:max' | jq '.data.result | length'
curl -s 'http://localhost:9090/api/v1/query?query=tenant:alert_threshold:connections' | jq '.data.result | length'
```

### Common Issues

#### Issue 1: PrometheusRule Not Loaded

**Symptom**: `kubectl get prometheusrule` shows it exists, but `curl /api/v1/rules` doesn't show it

**Troubleshoot**:

```bash
# Check Prometheus ruleSelector configuration
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'

# If empty or unset, modify kube-prometheus-stack Helm values:
helm upgrade kube-prometheus-stack \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelector={} \
  prometheus-community/kube-prometheus-stack -n monitoring

# Restart Prometheus
kubectl rollout restart statefulset/prometheus-kube-prometheus-stack-prometheus -n monitoring
```

#### Issue 2: ruleSelector Label Mismatch

**Symptom**: PrometheusRule exists but Operator doesn't include it

**Check**:

```bash
# View labels Prometheus expects
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# Confirm PrometheusRule has the label
kubectl get prometheusrule -n monitoring -o jsonpath='{.items[0].metadata.labels}' | jq .
```

**Fix**: Add missing label

```yaml
metadata:
  labels:
    release: kube-prometheus-stack        # ← Ensure this label exists
```

#### Issue 3: Alertmanager Cannot Recognize AlertmanagerConfig

**Symptom**: `kubectl get alertmanagerconfig` shows records, but Alertmanager config unchanged

**Troubleshoot**:

```bash
# Check Alertmanager alertmanagerConfigSelector
kubectl get alertmanager -n monitoring -o jsonpath='{.items[0].spec.alertmanagerConfigSelector}' | jq .

# If unset, edit Alertmanager resource
kubectl patch alertmanager kube-prometheus-stack-alertmanager -n monitoring --type merge -p \
  '{"spec":{"alertmanagerConfigSelector":{}}}'

# Restart Alertmanager
kubectl rollout restart statefulset/alertmanager-kube-prometheus-stack-alertmanager -n monitoring
```

#### Issue 4: Namespace Selector Across Multiple ns Fails to Scrape

**Symptom**: Only some tenant namespace targets are discovered

**Check**:

```bash
# Verify RBAC: Prometheus ServiceAccount must have list pods/services permissions across all ns
kubectl get rolebinding,clusterrolebinding -n monitoring | grep prometheus

# If missing, add ClusterRole manually
kubectl create clusterrole prometheus-discovery --verb=get,list,watch --resource=services,pods
kubectl create clusterrolebinding prometheus-discovery --clusterrole=prometheus-discovery \
  --serviceaccount=monitoring:prometheus-kube-prometheus-stack-prometheus
```

---

## FAQ

**Q: I already have native Prometheus config; can I mix with Operator?**
A: Not recommended. If necessary, use `additionalPrometheusRules` Helm value to inject extra rule files, avoid duplicate ServiceMonitor/PrometheusRule. See [ADR-008](adr/008-operator-native-integration-path.en.md).

**Q: How does Operator handle rule version upgrades?**
A: On each Dynamic Alerting upgrade, re-run `da-tools operator-generate`, commit new PrometheusRule to Git repo, GitOps tool auto-syncs. Zero-downtime upgrade.

**Q: Can I mix AlertmanagerConfig v1alpha1 and v1beta1?**
A: Not recommended. v1alpha1 is deprecated; migrate to v1beta1. Use `kubectl convert` for auto-conversion:
```bash
kubectl convert -f alertmanagerconfig-old.yaml --output-version monitoring.coreos.com/v1beta1 | kubectl apply -f -
```

**Q: How to implement Shadow Monitoring in the Operator path?**
A: Keep old AlertmanagerConfig alongside the new one, modify receiver to point to shadow alert destination. See [Shadow Monitoring SOP](shadow-monitoring-sop.en.md).

---

## Related Resources

| Resource | Relevance |
|----------|-----------|
| [BYO Prometheus Integration Guide](byo-prometheus-integration.en.md) | ⭐⭐⭐ |
| [BYO Alertmanager Integration Guide](byo-alertmanager-integration.en.md) | ⭐⭐⭐ |
| [ADR-008 — Operator CRD Path](adr/008-operator-native-integration-path.en.md) | ⭐⭐⭐ |
| [da-tools CLI Reference — operator-generate](cli-reference.en.md#operator-generate) | ⭐⭐ |
| [Architecture & Design](architecture-and-design.en.md) | ⭐⭐ |
| [Shadow Monitoring SOP](shadow-monitoring-sop.en.md) | ⭐⭐ |
| [Prometheus Operator Official Docs](https://prometheus-operator.dev/) | ⭐ |
| [kube-prometheus-stack Helm Chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | ⭐ |
