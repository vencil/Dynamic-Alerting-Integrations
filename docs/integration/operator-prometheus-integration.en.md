---
title: "Operator Prometheus Integration Guide"
tags: [operator, prometheus, integration]
audience: [platform-engineer]
version: v2.9.0
lang: en
---
# Operator Prometheus Integration Guide

> **Language / 語言：** **English (Current)** | [中文](./operator-prometheus-integration.md)

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
  name: tenant-exporters
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
        # ⛔ job must be tenant-exporters: every tenant rule pack selects up{job="tenant-exporters"}.
        # Without this, prometheus-operator names the job after the Service and healthy tenants page as absent.
        - targetLabel: job
          replacement: tenant-exporters
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

> **NetworkPolicy**: the chart creates a NetworkPolicy for the exporter that, by default, restricts ingress to the metrics port **without restricting the source** (`networkPolicy.allowExternal: true`) — the chart cannot know your Prometheus's pod labels, and a wrong guess silently cuts the scrape. To admit only your Prometheus, set `networkPolicy.allowExternal: false` and fill in `networkPolicy.prometheusNamespace` (the namespace it runs in) and `networkPolicy.prometheusPodLabels` (its pod labels; kube-prometheus-stack's carry `app.kubernetes.io/name: prometheus`); both must be non-empty, or the chart refuses to render.

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
  --components rules \
  --rule-packs-dir rule-packs/ \
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
            tenant:alert_threshold:mysql_connections
          for: 5m
          labels:
            severity: warning
            metric_group: mysql_connections
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

## Platform self-monitoring alerts (platform pack)

The platform self-monitoring alerts (`rule-packs/rule-pack-platform.yaml`, `Watchdog` included) read the platform components' own metrics. Under the Operator, Prometheus scrapes only what a ServiceMonitor selects, so both of the following are needed ([#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595)):

**1. Turn on the platform charts' ServiceMonitors** (off by default: a cluster without the `monitoring.coreos.com` CRDs cannot apply them):

| Chart | Values that turn it on | Service scraped (= `job`) |
|---|---|---|
| `threshold-exporter` | `rules.mode: operator` (`rules.operator.serviceMonitor.enabled` defaults to `true`) | `threshold-exporter` |
| `tenant-api` | `serviceMonitor.enabled: true` | `tenant-api` |
| `vector` | `serviceMonitor.enabled: true` (each endpoint follows its Service: `metrics.enabled` / `tenantProjections`) | `vector-metrics`, `vector-projection-gate-metrics` |
| `federation-gateway` | `serviceMonitor.enabled: true` (needs `auditLog.enabled`, on by default) | `federation-gateway` |
| `federation-reconciler` | `serviceMonitor.enabled: true` | `federation-reconciler` |

Each ServiceMonitor selects the Service carrying `dynamic-alerting.io/scrape: platform`, scrapes the same pod port the raw config's `platform-components` job scrapes, and sets **no `jobLabel`** — `job` is the Service name, as in raw mode, so a selector such as `job="tenant-api"` holds in both. They carry `release: kube-prometheus-stack` by default; set `serviceMonitor.labels` for another release name (`release: null` drops it).

**NetworkPolicy**: a ServiceMonitor on the right port still scrapes nothing if the chart's own NetworkPolicy refuses the Prometheus. Every chart assumes by default that Prometheus runs in the `monitoring` namespace; change it when kube-prometheus-stack lives elsewhere:

| Chart | Settings that admit the scrape | Default |
|---|---|---|
| `threshold-exporter` | `networkPolicy.allowExternal` / `prometheusNamespace` / `prometheusPodLabels` (see 1b) | metrics port open to any source |
| `tenant-api` | `networkPolicy.prometheusNamespace` + the pod-scoped allow-list `networkPolicy.internalPortAllow.prometheus` (raw `app=prometheus`) and `.prometheusOperator` (kube-prometheus-stack's `app.kubernetes.io/name=prometheus`) | pods with either label in `monitoring`; 8080 serves only probes and metrics (identity headers get 400), so the list admits only the Prometheus scrapers |
| `vector` | the chart ships no NetworkPolicy | — |
| `federation-gateway` | `networkPolicy.scrapeNamespaceLabel` (label of Prometheus's namespace) | any pod in `monitoring`; `null` = any namespace (`{}` does not clear the default — Helm merges maps) |
| `federation-reconciler` | `networkPolicy.scrapeNamespaceLabel` (scrape ingress); its VictoriaLogs egress has its own `networkPolicy.victorialogsNamespaceLabel` and does not widen with `null` | same |

⚠️ Changing federation-gateway `auditLog.metrics.port` or federation-reconciler `metricsPort` means the Prometheus side must admit the new port too: in this repo's raw stack that is `allow-prometheus-egress` in `k8s/03-monitoring/network-policies.yaml` (it lists the charts' default 3903 / 9099 only); likewise for any egress NetworkPolicy your kube-prometheus-stack has.

**2. Apply the `da-rule-pack-platform` that `operator-generate` emits.** It is the platform pack without the rules whose metric source a kube-prometheus-stack install lacks: `_lib_metric_sources.operator_split` derives which ones from the scrape gate's metric-family table (not a hand list); each gets a `NOTE:` line on stderr and is listed in that PrometheusRule's `dynamic-alerting.io/operator-excluded-rules` annotation. Today:

<!-- operator-excluded-rules:begin -->
| Alert | Why operator mode has no source for it |
|---|---|
| `PrometheusConfigReloadFailed` | Selects `job="prometheus"`: this repo's raw Prometheus self-scrape job; kube-prometheus-stack names that job after its own Service (its own `PrometheusBadConfig` covers the same failure) |
<!-- operator-excluded-rules:end -->

`tests/ops/test_operator_generate_rule_copies.py` compares this table with the derivation and goes red when they disagree.

⚠️ **`Watchdog` and kube-prometheus-stack's own alert of the same name.** Both carry only `alertname="Watchdog"` and `severity="none"` (evaluated by the same Prometheus, so the external labels match too); Alertmanager deduplicates by label set and receives **one** Watchdog — no second one, and no `severity`-based inhibit touches it. When routing is rendered by `generate-routes` (see [Operator Alertmanager Integration](operator-alertmanager-integration.en.md)), that one Watchdog is taken first by the platform's `watchdog-heartbeat` route; wire a dead-man's switch to the `watchdog-heartbeat` receiver in your base, not to kube-prometheus-stack's default route that sends it to `null`. Its annotations may be either copy's. Exception: with `enforcedNamespaceLabel` set on the Prometheus and the two PrometheusRules in different namespaces, the two carry different `namespace` labels and are not deduplicated.

⚠️ **Federation deployed without its ServiceMonitors means two critical alerts.** `FederationRevocationReconcileStale` (its metric-absent arm) and `FederationAuditPipelineSilent` decide whether the component is deployed from kube-state-metrics (the reconciler Deployment; a gateway pod with the `audit-metrics` container), not from `up` — a lost scrape target is exactly what they must catch. So with federation deployed but `serviceMonitor.enabled` still at its default `false`, both fire: revocation tamper-evidence and the audit pipeline really are unmonitored. That is the correct signal; turn on `serviceMonitor.enabled` in those two charts. A cluster without federation gets neither.

⚠️ **The few that still select `namespace="monitoring"`** (`CronJobLastRunFailed`, `ConfigReloaderHighRestarts`, `ConfigReloaderNotStarting`) watch this repo's raw manifests under `k8s/03-monitoring/` (the Prometheus / Alertmanager `config-reloader` sidecars; `CronJobLastRunFailed` watches whatever CronJob you put in `monitoring` — this repo ships none today), which are hard-wired to `monitoring`. They are emitted under the Operator too (kube-state-metrics has the series) but only see the namespace named `monitoring`: with kube-prometheus-stack installed there, its own Prometheus's `config-reloader` sidecar is watched as well. The platform charts' workloads (`TenantApiSingleWriterBreach`, `ThresholdExporterHighRestarts`) no longer pin a namespace and are watched wherever they are installed.

---

## Helm Chart: rules.mode Toggle

The threshold-exporter Helm chart adds a `rules.mode` toggle in v2.6.0:

```yaml
# values.yaml
rules:
  mode: operator    # Switch to Operator mode
  operator:
    serviceMonitor:
      enabled: true
      interval: 15s
```

When `mode: operator` is set: the chart automatically generates ServiceMonitor; Rule Pack is loaded via PrometheusRule CRD instead of ConfigMap projected volume — ⚠️ those PrometheusRules are **not rendered by the chart**; generate them with `da-tools operator-generate` and apply them (see above). The threshold-exporter itself remains unchanged—it still reads tenant thresholds from `conf.d/` ConfigMap.

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

PrometheusRules emitted by `migrate-to-operator` and `rule-pack-split` carry the same labels (one shared definition). The repo commits no pre-generated PrometheusRules — to generate them from the rule packs, write them into your own directory with `da-tools operator-generate --components rules --output-dir <your-dir>` ([#3427](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3427)).

**Diagnostic steps**:

```bash
# 1. Check actual ruleSelector on Prometheus CRD
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# 2. Check labels on your PrometheusRule
kubectl get prometheusrule -n monitoring --show-labels

# 3. If labels don't match, regenerate using operator-generate (auto dual-label)
da-tools operator-generate --components rules --namespace monitoring

# 4. Helm release not named kube-prometheus-stack: match the actual selector with --selector-label
da-tools operator-generate --selector-label release=my-prom
```

`--selector-label` is repeatable, lands on both the PrometheusRule and the ServiceMonitor, and overrides a default of the same key (#2075). The chart's own ServiceMonitor (`rules.mode: operator`) takes extra labels from the values key `rules.operator.serviceMonitor.labels`, which also defaults to `release: kube-prometheus-stack` (set `release: null` to drop it).

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
| [Operator Alertmanager Integration](operator-alertmanager-integration.en.md) | `generate-routes` → Secret → `configSecret` routing |
| [Operator GitOps Deployment](operator-gitops-deployment.en.md) | ArgoCD / Flux integration |
| [Operator Shadow Monitoring](operator-shadow-monitoring.en.md) | Dual-track observation strategy |
| [BYO Prometheus Integration](byo-prometheus-integration.en.md) | ConfigMap path (Path A) |
| [ADR-008](../adr/008-operator-native-integration-path.en.md) | Architecture Decision Record |
