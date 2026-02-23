# 遷移指南：從傳統 Prometheus 警報遷移至動態閾值架構

## 目錄

1. [概述與思維轉換](#1-概述與思維轉換)
2. [遷移前評估](#2-遷移前評估)
3. [基礎設施部署](#3-基礎設施部署)
4. [警報規則轉換 — 完整範例](#4-警報規則轉換--完整範例)
5. [Alertmanager 路由遷移](#5-alertmanager-路由遷移)
6. [遷移後驗證](#6-遷移後驗證)
7. [LLM 輔助批量轉換](#7-llm-輔助批量轉換)
8. [FAQ 與疑難排解](#8-faq-與疑難排解)

---

## 1. 概述與思維轉換

### 傳統架構的痛點

在傳統 Prometheus 架構中，**邏輯**與**數值**綁定在同一份 PromQL 裡：

```yaml
# 傳統：每個租戶都要複製一份，改裡面寫死的數字
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100   # ← 寫死
```

這導致：規則重複膨脹、修改需 reload Prometheus、租戶無法自助調整水位。

### 新架構的分工

| 角色 | 負責內容 | 修改方式 |
|------|----------|----------|
| **平台團隊** | 無數值的 PromQL 邏輯規則 + Recording Rules | 版本控制，低頻更新 |
| **租戶** | 純 YAML 數值 (`threshold-config`) | ConfigMap patch，即時生效 |
| **threshold-exporter** | 背景動態結合兩者 | 自動 hot-reload，無需重啟 |

### 三態邏輯 (Three-State Design)

每個租戶的每個指標都有三種狀態，這是理解新架構的關鍵：

| 狀態 | 設定方式 | 效果 |
|------|----------|------|
| **Custom** | 設定數值 (如 `"70"`) | 使用自訂閾值 |
| **Default** | 省略 key | 使用全域預設值 |
| **Disable** | 設定 `"disable"` | 不產生 metric，不觸發 alert |

---

## 2. 遷移前評估

開始遷移前，請先盤點現有環境：

### 2.1 Checklist

- [ ] **警報規則數量**：目前有多少條 alert rules？分佈在幾個 `rule_files` 中？
- [ ] **指標來源**：使用哪些 exporter？(mysqld_exporter, redis_exporter, node_exporter, etc.)
- [ ] **閾值類型分類**：
  - 純數值比較 (`> 80`, `< 10`) → 直接可遷，對應 Scenario A
  - 容器資源百分比 (`cpu > limit * 0.8`) → 可遷，對應 Scenario B
  - 字串/狀態匹配 (`CrashLoopBackOff`) → 可遷，對應 Scenario C
  - 複合條件 (`A AND B`) → 可遷，對應 Scenario D
  - 跨時間窗口 (`rate increase > 50% in 1h`) → 需平台團隊額外寫 Recording Rule
- [ ] **Alertmanager routing**：目前依據什麼標籤分派？(`instance`, `job`, `team`, `namespace`)
- [ ] **租戶數量**：有幾個團隊/服務需要獨立閾值？
- [ ] **通知管道**：Slack, PagerDuty, Email, Webhook？各租戶是否不同？

### 2.2 適用性判斷

以下規則類型**可直接遷移**（佔大部分場景）：

```
指標 > 固定數值                    → threshold YAML
指標 > 固定數值 (多層 severity)    → _critical 後綴
狀態 == 某個字串                   → state_filters
條件 A AND 條件 B                  → 複合 alert rule
```

以下需要**平台團隊額外工作**：

```
rate 變化率 > 百分比   → 需新增 Recording Rule 做 normalize
predict_linear()      → 需在 alert rule 層處理
histogram_quantile()  → 需在 Recording Rule 層處理
```

---

## 3. 基礎設施部署

### 3.1 前置條件

- Kubernetes 叢集 (已有 Prometheus + Alertmanager)
- `kubectl` 存取權限
- Helm 3

### 3.2 部署 threshold-exporter

```bash
# 1. Build image (或使用預建 image)
cd components/threshold-exporter
docker build -t threshold-exporter:latest .

# 2. 部署 (Helm)
helm install threshold-exporter ./components/threshold-exporter \
  -n monitoring \
  -f environments/local/threshold-exporter.yaml

# 3. 確認運行
kubectl get pods -n monitoring -l app=threshold-exporter
```

### 3.3 載入 Recording Rules

將 `configmap-prometheus.yaml` 中的 recording rules 合併至你的 Prometheus 配置。關鍵的 rule groups：

- `mysql-normalization`：MySQL 指標正規化
- `threshold-normalization`：閾值指標轉換
- `container-normalization`：容器資源正規化 (Scenario B)
- `state-matching`：狀態匹配正規化 (Scenario C)

### 3.4 驗證 Exporter 可用

```bash
# 確認 metrics endpoint 回應
kubectl port-forward svc/threshold-exporter 8080:8080 -n monitoring
curl http://localhost:8080/metrics | grep user_threshold
```

預期輸出包含：
```
user_threshold{tenant="db-a",component="mysql",metric="connections",severity="warning"} 70
```

---

## 4. 警報規則轉換 — 完整範例

以下以 **Percona MariaDB/MySQL Alert Rules** 為範本，示範五種典型場景的遷移。

### 4.1 基本數值比較 — 連線數

**傳統寫法 (Percona)**：
```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Too many connections on {{ $labels.instance }}"
```

**遷移步驟**：

**Step 1 — 平台團隊**：建立通用 alert rule（全平台做一次）
```yaml
- alert: MariaDBHighConnections
  expr: |
    (
      tenant:mysql_threads_connected:sum
      > on(tenant) group_left
      tenant:alert_threshold:connections
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High connections on {{ $labels.tenant }}"
```

**Step 2 — 租戶**：在 threshold-config 寫數值
```yaml
tenants:
  db-a:
    mysql_connections: "100"    # 對應原本的 > 100
```

### 4.2 多層嚴重度 — 連線數 Warning + Critical

**傳統寫法 (兩條分開的規則)**：
```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100
  labels:
    severity: warning

- alert: MySQLTooManyConnectionsCritical
  expr: mysql_global_status_threads_connected > 150
  labels:
    severity: critical
```

**遷移後 — 租戶只需寫**：
```yaml
tenants:
  db-a:
    mysql_connections: "100"            # warning 閾值
    mysql_connections_critical: "150"   # critical 閾值 (使用 _critical 後綴)
```

平台團隊的 alert rule 會自動處理降級邏輯：當 critical 觸發時，warning 被 `unless` 抑制。

### 4.3 Slave Replication Lag

**傳統寫法 (Percona)**：
```yaml
- alert: MySQLSlaveReplicationLag
  expr: mysql_slave_status_seconds_behind_master > 30
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "Slave lag {{ $value }}s on {{ $labels.instance }}"
```

**遷移步驟**：

**Step 1 — 平台團隊**：新增 Recording Rule + Alert Rule
```yaml
# Recording Rule (正規化)
- record: tenant:mysql_slave_lag:seconds
  expr: max by(tenant) (mysql_slave_status_seconds_behind_master)

# Threshold normalization
- record: tenant:alert_threshold:slave_lag
  expr: sum by(tenant) (user_threshold{metric="slave_lag", severity="warning"})

# Alert Rule
- alert: MariaDBSlaveLag
  expr: |
    (
      tenant:mysql_slave_lag:seconds
      > on(tenant) group_left
      tenant:alert_threshold:slave_lag
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
  for: 5m
  labels:
    severity: warning
```

**Step 2 — 租戶**：
```yaml
tenants:
  db-a:
    mysql_slave_lag: "30"    # 秒
```

**Step 3 — Exporter defaults (可選)**：
```yaml
defaults:
  mysql_slave_lag: 60    # 全域預設 60 秒
```

### 4.4 Slow Queries (Rate 類)

**傳統寫法 (Percona)**：
```yaml
- alert: MySQLHighSlowQueries
  expr: rate(mysql_global_status_slow_queries[5m]) > 0.1
  for: 5m
  labels:
    severity: warning
```

**遷移步驟**：

**Step 1 — 平台團隊**：rate 計算放在 Recording Rule 中
```yaml
# Recording Rule
- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))

# Threshold normalization
- record: tenant:alert_threshold:slow_queries
  expr: sum by(tenant) (user_threshold{metric="slow_queries", severity="warning"})

# Alert Rule
- alert: MariaDBHighSlowQueries
  expr: |
    (
      tenant:mysql_slow_queries:rate5m
      > on(tenant) group_left
      tenant:alert_threshold:slow_queries
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
  for: 5m
  labels:
    severity: warning
```

**Step 2 — 租戶**：
```yaml
tenants:
  db-a:
    mysql_slow_queries: "0.1"   # rate per second
```

### 4.5 Buffer Pool 使用率 (百分比計算類)

**傳統寫法**：
```yaml
- alert: MySQLInnoDBBufferPoolFull
  expr: |
    mysql_global_status_innodb_buffer_pool_pages_data
    / mysql_global_status_innodb_buffer_pool_pages_total * 100 > 95
  for: 10m
  labels:
    severity: warning
```

**遷移步驟**：

**Step 1 — 平台團隊**：百分比計算在 Recording Rule 完成
```yaml
# Recording Rule (百分比正規化)
- record: tenant:mysql_innodb_buffer_pool:percent
  expr: |
    sum by(tenant) (mysql_global_status_innodb_buffer_pool_pages_data)
    /
    sum by(tenant) (mysql_global_status_innodb_buffer_pool_pages_total)
    * 100

# Threshold normalization
- record: tenant:alert_threshold:innodb_buffer_pool
  expr: sum by(tenant) (user_threshold{metric="innodb_buffer_pool", severity="warning"})

# Alert Rule
- alert: MariaDBInnoDBBufferPoolHigh
  expr: |
    (
      tenant:mysql_innodb_buffer_pool:percent
      > on(tenant) group_left
      tenant:alert_threshold:innodb_buffer_pool
    )
    unless on(tenant)
    (user_state_filter{filter="maintenance"} == 1)
  for: 10m
  labels:
    severity: warning
```

**Step 2 — 租戶**：
```yaml
tenants:
  db-a:
    mysql_innodb_buffer_pool: "95"   # 百分比
```

### 4.6 三態操作範例 (Disable)

租戶 `db-b` 不需要 slave lag 監控（因為沒有 replica）：

```yaml
tenants:
  db-b:
    mysql_slave_lag: "disable"   # 不產生 metric，不觸發 alert
```

---

## 5. Alertmanager 路由遷移

### 5.1 傳統 Routing（基於 instance）

```yaml
route:
  group_by: ['alertname', 'instance']
  routes:
    - matchers:
        - instance=~"db-a-.*"
      receiver: "team-a-slack"
    - matchers:
        - instance=~"db-b-.*"
      receiver: "team-b-email"
```

### 5.2 遷移後 Routing（基於 tenant）

```yaml
route:
  group_by: ['tenant', 'alertname']   # 以 tenant 為第一維度
  routes:
    - matchers:
        - tenant="db-a"
      receiver: "team-a-slack"
      group_wait: 30s
      group_interval: 5m
    - matchers:
        - tenant="db-b"
      receiver: "team-b-email"
      group_wait: 1m
      group_interval: 10m
```

### 5.3 進階：多通道 + 嚴重度分層

```yaml
route:
  group_by: ['tenant', 'alertname']
  routes:
    - matchers:
        - tenant="db-a"
      receiver: "team-a-slack"          # warning → Slack
      routes:
        - matchers:
            - severity="critical"
          receiver: "team-a-pagerduty"  # critical → PagerDuty
    - matchers:
        - tenant="db-b"
      receiver: "team-b-slack"
```

---

## 6. 遷移後驗證

遷移不只是轉格式，**必須驗證行為一致**。本專案提供三個驗證工具：

### 6.1 確認閾值正確輸出

```bash
# 透過 Exporter 確認 user_threshold metric 存在且值正確
curl -s http://localhost:8080/metrics | grep 'user_threshold{.*connections'
# 預期: user_threshold{tenant="db-a",...,metric="connections",...} 100
```

### 6.2 確認 Alert 狀態

使用 `check_alert.py` 驗證每一條遷移後的 alert：

```bash
# 確認 alert 在正常情況下為 inactive（未超閾值）
python3 scripts/tools/check_alert.py MariaDBHighConnections db-a
# 預期輸出: {"alert": "MariaDBHighConnections", "tenant": "db-a", "state": "inactive"}
```

建議：對每條遷移過來的 alert，至少驗證一次 `inactive` 狀態。若環境允許，用 `patch_config.py` 將閾值暫時調低觸發 `firing`，再恢復。

### 6.3 租戶健康總檢

```bash
python3 scripts/tools/diagnose.py db-a
# 正常: {"status": "healthy", "tenant": "db-a"}
# 異常: 會附帶 issues 清單和 logs
```

### 6.4 驗證 Checklist

- [ ] 每個遷移的 alert 在正常負載下為 `inactive`
- [ ] 刻意觸發至少一條 alert，確認 `firing` → Alertmanager → 通知管道正常
- [ ] 測試三態：修改閾值 → hot-reload 生效 → 設 `disable` → alert 消失
- [ ] 確認 `_critical` 多層嚴重度的降級邏輯正確（warning 被 critical 覆蓋）
- [ ] Alertmanager routing 以 `tenant` 標籤正確分派到目標通知管道

---

## 7. LLM 輔助批量轉換

如果有大量傳統 alert rules 需要遷移，可以使用 LLM 加速。即使是較小的模型（如 Llama-3 8B、Gemma）也能處理這類結構化轉換任務。

### 7.1 System Prompt

將以下 prompt 作為 System Prompt 提供給 LLM：

```
你是一位 SRE 專家，負責將傳統 Prometheus Alert Rules 遷移到「動態多租戶閾值架構」。

在新架構中：
- 所有寫死的數字門檻必須抽離成 YAML 鍵值對
- Metric key 命名格式：<component>_<metric>（如 mysql_connections, redis_memory_percent）
- 支援多層嚴重度：用 _critical 後綴（如 mysql_connections_critical: "150"）
- 支援停用：值設為 "disable"

請讀取我貼上的傳統 Alert Rule YAML，完成以下工作：

1. **抽取閾值**：把寫死的數字轉換成 threshold-config.yaml 格式
2. **推斷 metric key**：根據 PromQL 中的指標名推斷合適的 key 命名
3. **標註嚴重度**：若原規則有 severity，使用 _critical 後綴語法
4. **標記需要平台支援的項目**：如果規則中有 rate()、predict_linear()、histogram_quantile()
   等需要 Recording Rule 正規化的計算，請標記出來告知平台團隊

範例輸入：
```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100
  labels:
    severity: warning

- alert: MySQLTooManyConnectionsCritical
  expr: mysql_global_status_threads_connected > 150
  labels:
    severity: critical

- alert: MySQLHighSlowQueries
  expr: rate(mysql_global_status_slow_queries[5m]) > 0.1
  labels:
    severity: warning
```

範例輸出：
```yaml
# === threshold-config.yaml (租戶填寫) ===
tenants:
  <tenant-name>:
    mysql_connections: "100"
    mysql_connections_critical: "150"
    mysql_slow_queries: "0.1"

# === 需要平台團隊處理 ===
# mysql_slow_queries: 原規則使用 rate()，需新增 Recording Rule:
#   tenant:mysql_slow_queries:rate5m = sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

現在，請等待我貼上傳統的 Alert Rules。
```

### 7.2 使用流程

```
1. 將上述 System Prompt 設定好
2. 貼上你的傳統 alert rules YAML（可一次貼多條）
3. LLM 輸出 threshold-config.yaml 片段 + 平台團隊待辦
4. 人工 Review 結果，確認 key 命名符合 <component>_<metric> 格式
5. 用 patch_config.py 逐一 apply，搭配 check_alert.py 驗證
```

### 7.3 注意事項

- LLM 輸出的 metric key 命名可能不一致，**務必人工 review** 確保命名規範
- 含 `rate()` / `histogram_quantile()` 的規則，LLM 只能幫你抽數值，Recording Rule 需平台團隊撰寫
- 建議每次餵 5-10 條規則，避免小模型上下文溢出影響品質

---

## 8. FAQ 與疑難排解

### Q: 修改 threshold-config 後多久生效？

threshold-exporter 預設每 30 秒 reload 一次（可在 Helm values 調整 `reloadInterval`）。ConfigMap 在 K8s 中的 propagation delay 約 1-2 分鐘，因此從 `kubectl patch` 到 alert 實際變化，預期 **1-3 分鐘**。

### Q: 新增一種指標需要改哪些東西？

| 步驟 | 負責人 | 修改檔案 |
|------|--------|----------|
| 1. 新增 Recording Rule (正規化) | 平台 | `configmap-prometheus.yaml` |
| 2. 新增 Threshold Recording Rule | 平台 | `configmap-prometheus.yaml` |
| 3. 新增 Alert Rule | 平台 | `configmap-prometheus.yaml` |
| 4. (可選) 新增全域預設值 | 平台 | `threshold-config.yaml` defaults |
| 5. 設定租戶閾值 | 租戶 | `threshold-config.yaml` tenants |

租戶不需動任何 PromQL。

### Q: 遷移過渡期可以新舊並存嗎？

可以。新架構的 alert rules 使用不同的 alertname（如 `MariaDBHighConnections` vs 傳統的 `MySQLTooManyConnections`），不會衝突。建議：

1. 先部署新 alert rules（觀察 `pending`/`inactive` 狀態）
2. 確認行為一致後，再移除舊 rules
3. 過渡期讓新舊 alert 並行，Alertmanager 可用 `alertname` 做 routing 區分

### Q: 如何確認 hot-reload 成功？

```bash
# 查看 exporter 的 /api/v1/config endpoint
curl http://localhost:8080/api/v1/config | python3 -m json.tool

# 查看 exporter logs
kubectl logs -n monitoring -l app=threshold-exporter --tail=20
# 預期看到: "Reloaded config: X tenants, Y defaults"
```
