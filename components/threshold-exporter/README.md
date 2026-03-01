# Threshold Exporter (v1.0.0)

> **核心 Component** — 集中式、config-driven 的 Prometheus metric exporter，將使用者設定的動態閾值轉換為 Prometheus metrics，實現 Scenario A–D + 多 DB 維度標籤 + regex 維度 + 排程式閾值。
>
> **其他文件：** [README](../../README.md) (概覽) · [Migration Guide](../../docs/migration-guide.md) (遷移指南) · [Architecture & Design](../../docs/architecture-and-design.md) (技術深度) · [Rule Packs](../../rule-packs/README.md) (規則包目錄)

## 架構

- **HA 架構**: 預設 2 Replicas，具備 PodAntiAffinity 與 PodDisruptionBudget，確保高可用性
- **Directory Scanner 模式** (`-config-dir`): ConfigMap 拆分為多檔，掛載至 `/etc/threshold-exporter/conf.d/`，按檔名排序合併
- **三態設計**: custom value / default / disable
- **多層嚴重度**: `"40:critical"` 後綴覆寫 severity
- **Hot-reload**: SHA-256 hash 比對，自動偵測 K8s ConfigMap symlink rotation
- **SAST 合規**: ReadHeaderTimeout (Gosec G112)、檔案權限 0o600 (CWE-276)

## Config 格式 (Directory Mode)

ConfigMap 拆分為 `_defaults.yaml` + 每租戶 `<tenant>.yaml`：

**`_defaults.yaml`** — 平台管理的全域設定 (`defaults` 與 `state_filters` 僅允許出現在 `_` 前綴檔案)：

```yaml
defaults:
  mysql_connections: 80
  mysql_cpu: 80
  container_cpu: 80
  container_memory: 85

state_filters:
  container_crashloop:
    reasons: ["CrashLoopBackOff"]
    severity: "critical"
  maintenance:
    reasons: []
    severity: "info"
    default_state: "disable"
```

**`db-a.yaml`** — 租戶覆寫 (僅允許 `tenants` 區塊)：

```yaml
tenants:
  db-a:
    mysql_connections: "70"
    container_cpu: "70"
    # 維度標籤 (YAML key 需加引號)
    "redis_queue_length{queue='tasks'}": "500"
    "redis_queue_length{queue='events', priority='high'}": "1000:critical"
    "redis_db_keys{db='db0'}": "disable"
```

### 維度標籤 (Dimensional Labels)

支援在 metric key 中指定額外的 Prometheus 標籤，用於 Redis DB、ES Index 等多維度場景：

```yaml
"metric_name{label1='value1', label2='value2'}": "threshold_value"
```

**重要規則**：
- YAML key 包含 `{` 時**必須加引號**
- 維度 key 為 tenant-only，**不繼承** defaults 預設值
- 不支援 `_critical` 後綴，改用 `"value:critical"` 語法覆寫 severity
- Prometheus 輸出會包含額外標籤：`user_threshold{..., queue="tasks", priority="high"} 500`

### Regex 維度標籤

支援在 metric key 中使用 `=~` 運算子指定 regex 匹配模式：

```yaml
"oracle_tablespace{tablespace=~'SYS.*'}": "95"
"oracle_ts{env='prod', tablespace=~'TEMP.*'}": "200"
```

**重要規則**：
- Regex pattern 以 `_re` 後綴 label 輸出：`user_threshold{..., tablespace_re="SYS.*"} 95`
- 實際匹配由 PromQL recording rules 透過 `label_replace` + `=~` 完成
- 可混合使用 exact (`=`) 和 regex (`=~`) label matcher
- Exporter 不進行實際 regex 匹配，僅輸出 pattern

### 排程式閾值 (Scheduled Thresholds)

支援在特定 UTC 時間窗口覆蓋閾值，適用於備份窗口等場景：

```yaml
tenants:
  db-a:
    mysql_connections:                # 結構化格式
      default: "70"
      overrides:
        - window: "01:00-09:00"       # UTC 備份窗口
          value: "1000"               # 提升閾值
        - window: "22:00-06:00"       # UTC 跨午夜窗口
          value: "disable"            # 停用告警
    mysql_cpu: "80"                   # 純量格式 (向後相容)
```

**重要規則**：
- 窗口格式：`HH:MM-HH:MM`（UTC-only），支援跨午夜
- 開始時間 inclusive、結束時間 exclusive
- 多個窗口重疊時，**第一個匹配** 的勝出
- `value` 支援所有現有語法：數值、`disable`、`"70:critical"`
- 純量字串格式完全向後相容，不需修改現有配置

### 邊界規則

| 檔案類型 | 允許的區塊 | 違規行為 |
|----------|-----------|---------|
| `_` 前綴 (`_defaults.yaml`) | `defaults`, `state_filters`, `tenants` | — |
| 租戶檔 (`db-a.yaml`) | 僅 `tenants` | 其他區塊自動忽略 + WARN log |

### 三態行為

| 設定 | 行為 | Prometheus 輸出 |
|------|------|-----------------|
| `"70"` | Custom value | `user_threshold{...} 70` |
| 省略不寫 | Use default | `user_threshold{...} 80` |
| `"disable"` | Disabled | 不產生 metric |

## Endpoints

| Path | 說明 |
|------|------|
| `GET /metrics` | Prometheus metrics (user_threshold gauge) |
| `GET /health` | Liveness probe |
| `GET /ready` | Readiness probe (config loaded?) |
| `GET /api/v1/config` | 查看當前 config 與 resolved thresholds (debug) |

## Metrics 輸出格式

```prometheus
# HELP user_threshold User-defined alerting threshold (config-driven)
# TYPE user_threshold gauge
user_threshold{tenant="db-a",component="mysql",metric="connections",severity="warning"} 70
user_threshold{tenant="db-a",component="mysql",metric="cpu",severity="warning"} 80
user_threshold{tenant="db-b",component="mysql",metric="cpu",severity="critical"} 40
# 維度標籤 — 額外 label 自動附加在標準 label 之後
user_threshold{tenant="redis-prod",component="redis",metric="queue_length",severity="critical",queue="tasks"} 500
user_threshold{tenant="es-prod",component="es",metric="index_store_size_bytes",severity="warning",index="logs-prod"} 107374182400
```

## Prometheus 整合

Recording rules 直接透傳 exporter 的 resolved values（無 fallback 邏輯）：

```yaml
# 基本閾值 — 僅按 tenant 聚合
- record: tenant:alert_threshold:connections
  expr: max by(tenant) (user_threshold{metric="connections"})

# 維度閾值 (exact label) — 必須包含維度 label
- record: tenant:alert_threshold:redis_queue_length
  expr: max by(tenant, queue) (user_threshold{metric="redis_queue_length"})

# Regex 維度閾值 — 透過 label_replace 將 _re pattern 轉為實際匹配
# Step 1: 提取 regex pattern
- record: tenant:alert_threshold:tablespace
  expr: max by(tenant, tablespace_re) (user_threshold{metric="tablespace", tablespace_re!=""})

# Step 2: Alert rule 中使用 =~ 匹配實際值
# oracle_tablespace_usage > on(tenant) group_left()
#   (tenant:alert_threshold:tablespace{tablespace_re=~"<pattern>"})
# 具體實現需根據實際 metric label 結構設計 recording rule chain
```

> **重要**: 當租戶使用維度標籤時，對應的 Recording Rule 與 Alert Rule 都必須在 `by()` / `on()` 中包含該維度 label。詳見 [migration-guide.md §7 平台團隊的 PromQL 適配](../../docs/migration-guide.md#平台團隊的-promql-適配-重要)。

> **排程式閾值**: Recording rules 不需要特別調整。`ScheduledValue` 的時間窗口在每次 scrape 時由 exporter 即時解析，recording rule 自動取得當下有效的閾值。

Service Discovery 透過 `prometheus.io/scrape: "true"` annotation 自動發現。

## 開發

```bash
# Build & load to Kind
make component-build COMP=threshold-exporter

# Deploy
make component-deploy COMP=threshold-exporter ENV=local

# View metrics
curl http://localhost:8080/metrics | grep user_threshold

# View resolved config
curl http://localhost:8080/api/v1/config
```

## 修改閾值

**強烈建議使用專案標準工具**，它會自動偵測單檔/多檔模式並安全更新：

```bash
# 基本閾值
python3 scripts/tools/patch_config.py db-a mysql_connections 50

# 停用指標
python3 scripts/tools/patch_config.py db-b container_cpu disable

# 維度閾值 (key 需加引號)
python3 scripts/tools/patch_config.py redis-prod 'redis_queue_length{queue="tasks"}' 500
python3 scripts/tools/patch_config.py redis-prod 'redis_queue_length{queue="temp"}' disable
```

Exporter 會在 reload-interval 內自動載入新設定 (SHA-256 hash 變更觸發)。

## 權威範本 (Multi-DB Examples)

`config/conf.d/examples/` 目錄提供三種 DB 類型的維度閾值配置範本：

| 檔案 | DB 類型 | 維度範例 |
|------|---------|----------|
| `redis-tenant.yaml` | Redis | queue, db |
| `elasticsearch-tenant.yaml` | Elasticsearch | index, node |
| `mongodb-tenant.yaml` | MongoDB | database, collection |
| `_defaults-multidb.yaml` | 多 DB 全域預設 | (維度 key 不支援 defaults) |
