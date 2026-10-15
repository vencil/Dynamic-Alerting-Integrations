---
title: "Threshold Exporter API Reference"
tags: [api, reference, threshold-exporter]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---

# Threshold Exporter API 參考

> **Language / 語言：** **中文 (Current)** | [English](./README.en.md)

Threshold Exporter 是 Multi-Tenant Dynamic Alerting 平台的核心元件，負責將租戶配置轉換為 Prometheus 指標、狀態過濾器和嚴重度去重標誌。本頁是各 GET 端點給人看的說明；給機器讀的契約在 [OpenAPI spec](./threshold-exporter-openapi.yaml)。

## 服務規格

| 項目 | 值 |
|------|-----|
| 監聽埠 | 8080 |
| 讀取逾時 | 5 秒 |
| 讀取標頭逾時 | 3 秒 |
| 寫入逾時 | 10 秒 |
| 空閒逾時 | 30 秒 |
| 最大標頭大小 | 8192 字節 |
| 指標格式 | Prometheus 文字格式 `text/plain; version=0.0.4`（沒有開 OpenMetrics） |

這張表有測試比對原始碼（`service_spec_readme_parity_test.go`）。

## API Overview

端點清單只列在一處：[threshold-exporter README §3.1 Endpoints](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/components/threshold-exporter/README.md#31-endpoints)。那張表有測試比對原始碼（`ops_readme_parity_test.go`），這裡不再複製；以下各節是 GET 端點的細節。

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

給 Kubernetes `livenessProbe` 用。行程起來就回應，與設定有沒有載入無關。

```bash
curl -s http://localhost:8080/health
```

| 情況 | 狀態碼 | 內文 |
|------|--------|------|
| 行程在跑 | 200 | `ok` |

---

## 3. GET /ready - 就緒探針

給 Kubernetes `readinessProbe` 用。第一次成功載入設定之前回 503；之後一律回 200，之後的 reload 失敗不會讓它變回 503（exporter 繼續服務上一版設定）。

```bash
curl -s http://localhost:8080/ready
```

| 情況 | 狀態碼 | 內文 |
|------|--------|------|
| 已載入過設定 | 200 | `ready` |
| 還沒載入過設定 | 503 | `config not loaded` |

§2、§3 這兩張表有測試比對 handler（`api_readme_examples_parity_test.go`）；兩端點的 Content-Type 見 [OpenAPI spec](./threshold-exporter-openapi.yaml)。Helm chart 的探針設定（liveness、readiness、startup）在 [`values.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/threshold-exporter/values.yaml)，這裡不再複製。

---

## 4. GET /api/v1/config - 設定除錯頁

給人看的純文字頁：目前載入的預設值、各租戶的值、解析後的閾值。格式不是契約，程式要讀請用 §5。

```bash
curl -s http://localhost:8080/api/v1/config
```

加 `?at=<RFC 3339 時間>` 可以看排程式覆寫（時間窗）在那個時間點解析成什麼值，不用等到那個時間。

### 範例

conf.d 裡有這兩個檔：

```yaml
# _defaults.yaml
defaults:
  container_cpu: 80
  mysql_connections: 80
```

```yaml
# tenant-a.yaml
tenants:
  tenant-a:
    container_cpu:
      default: "85"
      overrides:
        - window: "01:00-09:00"
          value: "95"
    mysql_connections: "70"
```

`curl -s "http://localhost:8080/api/v1/config?at=2026-03-12T03:30:00Z"` 的輸出（03:30 落在時間窗內，所以 `container_cpu` 解析成 95；各段內的行序不固定）：

```text
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

這個範例有測試實跑：把上面兩個 yaml 放進空目錄載入，再比對輸出（`api_readme_examples_parity_test.go`）。

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
| `parse_failed` | 這一版掃描中位元組無法 parse 的檔案（相對路徑、已排序）：這些位元組**沒有**被採用，該檔宣告的租戶也不在這一版裡，除非另一個可解析的檔也宣告它（任何載入路徑皆同，[#1980](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1980)）；一律是陣列。single-file 模式恆為空（無法 parse 的單檔不會被安裝） |

⚠️ `da_config_parse_failure_total` 回答的是另一個問題：它在每次掃描到壞檔時都會累加，與有沒有安裝新版無關，所以不能拿來判斷「目前這一版有沒有排除某個檔」。

---

## Prometheus 抓取設定

- Helm chart 在 `rules.mode: operator` 時會建立 ServiceMonitor（[`servicemonitor.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/helm/threshold-exporter/templates/servicemonitor.yaml)）。
- 自管的 Prometheus 照 [BYOP 整合指南](../integration/byo-prometheus-integration.md)「步驟 2：抓取 threshold-exporter」。

---

## 故障排查

### `/ready` 一直回 503

exporter 還沒成功載入過設定。看 log 裡的錯誤，並確認 conf.d 有掛進去（Helm chart 掛在 `/etc/threshold-exporter/conf.d`）：

```bash
kubectl logs <pod-name> -n monitoring
kubectl exec <pod-name> -n monitoring -- ls -la /etc/threshold-exporter/conf.d/
```

### `/metrics` 少了預期的指標

先看設定裡有沒有那個租戶與 key：

```bash
curl -s http://<pod-ip>:8080/api/v1/config | grep -A 20 "^Tenants"
```

### 改了設定，指標沒變

先看 exporter 目前服務的是哪一版，以及有沒有檔案因為 parse 失敗被排除（欄位意義見 §5）：

```bash
curl -s http://<pod-ip>:8080/api/v1/config/identity
```

`last_reload` 沒變表示還沒有新版設定裝上，可能是：ConfigMap 的變更還沒被 kubelet 同步進 Pod、還沒到下一次輪詢（`-reload-interval`），或這次載入失敗（錯誤在 log 裡）。`parse_failed` 列出的檔案沒有被採用，原因在 log 裡。

---

## 相關文件

- [OpenAPI 3.0 Spec](./threshold-exporter-openapi.yaml) - 給機器讀：各 GET 端點的 path、status code、media type 與 `/api/v1/config/identity` 的 JSON schema（有測試比對 handler：`openapi_spec_parity_test.go`）。說明與指標清單在本頁與上面連結的 README
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
