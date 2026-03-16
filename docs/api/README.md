---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.1.0
lang: zh
---

# Threshold Exporter API 參考

Threshold Exporter 是 Multi-Tenant Dynamic Alerting 平台的核心元件，負責將租戶配置轉換為 Prometheus 指標、狀態過濾器和嚴重度去重標誌。本文件詳細說明所有 API 端點、請求/回應格式、範例和 Kubernetes 整合方式。

## 服務規格

| 項目 | 值 |
|------|-----|
| 監聽埠 | 8080 |
| 讀取逾時 | 5 秒 |
| 讀取標頭逾時 | 3 秒 |
| 寫入逾時 | 10 秒 |
| 空閒逾時 | 30 秒 |
| 最大標頭大小 | 8192 字節 |
| 指標格式 | OpenMetrics text format |

## API 總覽

```
GET /metrics          → Prometheus 指標匯出 (200 OK)
GET /health           → 存活探針 (200 OK)
GET /ready            → 就緒探針 (200 OK / 503 Service Unavailable)
GET /api/v1/config    → 設定狀態除錯端點 (200 OK)
```

---

## 1. GET /metrics - Prometheus 指標匯出

### 說明

匯出所有租戶的 Prometheus 指標，包括閾值狀態、靜音模式、嚴重度去重旗標和租戶中繼資料。此端點是 Prometheus scrape_config 的目標。

### 請求

```bash
curl -s http://localhost:8080/metrics | head -50
```

### 回應

**狀態碼**: 200 OK  
**Content-Type**: `application/openmetrics-text; version=1.0.0`

### 指標類型

#### `user_threshold` - 閾值指標

包含按租戶、警示名稱和維度的閾值值。支援多維度標籤，允許按執行個體或其他維度維度進行細緻的閾值設定。

```
# HELP user_threshold Threshold values by tenant, alert, and dimensions
# TYPE user_threshold gauge
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute"} 80.0
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute",dimension="instance=prod-01"} 85.0
user_threshold{tenant="db-b",alertname="HighMemory",metric_group="memory"} 75.0
user_threshold{tenant="db-b",alertname="HighMemory",metric_group="memory",dimension_re="instance=~staging-.*"} 65.0
```

**標籤：**
- `tenant`: 租戶 ID
- `alertname`: 警示名稱（來自 Rule Pack）
- `metric_group`: 指標分組（自訂警示組織單位）
- `dimension` (可選): 特定維度的閾值（如 `instance=prod-01`）
- `dimension_re` (可選): 正規表達式維度選擇器

#### `user_state_filter` - 警示抑制狀態

表示警示是否被狀態過濾器抑制。

```
# HELP user_state_filter Alert suppression state
# TYPE user_state_filter gauge
user_state_filter{tenant="db-a",alertname="HighCPU",metric_group="compute"} 0
user_state_filter{tenant="db-b",alertname="HighMemory",metric_group="memory"} 1
```

**值：**
- `0`: 警示處於活躍狀態（未被抑制）
- `1`: 警示被抑制（狀態過濾器啟用）

#### `user_silent_mode` - 租戶靜音模式

表示租戶是否處於靜音模式（所有警示暫時靜音）。

```
# HELP user_silent_mode Tenant silent mode status
# TYPE user_silent_mode gauge
user_silent_mode{tenant="db-a"} 0
user_silent_mode{tenant="db-b"} 1
```

**值：**
- `0`: 正常模式（靜音模式已停用）
- `1`: 靜音模式已啟用（所有警示都被抑制）

#### `user_severity_dedup` - 嚴重度去重旗標

表示警示是否已啟用嚴重度去重。此設定控制 Alertmanager 如何抑制低嚴重度警示。

```
# HELP user_severity_dedup Severity deduplication flag
# TYPE user_severity_dedup gauge
user_severity_dedup{tenant="db-a",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-b",alertname="HighMemory"} 0
```

**值：**
- `0`: 嚴重度去重已停用
- `1`: 嚴重度去重已啟用

#### `tenant_metadata_info` - 租戶中繼資料

以標籤形式暴露租戶中繼資料的資訊指標。在 Prometheus Rule Pack 中用 `group_left` 進行動態註解注入。

```
# HELP tenant_metadata_info Tenant metadata information
# TYPE tenant_metadata_info info
tenant_metadata_info{tenant="db-a",team="platform",env="prod",sla_tier="gold"} 1
tenant_metadata_info{tenant="db-b",team="data",env="prod",sla_tier="silver"} 1
tenant_metadata_info{tenant="db-b",oncall="sre-team@example.com",alert_channel="#prod-db-alerts"} 1
```

**用途：** 在警示規則中動態注入 SLA 等級、團隊資訊或値班資訊。

#### `da_config_event` - 設定事件計數

追蹤設定重新載入事件和錯誤。

```
# HELP da_config_event Configuration event counter
# TYPE da_config_event counter
da_config_event{event_type="reload_success"} 42
da_config_event{event_type="reload_error"} 2
da_config_event{event_type="config_hash_sha256"} 0x82a4d7c9f1e...
```

**事件類型：**
- `reload_success`: 成功的設定重新載入次數
- `reload_error`: 失敗的設定重新載入次數
- `config_hash_sha256`: 當前設定的 SHA-256 雜湊值

### 完整範例

```bash
$ curl -s http://localhost:8080/metrics

# HELP user_threshold Threshold values by tenant, alert, and dimensions
# TYPE user_threshold gauge
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute"} 80.0
user_threshold{tenant="db-a",alertname="HighCPU",metric_group="compute",dimension="instance=prod-01"} 85.0
user_threshold{tenant="db-a",alertname="HighMemory",metric_group="memory"} 75.0
user_threshold{tenant="db-b",alertname="HighCPU",metric_group="compute"} 70.0
user_threshold{tenant="db-b",alertname="HighDiskUsage",metric_group="storage"} 90.0

# HELP user_state_filter Alert suppression state
# TYPE user_state_filter gauge
user_state_filter{tenant="db-a",alertname="HighCPU",metric_group="compute"} 0
user_state_filter{tenant="db-a",alertname="HighMemory",metric_group="memory"} 1
user_state_filter{tenant="db-b",alertname="HighCPU",metric_group="compute"} 0

# HELP user_silent_mode Tenant silent mode status
# TYPE user_silent_mode gauge
user_silent_mode{tenant="db-a"} 0
user_silent_mode{tenant="db-b"} 1

# HELP user_severity_dedup Severity deduplication flag
# TYPE user_severity_dedup gauge
user_severity_dedup{tenant="db-a",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-a",alertname="HighMemory"} 0
user_severity_dedup{tenant="db-b",alertname="HighCPU"} 1
user_severity_dedup{tenant="db-b",alertname="HighDiskUsage"} 0

# HELP tenant_metadata_info Tenant metadata information
# TYPE tenant_metadata_info info
tenant_metadata_info{tenant="db-a",team="platform",env="prod",sla_tier="gold",oncall="platform-team"} 1
tenant_metadata_info{tenant="db-b",team="data",env="staging",sla_tier="silver",oncall="data-team"} 1

# HELP da_config_event Configuration event counter
# TYPE da_config_event counter
da_config_event{event_type="reload_success"} 42
da_config_event{event_type="reload_error"} 2

# EOF
```

---

## 2. GET /health - 存活探針

### 說明

檢查服務是否正在運行。用於 Kubernetes `livenessProbe`。即使設定未加載，此端點也應回應。

### 請求

```bash
curl -s http://localhost:8080/health
```

### 回應

**狀態碼**: 200 OK  
**Content-Type**: `text/plain`

```
ok
```

### Kubernetes 設定範例

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3
```

---

## 3. GET /ready - 就緒探針

### 說明

檢查服務是否已加載設定。返回 200 表示就緒（已加載設定），返回 503 表示未就緒（設定未加載或正在重新載入）。用於 Kubernetes `readinessProbe`。

### 請求

```bash
curl -s http://localhost:8080/ready
```

### 成功回應（已就緒）

**狀態碼**: 200 OK  
**Content-Type**: `text/plain`

```
ready
```

### 失敗回應（未就緒）

**狀態碼**: 503 Service Unavailable  
**Content-Type**: `text/plain`

```
config not loaded
```

### Kubernetes 設定範例

```yaml
readinessProbe:
  httpGet:
    path: /ready
    port: 8080
  initialDelaySeconds: 5
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2
```

### 完整 Pod 健康探針設定

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: threshold-exporter
spec:
  containers:
  - name: threshold-exporter
    image: ghcr.io/vencil/threshold-exporter:v2.1.0
    ports:
    - containerPort: 8080
      name: metrics
    
    # 存活探針 - 檢查服務是否仍在運行
    livenessProbe:
      httpGet:
        path: /health
        port: 8080
      initialDelaySeconds: 10
      periodSeconds: 10
      timeoutSeconds: 3
      failureThreshold: 3
    
    # 就緒探針 - 檢查設定是否已加載
    readinessProbe:
      httpGet:
        path: /ready
        port: 8080
      initialDelaySeconds: 5
      periodSeconds: 5
      timeoutSeconds: 3
      failureThreshold: 2
    
    # 資源限制
    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        cpu: 500m
        memory: 512Mi
    
    # 掛載設定
    volumeMounts:
    - name: config
      mountPath: /etc/config
      readOnly: true
  
  volumes:
  - name: config
    configMap:
      name: threshold-exporter-config
```

---

## 4. GET /api/v1/config - 設定狀態除錯端點

### 說明

Debug 端點，暴露目前加載的設定狀態以純文字格式。支援 RFC3339 時間戳記查詢參數，用於檢查排程式覆寫在特定時間點的狀態。

### 請求

#### 查詢目前設定

```bash
curl -s http://localhost:8080/api/v1/config | head -50
```

#### 查詢特定時間點的設定

```bash
# 查詢 2026-03-12T14:30:00Z 時的排程式覆寫狀態
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T14:30:00Z" | head -50
```

### 查詢參數

| 參數 | 型別 | 說明 | 範例 |
|------|------|------|------|
| `at` | string (RFC3339) | 檢查設定狀態的時間點。省略時返回目前狀態。 | `2026-03-12T14:30:00Z` |

### 回應

**狀態碼**: 200 OK  
**Content-Type**: `text/plain`

### 回應範例

```
=== Threshold Exporter Configuration ===

Loaded At: 2026-03-12T10:00:00Z
Config File: /etc/config/thresholds.yaml
Hash: 82a4d7c9f1e3b5a2c8d4e6f9a1b3c5d7 (SHA-256)
Reload Interval: 30 seconds
Last Reload: 2026-03-12T10:05:30Z

=== Tenants (2) ===

[db-a]
  namespace: db-a
  cluster: dynamic-alerting-cluster
  
  Mode Configuration:
    Severity Dedup: enabled
    Silent Mode: false (expires: never)
    State Filter: [compute/HighCPU]
  
  Thresholds:
    compute/HighCPU: 80.0
    compute/HighCPU[instance=prod-01]: 85.0
    compute/HighCPU[instance=prod-02]: 82.0
    memory/HighMemory: 75.0
    storage/HighDiskUsage: 85.0
  
  Metadata:
    team: platform
    env: prod
    sla_tier: gold
    runbook_url: https://wiki.example.com/db-a
    oncall: platform-oncall@example.com
  
  Scheduled Overrides:
    compute/HighCPU:
      └─ 75.0 @ 09:00-17:00 Mon-Fri (weekdays business hours)
    memory/HighMemory:
      └─ 70.0 @ Mon 02:00-04:00 (weekly maintenance window)
  
  Routing:
    _routing_enforced: enabled (NOC + tenant channels)
    _routing_defaults.severity_critical: '#critical-alerts'
    _routing_defaults.severity_warning: '#general-alerts'
    _routing_overrides.HighCPU: '#compute-team' (per-alert override)

[db-b]
  namespace: db-b
  cluster: dynamic-alerting-cluster
  
  Mode Configuration:
    Severity Dedup: disabled
    Silent Mode: true (expires: 2026-03-12T15:30:00Z)
    State Filter: []
  
  Thresholds:
    memory/HighMemory: 65.0
    network/HighPacketLoss: 5.0
  
  Metadata:
    team: data
    env: staging
    sla_tier: silver
    runbook_url: https://wiki.example.com/db-b
    oncall: data-team@example.com
  
  Scheduled Overrides: (none)
  
  Routing:
    _routing_enforced: disabled
    _routing_defaults: (using platform defaults)

=== Validation Status ===

Config Hash: 82a4d7c9f1e3b5a2c8d4e6f9a1b3c5d7
Tenant Keys Valid: ✓ All 7 keys validated
Cardinality: db-a=18 series, db-b=5 series (total 23, limit per tenant: 500)
Routes Valid: ✓ All receivers reachable
Routing Policy: ✓ Webhook domains within allowlist

=== Events (Last 10 minutes) ===

2026-03-12T10:05:30Z [INFO] Config reloaded successfully
2026-03-12T09:55:15Z [INFO] ConfigMap change detected, triggering reload
2026-03-12T09:34:22Z [WARN] Cardinality warning: db-a approaching limit (18/500)
```

### 常見用途

#### 1. 驗證租戶設定已正確載入

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 30 "^\[db-a\]"
```

#### 2. 檢查排程式覆寫在特定時間點的狀態

假設 `compute/HighCPU` 在工作時間（09:00-17:00）有排程式覆寫，檢查上午 10 點 30 分的值：

```bash
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T10:30:00Z" | grep -A 10 "Scheduled Overrides"
```

#### 3. 確認設定雜湊和最後重新載入時間

```bash
curl -s http://localhost:8080/api/v1/config | head -20
```

#### 4. 驗證租戶中繼資料已正確設定

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 10 "Metadata:"
```

---

## Prometheus Scrape 設定

### 簡單設定

```yaml
scrape_configs:
  - job_name: threshold-exporter
    static_configs:
      - targets: ['localhost:8080']
    scrape_interval: 30s
    scrape_timeout: 10s
```

### Kubernetes 服務發現設定

```yaml
scrape_configs:
  - job_name: threshold-exporter
    kubernetes_sd_configs:
      - role: pod
        namespaces:
          names:
            - monitoring
    relabel_configs:
      # 只抓取標有 'app=threshold-exporter' 的 Pod
      - source_labels: [__meta_kubernetes_pod_label_app]
        action: keep
        regex: threshold-exporter
      
      # 使用 Pod 名稱作為實例標籤
      - source_labels: [__meta_kubernetes_pod_name]
        action: replace
        target_label: instance
      
      # 添加叢集標籤
      - source_labels: [__meta_kubernetes_namespace]
        action: replace
        target_label: cluster
    
    scrape_interval: 30s
    scrape_timeout: 10s
```

---

## 故障排查

### 問題：readinessProbe 返回 503，"config not loaded"

**原因：** 設定檔未被正確掛載或加載失敗。

**解決方案：**
```bash
# 檢查 Pod 日誌
kubectl logs <pod-name> -n monitoring

# 驗證 ConfigMap 是否存在
kubectl get configmap threshold-exporter-config -n monitoring

# 檢查 ConfigMap 內容
kubectl get configmap threshold-exporter-config -n monitoring -o yaml

# 驗證掛載路徑
kubectl exec <pod-name> -n monitoring -- ls -la /etc/config/
```

### 問題：/metrics 端點返回空結果或缺少預期指標

**原因：** 租戶設定未加載或設定有語法錯誤。

**解決方案：**
```bash
# 檢查設定除錯端點
curl -s http://<pod-ip>:8080/api/v1/config | head -100

# 查看 Pod 事件日誌
kubectl describe pod <pod-name> -n monitoring

# 檢查設定驗證日誌
kubectl logs <pod-name> -n monitoring | grep -i "validation\|error"
```

### 問題：設定變更後指標未更新

**原因：** 設定重新載入失敗或尚未觸發。

**解決方案：**
```bash
# 檢查設定事件計數是否增加
curl -s http://<pod-ip>:8080/metrics | grep da_config_event

# 檢查 ConfigMap 的更新時間
kubectl get configmap threshold-exporter-config -n monitoring -o wide

# 查看設定重新載入日誌
kubectl logs <pod-name> -n monitoring | tail -50
```

---

## 相關文件

- [OpenAPI 3.0 Spec](./threshold-exporter-openapi.yaml) - 完整 API 規範
- [Threshold Exporter 架構](../architecture-and-design.md#2-核心設計config-driven-架構) - 詳細設計文件
- [Tenant 快速入門](../getting-started/for-tenants.md) - 租戶設定指南
- [Platform Engineers 快速入門](../getting-started/for-platform-engineers.md) - 部署和運維指南

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Threshold Exporter API Reference"](README.md) | ⭐⭐⭐ |
| ["da-tools CLI Reference"](../cli-reference.md) | ⭐⭐⭐ |
| ["性能分析與基準測試 (Performance Analysis & Benchmarks)"](../benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](../byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](../byo-prometheus-integration.md) | ⭐⭐ |
| ["da-tools Quick Reference"](../cheat-sheet.md) | ⭐⭐ |
| ["術語表"](../glossary.md) | ⭐⭐ |
| ["Grafana Dashboard 導覽"](../grafana-dashboards.md) | ⭐⭐ |
