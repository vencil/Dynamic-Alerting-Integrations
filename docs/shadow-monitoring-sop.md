# Shadow Monitoring SRE SOP

> **受眾**: SRE / Platform Engineer / DevOps
> **前提**: 已完成 `migrate_rule.py` 轉換，新舊 Recording Rule 同時在 Prometheus 運行
> **工具**: `validate_migration.py`（`--watch` 持續模式 / 單次模式）

---

## 1. 概述

Shadow Monitoring 是遷移流程的**並行驗證階段**：新規則（`custom_` prefix）與舊規則同時運行，透過 `validate_migration.py` 持續比對數值輸出，確認行為等價後再切換。

本 SOP 涵蓋：啟動 → 日常巡檢 → 異常處理 → 收斂判定 → 退出。

## 2. 啟動 Shadow Monitoring

### 2.1 前置檢查

```bash
# 確認新規則已載入 Prometheus
curl -s http://localhost:9090/api/v1/rules | \
  python3 -c "import sys,json; rules=json.load(sys.stdin)['data']['groups']; \
  print(f'Rule groups: {len(rules)}'); \
  [print(f'  {g[\"name\"]}: {len(g[\"rules\"])} rules') for g in rules]"

# 確認 prefix-mapping.yaml 存在
ls -la migration_output/prefix-mapping.yaml
```

### 2.2 Alertmanager 攔截設定

新規則帶 `migration_status: shadow` label，Alertmanager 必須攔截以避免誤報：

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

### 2.3 啟動持續比對

**方式 A: 本地 port-forward（開發/小型環境）**

```bash
kubectl port-forward svc/prometheus 9090:9090 -n monitoring &

python3 scripts/tools/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
# 300 秒間隔 × 4032 輪 ≈ 14 天
```

**方式 B: K8s Job（生產環境推薦）**

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
          image: python:3.11-slim
          command:
            - python3
            - /scripts/validate_migration.py
            - --mapping
            - /config/prefix-mapping.yaml
            - --prometheus
            - http://prometheus.monitoring.svc.cluster.local:9090
            - --watch
            - --interval
            - "300"
            - --rounds
            - "4032"
            - -o
            - /output
          volumeMounts:
            - name: scripts
              mountPath: /scripts
            - name: config
              mountPath: /config
            - name: output
              mountPath: /output
      volumes:
        - name: scripts
          configMap:
            name: migration-scripts
        - name: config
          configMap:
            name: prefix-mapping
        - name: output
          emptyDir: {}
      restartPolicy: OnFailure
```

## 3. 日常巡檢流程

### 3.1 檢查頻率

| 階段 | 頻率 | 重點 |
|------|------|------|
| Day 1-3 | 每日 2 次 | 確認無系統性 mismatch |
| Day 4-7 | 每日 1 次 | 觀察業務高峰期差異 |
| Week 2 | 隔日 1 次 | 確認長期穩定性 |

### 3.2 巡檢操作

```bash
# 1. 查看最新 CSV 報告
tail -20 validation_output/validation-report.csv

# 2. 快速統計 mismatch
grep "mismatch" validation_output/validation-report.csv | wc -l

# 3. 若使用 K8s Job，查看 Job 日誌
kubectl logs job/shadow-monitor -n monitoring --tail=50
```

### 3.3 健康指標

| 指標 | 正常 | 需調查 |
|------|------|--------|
| mismatch 比例 | 0% | > 0% |
| missing 資料 | 0 | > 0（可能 Recording Rule 名稱不對） |
| 連續 mismatch | 無 | 同一 tenant 連續 3+ 輪 mismatch |

## 4. 異常處理 Playbook

### 4.1 數值 Mismatch

**症狀**: `validate_migration.py` 報告 `mismatch`，delta ≠ 0

**排查步驟**:

```bash
# 1. 確認具體哪些 tenant/metric 不一致
python3 scripts/tools/validate_migration.py \
  --old "<old_query>" --new "<new_query>" \
  --prometheus http://localhost:9090

# 2. 直接查詢 Prometheus，比對原始數據
curl -s "http://localhost:9090/api/v1/query?query=<old_query>" | python3 -m json.tool
curl -s "http://localhost:9090/api/v1/query?query=<new_query>" | python3 -m json.tool

# 3. 檢查 Recording Rule 定義是否等價
curl -s http://localhost:9090/api/v1/rules | \
  python3 -c "import sys,json; [print(r['query']) for g in json.load(sys.stdin)['data']['groups'] for r in g['rules'] if '<metric_name>' in r.get('record','')]"
```

**常見原因與修復**:

| 原因 | 特徵 | 修復 |
|------|------|------|
| 聚合方式不同 | 新值 = 舊值 × N | 確認 `max by` vs `sum by` vs `avg by` |
| label 不匹配 | `new_missing` / `old_missing` | 檢查 `by()` 子句的 label 名稱 |
| 評估時間窗口不同 | delta 極小但穩定 | 確認 `rate[5m]` / `[1m]` 等窗口一致 |
| 計數器重置 | 偶發大 delta | Rate 計算的正常現象，觀察是否收斂 |

### 4.2 Missing 資料

**症狀**: `old_missing` 或 `new_missing`

```bash
# 確認 metric 是否存在於 Prometheus
curl -s "http://localhost:9090/api/v1/label/__name__/values" | \
  python3 -c "import sys,json; names=json.load(sys.stdin)['data']; \
  [print(n) for n in names if 'custom_' in n or '<keyword>' in n]"
```

**可能原因**:
- 新 Recording Rule 尚未被 Prometheus evaluate（等待 1-2 個 evaluation interval）
- `prefix-mapping.yaml` 中的 query 名稱拼寫錯誤
- 該 tenant 的 metric 已被 `disable`（三態機制）

### 4.3 validate_migration.py 本身失敗

```bash
# 確認 Prometheus 可達
curl -s http://localhost:9090/-/healthy

# 確認 prefix-mapping.yaml 格式正確
python3 -c "import yaml; print(yaml.safe_load(open('migration_output/prefix-mapping.yaml')))"

# K8s Job 重啟
kubectl delete job shadow-monitor -n monitoring
kubectl apply -f shadow-monitor-job.yaml
```

## 5. 收斂判定標準

### 5.1 切換條件（全部滿足）

| 條件 | 驗證方式 |
|------|----------|
| 連續 7 天 0 mismatch | CSV 報告最後 2016 筆（7天 × 288次/天@5min）全部 `match` |
| 覆蓋業務高峰 + 低谷 | 確認報告時間戳涵蓋 peak hours |
| 覆蓋維護窗口 | 確認報告時間戳涵蓋週末/備份時段 |
| 所有 tenant 均參與比對 | CSV 中每個 tenant 至少有資料 |

### 5.2 收斂確認指令

```bash
# 統計過去 7 天的 mismatch
awk -F',' '$8 == "mismatch" {count++} END {print "Mismatches:", count+0}' \
  validation_output/validation-report.csv

# 確認所有 tenant 都有比對
awk -F',' 'NR>1 {tenants[$2]++} END {for(t in tenants) print t, tenants[t]}' \
  validation_output/validation-report.csv
```

## 6. 退出 Shadow Monitoring

### 6.1 切換步驟

```bash
# 1. 停止 Shadow Monitor Job
kubectl delete job shadow-monitor -n monitoring

# 2. 移除舊 Recording Rule
#    (具體操作依環境：刪除 ConfigMap 或 Helm 移除)

# 3. 移除新規則的 migration_status: shadow label
#    更新 Alert Rule 定義，去掉 shadow label

# 4. 移除 Alertmanager 的 shadow 攔截 route

# 5. 驗證切換後 alert 正常觸發
python3 scripts/tools/check_alert.py MariaDBHighConnections db-a
python3 scripts/tools/diagnose.py db-a
```

### 6.2 回退（如有問題）

```bash
# 1. 恢復舊 Recording Rule（如已保留原始 yaml）
kubectl apply -f old-recording-rules.yaml

# 2. 重新掛上 shadow label（讓新規則回到 shadow 狀態）

# 3. 重啟 Shadow Monitor，重新進入觀察
python3 scripts/tools/validate_migration.py \
  --mapping migration_output/prefix-mapping.yaml \
  --prometheus http://localhost:9090 \
  --watch --interval 300 --rounds 4032
```

### 6.3 清理

```bash
# 移除遷移產物
rm -rf migration_output/
rm -rf validation_output/

# 若不再需要 custom_ prefix 規則，使用 deprecate_rule.py
python3 scripts/tools/deprecate_rule.py custom_mysql_connections --execute
```

## 7. 快速參考卡

```
┌──────────────────────────────────────────────────┐
│ Shadow Monitoring 生命週期                        │
│                                                    │
│  migrate_rule.py → 新規則部署 → Alertmanager 攔截 │
│       ↓                                            │
│  validate_migration.py --watch                     │
│       ↓                                            │
│  日常巡檢 (1-2 週)                                 │
│       ↓                                            │
│  收斂判定 (7 天 0 mismatch)                        │
│       ↓                                            │
│  切換：移除舊規則 + 移除 shadow label              │
│       ↓                                            │
│  清理：deprecate_rule.py / rm 產物                 │
└──────────────────────────────────────────────────┘
```
