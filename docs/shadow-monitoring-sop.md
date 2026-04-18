---
title: "Shadow Monitoring SRE SOP"
tags: [migration, shadow-monitoring, sop]
audience: [sre, platform-engineer]
version: v2.7.0
lang: zh
---
# Shadow Monitoring SRE SOP

> **Language / 語言：** **中文 (Current)** | [English](./shadow-monitoring-sop.en.md)

> **受眾**: SRE / Platform Engineer / DevOps
> **前提**: 已完成 `da-tools migrate` 轉換，新舊 Recording Rule 同時在 Prometheus 運行
> **工具**: `da-tools validate`（`--watch` 持續模式 / 單次模式）、`da-tools diagnose`（健康檢查）

---

## 1. 概述

Shadow Monitoring 是遷移流程的**並行驗證階段**：新規則（`custom_` prefix）與舊規則同時運行，透過 `da-tools validate` 持續比對數值輸出，確認行為等價後再切換。

`migrate_rule.py` 產出的 Alert Rule 自動帶有 `migration_status: shadow` label，Alertmanager 據此攔截通知以避免誤報。

本 SOP 涵蓋：前置準備 → 啟動 → 日常巡檢 → 異常處理 → 收斂判定 → 退出。

## 2. 前置準備

### 2.1 配置驗證

部署 shadow 規則前，先驗證整體配置正確性：

```bash
# 一站式配置驗證（YAML syntax + schema + routes + custom rules）
da-tools validate-config --config-dir /data/conf.d

# 或本地 Python 執行
python3 scripts/tools/ops/validate_config.py --config-dir components/threshold-exporter/config/conf.d
```

### 2.2 確認新規則已載入

```bash
# 自動化前置檢查（規則載入 + mapping + AM 攔截）
da-tools shadow-verify preflight \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090
```

> 手動替代：`curl -s http://localhost:9090/api/v1/rules | python3 -c "..."` + `ls -la migration_output/prefix-mapping.yaml`

### 2.3 Alertmanager Interception Configuration

`migrate_rule.py` 產出的規則已自帶 `migration_status: shadow` label，Alertmanager 必須攔截以避免誤報：

```yaml
# alertmanager.yml — 新增 route
route:
  routes:
    - matchers:
        - migration_status="shadow"
      receiver: "null"
      continue: false
receivers:
  - name: "null"
```

### 2.4 基線建立（選用）

對關鍵 tenant 執行負載觀測，建立遷移前的 baseline 數據作為比對參考：

```bash
python3 scripts/tools/ops/baseline_discovery.py --tenant db-a --duration 1800 --interval 30
```

產出包含 p50/p90/p95/p99 統計與閾值建議 CSV，可在 shadow 期間比對趨勢是否偏移。

## 3. 啟動 Shadow Monitoring

### 3.1 本地 port-forward（開發/小型環境）

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:v2.7.0 \
  validate --mapping /data/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 秒間隔 × 4032 輪 ≈ 14 天
```

> **已 clone 專案？** 也可直接執行 Python 腳本：
> ```bash
> python3 scripts/tools/ops/validate_migration.py \
>   --mapping migration_output/prefix-mapping.yaml \
>   --prometheus http://localhost:9090 \
>   --watch --interval 300 --rounds 4032
> ```

### 3.2 K8s Job（生產環境推薦）

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: shadow-monitor
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: validator
          image: ghcr.io/vencil/da-tools:v2.7.0
          env:
            - name: PROMETHEUS_URL
              value: http://prometheus.monitoring.svc.cluster.local:9090
          command: ["da-tools"]
          args:
            - validate
            - --mapping
            - /config/prefix-mapping.yaml
            - --watch
            - --interval
            - "300"
            - --rounds
            - "4032"
            - -o
            - /output
          volumeMounts:
            - name: config
              mountPath: /config
            - name: output
              mountPath: /output
      volumes:
        - name: config
          configMap:
            name: prefix-mapping
        - name: output
          emptyDir: {}
      restartPolicy: OnFailure
```

### 3.3 關鍵參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--interval` | 60s | 比對間隔（生產建議 300s） |
| `--rounds` | 10 | 比對輪數（14 天 ≈ 4032 輪 @300s） |
| `--tolerance` | 0.001 | 數值誤差容忍度（0.1%），rate-based metrics 可適度放寬 |
| `-o` | `validation_output` | 報告輸出目錄 |

## 4. 日常巡檢流程

初期（Day 1–3）建議每日至少檢查一次，確認無系統性 mismatch；穩定後（Week 2 起）可降低至隔日。

### 4.1 巡檢操作

```bash
# 一鍵巡檢（mismatch 統計 + tenant 覆蓋率 + 運營模式）
da-tools shadow-verify runtime \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090

# 若使用 K8s Job，查看 Job 日誌
kubectl logs job/shadow-monitor -n monitoring --tail=50
```

> `shadow-verify runtime` 會自動檢查 CSV 中的 mismatch 比例、tenant 覆蓋率，以及 Prometheus 中的 silent/maintenance 模式狀態。若 tenant 處於 `silent` 或 `maintenance` 模式，shadow 比對數值仍有效，但切換後 alert 不會觸發，直到恢復為 `normal`。

### 4.2 健康指標

| 指標 | 正常 | 需調查 |
|------|------|--------|
| mismatch 比例 | 0% | > 0% |
| missing 資料 | 0 | > 0（Recording Rule 名稱或 label 不匹配） |
| 連續 mismatch | 無 | 同一 tenant 連續 3+ 輪 |
| operational_mode | normal | silent / maintenance（切換前需恢復） |

## 5. 異常處理 Playbook

### 5.1 數值 Mismatch

**症狀**: `da-tools validate` 報告 `mismatch`，delta ≠ 0

```bash
# 單筆 query 比對
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.7.0 \
  validate --old "<old_query>" --new "<new_query>" \
  --prometheus http://localhost:9090

# 直接查詢 Prometheus 比對原始數據
curl -s "http://localhost:9090/api/v1/query?query=<old_query>" | python3 -m json.tool
curl -s "http://localhost:9090/api/v1/query?query=<new_query>" | python3 -m json.tool
```

**常見原因與修復**:

| 原因 | 特徵 | 修復 |
|------|------|------|
| 聚合方式不同 | 新值 = 舊值 × N | 確認 `max by` vs `sum by` vs `avg by` |
| label 不匹配 | `new_missing` / `old_missing` | 檢查 `by()` 子句的 label 名稱 |
| 評估時間窗口不同 | delta 極小但穩定 | 確認 `rate[5m]` / `[1m]` 等窗口一致 |
| 計數器重置 | 偶發大 delta | Rate 計算的正常現象，觀察是否收斂 |
| tolerance 過嚴 | delta 極小但穩定報 mismatch | 調高 `--tolerance`（如 `0.01` = 1%） |

### 5.2 Missing 資料

**症狀**: `old_missing` 或 `new_missing`

```bash
# 確認 metric 是否存在於 Prometheus
curl -s "http://localhost:9090/api/v1/label/__name__/values" | \
  python3 -c "import sys,json; names=json.load(sys.stdin)['data']; \
  [print(n) for n in names if 'custom_' in n or '<keyword>' in n]"
```

可能原因：新 Recording Rule 尚未被 evaluate（等待 1–2 個 evaluation interval）、`prefix-mapping.yaml` 拼寫錯誤、該 tenant metric 已被 `disable`（三態機制）。

### 5.3 da-tools validate Itself Fails

```bash
# 確認 Prometheus 可達
curl -s http://localhost:9090/-/healthy

# 確認 prefix-mapping.yaml 格式正確
python3 -c "import yaml; print(yaml.safe_load(open('migration_output/prefix-mapping.yaml')))"

# K8s Job 重啟
kubectl delete job shadow-monitor -n monitoring
kubectl apply -f shadow-monitor-job.yaml
```

## 6. 收斂判定標準

### 6.1 切換條件（全部滿足）

| 條件 | 驗證方式 |
|------|----------|
| 連續 7 天 0 mismatch | CSV 報告最近 7 天全部 `match` |
| 覆蓋業務高峰 + 低谷 | 確認報告時間戳涵蓋 peak hours |
| 覆蓋維護窗口 | 確認報告時間戳涵蓋週末/備份時段 |
| 所有 tenant 均參與比對 | CSV 中每個 tenant 至少有資料 |
| 運營模式為 normal | `da-tools diagnose` 確認無 silent/maintenance |

### 6.2 收斂確認

```bash
# 一鍵收斂驗證（7 天 zero-mismatch + readiness JSON + 運營模式）
da-tools shadow-verify convergence \
  --report-csv validation_output/validation-report.csv \
  --readiness-json validation_output/cutover-readiness.json \
  --prometheus http://localhost:9090

# 或全階段一次執行
da-tools shadow-verify all \
  --mapping migration_output/prefix-mapping.yaml \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090
```

## 7. 退出 Shadow Monitoring

### 7.1 自動化切換（推薦）

v1.10.0 提供 `da-tools cutover`，單一指令自動完成以下全部步驟：

```bash
# Step 1: Dry run — 預覽切換步驟，不做任何變更
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.7.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# 預期輸出：
#   [DRY RUN] Would delete job shadow-monitor in namespace monitoring
#   [DRY RUN] Would remove old recording rules for tenant db-a
#   [DRY RUN] Would remove migration_status:shadow label
#   [DRY RUN] Would remove Alertmanager shadow route for db-a
#   [DRY RUN] Would verify alerts via check-alert + diagnose

# Step 2: 執行切換
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.7.0 \
  cutover --readiness-json /data/cutover-readiness.json --tenant db-a

# Step 3: 批次切換多個 tenant（逐一執行）
for tenant in db-a db-b db-c; do
  docker run --rm --network=host \
    -v $(pwd)/validation_output:/data \
    -e PROMETHEUS_URL=http://localhost:9090 \
    ghcr.io/vencil/da-tools:v2.7.0 \
    cutover --readiness-json /data/cutover-readiness.json --tenant "$tenant"
done
```

**`--force` 的使用時機：**

| 情境 | 是否用 `--force` | 說明 |
|------|-----------------|------|
| 有 `cutover-readiness.json` | 不需要 | readiness JSON 已證明收斂 |
| 手動分析 CSV 確認收斂 | 用 `--force` | 繞過 readiness 檢查 |
| 測試環境快速驗證 | 用 `--force` | 測試用途不需嚴格收斂 |
| 生產環境未確認收斂 | **不要用** | 風險過高，先完成收斂確認 |

> **注意**：`--force` 跳過的是 readiness 檢查，不會跳過切換後的 `check-alert` / `diagnose` 健康驗證。如果切換後驗證失敗，工具會報錯但不會自動回退——需手動執行 §7.2 回退步驟。

### 7.1b 手動切換步驟

如不使用自動化工具，手動依序執行：

```bash
# 1. 停止 Shadow Monitor Job
kubectl delete job shadow-monitor -n monitoring

# 2. 移除舊 Recording Rule
#    (具體操作依環境：刪除 ConfigMap 或 Helm 移除)

# 3. 移除新規則的 migration_status: shadow label
#    更新 Alert Rule 定義，去掉 shadow label

# 4. 移除 Alertmanager 的 shadow 攔截 route

# 5. 驗證切換後 alert 正常觸發
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.7.0 \
  check-alert MariaDBHighConnections db-a

# 6. 租戶健康總檢
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.7.0 diagnose db-a
```

### 7.2 回退（如有問題）

```bash
# 1. 恢復舊 Recording Rule（如已保留原始 yaml）
kubectl apply -f old-recording-rules.yaml

# 2. 重新掛上 shadow label（讓新規則回到 shadow 狀態）

# 3. 重啟 Shadow Monitor
docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:v2.7.0 \
  validate --mapping /data/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
```

### 7.3 清理

```bash
# 移除遷移產物
rm -rf migration_output/
rm -rf validation_output/

# 批次下架不再需要的 custom_ prefix 規則
docker run --rm -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools:v2.7.0 \
  deprecate custom_mysql_connections custom_mysql_replication_lag --execute
```

## 8. 自動化工具

以下工具減少 Shadow Monitoring 的人工操作：

| 工具 | 用法 | 效果 |
|------|------|------|
| **Auto-convergence** ✅ | `validate --auto-detect-convergence --stability-window 5` | 追蹤每個 metric pair 的跨 round 狀態，所有 pairs 連續 N 輪 match 後自動產出 `cutover-readiness.json` 並停止 watch |
| **Batch health report** ✅ | `batch-diagnose`（da-tools CLI） | 切換後自動發現 tenants → 並行 `diagnose` → health score + remediation steps |
| **Threshold backtest** ✅ | `backtest --git-diff --prometheus <url>` | PR 修改 threshold 時回測 7 天歷史數據，CI 自動產出風險評估 |
| **Shadow Dashboard** ✅ | Grafana 掛載 `shadow-monitoring-dashboard.json`（見下方 §8.1） | 即時顯示 shadow rule 數量、per-tenant 狀態、old/new metric 對比趨勢、delta 收斂圖 |
| **One-command cutover** ✅ | `da-tools cutover --readiness-json <path> --tenant <t>`（見 §7.1） | 單一指令完成切換全流程。支援 `--dry-run` 預覽、`--force` 跳過 readiness 檢查 |
| **Shadow verify** ✅ | `da-tools shadow-verify <preflight\|runtime\|convergence\|all>` | 三階段自動驗證：前置檢查 + 巡檢 + 收斂判定，替代手動 curl + awk 操作 |

### 8.1 Shadow Dashboard 部署與使用

**Dashboard 檔案位置：** `k8s/03-monitoring/shadow-monitoring-dashboard.json`

**匯入方式：**

```bash
# 方式 A：一鍵匯入（自動建立 ConfigMap + 標記 sidecar label）
da-tools grafana-import \
  --dashboard k8s/03-monitoring/shadow-monitoring-dashboard.json \
  --namespace monitoring

# 方式 B：Grafana UI 手動匯入
# 開啟 Grafana → Dashboards → Import → 上傳 JSON → 選擇 Prometheus data source
```

**5 個 Panel 解讀：**

| Panel | 看什麼 | 正常狀態 | 需關注 |
|-------|--------|---------|--------|
| **Shadow Rules Active** | 目前活躍的 shadow rule 數量 | 遷移中 > 0；切換後 = 0 | 切換後仍 > 0 表示有殘留 |
| **Per-Tenant Status** | 每個 tenant 的 shadow 狀態 | 所有 tenant 列為 `active` 或 `converged` | 某 tenant `stale`（長時間無更新） |
| **Old vs New Comparison** | old/new metric 數值疊圖 | 兩線重合 | 兩線持續偏離（需調查原因） |
| **Delta Trend** | old-new 差值趨勢 | 趨近 0 並穩定 | 持續非零或震盪 |
| **Inhibited Shadow Alerts** | 被 Alertmanager 攔截的 shadow alert 數量 | 低且穩定 | 突然飆升（新規則可能有誤報） |

> **Panel 3/4 配置提示**：「Old vs New Comparison」和「Delta Trend」需要手動填入 Template Variables `$old_metric` 和 `$new_metric`（Prometheus metric 名稱）。其餘 Panel 零配置即可使用。

## 9. 快速參考卡

```
┌─────────────────────────────────────────────────────────────┐
│ Shadow Monitoring 生命週期                                    │
│                                                               │
│  validate-config → 配置驗證                                   │
│       ↓                                                       │
│  da-tools migrate → 新規則部署 → Alertmanager 攔截            │
│       ↓                                                       │
│  da-tools validate --watch --auto-detect-convergence          │
│       ↓                                                       │
│  日常巡檢 + da-tools diagnose / batch-diagnose (1-2 週)       │
│       ↓                                                       │
│  收斂判定 (自動: cutover-readiness.json / 手動: 7天0 mismatch)│
│       ↓                                                       │
│  da-tools cutover --readiness-json ... --tenant ... (§7.1)   │
│       ↓                                                       │
│  清理：da-tools deprecate (支援批次) / rm 產物                │
└─────────────────────────────────────────────────────────────┘
```

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Shadow Monitoring SRE SOP"] | ⭐⭐⭐ |
| ["AST 遷移引擎架構"](./migration-engine.md) | ⭐⭐ |
| ["場景：Shadow Monitoring 全自動切換工作流"](scenarios/shadow-monitoring-cutover.md) | ⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.md) | ⭐⭐ |
| ["性能分析與基準測試 (Performance Analysis & Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](integration/byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](integration/byo-prometheus-integration.md) | ⭐⭐ |
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐ |
