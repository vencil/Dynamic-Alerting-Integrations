---
title: "Prometheus Operator 整合手冊"
tags: [operator, integration, kube-prometheus-stack]
audience: [platform-engineer]
version: v2.5.0
lang: zh
---
# Prometheus Operator 整合手冊

> **受眾**：Platform Engineers、SREs
> **版本**：v2.3.0
> **前置閱讀**：[BYO Prometheus 整合指南](byo-prometheus-integration.md)、[架構與設計](architecture-and-design.md)
> **相關 ADR**：[ADR-008 — Operator CRD 路徑](adr/008-operator-native-integration-path.md)

---

## Overview

本指南涵蓋 Prometheus Operator (kube-prometheus-stack) 環境下，Dynamic Alerting 平台的完整整合方案。與 [BYO Prometheus](byo-prometheus-integration.md) 的原生配置方式相比，**Operator 路徑使用聲明式 CRD**（ServiceMonitor、PrometheusRule、AlertmanagerConfig）取代手動 ConfigMap 掛載，自動化程度更高。

### When to Choose the Operator Path?

| 特徵 | 原生 ConfigMap | Operator CRD | 推薦 |
|------|----------------|--------------|------|
| 已裝 kube-prometheus-stack | ❌ | ✅ | **Operator** |
| Helm Values 管理 Prometheus 配置 | ✅ | ❌ | ConfigMap |
| 多 Prometheus 實例 | ⚠️ (複雜) | ✅ | **Operator** |
| GitOps (ArgoCD/Flux) | ✅ | ✅ | **Operator** 更優 |
| 簡單部署（1 Prometheus） | ✅ | ✅ | 皆可，看團隊偏好 |

---

## Prerequisites: Verify Operator Installation

### Check CRDs Exist

```bash
# 檢查 PrometheusRule、ServiceMonitor、AlertmanagerConfig CRD
kubectl get crd prometheusrules.monitoring.coreos.com
kubectl get crd servicemonitors.monitoring.coreos.com
kubectl get crd alertmanagerconfigs.monitoring.coreos.com

# 一行檢查全部
kubectl get crd | grep -E 'prometheus|alertmanager'
```

### Verify Operator Version

Dynamic Alerting v2.3.0 要求 Prometheus Operator **≥ 0.65.0**（支援 PrometheusRule `v1beta1`、AlertmanagerConfig `v1beta1`）：

```bash
# 查詢 Operator 版本
kubectl get deployment -n monitoring prometheus-operator -o jsonpath='{.spec.template.spec.containers[0].image}'
# 預期格式：ghcr.io/prometheus-operator/prometheus-operator:v0.x.y

# 查詢可用的 API 版本
kubectl api-versions | grep monitoring.coreos.com
# 預期輸出包含：monitoring.coreos.com/v1beta1
```

### If Operator is Not Installed

使用 Helm 安裝 kube-prometheus-stack：

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --values values.yaml                           # 參考下節
```

參考 values 配置（`values.yaml`）：

```yaml
prometheus:
  prometheusSpec:
    ruleSelectorNilUsesHelmValues: false         # ★ 必須 false，允許外部 PrometheusRule
    ruleSelector: {}                             # 空 selector = 納入所有 PrometheusRule
    serviceMonitorSelectorNilUsesHelmValues: false
    serviceMonitorSelector: {}
    # 其他配置...

alertmanager:
  alertmanagerSpec:
    alertmanagerConfigSelector: {}               # 納入所有 AlertmanagerConfig
```

> **重要**：`ruleSelectorNilUsesHelmValues: false` 與 `ruleSelector: {}` 的組合告訴 Prometheus Operator 接納所有 PrometheusRule（包括非 kube-prometheus-stack 部署的）。

---

## Step 1: ServiceMonitor — Inject tenant Label + Scrape threshold-exporter

### Step 1a. Create ServiceMonitor for Database Exporters

針對你現有的資料庫 exporters（MySQL、PostgreSQL、Redis 等）注入 `tenant` 標籤：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: tenant-db-exporters
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ← 必須匹配 Prometheus 的 serviceMonitorSelector
spec:
  namespaceSelector:
    matchNames:
      - db-a
      - db-b
      # 新增其他 tenant namespace...
  selector:
    matchLabels:
      prometheus.io/scrape: "true"       # ← 對應你 Service 的 annotation
  endpoints:
    - port: metrics                      # ← Service port 的名稱，非數字
      interval: 10s
      relabelings:
        # ★ 核心：將 namespace 名稱注入為 tenant 標籤
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: tenant
        # （可選）去除 Pod 名稱，保留簡潔
        - action: drop
          sourceLabels: [__meta_kubernetes_pod_name]
          regex: ".*-debug.*"
```

**應用**：

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
      app: threshold-exporter           # ← 對應 threshold-exporter Service 的 label
  endpoints:
    - port: http                        # ← threshold-exporter Service port 的名稱
      interval: 15s
```

**應用**：

```bash
kubectl apply -f servicemonitor-threshold-exporter.yaml
```

### Verify ServiceMonitor

```bash
# 確認 ServiceMonitor 被 Prometheus 發現
kubectl get servicemonitor -n monitoring

# 查看 Prometheus 產生的 scrape config（會包含 __meta_kubernetes_namespace → tenant relabel）
kubectl exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -- \
  cat /etc/prometheus/config_out/prometheus.env.yaml | \
  grep -A 20 "tenant-db-exporters"

# 查詢 tenant 標籤是否正確注入
curl -s 'http://localhost:9090/api/v1/query?query=up{tenant!=""}' | jq '.data.result | length'
```

---

## Step 2: PrometheusRule — Auto-convert Rule Packs

### Recommended: Use da-tools operator-generate

**推薦方式**：使用 `da-tools` 內建的 `operator-generate` 命令，將平台的規則包自動轉換為 PrometheusRule CRD。

```bash
# 安裝/更新 da-tools
docker pull ghcr.io/vencil/da-tools:v2.4.0

# 指定 config-dir，自動掃描並產出 PrometheusRule
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops                                    # （可選）輸出可 GitOps 部署的 YAML

# 查看產出的 CRD
ls -la rules-crd/
```

**輸出結果**：

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

**應用到叢集**：

```bash
# 一次應用全部
kubectl apply -f rules-crd/

# 或使用 kubectl kustomize / ArgoCD / Flux 部署
kustomize build rules-crd/ | kubectl apply -f -
```

### Manual Method (if not using da-tools)

#### 2a. Extract Rule Pack Contents

從現有的 ConfigMap 中提取規則：

```bash
# 檢查現有 ConfigMap
kubectl get configmap -n monitoring | grep prometheus-rules

# 提取特定規則包（以 MariaDB 為例）
kubectl get configmap prometheus-rules-mariadb -n monitoring -o yaml > mariadb-cm.yaml

# 查看 ConfigMap 內容
cat mariadb-cm.yaml
```

#### 2b. Convert to PrometheusRule

參考下列範例，將 ConfigMap 的 `data` 欄位轉換為 PrometheusRule 的 `spec.groups[]`：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: dynamic-alerts-mariadb
  namespace: monitoring
  labels:
    release: kube-prometheus-stack       # ★ Prometheus 的 ruleSelector 必須納入此 label
spec:
  groups:
    # 以下 groups 來自原 ConfigMap 的 mariadb-recording.yml 與 mariadb-alert.yml
    - name: mariadb-normalization
      interval: 30s
      rules:
        - record: tenant:mysql_global_status_connections:max
          expr: max by (tenant) (mysql_global_status_connections)
        - record: tenant:mysql_global_status_threads_connected:max
          expr: max by (tenant) (mysql_global_status_threads_connected)
        # ... 更多 recording rules

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
        # ... 更多 alert rules
```

#### 2c. Apply to Cluster

```bash
kubectl apply -f prometheusrule-mariadb.yaml
```

### Verify PrometheusRule

```bash
# 確認 PrometheusRule 被 Operator 發現
kubectl get prometheusrule -n monitoring

# 詳細檢查特定規則
kubectl describe prometheusrule dynamic-alerts-mariadb -n monitoring

# 檢查 Prometheus 是否正確載入（查看規則評估狀態）
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '.data.groups[] | select(.name | contains("mariadb"))'

# 確認無評估錯誤
curl -s 'http://localhost:9090/api/v1/rules' | \
  jq '[.data.groups[].rules[] | select(.lastError != "")] | length'
# 預期：0
```

---

## Step 3: AlertmanagerConfig — Dynamic Routing Configuration

### Prerequisite: Alertmanager Operator Configuration

確認 Prometheus Operator Helm Chart 已啟用 Alertmanager 並設定為接納 AlertmanagerConfig：

```yaml
# values.yaml
alertmanager:
  enabled: true
  alertmanagerSpec:
    alertmanagerConfigSelector: {}        # 空 selector = 納入所有 AlertmanagerConfig
    # 或指定特定 label
    # alertmanagerConfigSelector:
    #   matchLabels:
    #     release: kube-prometheus-stack
```

### 3a. Use da-tools operator-generate to Output AlertmanagerConfig

```bash
# 與 PrometheusRule 同時產出
da-tools operator-generate \
  --config-dir /path/to/conf.d \
  --output-dir ./rules-crd \
  --gitops

# 產出結果包含
ls rules-crd/alertmanagerconfig-*.yaml
```

### 3b. Manually Create AlertmanagerConfig

基於 tenant YAML 的 `_routing` 配置，產出 per-tenant AlertmanagerConfig：

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
    # 匹配該 tenant 的所有告警
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
    # Severity dedup 規則：Critical 抑制 Warning
    - sourceMatchers:
        - name: severity
          value: critical
      targetMatchers:
        - name: severity
          value: warning
        - name: tenant
          value: db-a

    # Silent Mode：maintenance 標籤抑制所有告警
    - sourceMatchers:
        - name: alertstate
          value: silent
      targetMatchers:
        - name: tenant
          value: db-a
```

**應用**：

```bash
kubectl apply -f alertmanagerconfig-db-a.yaml
```

### Verify AlertmanagerConfig

```bash
# 確認 AlertmanagerConfig 被 Operator 發現
kubectl get alertmanagerconfig -n monitoring

# 檢查 Alertmanager 配置是否正確（需要 port-forward）
kubectl port-forward svc/alertmanager -n monitoring 9093:9093 &
curl -s 'http://localhost:9093/api/v1/status' | jq '.config'

# 查詢特定 tenant 的路由
curl -s 'http://localhost:9093/api/v1/alerts?silenced=false&inhibited=false' | jq '.'
```

---

## Migration Path: From ConfigMap to Operator CRD

如果你已有基於 ConfigMap 的整合，以下是逐步遷移的流程：

### Phase 1: Parallel Run (1 week)

同時部署 ConfigMap 與 CRD，監控兩端的告警產出是否一致：

```bash
# 保留舊的 ConfigMap 部署
kubectl get configmap -n monitoring | grep prometheus-rules

# 新增 PrometheusRule CRD
kubectl apply -f prometheusrule-*.yaml

# Prometheus 會同時載入兩組規則，規則名稱相同會報錯
# 檢查衝突
kubectl logs -n monitoring prometheus-kube-prometheus-stack-prometheus-0 | grep -i "duplicate\|conflict"
```

### Phase 2: Switch Alert Routes (1 day)

一旦 Alertmanager 收到的告警數量穩定，逐步遷移路由配置至 AlertmanagerConfig：

```bash
# 應用 AlertmanagerConfig
kubectl apply -f alertmanagerconfig-*.yaml

# 檢查 Alertmanager 配置變化（應無差異）
curl -s http://localhost:9093/api/v1/alerts | jq '.alerts | length'
```

### Phase 3: Clean Up ConfigMap (1 day later)

確認 CRD 運行穩定後，刪除舊的 ConfigMap：

```bash
# 備份舊配置
kubectl get configmap -n monitoring | grep prometheus-rules | while read -r cm; do
  kubectl get configmap "$cm" -n monitoring -o yaml > "backup-${cm}.yaml"
done

# 刪除舊 ConfigMap
kubectl delete configmap prometheus-rules-mariadb prometheus-rules-redis ... -n monitoring
```

### Rollback Plan

若遇到問題，快速回滾：

```bash
# 恢復 ConfigMap
kubectl apply -f backup-*.yaml

# 刪除 CRD（會自動回復舊配置）
kubectl delete -f prometheusrule-*.yaml
```

---

## Namespace 策略：Cluster-wide vs Namespace-scoped

### Cluster-wide Deployment (Recommended)

所有 ServiceMonitor、PrometheusRule、AlertmanagerConfig 部署在 `monitoring` namespace：

**優點**：
- 集中管理，便於 GitOps
- Prometheus 單一實例可覆蓋所有租戶

**適用場景**：
- 單一 Prometheus 實例
- Tenant 各自在獨立 namespace

**配置**：

```yaml
# ServiceMonitor 在 monitoring namespace，但 namespaceSelector 跨越多個 tenant ns
namespaceSelector:
  matchNames:
    - db-a
    - db-b
    - db-c
```

### Namespace-scoped Deployment

每個 tenant namespace 部署自己的 ServiceMonitor 與 PrometheusRule（使用 aggregating Prometheus）：

**優點**：
- Tenant 自主管理自己的規則
- 適合多個 Prometheus 實例（per-tenant 或 per-region）

**缺點**：
- 管理複雜度高
- 規則重複定義

**配置示例**：

```yaml
# db-a namespace 內的 ServiceMonitor
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: db-a-exporters
  namespace: db-a                           # ← 在 tenant namespace
spec:
  namespaceSelector:
    matchNames: ["db-a"]
  selector:
    matchLabels:
      app: mysql-exporter
```

**推薦**：對大多數部署，使用 **Cluster-wide**。

---

## GitOps 整合

### ArgoCD

建立 ArgoCD Application，指向存放 CRD 的目錄：

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
    path: prometheus-operator-crds/          # ← CRD 目錄
  destination:
    server: https://kubernetes.default.svc
    namespace: monitoring
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

### Flux

建立 Flux Kustomization：

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
# 產出可直接用 GitOps 部署的 YAML
da-tools operator-generate \
  --config-dir ./conf.d \
  --output-dir ./gitops/monitoring \
  --gitops                                  # 輸出包含 kustomization.yaml

# Push 到 Git
git add gitops/
git commit -m "Update Prometheus Operator CRDs"
git push
```

---

## Verification & Troubleshooting

### Complete Checklist

```bash
# 1. CRD 已安裝
kubectl get crd | grep monitoring.coreos.com

# 2. ServiceMonitor 被發現
kubectl get servicemonitor -n monitoring

# 3. PrometheusRule 被載入（無衝突）
kubectl get prometheusrule -n monitoring
curl -s http://localhost:9090/api/v1/rules | jq '.data.groups | length'

# 4. AlertmanagerConfig 被應用
kubectl get alertmanagerconfig -n monitoring

# 5. 指標與閾值向量匹配正常
curl -s 'http://localhost:9090/api/v1/query?query=tenant:mysql_threads_connected:max' | jq '.data.result | length'
curl -s 'http://localhost:9090/api/v1/query?query=tenant:alert_threshold:connections' | jq '.data.result | length'
```

### Common Issues

#### Issue 1: PrometheusRule Not Loaded

**症狀**：`kubectl get prometheusrule` 顯示存在，但 `curl /api/v1/rules` 看不到

**排查**：

```bash
# 檢查 Prometheus 的 ruleSelector 設定
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}'

# 若為空或未設定，需修改 kube-prometheus-stack Helm values：
helm upgrade kube-prometheus-stack \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelector={} \
  prometheus-community/kube-prometheus-stack -n monitoring

# 重啟 Prometheus
kubectl rollout restart statefulset/prometheus-kube-prometheus-stack-prometheus -n monitoring
```

#### Issue 2: ruleSelector Label Mismatch

**症狀**：PrometheusRule 存在，但 Operator 沒有納入

**檢查**：

```bash
# 查看 Prometheus 期望的 label
kubectl get prometheus -n monitoring -o jsonpath='{.items[0].spec.ruleSelector}' | jq .

# 確認 PrometheusRule 有該 label
kubectl get prometheusrule -n monitoring -o jsonpath='{.items[0].metadata.labels}' | jq .
```

**修正**：補齊 label

```yaml
metadata:
  labels:
    release: kube-prometheus-stack        # ← 確保此 label 存在
```

#### Issue 3: Alertmanager Cannot Recognize AlertmanagerConfig

**症狀**：`kubectl get alertmanagerconfig` 有紀錄，但 Alertmanager 配置未變化

**排查**：

```bash
# 檢查 Alertmanager 的 alertmanagerConfigSelector
kubectl get alertmanager -n monitoring -o jsonpath='{.items[0].spec.alertmanagerConfigSelector}' | jq .

# 若未設定，編輯 Alertmanager 資源
kubectl patch alertmanager kube-prometheus-stack-alertmanager -n monitoring --type merge -p \
  '{"spec":{"alertmanagerConfigSelector":{}}}'

# 重啟 Alertmanager
kubectl rollout restart statefulset/alertmanager-kube-prometheus-stack-alertmanager -n monitoring
```

#### Issue 4: Namespace Selector Across Multiple ns Fails to Scrape

**症狀**：只有部分 tenant namespace 的 target 被發現

**檢查**：

```bash
# 確認 RBAC：Prometheus ServiceAccount 須有 list pods/services 的權限跨越所有 ns
kubectl get rolebinding,clusterrolebinding -n monitoring | grep prometheus

# 若無，手動新增 ClusterRole
kubectl create clusterrole prometheus-discovery --verb=get,list,watch --resource=services,pods
kubectl create clusterrolebinding prometheus-discovery --clusterrole=prometheus-discovery \
  --serviceaccount=monitoring:prometheus-kube-prometheus-stack-prometheus
```

---

## FAQ

**Q：我已有原生 Prometheus 配置，可以與 Operator 混用嗎？**
A：不建議。若必須，使用 `additionalPrometheusRules` Helm value 注入額外的 rule 文件，避免 ServiceMonitor/PrometheusRule 重複。詳見 [ADR-008](adr/008-operator-native-integration-path.md)。

**Q：Operator 如何處理規則版本升級？**
A：每次 Dynamic Alerting 升級時，重新執行 `da-tools operator-generate`，提交新的 PrometheusRule 至 Git repo，GitOps 工具自動同步。無停機升級。

**Q：能否混用 AlertmanagerConfig v1alpha1 與 v1beta1？**
A：不建議。v1alpha1 已棄用，建議遷移至 v1beta1。使用 `kubectl convert` 進行自動轉換：
```bash
kubectl convert -f alertmanagerconfig-old.yaml --output-version monitoring.coreos.com/v1beta1 | kubectl apply -f -
```

**Q：如何在 Operator 路徑下實現 Shadow Monitoring？**
A：保留舊的 AlertmanagerConfig 與新的並行，修改 receiver 指向影子告警目的地。詳見 [Shadow Monitoring SOP](shadow-monitoring-sop.md)。

---

## Related Resources

| 資源 | 相關性 |
|------|--------|
| [BYO Prometheus 整合指南](byo-prometheus-integration.md) | ⭐⭐⭐ |
| [BYO Alertmanager 整合指南](byo-alertmanager-integration.md) | ⭐⭐⭐ |
| [ADR-008 — Operator CRD 路徑](adr/008-operator-native-integration-path.md) | ⭐⭐⭐ |
| [da-tools CLI Reference — operator-generate](cli-reference.md#operator-generate) | ⭐⭐ |
| [架構與設計](architecture-and-design.md) | ⭐⭐ |
| [Shadow Monitoring SOP](shadow-monitoring-sop.md) | ⭐⭐ |
| [Prometheus Operator 官方文檔](https://prometheus-operator.dev/) | ⭐ |
| [kube-prometheus-stack Helm Chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack) | ⭐ |
