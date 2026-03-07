# Shadow Monitoring SRE SOP

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
python3 scripts/tools/validate_config.py --config-dir components/threshold-exporter/config/conf.d
```

### 2.2 確認新規則已載入

```bash
# 確認 Prometheus 已載入新規則
curl -s http://localhost:9090/api/v1/rules | \
  python3 -c "import sys,json; rules=json.load(sys.stdin)['data']['groups']; \
  print(f'Rule groups: {len(rules)}'); \
  [print(f'  {g[\"name\"]}: {len(g[\"rules\"])} rules') for g in rules]"

# 確認 prefix-mapping.yaml 存在（migrate 產出）
ls -la migration_output/prefix-mapping.yaml
```

### 2.3 Alertmanager 攔截設定

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
python3 scripts/tools/baseline_discovery.py --tenant db-a --duration 1800 --interval 30
```

產出包含 p50/p90/p95/p99 統計與閾值建議 CSV，可在 shadow 期間比對趨勢是否偏移。

## 3. 啟動 Shadow Monitoring

### 3.1 本地 port-forward（開發/小型環境）

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:1.8.0 \
  validate --mapping /data/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 秒間隔 × 4032 輪 ≈ 14 天
```

> **已 clone 專案？** 也可直接執行 Python 腳本：
> ```bash
> python3 scripts/tools/validate_migration.py \
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
          image: ghcr.io/vencil/da-tools:1.8.0
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
# 1. 查看最新 CSV 報告
tail -20 validation_output/validation-report.csv

# 2. 快速統計 mismatch
grep "mismatch" validation_output/validation-report.csv | wc -l

# 3. 若使用 K8s Job，查看 Job 日誌
kubectl logs job/shadow-monitor -n monitoring --tail=50

# 4. 租戶健康檢查（確認 exporter 正常 + 運營模式）
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 \
  diagnose db-a
```

`diagnose` 輸出包含 `operational_mode` 欄位。若 tenant 處於 `silent` 或 `maintenance` 模式，shadow 比對數值仍有效，但切換後 alert 不會觸發，直到恢復為 `normal`。

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
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 \
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

### 5.3 da-tools validate 本身失敗

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
# 統計過去 7 天的 mismatch
awk -F',' '$8 == "mismatch" {count++} END {print "Mismatches:", count+0}' \
  validation_output/validation-report.csv

# 確認所有 tenant 都有比對
awk -F',' 'NR>1 {tenants[$2]++} END {for(t in tenants) print t, tenants[t]}' \
  validation_output/validation-report.csv

# 確認 tenant 運營模式
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 diagnose db-a
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 diagnose db-b
```

## 7. 退出 Shadow Monitoring

### 7.1 切換步驟

```bash
# 1. 停止 Shadow Monitor Job
kubectl delete job shadow-monitor -n monitoring

# 2. 移除舊 Recording Rule
#    (具體操作依環境：刪除 ConfigMap 或 Helm 移除)

# 3. 移除新規則的 migration_status: shadow label
#    更新 Alert Rule 定義，去掉 shadow label

# 4. 移除 Alertmanager 的 shadow 攔截 route

# 5. 驗證切換後 alert 正常觸發
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 \
  check-alert MariaDBHighConnections db-a

# 6. 租戶健康總檢
docker run --rm --network=host ghcr.io/vencil/da-tools:1.8.0 diagnose db-a
```

### 7.2 回退（如有問題）

```bash
# 1. 恢復舊 Recording Rule（如已保留原始 yaml）
kubectl apply -f old-recording-rules.yaml

# 2. 重新掛上 shadow label（讓新規則回到 shadow 狀態）

# 3. 重啟 Shadow Monitor
docker run --rm --network=host \
  -v $(pwd)/migration_output:/data \
  ghcr.io/vencil/da-tools:1.8.0 \
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
docker run --rm -v $(pwd)/conf.d:/data/conf.d ghcr.io/vencil/da-tools:1.8.0 \
  deprecate custom_mysql_connections custom_mysql_replication_lag --execute
```

## 8. 自動化展望

以下為規劃中的改進，將進一步減少 Shadow Monitoring 的人工操作：

| 項目 | 說明 |
|------|------|
| Auto-convergence 偵測 | `validate --converged` 內建收斂判定，達標自動產出 ready-to-cut 報告 |
| Shadow Dashboard | Grafana 面板即時顯示 mismatch 趨勢、per-tenant 收斂狀態 |
| One-command cutover | 單一指令完成 §7.1 所有步驟（停止 Job → 移除舊規則 → 去 label → 驗證） |

## 9. 快速參考卡

```
┌─────────────────────────────────────────────────────────────┐
│ Shadow Monitoring 生命週期                                    │
│                                                               │
│  validate-config → 配置驗證                                   │
│       ↓                                                       │
│  da-tools migrate → 新規則部署 → Alertmanager 攔截            │
│       ↓                                                       │
│  da-tools validate --watch (--tolerance 0.001)                │
│       ↓                                                       │
│  日常巡檢 + da-tools diagnose (1-2 週)                        │
│       ↓                                                       │
│  收斂判定 (7 天 0 mismatch + normal mode)                     │
│       ↓                                                       │
│  切換：移除舊規則 + 移除 shadow label + check-alert 驗證      │
│       ↓                                                       │
│  清理：da-tools deprecate (支援批次) / rm 產物                │
└─────────────────────────────────────────────────────────────┘
```
