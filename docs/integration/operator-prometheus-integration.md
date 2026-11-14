---
title: "Operator Prometheus 整合指南"
tags: [operator, prometheus, integration]
audience: [platform-engineer]
version: v2.9.0
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
        # ★ 核心：將 namespace 注入為 tenant 標籤
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
        # ⛔ job 必須是 tenant-exporters：每個租戶 rule pack 都選 up{job="tenant-exporters"}。
        # 沒有這條，prometheus-operator 會用 Service 名稱當 job，健康的租戶也會被當成缺席而 page。
        - targetLabel: job
          replacement: tenant-exporters
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

> **NetworkPolicy**：chart 預設替 exporter 建 NetworkPolicy，入站**只限 metrics port、不限來源**（`networkPolicy.allowExternal: true`）——chart 無從得知你的 Prometheus 帶什麼 pod label，猜錯會讓 scrape 靜默斷掉。要把入站收到只放 Prometheus，設 `networkPolicy.allowExternal: false`，並填 `networkPolicy.prometheusNamespace`（它所在的 namespace）與 `networkPolicy.prometheusPodLabels`（它 pod 上的 label，kube-prometheus-stack 是 `app.kubernetes.io/name: prometheus`）；兩者都不可為空，否則 chart 拒絕 render。

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
  --components rules \
  --rule-packs-dir rule-packs/ \
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
            tenant:mysql_connection_usage:percent
              > on(tenant) group_left()
            tenant:alert_threshold:mysql_connection_usage_percent
          for: 5m
          labels:
            severity: warning
            metric_group: mysql_connection_usage_percent
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

## 平台自監控告警（platform pack）

平台自監控告警（`rule-packs/rule-pack-platform.yaml`，含 `Watchdog`）讀的是平台元件自己的 metrics。Operator 模式下 Prometheus 只 scrape ServiceMonitor 選到的東西，所以要兩件事都做（[#1595](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1595)）：

**1. 打開平台 chart 的 ServiceMonitor**（預設關閉：沒有 `monitoring.coreos.com` CRD 的叢集套不上）：

| Chart | 打開的 values | 抓的 Service（= `job`） |
|---|---|---|
| `threshold-exporter` | `rules.mode: operator`（`rules.operator.serviceMonitor.enabled` 預設 `true`） | `threshold-exporter` |
| `tenant-api` | `serviceMonitor.enabled: true` | `tenant-api` |
| `vector` | `serviceMonitor.enabled: true`（端點跟著 `metrics.enabled`／`tenantProjections` 各自的 Service 走） | `vector-metrics`、`vector-projection-gate-metrics` |
| `federation-gateway` | `serviceMonitor.enabled: true`（需 `auditLog.enabled`，預設開） | `federation-gateway` |
| `federation-reconciler` | `serviceMonitor.enabled: true` | `federation-reconciler` |

每個 ServiceMonitor 選的是帶 `dynamic-alerting.io/scrape: platform` 的那個 Service、抓的是 raw 設定 `platform-components` job 抓的同一個 pod port，且**不設 `jobLabel`**——`job` 就是 Service 名稱，與 raw 模式相同，所以告警裡 `job="tenant-api"` 這類 selector 兩邊都成立。預設帶 `release: kube-prometheus-stack`，其他 release 名稱用 `serviceMonitor.labels` 改（`release: null` 可拿掉）。

**NetworkPolicy**：ServiceMonitor 選對了 port，若 chart 自己的 NetworkPolicy 擋下 Prometheus，scrape 仍然是空的。各 chart 預設都假設 Prometheus 在 `monitoring` namespace；kube-prometheus-stack 裝在別處就要改：

| Chart | 放行 scrape 的設定 | 預設 |
|---|---|---|
| `threshold-exporter` | `networkPolicy.allowExternal`／`prometheusNamespace`／`prometheusPodLabels`（見 1b） | metrics port 不限來源 |
| `tenant-api` | `networkPolicy.prometheusNamespace` ＋ pod 級白名單 `networkPolicy.internalPortAllow.prometheus`（raw 的 `app=prometheus`）與 `.prometheusOperator`（kube-prometheus-stack 的 `app.kubernetes.io/name=prometheus`） | `monitoring` 裡帶這兩組 label 之一的 pod；8080 只有 probe 與 metrics（帶身分 header 回 400），白名單只放抓 metrics 的 Prometheus |
| `vector` | chart 不帶 NetworkPolicy | — |
| `federation-gateway` | `networkPolicy.scrapeNamespaceLabel`（Prometheus 所在 namespace 的 label） | `monitoring` 裡任何 pod；設 `null` 表示任何 namespace（`{}` 不會清掉預設值，Helm 會合併 map） |
| `federation-reconciler` | `networkPolicy.scrapeNamespaceLabel`（scrape 入站）；它查 VictoriaLogs 的出站另由 `networkPolicy.victorialogsNamespaceLabel` 決定，不跟著 `null` 放寬 | 同上 |

⚠️ 改了 federation-gateway 的 `auditLog.metrics.port` 或 federation-reconciler 的 `metricsPort`，Prometheus 那一側也要放行新 port：本 repo 的 raw stack 是 `k8s/03-monitoring/network-policies.yaml` 的 `allow-prometheus-egress`（只列 chart 預設的 3903／9099）；你的 kube-prometheus-stack 若有 egress NetworkPolicy，同理。

**2. 套用 `operator-generate` 產出的 `da-rule-pack-platform`。** 它是平台 pack 去掉 kube-prometheus-stack 沒有指標來源的那幾條：哪幾條由 `_lib_metric_sources.operator_split` 依 scrape 閘門的 metric family 表推導（不是手列），產出時每條在 stderr 印一行 `NOTE:`，也列在該 PrometheusRule 的 `dynamic-alerting.io/operator-excluded-rules` annotation。目前是：

<!-- operator-excluded-rules:begin -->
| 告警 | 為什麼 Operator 模式沒有它的來源 |
|---|---|
| `PrometheusConfigReloadFailed` | 選 `job="prometheus"`：那是本 repo raw 設定的 Prometheus 自 scrape job，kube-prometheus-stack 以自己的 Service 名稱命名該 job（它自帶的 `PrometheusBadConfig` 涵蓋同一件事） |
<!-- operator-excluded-rules:end -->

`tests/ops/test_operator_generate_rule_copies.py` 比對上表與推導結果，兩邊不一致就紅。

⚠️ **`Watchdog` 與 kube-prometheus-stack 自帶的同名告警。** 兩者的 label 都只有 `alertname="Watchdog"`、`severity="none"`（同一個 Prometheus 評估，external labels 也相同），Alertmanager 依 label 集合去重，收到的是**一條** Watchdog；不會多出第二條、也不受 `severity` 型 inhibit 影響。路由由 `generate-routes` 渲染時（見 [Operator Alertmanager 整合](operator-alertmanager-integration.md)），這一條 Watchdog 先被平台的 `watchdog-heartbeat` 路由收下；dead-man's-switch 接在你 base 裡的 `watchdog-heartbeat` receiver 上，而不是 kube-prometheus-stack 預設送往 `null` 的那條路由。它的 annotations 可能是兩份中任一份的。例外：Prometheus 設了 `enforcedNamespaceLabel` 且兩份 PrometheusRule 在不同 namespace 時，兩條帶不同 `namespace` label，不會去重。

⚠️ **部署了 federation 卻沒開 ServiceMonitor，會收到兩條 critical。** `FederationRevocationReconcileStale`（指標不存在的那一半）與 `FederationAuditPipelineSilent` 以 kube-state-metrics 判斷元件是否部署（reconciler 的 Deployment、gateway 帶 `audit-metrics` 容器的 pod），不看 `up`——scrape 目標遺失正是它們要抓的狀況。所以 federation 有部署、但 `serviceMonitor.enabled` 還是預設的 `false` 時，這兩條會響：撤銷竄改偵測與稽核管線確實沒人在看。這是正確的訊號；把那兩個 chart 的 `serviceMonitor.enabled` 打開即可。沒部署 federation 的叢集不會收到。

⚠️ **仍然選 `namespace="monitoring"` 的幾條**（`CronJobLastRunFailed`、`ConfigReloaderHighRestarts`、`ConfigReloaderNotStarting`）看的是本 repo `k8s/03-monitoring/` 的 raw manifest（Prometheus／Alertmanager 的 `config-reloader` sidecar；`CronJobLastRunFailed` 看你放進 `monitoring` 的 CronJob，本 repo 目前沒出貨任何一支），那些 manifest 寫死在 `monitoring`。Operator 模式下它們照樣輸出（kube-state-metrics 有這些 series），但只看名為 `monitoring` 的 namespace：kube-prometheus-stack 裝在那裡時，它自己 Prometheus 的 `config-reloader` sidecar 也會被看到。平台 chart 的工作負載（`TenantApiSingleWriterBreach`、`ThresholdExporterHighRestarts`）不再釘 namespace，裝在哪都看得到。

---

## Helm Chart: rules.mode 切換

threshold-exporter Helm chart 在 v2.6.0 新增 `rules.mode` toggle：

```yaml
# values.yaml
rules:
  mode: operator    # 切換至 Operator 模式
  operator:
    serviceMonitor:
      enabled: true
      interval: 15s
```

設定 `mode: operator` 後：chart 自動產出 ServiceMonitor；Rule Pack 改由 PrometheusRule CRD 載入（而非 ConfigMap projected volume）——⚠️ 這些 PrometheusRule **不是 chart 產生的**，請用 `da-tools operator-generate` 產出後 apply（見上文）。threshold-exporter 本身不變——它仍讀取 `conf.d/` ConfigMap 中的 tenant 閾值。

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

`migrate-to-operator` 與 `rule-pack-split` 產出的 PrometheusRule 帶同一組 label（同一份定義）。repo 不提交預先產生的 PrometheusRule——從 rule pack 產生時，用 `da-tools operator-generate --components rules --output-dir <你的目錄>` 產生到自己的目錄（[#3427](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3427)）。

**診斷步驟**：

```bash
# 1. 查看 Prometheus 實際要求的 ruleSelector
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# 2. 查看你的 PrometheusRule 上的 labels
kubectl get prometheusrule -n monitoring --show-labels

# 3. 若 label 不匹配，使用 operator-generate 重新產出（自動帶雙 label）
da-tools operator-generate --components rules --namespace monitoring

# 4. Helm release 不叫 kube-prometheus-stack：用 --selector-label 對上實際的 selector
da-tools operator-generate --selector-label release=my-prom
```

`--selector-label` 可重複，同時加在 PrometheusRule 與 ServiceMonitor 上，同 key 覆寫預設（#2075）。chart 自己的 ServiceMonitor（`rules.mode: operator`）則用 values `rules.operator.serviceMonitor.labels` 加 label，預設同樣是 `release: kube-prometheus-stack`（設 `release: null` 可拿掉）。

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
| [Operator Alertmanager 整合](operator-alertmanager-integration.md) | `generate-routes` → Secret → `configSecret` 路由配置 |
| [Operator GitOps 部署](operator-gitops-deployment.md) | ArgoCD / Flux 整合 |
| [Operator Shadow Monitoring](operator-shadow-monitoring.md) | 雙軌觀察策略 |
| [BYO Prometheus 整合](byo-prometheus-integration.md) | ConfigMap 路徑（Path A） |
| [ADR-008](../adr/008-operator-native-integration-path.md) | 架構決策記錄 |
