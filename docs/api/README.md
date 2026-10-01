---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# Threshold Exporter API 參考

> **Language / 語言：** **中文 (Current)** | [English](./README.en.md)

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

## API Overview

```
GET /metrics          → Prometheus 指標匯出 (200 OK)
GET /health           → 存活探針 (200 OK)
GET /ready            → 就緒探針 (200 OK / 503 Service Unavailable)
GET /api/v1/config    → 設定狀態除錯端點 (200 OK)
GET /api/v1/config/identity → 目前服務的設定之識別（給機器讀的 JSON 契約，200 OK）
```

---

## 1. GET /metrics - Prometheus Metrics Export

### 說明

匯出所有租戶的 Prometheus 指標，包括閾值狀態、靜音模式、嚴重度去重旗標和租戶中繼資料。此端點是 Prometheus scrape_config 的目標。

### 請求

```bash
curl -s http://localhost:8080/metrics | head -50
```

### 回應

**狀態碼**: 200 OK  
**Content-Type**: `text/plain; version=0.0.4`（Prometheus 文字格式；沒有開 OpenMetrics，送 `Accept: application/openmetrics-text` 也一樣）

### 指標清單

每個 metric 的名稱、型別與 label 只列在一處：[threshold-exporter README §3.3 Metrics](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#33-metrics)。那張表有測試逐條比對原始碼（`metrics_readme_parity_test.go`），這裡不再複製。

要看自己環境實際輸出什麼，直接抓：

```bash
curl -s http://localhost:8080/metrics | grep -E '^(user_|tenant_|da_)'
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
    image: ghcr.io/vencil/threshold-exporter:v2.9.0
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
      mountPath: /etc/threshold-exporter/conf.d
      readOnly: true
  
  volumes:
  - name: config
    configMap:
      name: threshold-config
```

---

## 4. GET /api/v1/config - Configuration State Debug Endpoint

### 說明

Debug 端點，暴露目前加載的設定狀態以純文字格式。支援 RFC3339 時間戳記查詢參數，用於檢查排程式覆寫在特定時間點的狀態。

### 請求

#### 查詢目前設定

```bash
curl -s http://localhost:8080/api/v1/config | head -50
```

#### 查詢特定時間點的設定

```bash
# 查詢 2026-03-12T03:30:00Z 時的排程式覆寫狀態（即下方回應範例）
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T03:30:00Z" | head -50
```

### 查詢參數

| 參數 | 型別 | 說明 | 範例 |
|------|------|------|------|
| `at` | string (RFC3339) | 檢查設定狀態的時間點。省略時返回目前狀態。 | `2026-03-12T14:30:00Z` |

### 回應

**狀態碼**: 200 OK  
**Content-Type**: `text/plain`

### 回應範例

以下是輸出（`handlers.go` 的 `configViewHandler`；一個 `_defaults.yaml` 加一個租戶檔、帶 `?at=` 時）。

```
Config loaded: true
Last reload:   2026-03-12T10:05:30Z
Config mode:   directory
Resolve at:    2026-03-12T03:30:00Z (overridden)

Defaults (2 metrics):
  container_cpu: 80
  mysql_connections: 80

Tenants (1):
  tenant-a:
    container_cpu: 85 (+ 1 time overrides)
    mysql_connections: 70

Resolved thresholds:
  tenant=tenant-a metric=cpu value=95 severity=warning component=container
  tenant=tenant-a metric=connections value=70 severity=warning component=mysql
```

### 常見用途

#### 1. 驗證租戶設定已正確載入

```bash
curl -s http://localhost:8080/api/v1/config | grep -A 20 "^Tenants"
```

#### 2. 檢查排程式覆寫在特定時間點的狀態

```bash
curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T10:30:00Z" | grep -A 50 "^Resolved thresholds"
```

#### 3. 確認最後重新載入時間與載入模式

```bash
curl -s http://localhost:8080/api/v1/config | head -3
```

---

## 5. GET /api/v1/config/identity - Config Identity（機器契約）

### 說明

回報這個 exporter **目前服務的是哪一版設定位元組**，以及那一版裡哪些檔案因無法 parse 而被排除。這是**給機器讀的契約**，以 `schema` 欄位版本化（目前為 `1`；欄位改變意義或移除時才升版，新增欄位不升版）；人讀的除錯頁仍是上方的 `/api/v1/config`。`patch-config` 用它做寫後驗收（見 [cli-reference](../cli-reference.md) §patch-config）。所有欄位在同一次安裝的同一個鎖窗內取值，不會混到兩次 reload 的狀態。

```bash
curl -s http://localhost:8080/api/v1/config/identity
```

### 回應

**狀態碼**: 200 OK（尚未載入任何設定時也是 200，`loaded: false`；是否就緒請看 `/ready`）  
**Content-Type**: `application/json`；只接受 `GET`／`HEAD`（其他方法 405）

```json
{"schema":1,"loaded":true,"mode":"directory","last_reload":"2026-09-27T01:02:03.456789Z","config_hash":"<64 位 hex>","parse_failed":["tenant-b.yaml"]}
```

| 欄位 | 說明 |
|------|------|
| `schema` | 契約版本，目前 `1` |
| `loaded` | 是否已安裝過設定 |
| `mode` | `directory`（`-config-dir`）或 `single-file`（`-config`） |
| `last_reload` | 安裝時間，UTC、RFC 3339 含奈秒；未載入時為 `""` |
| `config_hash` | directory 模式：conf.d 裡 exporter 會讀的檔案（非 `.` 開頭、副檔名 `.yaml`／`.yml` 不分大小寫），依相對路徑排序後各自 SHA-256，hex 串接後再 SHA-256；single-file 模式：該檔位元組的 SHA-256 |
| `parse_failed` | 這一版掃描中位元組無法 parse 的檔案（相對路徑、已排序）：這些位元組**沒有**被採用，但在 flat 模式下該檔的租戶可能仍以上一版的值服務；一律是陣列。single-file 模式恆為空（無法 parse 的單檔不會被安裝） |

⚠️ `da_config_parse_failure_total` 回答的是另一個問題：它在每次掃描到壞檔時都會累加，與有沒有安裝新版無關，所以不能拿來判斷「目前這一版有沒有排除某個檔」。

---

## Prometheus Scrape Configuration

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
kubectl get configmap threshold-config -n monitoring

# 檢查 ConfigMap 內容
kubectl get configmap threshold-config -n monitoring -o yaml

# 驗證掛載路徑
kubectl exec <pod-name> -n monitoring -- ls -la /etc/threshold-exporter/conf.d/
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
# 檢查 ConfigMap 的更新時間
kubectl get configmap threshold-config -n monitoring -o wide

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
| ["性能基準 (Performance Benchmarks)"](../benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](../integration/byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](../integration/byo-prometheus-integration.md) | ⭐⭐ |
| ["da-tools Quick Reference"](../cheat-sheet.md) | ⭐⭐ |
| ["術語表"](../glossary.md) | ⭐⭐ |
| ["Grafana Dashboard 導覽"](../grafana-dashboards.md) | ⭐⭐ |
