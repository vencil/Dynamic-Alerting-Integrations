---
title: "Operator Prometheus 整合指南"
tags: [operator, prometheus, integration]
audience: [platform-engineer]
version: v2.7.0
lang: zh
---
# Operator Prometheus 整合指南

> **Language / 語言：** **中文 (Current)** | [English](./operator-prometheus-integration.en.md)

> **受眾**：Platform Engineers、SREs
> **版本**：v2.6.0
> **對應文件**：[BYO Prometheus 整合指南](byo-prometheus-integration.md)（ConfigMap 路徑）
> **相關 ADR**：[ADR-008 — Operator CRD 路徑](../adr/008-operator-native-integration-path.md)

---

## Overview

本指南涵蓋 Prometheus Operator（kube-prometheus-stack）環境下，如何將 Dynamic Alerting 的 **Prometheus 端**（ServiceMonitor + PrometheusRule）與你的叢集整合。

如果你尚未決定使用 ConfigMap 或 Operator 路徑，請先參考 [Deployment Decision Matrix](../getting-started/decision-matrix.md)。

---

## Prerequisites

### 確認 CRD 已安裝

```bash
kubectl get crd prometheusrules.monitoring.coreos.com
kubectl get crd servicemonitors.monitoring.coreos.com

# 一行檢查
kubectl get crd | grep -E 'prometheus|servicemonitor'
```

### 確認 Operator 版本

Dynamic Alerting v2.6.0 要求 Prometheus Operator **≥ 0.65.0**：

```bash
kubectl get deployment -n monitoring prometheus-operator \
  -o jsonpath='{.spec.template.spec.containers[0].image}'

kubectl api-versions | grep monitoring.coreos.com
# 預期包含：monitoring.coreos.com/v1beta1
```

### 安裝 kube-prometheus-stack（如尚未安裝）

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values values.yaml
```

**關鍵 values.yaml 配置**：

```yaml
prometheus:
  prometheusSpec:
    ruleSelectorNilUsesHelmValues: false   # ★ 必須 false，允許外部 PrometheusRule
    ruleSelector: {}                       # 空 selector = 納入所有 PrometheusRule
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelector: {}
```

> `ruleSelectorNilUsesHelmValues: false` 與 `ruleSelector: {}` 的組合告訴 Prometheus Operator 接納所有 PrometheusRule，包括非 kube-prometheus-stack 部署的。

---

## Step 1: ServiceMonitor — 注入 tenant 標籤 + 抓取 threshold-exporter

### 1a. 為資料庫 Exporter 建立 ServiceMonitor

為你現有的資料庫 exporters（MySQL、PostgreSQL、Redis 等）注入 `tenant` 標籤：

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
        # ★ 核心：將 namespace 注入為 tenant 標籤
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
```

### 1b. 為 threshold-exporter 建立 ServiceMonitor

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

> **Helm 捷徑**：設定 `rules.mode: operator` 時，threshold-exporter chart 會自動產出 ServiceMonitor（見 `values.yaml` 的 `rules.operator.serviceMonitor`）。

### 驗證 ServiceMonitor

```bash
kubectl get servicemonitor -n monitoring
curl -s 'http://localhost:9090/api/v1/query?query=up{tenant!=""}' | jq '.data.result | length'
```

---

## Step 2: PrometheusRule — 自動轉換 Rule Pack

### 推薦方式：da-tools operator-generate

```bash
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops

kubectl apply -f rules-crd/
```

### 手動方式

從 ConfigMap 提取規則內容，轉換為 PrometheusRule CRD：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: dynamic-alerts-mariadb
  namespace: monitoring
  labels:
    prometheus: kube-prometheus         # ★ da-tools operator-generate 預設
    release: kube-prometheus-stack      # ★ kube-prometheus-stack 預設 ruleSelector
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

### 驗證 PrometheusRule

```bash
kubectl get prometheusrule -n monitoring
curl -s 'http://localhost:9090/api/v1/rules' | jq '.data.groups | length'
# 確認無評估錯誤
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '[.data.groups[].rules[] | select(.lastError != "")] | length'
```

---

## Helm Chart: rules.mode 切換

threshold-exporter Helm chart 在 v2.6.0 新增 `rules.mode` toggle：

```yaml
# values.yaml
rules:
  mode: operator    # 切換至 Operator 模式
  operator:
    ruleLabels:
      prometheus: kube-prometheus
    serviceMonitor:
      enabled: true
      interval: 15s
```

設定 `mode: operator` 後：chart 自動產出 ServiceMonitor；Rule Pack 改由 PrometheusRule CRD 載入（而非 ConfigMap projected volume）。threshold-exporter 本身不變——它仍讀取 `conf.d/` ConfigMap 中的 tenant 閾值。

---

## Troubleshooting

### PrometheusRule 未被載入

檢查 Prometheus 的 ruleSelector 是否匹配：

```bash
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'
```

若為空或不匹配，修改 kube-prometheus-stack values 並 `helm upgrade`。

### ruleSelector 雙 Label 匹配

`operator-generate` 產出的 PrometheusRule 包含 **兩個** selector label：

```yaml
metadata:
  labels:
    release: kube-prometheus-stack    # Helm chart 預設 selector
    prometheus: kube-prometheus        # Prometheus CRD spec.ruleSelector
```

**為什麼需要兩個？** `kube-prometheus-stack` Helm chart 根據安裝方式不同，Prometheus CRD 的 `spec.ruleSelector.matchLabels` 可能使用 `release` 或 `prometheus` 其中一個（或兩者皆有）。同時帶兩個 label 可確保在所有常見安裝配置下都能被正確載入。

**診斷步驟**：

```bash
# 1. 查看 Prometheus 實際要求的 ruleSelector
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# 2. 查看你的 PrometheusRule 上的 labels
kubectl get prometheusrule -n monitoring --show-labels

# 3. 若 label 不匹配，使用 operator-generate 重新產出（自動帶雙 label）
da-tools operator-generate --components rules --namespace monitoring
```

> **常見陷阱**：手動撰寫的 PrometheusRule 只帶 `release: kube-prometheus-stack`，但部分環境的 ruleSelector 要求 `prometheus: kube-prometheus`。使用 `operator-generate` 產出可避免此問題。

### Namespace Selector 跨 namespace 失敗

確認 Prometheus ServiceAccount 有跨 namespace 的 RBAC 權限：

```bash
kubectl get clusterrolebinding | grep prometheus
```

---

## 相關文件

| 文件 | 說明 |
|------|------|
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | AlertmanagerConfig CRD 路由配置 |
| [Operator GitOps 部署](operator-gitops-deployment.md) | ArgoCD / Flux 整合 |
| [Operator Shadow Monitoring](operator-shadow-monitoring.md) | 雙軌觀察策略 |
| [BYO Prometheus 整合](byo-prometheus-integration.md) | ConfigMap 路徑（Path A） |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 架構決策記錄 |
