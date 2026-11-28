---
title: "Shadow Monitoring SRE SOP"
tags: [migration, shadow-monitoring, sop]
audience: [sre, platform-engineer]
version: v2.9.0
lang: zh
---
# Shadow Monitoring SRE SOP

> **Language / 語言：** **中文 (Current)** | [English](./shadow-monitoring-sop.en.md)

> **受眾**: SRE / Platform Engineer / DevOps
> **前提**: 已完成 `da-tools migrate` 轉換，legacy 規則與 migrated 規則同時在 Prometheus 運行
> **注意**: 現在的 `da-tools migrate` 把規則原樣遷移（告警帶 `da_migrated: "true"`），遷移對照是 `.migration/mapping/<tenant>.yaml`；`da-tools validate` 讀這份對照、比對兩邊響的告警集合（[#4227](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4227)）。數值比對（`--old`／`--new`、`prefix-mapping.yaml`、`--tolerance`）已移除。§7 切換步驟裡的 `migration_status: shadow` 屬於舊版 migrate。⚠️ v2.9.0 映像還是數值比對的舊版 validate，本 SOP 的 validate 指令在那顆映像上不適用。 <!-- image-caveat: v2.9.0 -->
> **工具**: `da-tools validate`（`--watch` 持續模式 / 單次模式）、`da-tools diagnose`（健康檢查）

---

## 1. 概述

Shadow Monitoring 是遷移流程的**並行驗證階段**：migrated 規則（`migrate-rule` 原樣遷移、注入租戶的規則）與 legacy 規則同時運行，透過 `da-tools validate` 持續比對兩邊各自響的告警集合，確認行為一致後再切換。

`migrate_rule.py` 產出的告警帶 `da_migrated: "true"` label；`generate-routes` 產生的路由把它們送到只有名字、不通知任何人的 receiver `migrated-alerts-shadow`，影子期同一件事不會 page 兩次，告警仍留在 Alertmanager UI 與 `ALERTS` 序列供比對。

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

> 直接跑 `validate_config.py` 需要 da-guard：先 `make da-guard-build` 並設 `DA_GUARD_BINARY=.build/da-guard`（da-tools 映像已內建）；詳見 [validate-config 的前置需求](cli-reference.md#validate-config)。

### 2.2 確認新規則已載入

```bash
# 自動化前置檢查（mapping + migrated 規則載入 + AM shadow receiver）
da-tools shadow-verify preflight \
  --mapping migration_output/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090
```

> `preflight` 讀 `migrate-rule` 寫的對照：給 prefix-mapping、不是 `migrate-rule` 寫的形狀、或沒有任何要比對的告警，`mapping_file` 一項 FAIL、結束碼 2。接著查對照裡每條告警是否已以該租戶的 migrated 規則（`da_migrated="true"`、`tenant`）載入，以及 Alertmanager 設定裡有沒有 receiver `migrated-alerts-shadow`。
>
> 手動替代：`curl -s http://localhost:9090/api/v1/rules | python3 -c "..."` + `ls -la migration_output/.migration/mapping/db-a.yaml`

### 2.3 Alertmanager Interception Configuration

migrated 告警帶 `da_migrated: "true"`，Alertmanager 必須把它們送到不通知任何人的 receiver，避免影子期同一件事 page 兩次。這條路由由 `generate-routes` 產生（排在平台路由的第 5 條），不要手寫；產生出來長這樣：

```yaml
route:
  routes:
    - matchers:
        - da_migrated="true"
      group_by: [tenant, alertname]
      receiver: migrated-alerts-shadow
      continue: false
receivers:
  - name: migrated-alerts-shadow
```

細節（`_routing_migrated`、切換時怎麼排除租戶）見 [generate-routes](cli-reference.md#generate-routes)。

### 2.4 基線建立（選用）

對關鍵 tenant 執行負載觀測，建立遷移前的 baseline 數據作為比對參考：

```bash
python3 scripts/tools/ops/baseline_discovery.py --tenant db-a --duration 1800 --interval 30
```

產出包含 p50/p90/p95/p99 統計與閾值建議 CSV，可在 shadow 期間比對趨勢是否偏移。

## 3. 啟動 Shadow Monitoring

⚠️ 本節的 `ghcr.io/vencil/da-tools:v2.9.0` 映像裡的 validate 還是數值比對，讀不了 `.migration/mapping/<tenant>.yaml`；下一版映像發布前，請從 repo 以 `python3 scripts/tools/ops/validate_migration.py`（參數相同）執行。 <!-- image-caveat: v2.9.0 -->

### 3.1 本地 port-forward（開發/小型環境）

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

docker run --rm --network=host \
  -v $(pwd)/migration_output:/data:ro \
  ghcr.io/vencil/da-tools:v2.9.0 \
  validate --mapping /data/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 秒間隔 × 4032 輪 ≈ 14 天
# legacy 規則還在另一台 Prometheus 時加 --legacy-prometheus <URL>（預設與 --prometheus 相同）
```

> **已 clone 專案？** 也可直接執行 Python 腳本：
> ```bash
> python3 scripts/tools/ops/validate_migration.py \
>   --mapping migration_output/.migration/mapping/db-a.yaml \
>   --prometheus http://localhost:9090 \
>   --watch --interval 300 --rounds 4032
> ```

### 3.2 K8s Job（生產環境推薦）

⚠️ 下面的 Job 用 v2.9.0 映像，它的 validate 還是數值比對，讀不了新的遷移對照；下一版映像發布前不要部署這個 Job，改在能連到 Prometheus 的機器上從 repo 執行 §3.1 的 `python3 scripts/tools/ops/validate_migration.py … --watch`。 <!-- image-caveat: v2.9.0 -->

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
          image: ghcr.io/vencil/da-tools:v2.9.0
          env:
            - name: PROMETHEUS_URL
              value: http://prometheus.monitoring.svc.cluster.local:9090
          command: ["da-tools"]
          args:
            - validate
            - --mapping
            - /config/db-a.yaml
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
            name: migration-mapping-db-a
        - name: output
          emptyDir: {}
      restartPolicy: OnFailure
```

### 3.3 關鍵參數

| 參數 | 預設值 | 說明 |
|------|--------|------|
| `--interval` | 60s | 比對間隔，也是告警取樣窗口（生產建議 300s）；兩邊規則組的評估間隔都不可大於它，否則結束碼 2 |
| `--rounds` | 10 | 比對輪數（14 天 ≈ 4032 輪 @300s） |
| `--legacy-prometheus` | 同 `--prometheus` | legacy 規則所在的 Prometheus；`--prometheus` 是 migrated 規則那一側 |
| `-o` | `validation_output` | 報告輸出目錄 |

完整旗標與結束碼見 [validate](cli-reference.md#validate)。

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

> `shadow-verify runtime` 會自動檢查 CSV 中的 mismatch 比例（`mismatch` 與 `pin_selects_nothing`、`rule_not_loaded`、`window_too_short`）、不可驗列（`unverifiable`，有一列就 FAIL）、tenant 覆蓋率，以及 Prometheus 中的 silent/maintenance 模式狀態。若 tenant 處於 `silent` 或 `maintenance` 模式，shadow 比對結果仍有效，但切換後 alert 不會觸發，直到恢復為 `normal`。

### 4.2 健康指標

| 指標 | 正常 | 需調查 |
|------|------|--------|
| mismatch 比例 | 0% | > 0% |
| 不可驗列（`unverifiable`） | 0 | > 0（兩邊都選不到序列，或 legacy 告警既無 `tenant` 也無 `namespace`） |
| 連續 mismatch | 無 | 同一 tenant 連續 3+ 輪 |
| operational_mode | normal | silent / maintenance（切換前需恢復） |

## 5. 異常處理 Playbook

### 5.1 告警集合 Mismatch

**症狀**: `validation-report.csv` 有 `Kind` 為 `alert`、`Status` 為 `mismatch` 的列：`Legacy` 1、`Migrated` 0 是只有 legacy 規則在這一輪的窗口內響過，反過來是只有 migrated 規則響過。`Labels` 欄是比對鍵裡的其餘 label（已去掉 `tenant`、`da_migrated`、`alertstate`）。

```bash
# 列出 mismatch 列
grep ',mismatch,' validation_output/validation-report.csv | head -20

# 直接查兩邊在窗口內響過的告警（窗口與 --interval 相同；alertstate 換成 pending 再查一次）
curl -s http://localhost:9090/api/v1/query --data-urlencode \
  'query=max_over_time(ALERTS{alertname="<告警名>", da_migrated="true", tenant="db-a", alertstate="firing"}[5m])' | python3 -m json.tool
# legacy 那一側查 legacy 規則所在的 Prometheus（給了 --legacy-prometheus 就是那一台）
export LEGACY_PROMETHEUS=<--legacy-prometheus 給的 URL>   # 沒給 --legacy-prometheus 時兩端是同一台，可略過
curl -s "${LEGACY_PROMETHEUS:-http://localhost:9090}/api/v1/query" --data-urlencode \
  'query=max_over_time(ALERTS{alertname="<告警名>", da_migrated="", alertstate="firing"}[5m])' | python3 -m json.tool
```

**常見原因與修復**:

| 原因 | 特徵 | 修復 |
|------|------|------|
| label 組合不同 | 同一條告警、同一輪有兩列 mismatch（一列 1／0、一列 0／1），`Labels` 不同 | 比對兩列的 `Labels`，找出是哪個 label 只在一邊或值不同 |
| 注入後選到的序列較少 | 只在 legacy 響，且只出現在部分序列 | 檢查對照裡該告警的 `legacy`／`pinned` selector：租戶歸屬的假設是否正確 |
| 同名但沒有遷移的 legacy 規則 | 同一條告警一直只在 legacy 響 | 已知限制：legacy 端以 alertname 查 `ALERTS`，另一條同名、沒遷移的規則（例如在別的檔案裡）沒有 migrated 對應 |

`Status` 為 `other_tenant` 的列是 legacy 側其他租戶的告警，注入本來就會收窄掉，不算 mismatch。

### 5.2 不可驗與選不到資料

**症狀**: `Status` 為 `unverifiable` 或 `pin_selects_nothing`

- `Kind` 為 `selector`、`Status` 為 `pin_selects_nothing`：legacy selector 有序列、注入後的 pinned selector 是 0，migrated 規則永遠不會響。`validate` 以結束碼 2 停止（`--watch` 也立刻停）。常見原因是租戶歸屬的假設錯了，或 `job=`／`instance=` 與平台的抓取設定不一致；修正後用 `migrate-rule` 重產規則與對照。
- `Kind` 為 `selector`、`Status` 為 `unverifiable`：兩邊都是 0，現在沒有資料可驗。
- `Kind` 為 `alert`、`Status` 為 `unverifiable`：legacy 告警既沒有 `tenant` 也沒有 `namespace`，分不出屬於哪個租戶。

不可驗一律不放行（結束碼 1，readiness 不 ready）；要切換只能用 `cutover --force`。

### 5.3 da-tools validate Itself Fails

```bash
# 確認 Prometheus 可達
curl -s http://localhost:9090/-/healthy

# 對照不必另外檢查：da-tools validate 以 --mapping 讀它時就照 YAML 規格嚴格讀，
# 打不開、不是 YAML、頂層不是 mapping，都會點名旗標與原因、以 rc 2 結束；
# 是 prefix-mapping（要用新版 migrate-rule 重產）、或不是 migrate-rule 寫的形狀，同樣 rc 2 並點名
# 規則沒載入（Kind=rule、rule_not_loaded）或規則組的評估間隔大於 --interval（window_too_short）也是 rc 2

# K8s Job 重啟
kubectl delete job shadow-monitor -n monitoring
kubectl apply -f shadow-monitor-job.yaml
```

## 6. 收斂判定標準

### 6.1 切換條件（全部滿足）

| 條件 | 驗證方式 |
|------|----------|
| 連續 7 天 0 mismatch | CSV 報告最近 7 天沒有 `mismatch`、`pin_selects_nothing`、`rule_not_loaded`、`window_too_short` 或 `unverifiable` 列 |
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
  --mapping migration_output/.migration/mapping/db-a.yaml \
  --report-csv validation_output/validation-report.csv \
  --prometheus http://localhost:9090
```

## 7. 退出 Shadow Monitoring

### 7.1 自動化切換（推薦）

v1.10.0 提供 `da-tools cutover`，單一指令自動完成以下全部步驟：

```bash
# Step 1: Dry run — 預覽切換步驟，不做任何變更
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data:ro \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.9.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# 預期輸出（列出會執行的 kubectl 命令，不做任何變更；v2.9.0 映像印在
# stdout，之後的版本改印在 stderr）：
# ▸ Stop Shadow Monitor Job...
#   [dry-run] kubectl delete job shadow-monitor -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove old Recording Rules...
#   [dry-run] kubectl delete configmap prometheus-rules-old -n monitoring --ignore-not-found=true
#   ✓ (dry-run)
# ▸ Remove shadow label from rules...
#   [dry-run] kubectl label configmap prometheus-rules -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Remove Alertmanager shadow route...
#   [dry-run] kubectl label configmap alertmanager-config -n monitoring migration_status-
#   ✓ (dry-run)
# ▸ Verify tenant health...
#   [dry-run] query http://localhost:9090 for tenant=db-a health
#   ✓ (dry-run)
#
# ✅ Cutover completed successfully.
# Next: run 'da-tools batch-diagnose' for full health report.

# Step 2: 執行切換
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data:ro \
  -e PROMETHEUS_URL=http://localhost:9090 \
  ghcr.io/vencil/da-tools:v2.9.0 \
  cutover --readiness-json /data/cutover-readiness.json --tenant db-a

# Step 3: 批次切換多個 tenant（逐一執行；每個租戶用自己的 readiness，validate 以 -o validation_output/<租戶> 分別跑）
for tenant in db-a db-b db-c; do
  docker run --rm --network=host \
    -v $(pwd)/validation_output:/data:ro \
    -e PROMETHEUS_URL=http://localhost:9090 \
    ghcr.io/vencil/da-tools:v2.9.0 \
    cutover --readiness-json "/data/$tenant/cutover-readiness.json" --tenant "$tenant"
done
```
<!-- image-caveat: v2.9.0 — 上方區塊裡「v2.9.0 映像印在 stdout」那段註解 -->

**`--force` 的使用時機：**

| 情境 | 是否用 `--force` | 說明 |
|------|-----------------|------|
| 有 `cutover-readiness.json` | 不需要 | readiness JSON 已證明收斂 |
| 手動分析 CSV 確認收斂 | 用 `--force` | 繞過 readiness 檢查 |
| 測試環境快速驗證 | 用 `--force` | 測試用途不需嚴格收斂 |
| 生產環境未確認收斂 | **不要用** | 風險過高，先完成收斂確認 |

> **注意**：`--force` 只跳過 readiness 檢查，五個步驟照樣執行，包括最後的健康驗證。健康驗證只查一件事：`count(user_threshold{tenant="<租戶>"})` 有沒有結果。它不跑 `check-alert` 或 `diagnose`，切換後請另跑 `da-tools batch-diagnose`。任一步失敗時，工具印出 `❌ Cutover failed at step: <步驟>` 並以非零碼結束，但不會自動回退。失敗步驟之前的步驟已經生效，需手動執行 §7.2 回退步驟。

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
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.9.0 \
  check-alert MariaDBHighConnections db-a

# 6. 租戶健康總檢
docker run --rm --network=host ghcr.io/vencil/da-tools:v2.9.0 diagnose db-a
```

### 7.2 回退（如有問題）

⚠️ 下面重新驗證的指令用 v2.9.0 映像；下一版映像發布前，改從 repo 執行 `python3 scripts/tools/ops/validate_migration.py`（參數相同）。 <!-- image-caveat: v2.9.0 -->

```bash
# 1. 恢復舊 Recording Rule（如已保留原始 yaml）
kubectl apply -f old-recording-rules.yaml

# 2. 重新掛上 shadow label（讓新規則回到 shadow 狀態）

# 3. 重啟 Shadow Monitor
docker run --rm --network=host \
  -v $(pwd)/migration_output:/data:ro \
  ghcr.io/vencil/da-tools:v2.9.0 \
  validate --mapping /data/.migration/mapping/db-a.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
```

### 7.3 清理

```bash
# 移除遷移產物
rm -rf migration_output/
rm -rf validation_output/

# 批次下架不再需要的 custom_ prefix 規則
docker run --rm --user $(id -u):$(id -g) \
  -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools:v2.9.0 \
  deprecate custom_mysql_connections custom_mysql_replication_lag \
  --config-dir /data/conf.d --execute
```

## 8. 自動化工具

以下工具減少 Shadow Monitoring 的人工操作：

| 工具 | 用法 | 效果 |
|------|------|------|
| **Auto-convergence** ✅ | `validate --auto-detect-convergence --stability-window 5` | 連續 N 輪乾淨（規則都已載入、預檢乾淨、告警集合一致、沒有不可驗、查詢都有回答）後自動產出 `cutover-readiness.json` 並停止 watch |
| **Batch health report** ✅ | `batch-diagnose`（da-tools CLI） | 切換後自動發現 tenants → 並行 `diagnose` → health score + remediation steps |
| **Threshold backtest** ✅ | `backtest --config-dir <PR 的 conf.d> --baseline <base 的 conf.d> --prometheus <url>` | PR 修改 threshold 時回測 7 天歷史數據，CI 自動產出風險評估 |
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
| ["性能基準 (Performance Benchmarks)"](./benchmarks.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](integration/byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](integration/byo-prometheus-integration.md) | ⭐⭐ |
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐ |
