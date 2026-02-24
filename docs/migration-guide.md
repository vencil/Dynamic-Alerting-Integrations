# 遷移指南：從傳統 Prometheus 警報遷移至動態多租戶閾值架構

## 目錄

1. [為什麼要遷移？](#1-為什麼要遷移)
2. [Step 0 — 建立正規化層](#2-step-0--建立正規化層)
3. [Step 1 — 使用 migrate_rule.py 自動轉換](#3-step-1--使用-migrate_rulepy-自動轉換)
4. [Step 2 — 選擇聚合模式 (Max vs. Sum)](#4-step-2--選擇聚合模式-max-vs-sum)
5. [實戰範例：五種遷移場景](#5-實戰範例五種遷移場景)
6. [Alertmanager 路由遷移](#6-alertmanager-路由遷移)
7. [遷移後驗證](#7-遷移後驗證)
8. [LLM 輔助手動轉換](#8-llm-輔助手動轉換)
9. [目錄模式 (Directory Mode)](#9-目錄模式-directory-mode)
10. [維度標籤 — 多 DB 類型支援 (Phase 2B)](#10-維度標籤--多-db-類型支援-phase-2b)
11. [FAQ](#11-faq)

---

## 1. 為什麼要遷移？

### 傳統架構的痛點

在傳統 Prometheus 架構中，邏輯與數值被綁死在同一份 PromQL 裡：

```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100   # ← 寫死
```

這會引發三個問題：每個租戶都需要複製一整條規則只為改一個數字；修改閾值需要 reload Prometheus（甚至影響全平台）；租戶完全無法自助調整水位。

更根本的是，大部分團隊在遷移前**根本沒有 Recording Rules 的概念**。他們的規則狀態通常是：原始指標直接寫進 alert expr、高基數 (cardinality) label 散落各處、單節點與叢集邏輯混在一起。這意味著遷移的第一步，不是轉格式，而是**建立正規化層**。

### 新架構的分工

| 角色 | 負責內容 | 修改方式 |
|------|----------|----------|
| **平台團隊** | 無數值的 PromQL 邏輯 + Recording Rules | 版本控制，低頻更新 |
| **租戶** | 純 YAML 數值 (`threshold-config`) | ConfigMap patch，即時生效 |
| **threshold-exporter** | 背景動態結合兩者 | 自動 hot-reload，無需重啟 |

### 三態邏輯 (Three-State Design)

每個租戶的每個指標有三種狀態：

| 狀態 | 設定方式 | 效果 |
|------|----------|------|
| **Custom** | 設定數值 (如 `"70"`) | 使用自訂閾值 |
| **Default** | 省略 key | 使用全域預設值 |
| **Disable** | 設定 `"disable"` | 不產生 metric，不觸發 alert |

---

## 2. Step 0 — 建立正規化層

> **這是整個遷移最重要的一步。** 在碰任何閾值設定之前，先把原始指標轉為 `tenant:` 開頭的正規化指標。

### 為什麼需要正規化？

傳統環境中，指標帶著 `instance`、`job`、`pod` 等高基數 label，且單節點與叢集的語義完全不同。正規化層的目的是：

1. **抹平單節點 vs. 叢集差異**：無論底層是一台 MariaDB 還是三台 Galera，上層 alert 只看 `tenant` 維度。
2. **降低基數 (Cardinality)**：Recording Rule 在寫入時已完成聚合，alert eval 成本大幅降低。
3. **做到 Tenant-agnostic**：所有 alert rule 與 threshold-exporter 的 Go 程式碼中，禁止 hardcode 任何 tenant ID。

### 正規化命名規範

```
tenant:<component>_<metric>:<aggregation_function>
```

範例：

| 原始指標 | 正規化後 | 說明 |
|----------|----------|------|
| `mysql_global_status_threads_connected` | `tenant:mysql_threads_connected:max` | 單點上限，取 max |
| `rate(mysql_global_status_slow_queries[5m])` | `tenant:mysql_slow_queries:rate5m` | 叢集總量，取 sum 後算 rate |
| `mysql_slave_status_seconds_behind_master` | `tenant:mysql_slave_lag:max` | 最差節點延遲 |

### 建立步驟

```yaml
# 加入 Prometheus configmap (Recording Rules)
groups:
  - name: mysql-normalization
    rules:
      - record: tenant:mysql_threads_connected:max
        expr: max by(tenant) (mysql_global_status_threads_connected)

      - record: tenant:mysql_slow_queries:rate5m
        expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

> **注意**：聚合函式的選擇 (`max` vs. `sum`) 是一個架構決策，詳見下方 Step 2。

---

## 3. Step 1 — 使用 migrate_rule.py 自動轉換

本專案提供 `scripts/tools/migrate_rule.py` 作為遷移的核心入口。它採用 80/20 法則，自動處理大部分常見規則，並對複雜情況提供優雅降級。

### 用法

```bash
python3 scripts/tools/migrate_rule.py <legacy-rules.yml>
```

### 三種處理情境

| 情境 | 觸發條件 | 工具行為 |
|------|----------|----------|
| ✅ **完美解析** | 簡單的 `指標 > 數值` | 自動產出完整三件套 |
| ⚠️ **複雜表達式** | 含 `rate()`, `[5m]`, 數學運算 | 產出三件套，但標記 `TODO` 請人工確認聚合模式 |
| 🚨 **無法解析** | `absent()`, `predict_linear()` 等語義不同的函式 | 不產出，改給一段可直接交給 LLM 的 Prompt |

### 工具輸出的「三件套」

對於每一條可解析的規則，工具會輸出：

1. **Tenant Config** — 租戶需填入 `db-*.yaml` 的 YAML 片段 (metric key + 閾值)。
2. **Platform Recording Rule** — 平台團隊需加入 Prometheus 的正規化 Recording Rule。對於複雜表達式會標記 `TODO` 提醒選擇 `sum` 或 `max`。
3. **Platform Alert Rule** — 包含 `group_left` 動態比較 + `unless maintenance` 抑制邏輯的完整 alert rule。

### 範例輸出 (簡單規則)

```
✅ 狀態: [完美解析]
提取閾值: 150 (Severity: warning)

--- 1. Tenant Config ---
mysql_global_status_threads_connected: "150"

--- 2. Platform Recording Rule ---
- record: tenant:mysql_global_status_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)

--- 3. Platform Dynamic Alert Rule ---
- alert: MySQLTooManyConnections
  expr: |
    (
      tenant:mysql_global_status_threads_connected:max
      > on(tenant) group_left
      tenant:alert_threshold:mysql_global_status_threads_connected
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
```

> **重要**：工具產出的 metric key 使用原始 exporter 的完整名稱 (如 `mysql_global_status_threads_connected`)。實際部署時，平台團隊應依據命名規範縮短為語義化名稱 (如 `mysql_connections`)。

---

## 4. Step 2 — 選擇聚合模式 (Max vs. Sum)

這是遷移過程中最關鍵的架構決策。每個指標都必須明確選擇聚合模式。

### 最弱環節模式 — `max by(tenant)`

適用於有「單點物理上限」的資源。即使叢集其他節點很閒，**任何一個節點爆滿就是故障**。

典型場景：

| 指標 | 原因 |
|------|------|
| `mysql_global_status_threads_connected` | MariaDB 單節點有 `max_connections` 上限，任一節點連線爆滿即故障 |
| `mysql_slave_status_seconds_behind_master` | Replication lag 看的是最落後的那台 slave |
| `node_filesystem_avail_bytes` | 磁碟空間爆了就是爆了，不能用其他節點的剩餘空間來平均 |

```yaml
- record: tenant:mysql_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)
```

### 叢集總量模式 — `sum by(tenant)`

適用於評估「整體系統負載」。個別節點的絕對值不重要，看的是整個租戶的**聚合效果**。

典型場景：

| 指標 | 原因 |
|------|------|
| `rate(mysql_global_status_slow_queries[5m])` | 慢查詢是分散在各節點，要加總才能看出租戶整體健康 |
| `rate(mysql_global_status_bytes_received[5m])` | 流量要看叢集加總，單節點流量高可能只是負載不均 |
| `container_cpu_usage_seconds_total` | 容器 CPU 使用看叢集總量才有意義 |

```yaml
- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))
```

### 決策矩陣

```
問自己：「一個節點超標，其他節點正常，是否代表有問題？」
  ├── 是 → max by(tenant) (最弱環節)
  └── 否 → sum by(tenant) (叢集總量)
```

---

## 5. 實戰範例：五種遷移場景

以下以 Percona MariaDB Alert Rules 為範本，示範從傳統寫法到新架構的完整遷移路徑。

### 5.1 Scenario A — 基本數值比較 (連線數)

**傳統寫法**：
```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100
  for: 5m
  labels:
    severity: warning
```

**遷移三件套**：

```yaml
# 1. Recording Rule (平台)
- record: tenant:mysql_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)

# 2. Alert Rule (平台) — 注意 group_left + unless maintenance
- alert: MariaDBHighConnections
  expr: |
    (
      tenant:mysql_threads_connected:max
      > on(tenant) group_left
      tenant:alert_threshold:connections
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
  for: 5m
  labels:
    severity: warning

# 3. Tenant Config (租戶)
tenants:
  db-a:
    mysql_connections: "100"
```

### 5.2 Scenario A+ — 多層嚴重度 (Warning + Critical)

**傳統寫法 (兩條規則)**：
```yaml
- alert: MySQLTooManyConnections
  expr: mysql_global_status_threads_connected > 100
  labels: { severity: warning }

- alert: MySQLTooManyConnectionsCritical
  expr: mysql_global_status_threads_connected > 150
  labels: { severity: critical }
```

**遷移後 — 租戶只需寫**：
```yaml
tenants:
  db-a:
    mysql_connections: "100"            # warning 閾值
    mysql_connections_critical: "150"   # _critical 後綴 → 自動產生 critical alert
```

平台的 alert rule 會自動處理降級邏輯：critical 觸發時，warning 被 `unless` 抑制，避免 alert fatigue。

### 5.3 Scenario B — Replication Lag (最弱環節)

**傳統寫法**：
```yaml
- alert: MySQLSlaveReplicationLag
  expr: mysql_slave_status_seconds_behind_master > 30
  for: 5m
  labels: { severity: warning }
```

**遷移三件套**：
```yaml
# Recording Rule — 聚合選擇 max (最弱環節：看最落後的 slave)
- record: tenant:mysql_slave_lag:max
  expr: max by(tenant) (mysql_slave_status_seconds_behind_master)

# Alert Rule
- alert: MariaDBSlaveLag
  expr: |
    (
      tenant:mysql_slave_lag:max
      > on(tenant) group_left
      tenant:alert_threshold:slave_lag
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
  for: 5m

# Tenant Config
tenants:
  db-a:
    mysql_slave_lag: "30"
  db-b:
    mysql_slave_lag: "disable"   # db-b 沒有 replica，停用此 alert
```

### 5.4 Scenario C — Rate 類指標 (慢查詢)

**傳統寫法**：
```yaml
- alert: MySQLHighSlowQueries
  expr: rate(mysql_global_status_slow_queries[5m]) > 0.1
  for: 5m
  labels: { severity: warning }
```

**遷移三件套**：
```yaml
# Recording Rule — 聚合選擇 sum (叢集總量：慢查詢要看整體)
- record: tenant:mysql_slow_queries:rate5m
  expr: sum by(tenant) (rate(mysql_global_status_slow_queries[5m]))

# Alert Rule
- alert: MariaDBHighSlowQueries
  expr: |
    (
      tenant:mysql_slow_queries:rate5m
      > on(tenant) group_left
      tenant:alert_threshold:slow_queries
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
  for: 5m

# Tenant Config
tenants:
  db-a:
    mysql_slow_queries: "0.1"
```

### 5.5 Scenario D — 百分比計算類 (Buffer Pool)

**傳統寫法**：
```yaml
- alert: MySQLInnoDBBufferPoolFull
  expr: |
    mysql_global_status_innodb_buffer_pool_pages_data
    / mysql_global_status_innodb_buffer_pool_pages_total * 100 > 95
  for: 10m
  labels: { severity: warning }
```

**遷移三件套**：
```yaml
# Recording Rule — 百分比計算在此完成，上層只比純數字
- record: tenant:mysql_innodb_buffer_pool:percent
  expr: |
    max by(tenant) (mysql_global_status_innodb_buffer_pool_pages_data)
    /
    max by(tenant) (mysql_global_status_innodb_buffer_pool_pages_total)
    * 100

# Alert Rule
- alert: MariaDBInnoDBBufferPoolHigh
  expr: |
    (
      tenant:mysql_innodb_buffer_pool:percent
      > on(tenant) group_left
      tenant:alert_threshold:innodb_buffer_pool
    )
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
  for: 10m

# Tenant Config
tenants:
  db-a:
    mysql_innodb_buffer_pool: "95"
```

---

## 6. Alertmanager 路由遷移

### 傳統 Routing (基於 instance)

```yaml
route:
  group_by: ['alertname', 'instance']
  routes:
    - matchers: [instance=~"db-a-.*"]
      receiver: "team-a-slack"
    - matchers: [instance=~"db-b-.*"]
      receiver: "team-b-email"
```

### 遷移後 Routing (基於 tenant)

```yaml
route:
  group_by: ['tenant', 'alertname']
  routes:
    - matchers: [tenant="db-a"]
      receiver: "team-a-slack"
      routes:
        - matchers: [severity="critical"]
          receiver: "team-a-pagerduty"
    - matchers: [tenant="db-b"]
      receiver: "team-b-slack"
```

核心差異：以 `tenant` 為第一維度分派，取代散亂的 `instance` regex。支援嵌套路由實現嚴重度分層（warning → Slack, critical → PagerDuty）。

---

## 7. 遷移後驗證

### 7.1 確認閾值正確輸出

```bash
curl -s http://localhost:8080/metrics | grep 'user_threshold{.*connections'
# 預期: user_threshold{...,metric="connections",...} 100
```

### 7.2 確認 Alert 狀態

```bash
python3 scripts/tools/check_alert.py MariaDBHighConnections db-a
# 預期: {"alert": "MariaDBHighConnections", "tenant": "db-a", "state": "inactive"}
```

### 7.3 租戶健康總檢

```bash
python3 scripts/tools/diagnose.py db-a
# 正常: {"status": "healthy", "tenant": "db-a"}
```

### 7.4 驗證 Checklist

- [ ] 每個遷移的 alert 在正常負載下為 `inactive`
- [ ] 刻意觸發至少一條 alert，確認 `firing` → Alertmanager → 通知管道正常
- [ ] 測試三態：修改閾值 → hot-reload 生效 → 設 `disable` → alert 消失
- [ ] 確認 `_critical` 多層嚴重度的降級邏輯 (warning 被 critical 的 `unless` 抑制)
- [ ] Alertmanager routing 以 `tenant` 標籤正確分派

---

## 8. LLM 輔助手動轉換

當 `migrate_rule.py` 遇到無法解析的規則（情境 3），它會自動產出一段可直接交給 LLM 的 Prompt。你也可以用以下 System Prompt 進行批量轉換。

### System Prompt

```
你是一位 SRE 專家，負責將傳統 Prometheus Alert Rules 遷移到「動態多租戶閾值架構」。

在新架構中：
- 所有寫死的門檻值必須抽離成 YAML 鍵值對
- Metric key 格式：<component>_<metric>（如 mysql_connections）
- 多層嚴重度：用 _critical 後綴（如 mysql_connections_critical: "150"）
- 停用：值設為 "disable"

請完成以下工作：
1. 抽取閾值 → threshold-config.yaml 格式
2. 提供正規化 Recording Rule（標註 sum/max 選擇理由）
3. 提供包含 group_left + unless maintenance 的 Alert Rule
4. 標記需要平台額外處理的項目（rate, predict_linear 等）

範例輸出：
---
# Tenant Config
tenants:
  <tenant>:
    mysql_connections: "100"

# Recording Rule (max — 單點上限)
- record: tenant:mysql_threads_connected:max
  expr: max by(tenant) (mysql_global_status_threads_connected)

# Alert Rule
- alert: MariaDBHighConnections
  expr: |
    (tenant:mysql_threads_connected:max > on(tenant) group_left tenant:alert_threshold:connections)
    unless on(tenant) (user_state_filter{filter="maintenance"} == 1)
---

現在，請等待我貼上傳統的 Alert Rules。
```

### 使用流程

1. 設定 System Prompt → 貼上傳統 rules YAML（建議每次 5-10 條）
2. 審核 LLM 輸出：特別注意 metric key 命名是否符合 `<component>_<metric>` 規範
3. 對每個 Recording Rule，確認聚合模式 (max/sum) 是否合理
4. 用 `patch_config.py` 逐一 apply，搭配 `check_alert.py` 驗證

---

## 9. 目錄模式 (Directory Mode)

自 Phase 2C 起，threshold-exporter 支援目錄掃描模式。ConfigMap 從單一 `config.yaml` 拆分為多個 YAML 檔案，更適合 GitOps 工作流。

### 檔案結構

```
conf.d/
  _defaults.yaml     ← 平台管理（底線前綴確保最先載入）
  db-a.yaml           ← 租戶 db-a 的閾值
  db-b.yaml           ← 租戶 db-b 的閾值
```

### 邊界規則

| 內容 | 允許位置 | 違規處理 |
|------|----------|----------|
| `defaults` | 僅 `_defaults.yaml` | 忽略 + WARN log |
| `state_filters` | 僅 `_defaults.yaml` | 忽略 + WARN log |
| `tenants` | 任何檔案 | 深度合併，後讀覆蓋先讀 |

### 向後相容

Exporter 同時支援 `-config`（單檔）和 `-config-dir`（目錄）模式，自動偵測。`patch_config.py` 和 `_lib.sh` 的 `get_cm_value()` 也已支援雙模式。Hot-reload 使用 SHA-256 內容 hash 比對（而非 ModTime），對 K8s ConfigMap 的 symlink rotation 更可靠。

---

## 10. 維度標籤 — 多 DB 類型支援 (Phase 2B)

當平台支援 Redis、Elasticsearch、MongoDB 等多種 DB 類型時，同一個指標可能需要依「維度」設定不同閾值。例如：Redis 的不同 queue、ES 的不同 index、MongoDB 的不同 database。

### 語法

在 ConfigMap 中，使用 `"metric{label=\"value\"}"` 格式的 key：

```yaml
tenants:
  redis-prod:
    redis_queue_length: "1000"                              # 全域預設
    "redis_queue_length{queue=\"order-processing\"}": "100"  # order queue 較嚴格
    "redis_queue_length{queue=\"analytics\"}": "5000"        # analytics 容許較長
    "redis_queue_length{queue=\"temp\"}": "disable"          # 停用 temp queue 監控
```

支援多重 label：

```yaml
    "mongodb_collection_count{database=\"orders\",collection=\"transactions\"}": "10000000"
```

### 設計約束

| 約束 | 說明 |
|------|------|
| **YAML 需加引號** | 含 `{` 的 key 必須用雙引號包裹 |
| **不支援 `_critical` 後綴** | 維度 key 改用 `"value:severity"` 語法，如 `"500:critical"` |
| **Tenant-only** | 維度 key 不繼承 `defaults`，僅允許在租戶設定中使用 |
| **三態仍適用** | 數值=Custom, 省略=Default (僅基本 key), `"disable"`=停用 |

### Severity 指定

維度 key 使用 `"value:severity"` 格式指定嚴重度：

```yaml
    "redis_queue_length{queue=\"orders\"}": "100"           # 預設 warning
    "redis_queue_length{queue=\"orders\"}": "500:critical"  # 明確指定 critical
```

### migrate_rule.py 維度偵測

`migrate_rule.py` 會自動偵測 PromQL 中的 label matcher，並在輸出中提供維度配置建議：

```
📐 偵測到維度標籤 (Dimensional Labels):
   若需為不同維度設定不同閾值，可使用以下 ConfigMap 語法：
   "redis_queue_length{queue="order-processing"}": "500"
```

### 參考範本

`components/threshold-exporter/config/conf.d/examples/` 目錄包含三種 DB 類型的權威範本：

| 檔案 | DB 類型 | 維度範例 |
|------|---------|----------|
| `redis-tenant.yaml` | Redis | queue, db |
| `elasticsearch-tenant.yaml` | Elasticsearch | index, node |
| `mongodb-tenant.yaml` | MongoDB | database, collection |
| `_defaults-multidb.yaml` | 多 DB 全域預設 | (無維度 — defaults 不支援) |

---

## 11. FAQ

### Q: 修改 threshold-config 後多久生效？

Exporter 每 30 秒 reload 一次，K8s ConfigMap propagation 約 1-2 分鐘。從 `kubectl patch` 到 alert 變化，預期 1-3 分鐘。

### Q: 新增一種指標需要改哪些東西？

| 步驟 | 負責人 | 修改檔案 |
|------|--------|----------|
| 1. 新增 Recording Rule | 平台 | `configmap-prometheus.yaml` |
| 2. 新增 Alert Rule | 平台 | `configmap-prometheus.yaml` |
| 3. (可選) 全域預設值 | 平台 | `_defaults.yaml` |
| 4. 設定租戶閾值 | 租戶 | `db-*.yaml` |

租戶不需動任何 PromQL。

### Q: 遷移過渡期可以新舊並存嗎？

可以。新架構的 alert 使用不同 alertname（如 `MariaDBHighConnections` vs 傳統的 `MySQLTooManyConnections`），不會衝突。建議先部署新 alert 觀察，確認行為一致後再移除舊 rules。

### Q: 維度 key 可以設定在 defaults 裡嗎？

不行。維度 key (含 `{}`) 是設計上 tenant-only 的功能。`_defaults.yaml` 只接受基本 key。這是因為維度閾值本質上是高度客製化的 (每個租戶的 queue/index/database 都不同)，全域預設沒有意義。

### Q: 維度 key 怎麼指定 critical？

不使用 `_critical` 後綴 (因為 `metric{label="value"}_critical` 語法會很混亂)。改用 `"value:severity"` 語法：`"redis_queue_length{queue=\"orders\"}": "500:critical"`。

### Q: 如何確認 hot-reload 成功？

```bash
kubectl logs -n monitoring -l app=threshold-exporter --tail=20
# 預期: "Config loaded (directory): X defaults, Y state_filters, Z tenants"
```
