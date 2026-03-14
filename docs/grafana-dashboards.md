---
title: "Grafana Dashboard 導覽"
tags: [monitoring, grafana, dashboard, operations]
audience: [platform-engineer, sre, devops]
version: v2.0.0-preview.3
lang: zh
---

# Grafana Dashboard 導覽

> **v2.0.0-preview** | 適用對象：Platform Engineer、SRE、DevOps
>
> 相關文件：[Architecture](./architecture-and-design.md) · [Troubleshooting](./troubleshooting.md) · [Shadow Monitoring SOP](./shadow-monitoring-sop.md)

本文檔介紹 Dynamic Alerting 平台提供的兩份 Grafana Dashboard，說明如何部署、使用和排查問題。

## 概覽

Dynamic Alerting 提供兩份運維導向的 Dashboard：

| 名稱 | 用途 | 受眾 |
|------|------|------|
| **Dynamic Alerting — Platform Overview** | 平台整體健康度、Tenant 狀態、Threshold 分佈 | Platform Engineer / NOC |
| **Shadow Monitoring Progress** | Migration 期間舊新 Rule 收斂進度 | SRE / Migration Lead |

---

## Dashboard 1: Dynamic Alerting — Platform Overview

### 部署

#### 方法 A：Grafana UI 匯入

1. Grafana 左側欄 → **Dashboards** → **New** → **Import**
2. 上傳 JSON 檔：`k8s/03-monitoring/dynamic-alerting-overview.json`
3. 選擇 Prometheus datasource，點擊 **Import**

#### 方法 B：ConfigMap Sidecar 自動部署

```bash
# 使用 grafana-import 工具自動建立 ConfigMap + 標記 label
da-tools grafana-import \
  --dashboard k8s/03-monitoring/dynamic-alerting-overview.json \
  --name grafana-dashboard-overview --namespace monitoring
```

Sidecar 會自動偵測 `grafana_dashboard=1` label，將 ConfigMap 掛載至 Grafana provisioning 目錄。

### Panel 快速參考（Stat Panels）

| # | Panel | PromQL | 正常 | 異常排查 |
|---|-------|--------|------|----------|
| 1 | Active Tenants | `count(count by(tenant) (user_threshold))` | 非零 | 突降→檢查配置或 reload |
| 2 | Total Thresholds | `count(user_threshold)` | 穩定非零 | 下降>10%→檢查 exporter/scrape |
| 3 | Warning/Critical | `count(user_threshold{severity="warning\|critical"})` | Critical 20-40% | Critical>50%→檢查 Rule Pack |
| 4 | Silent Mode | `count(user_silent_mode) or vector(0)` | 0 或計畫維護 | 非預期>0→檢查 `_silent_mode` |
| 5 | Maintenance Mode | `count(user_state_filter{filter="maintenance"})` | 0 或計畫維護 | 非預期>0→檢查 `_state_maintenance` |
| 6 | Dedup Disabled | `count(user_severity_dedup{mode="disable"})` | 0 或小部分 | 非預期>0→檢查 `_severity_dedup` |

---

#### 7-8. Tenant State Overview (Table) + Thresholds by Component (BarChart)

- **Tenant State Overview**: 按 Tenant 聚合顯示 Thresholds 數量、Silent/Maintenance/Dedup 運營狀態。點擊列排序可快速定位異常。
- **Thresholds by Component**: `count by(component) (user_threshold)` — 按 DB 類型分佈 Threshold 數量，反映基礎設施構成。

---

#### 9-10. Thresholds per Tenant (BarChart) + Active State Filters (Table)

- **Thresholds per Tenant**: `count by(tenant) (user_threshold)` — 各 Tenant 的 Threshold 序列數。接近 500 上限時變紅（Cardinality Guard）。詳見 [Architecture §2.3](./architecture-and-design.md#cardinality-guard)。
- **Active State Filters**: `user_state_filter` — 目前啟用的 State Filter 詳細列表（maintenance、crashloop 等），通常為空或計畫維護。

---

#### 11. Threshold Changes (1h) (TimeSeries)

`sum by(tenant) (changes(user_threshold[10m]))` — 過去 1 小時各 Tenant 的 Threshold 變化次數。Config push 後出現尖峰屬正常；頻繁尖峰（每幾分鐘）→ ConfigMap 被頻繁編輯。

---

### 使用技巧

1. **設定時間範圍：** 預設為 `now-1h`。點擊右上角時間選擇器可切換至 6h、24h、7d 等。

2. **告警相關異常：** 若 Panel 資料為空或顯示 `No Data`，確認：
   - Prometheus datasource 連接正常（Grafana → Configuration → Data sources）
   - `user_threshold`、`user_silent_mode` 等指標在 Prometheus 中存在（在 Prometheus UI 查詢 `user_threshold` 確認）

3. **匯出資料：** 若需統計報表，可點擊 Panel 右上角 → Download → CSV。

---

## Dashboard 2: Shadow Monitoring Progress

### 部署

#### 方法 A：Grafana UI 匯入

1. Grafana 左側欄 → **Dashboards** → **New** → **Import**
2. 上傳 JSON 檔：`k8s/03-monitoring/shadow-monitoring-dashboard.json`
3. 選擇 Prometheus datasource，點擊 **Import**

#### 方法 B：ConfigMap Sidecar 自動部署

```bash
da-tools grafana-import \
  --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
  --name grafana-dashboard-shadow --namespace monitoring
```

### 面板詳解

此 Dashboard 專用於 Shadow Monitoring migration 期間，追蹤舊新 Recording Rule 的收斂進度。完成 cutover 後可安全移除。

#### 1. Shadow Rules Active (Stat)

**PromQL:** `count({migration_status="shadow"}) or vector(0)`

**含義：** 目前標記為 `migration_status=shadow` 的 Recording Rule 指標序列數。

**正常狀態：** Migration 前期非零（舊 rule 運行），shadow 期間非零（並行），Cutover 後為 0。

**異常時：** Cutover 後仍 > 0 → 檢查舊 rule 未刪除；預期 shadow rule 但為 0 → 驗證 `migration_status: shadow` 標籤。詳見 [SOP](./shadow-monitoring-sop.md)。

**相關文件：** 參見 [Shadow Monitoring SOP](./shadow-monitoring-sop.md)。

---

#### 2. Per-Tenant Shadow Status (Table)

**PromQL:** `count by(tenant) ({migration_status="shadow"})` (instant query)

**含義：** 各 Tenant 的 shadow rule 數量。用於驗證所有 Tenant 都已部署 shadow rule。

**正常狀態：** Migration 期間各 Tenant 應 > 0，Cutover 前確認全部非空。

**異常時：** Tenant 為 0 → shadow rule 未部署；預期 Tenant 缺失 → 檢查配置的 custom rule。

---

#### 3. Inhibited Shadow Alerts (Stat)

**PromQL (組合):**
- `count(ALERTS{migration_status="shadow", alertstate="pending"}) or vector(0)` → Pending 狀態
- `count(ALERTS{migration_status="shadow", alertstate="firing"}) or vector(0)` → Firing 狀態

**含義：** Shadow alert 目前被 Alertmanager inhibit 規則抑制的狀態。Shadow 期間應有 inhibit 規則專門抑制舊 rule 的告警，防止重複通知。

**正常狀態：** Shadow 期間 > 0（舊 alert 被抑制）。

**異常時：** Shadow 期間為 0 → 檢查 inhibit 規則配置；Pending > Firing → alert 等待 evaluation cycles，通常無礙。

**相關配置：** 參見 [Shadow Monitoring Cutover](./scenarios/shadow-monitoring-cutover.md)。

---

#### 4. Old vs New Metric Comparison (TimeSeries)

**PromQL:**
- `$old_metric{tenant=~"$tenant"}`
- `$new_metric{tenant=~"$tenant"}`

**含義：** 並排顯示舊舊 recording rule 和新 recording rule 的數值時序。用於視覺化驗證兩邊數據是否收斂。

**使用方法：**

1. 在頂部 Template Variables 設定：
   - **Tenant**：選擇要檢查的 Tenant（支援多選）
   - **Old Metric**：輸入舊 metric 名稱，例如 `mysql_global_status_threads_connected`
   - **New Metric**：輸入新 metric 名稱，例如 `tenant:custom_mysql_global_status_threads_connected:max`

2. 查看圖表：
   - 若兩條線重合，表示數據完全一致（綠燈，可 cutover）
   - 若線條有偏差但趨勢一致，可能是採樣率或聚合函數差異（需評估是否可接受）
   - 若線條完全分離或反向，表示遷移邏輯有問題（紅燈，需修復）

**正常狀態：** 線條重合或趨勢一致，差異 < 5%。

**異常時：** 線條分離或反向 → 檢查新 rule PromQL；線條突然中斷 → exporter 或 scrape job 失敗。詳見通用診斷清單。

---

#### 5. Delta Trend |old - new| (TimeSeries)

**PromQL:** `abs($old_metric{tenant=~"$tenant"} - $new_metric{tenant=~"$tenant"})`

**含義：** 舊新 metric 的絕對差值，應隨時間趨向 0。色彩編碼：
- 綠 (delta < 0.01)：誤差極小，基本收斂
- 黃 (0.01 ≤ delta < 0.1)：可接受範圍內
- 紅 (delta ≥ 0.1)：較大差異，需評估

**正常狀態：** 平穩下降趨向 0（綠線）。

**異常時：** 持續紅色（delta > 0.1）→ 新 rule PromQL 有誤；突然上升 → exporter 數據質量差。綠色 ≥ 24h 可 cutover；紅色持續需修復。詳見 [Cutover 決策](./scenarios/shadow-monitoring-cutover.md)。

**相關文件：** 參見 [Shadow Monitoring Cutover 決策準則](./scenarios/shadow-monitoring-cutover.md)。

---

### Template Variables 配置

若需修改變數定義（例如新增 Tenant 或變更 metric 名稱），點擊 Dashboard 左上角 ⚙️ (Settings) → **Variables**。

| 變數 | 類型 | 用途 |
|------|------|------|
| `tenant` | Query (multi-select) | 從 `{migration_status="shadow"}` 標籤動態提取 Tenant 列表 |
| `old_metric` | Textbox | 手動輸入舊 metric 名稱 |
| `new_metric` | Textbox | 手動輸入新 metric 名稱 |
| `DS_PROMETHEUS` | Datasource (Prometheus) | Prometheus 資料來源 |

---

### 使用技巧

1. **多租戶對比：** 在 Tenant 變數選擇多個值，可同時看 multiple tenant 的收斂進度。

2. **時間範圍：** 預設為 `now-7d`（過去 7 天），可調整為更長範圍以檢視整個 shadow 期間的趨勢。

3. **刷新頻率：** 右上角可設定自動刷新（預設不刷新）。推薦在 shadow 期間設為 30s 或 1m，便於即時監控。

4. **儲存檢查點：** 若需記錄 cutover 前夕的收斂狀態，可截圖或點擊右上角 Dashboard menu → **Share** → 複製 URL。

---

## 常見問題與排查

### 通用診斷步驟

```bash
# 1. Prometheus 連接
curl -sf http://localhost:9090/-/healthy

# 2. 指標存在性
curl -s 'http://localhost:9090/api/v1/query?query=user_threshold' | jq '.data.result | length'

# 3. Grafana datasource（UI: Configuration → Data sources → Prometheus → Test）
```

### 常見症狀

| 症狀 | 排查方向 |
|------|----------|
| Panel 顯示 "No Data" | 確認 Prometheus datasource 連接 + 指標存在 + 時間範圍覆蓋 |
| Tenant 資料突然消失 | 檢查 tenant config + reload 日誌 + scrape 狀態 |
| Cardinality 告警 (>500) | `kubectl logs` 搜尋 truncate → 停用不必要 custom rule |
| Shadow 兩條線無法重合 | 比較舊新 PromQL 邏輯 + label 結構 + 聚合函數差異 |
| Dashboard 刷新遲鈍 | 檢查 Prometheus 查詢性能 (`-w '%{time_total}'`) + Grafana 日誌 |

---

## 整合與擴展

### 與其他 Dashboard 的連結

- **Dynamic Alerting Overview** 頁面的左上角有連結指向關鍵文件（Troubleshooting、Architecture）
- **Shadow Monitoring Dashboard** 的 Panel 標題包含文件超連結（點擊 Panel 標題可跳轉相關 SOP）

---

## 維護與生命週期

### 定期檢查

- **每週：** 檢查 Cardinality (Panel 9) 是否接近上限
- **每月：** 確認 Active Tenants (Panel 1) 與預期相符
- **計畫維護期間：** 監控 Silent / Maintenance Mode Panels 確保靜音生效

### 升級 Dashboard

當 Platform 版本升級時，可能會新增或修改 Panel。比較新舊 JSON 檔案後更新 ConfigMap：

```bash
diff -u k8s/03-monitoring/dynamic-alerting-overview.json.old \
          k8s/03-monitoring/dynamic-alerting-overview.json

kubectl create configmap grafana-dashboard-overview \
  --from-file=dynamic-alerting-overview.json=k8s/03-monitoring/dynamic-alerting-overview.json \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

### 移除 Shadow Dashboard

Cutover 完成後（所有 Tenant 的舊 rule 已移除），可刪除 Shadow Monitoring Dashboard：

```bash
# 在 Grafana UI：Dashboards → Shadow Monitoring Progress → 右上角 menu → Delete

# 或透過 ConfigMap：
kubectl delete configmap grafana-dashboard-shadow -n monitoring
```

---

## API 端點健康監控

除了上述 Dashboard 之外，建議搭配 **Blackbox Exporter** 監控 threshold-exporter 的 API 端點可用性，確保告警管線的基礎健康。

### 監控目標

| 端點 | 用途 | 預期回應 | 建議頻率 |
|------|------|---------|---------|
| `/health` | Liveness 探針 | HTTP 200 | 15s |
| `/ready` | Readiness 探針（含 config 載入狀態） | HTTP 200 | 15s |
| `/metrics` | Prometheus 指標端點 | HTTP 200 + 含 `user_threshold` | 30s |
| `/api/v1/config` | 執行時配置 API | HTTP 200 + JSON | 60s |

### Blackbox Exporter 配置

```yaml
# blackbox.yml
modules:
  http_threshold_exporter:
    prober: http
    timeout: 5s
    http:
      valid_http_versions: ["HTTP/1.1", "HTTP/2.0"]
      valid_status_codes: [200]
      method: GET
      fail_if_body_not_matches_regexp:
        - "user_threshold"   # /metrics 端點應包含此指標
```

```yaml
# prometheus.yml — scrape_configs 片段
- job_name: "blackbox-threshold-exporter"
  metrics_path: /probe
  params:
    module: [http_threshold_exporter]
  static_configs:
    - targets:
        - "http://threshold-exporter:8080/health"
        - "http://threshold-exporter:8080/ready"
        - "http://threshold-exporter:8080/metrics"
  relabel_configs:
    - source_labels: [__address__]
      target_label: __param_target
    - source_labels: [__param_target]
      target_label: instance
    - target_label: __address__
      replacement: blackbox-exporter:9115
```

### 建議 Alert Rule

```yaml
# rule-packs/platform-health.yml（可選擴充）
- alert: ThresholdExporterEndpointDown
  expr: probe_success{job="blackbox-threshold-exporter"} == 0
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "threshold-exporter 端點 {{ $labels.instance }} 無回應"
    description: "Blackbox probe 連續 2 分鐘失敗，可能影響告警管線。"
```

### Grafana Panel 建議

在 Platform Overview Dashboard 新增一行（Row: API Health），包含以下 panel：

| Panel | 查詢 | 視覺化 |
|-------|------|--------|
| Endpoint Status | `probe_success{job="blackbox-threshold-exporter"}` | Stat（綠/紅） |
| Response Latency | `probe_duration_seconds{job="blackbox-threshold-exporter"}` | Time series |
| SSL Cert Expiry | `probe_ssl_earliest_cert_expiry - time()` | Stat（天數） |
| Uptime (24h) | `avg_over_time(probe_success{...}[24h]) * 100` | Gauge（百分比） |

---

## 相關資源

| 資源 | 用途 |
|------|------|
| [Architecture & Design](./architecture-and-design.md) | Platform 整體設計與核心概念 |
| [Troubleshooting](./troubleshooting.md) | 常見問題與排查方法 |
| [Shadow Monitoring SOP](./shadow-monitoring-sop.md) | Shadow Monitoring 完整操作指南 |
| [Shadow Monitoring Cutover](./scenarios/shadow-monitoring-cutover.md) | Cutover 決策準則與自動化工具 |
| [API Endpoints](./api/README.md) | threshold-exporter API 端點參考 |
| [Prometheus Targets](http://localhost:9090/targets) | 實時 scrape 狀態監控 |
| [Prometheus Rules](http://localhost:9090/rules) | Recording rule 和 alert rule 列表 |

---

**版本：** | **最後更新：** 2026-03-12
