# Threshold Exporter (v2.7.0)

> **核心 Component** — config-driven 的 Prometheus metric exporter，將 YAML 閾值設定轉換為 Prometheus metrics。相比硬寫 PromQL 規則（修改需重啟 Prometheus、規則數隨 tenant × metric 線性增長），threshold-exporter 以 YAML 驅動實現零停機更新、租戶級隔離、固定 O(M) 規則複雜度。
>
> **其他文件：** [README](../../README.md) (概覽) · [Helm Chart](../../helm/threshold-exporter/) (部署用 chart) · [Migration Guide](../../docs/migration-guide.md) (遷移指南) · [Architecture & Design](../../docs/architecture-and-design.md) (技術深度) · [Rule Packs](../../rule-packs/README.md) (規則包目錄)

## 架構

- **HA 架構**: 預設 2 Replicas，具備 PodAntiAffinity 與 PodDisruptionBudget，確保高可用性
- **Directory Scanner 模式** (`-config-dir`): ConfigMap 拆分為多檔，掛載至 `/etc/threshold-exporter/conf.d/`，按檔名排序合併；v2.7.0 支援 **階層式** `conf.d/<domain>/<region>/<tenant>.yaml` (ADR-017)
- **_defaults.yaml 繼承引擎 (v2.7.0, ADR-018)**: L0（頂層 defaults）→ L1（domain）→ L2（region）→ L3（tenant override）四層深合併；array 替換語義 + null-as-delete
- **三態設計**: custom value / default / disable
- **多層嚴重度**: `"40:critical"` 後綴覆寫 severity
- **Hot-reload**: v2.6.x 為 SHA-256 單一 hash；**v2.7.0 升級為 Dual-Hash**——`source_hash`（原始文件）+ `merged_hash`（套完繼承後的 canonical JSON）並行追蹤，僅 merged_hash 變化才觸發 reload
- **300ms Debounce (v2.7.0)**: `time.AfterFunc` + `sync.Mutex` 雙保護吸收 K8s ConfigMap symlink rotation 的連續寫入；可用 `--scan-debounce` flag 調整
- **Cardinality Guard**: 每租戶最多 500 個 metric（超限自動截斷 + ERROR log），防止 TSDB 爆炸
- **Schema Validation**: `ValidateTenantKeys()` 自動偵測 typo 和非法 key，支援保留前綴 (`_state_*`, `_routing*`)
- **SAST 合規**: ReadHeaderTimeout (Gosec G112)、檔案權限 0o600 (CWE-276)

## v2.7.0：conf.d/ 階層 + 繼承引擎 + Dual-Hash 熱重載

**ADR-017 / ADR-018** 把 Directory Scanner 從「扁平多檔」升級為「多層目錄 + `_defaults.yaml` 繼承」，讓 1000+ 租戶場景的配置重複度降到最低，且 ConfigMap symlink rotation 不會觸發假 reload。

**四層繼承 (L0→L3)**：

```
conf.d/
├── _defaults.yaml             # L0 平台預設
├── mysql/
│   ├── _defaults.yaml         # L1 domain 預設
│   ├── us-east/
│   │   ├── _defaults.yaml     # L2 region 預設
│   │   ├── db-a.yaml          # L3 tenant override
│   │   └── db-b.yaml
│   └── ap-northeast/
│       └── db-c.yaml
└── redis/
    ├── _defaults.yaml
    └── cache-1.yaml
```

**合併語義**（ADR-018）：

- **Deep merge**: map 遞迴合併，子鍵各自套用下層覆寫
- **Array 替換**: list 型欄位整包覆寫（不合併），避免繼承鏈造成的意外累加
- **null-as-delete**: 下層把某個鍵設 `null` → 從 effective 結果中刪除，等同「顯式否決上層預設」
- **保留前綴**: `_state_*` / `_routing*` / `_metadata` 只允許出現在 `_` 前綴檔案，不會被 L3 tenant 檔案覆寫

**Dual-Hash 熱重載**：

| Hash | 覆蓋範圍 | 觸發 reload 時機 |
|------|---------|------------------|
| `source_hash` | 原始 YAML 文件內容（未套繼承） | 只做 diff 觀察，不直接觸發 |
| `merged_hash` | 套完 L0→L3 繼承後的 canonical JSON | **變化才 reload** |

這解決了 v2.6.x 的痛點：ConfigMap 被 K8s symlink rotation 修改 mtime 但內容沒變 → v2.6.x 會假 reload；v2.7.0 因為 merged_hash 不變而跳過。v2.8.0 (Issue #61) 將該事件依 effect 拆 `da_config_defaults_change_noop_total`（cosmetic）+ `da_config_defaults_shadowed_total`（shadowed by override）兩個 counter。

**300ms Debounce**：

```
fsnotify event ──┐
                 ├─→ triggerDebouncedReload(reason)
fsnotify event ──┤       │
                 │       ├─ time.AfterFunc(300ms)
fsnotify event ──┘       │
                         └─→ fireDebounced() → single reload
```

5 種 reload reason 被 `da_config_reload_trigger_total{reason}` counter 追蹤：`source` / `defaults` / `new_tenant` / `delete` / `forced`。

**新增 Prometheus Metrics (v2.7.0)**：

| Metric | 類型 | 說明 |
|--------|------|------|
| `da_config_scan_duration_seconds` | Histogram | 每次 fullDirLoad 的耗時分佈 |
| `da_config_reload_trigger_total{reason}` | Counter | 按 reason 分的 reload 觸發次數 |
| `da_config_defaults_change_noop_total` | Counter | merged_hash 未變的 noop 次數（symlink rotation 吸收驗證）。**v2.8.0 (Issue #61) 起 cosmetic-only**；shadowed 移到下方新 counter |
| `da_config_defaults_shadowed_total` | Counter | **v2.8.0** — defaults 變更被 tenant override 擋下（從 noop_total 拆出） |
| `da_config_blast_radius_tenants_affected` | Histogram | **v2.8.0** — 每 tick `(reason, scope, effect)` 受影響 tenant 分佈 |

**相關 CLI (`da-tools` package)**：

- `da-tools describe-tenant <tenant_id> [--conf-d <dir>] [--show-sources]` — 展示 L0→L3 繼承鏈 + effective config
- `da-tools describe-tenant <tenant_id> --what-if <defaults.yaml>` — 模擬 `_defaults.yaml` 變動 → `merged_hash` 對比 + per-key diff
- `da-tools migrate-conf-d --conf-d <dir> [--dry-run|--apply] [--infer-from metadata]` — 扁平 → 階層 automated migration，`git mv` 保留歷史

**Mixed-mode 共存**：舊的扁平 `tenants/*.yaml` 與新的 `conf.d/` 分層可並存於同一次 load，平滑遷移不強制一次切。

## Config 與 Image 分離原則

Helm chart (`values.yaml`) **預設不包含任何測試租戶資料**。`thresholdConfig.tenants` 為空物件 (`{}`)，客戶部署時透過 values-override 或 GitOps 注入自身的租戶設定。

| 來源 | 內容 | 用途 |
|------|------|------|
| `values.yaml` | defaults + state_filters + `tenants: {}` | 生產基底，不帶測試資料 |
| `environments/local/threshold-exporter.yaml` | db-a、db-b 測試租戶 | 開發/測試用 (`make component-deploy ENV=local`) |
| `environments/ci/threshold-exporter.yaml` | `tenants: {}` | CI 環境，依 pipeline 注入 |
| `config/conf.d/` | _defaults + db-a + db-b (標註 DEVELOPMENT EXAMPLE) | Directory Scanner 格式參考範本 |
| `config/conf.d/examples/` | Redis、MongoDB、Elasticsearch 多 DB 維度範本 | 文件參考 |

> **Docker image 只包含 Go binary**，不含任何 config 檔案。Config 完全透過 ConfigMap volume mount 在 runtime 注入。

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

### 租戶 Metadata (v1.11.0)

透過 `_metadata` 區塊為租戶附加營運資訊，支援 Dynamic Runbook Injection：

```yaml
tenants:
  db-a:
    _metadata:
      runbook_url: "https://wiki.example.com/runbooks/{{tenant}}"
      owner: "dba-team"
      tier: "gold"
    mysql_connections: "70"
```

**重要規則**：
- `_metadata` 為保留 key，不產生 `user_threshold` gauge
- Exporter 為每個含 `_metadata` 的 tenant 無條件輸出 `tenant_metadata_info` info metric（值永遠為 1）
- `{{tenant}}` 佔位符在輸出時自動替換為實際 tenant 名稱
- Rule Pack 的 Alert Rules 透過 `group_left(runbook_url, owner, tier) tenant_metadata_info` 將 metadata 注入 alert annotations

### 三態運營模式 (Operational Modes)

租戶可透過保留 key 控制告警行為。三種模式皆支援 `expires` 自動失效。

**Silent Mode** (`_silent_mode`) — 保留 TSDB 紀錄但攔截通知：

```yaml
tenants:
  db-a:
    # 純量格式（向後相容）
    _silent_mode: "warning"           # warning / critical / all / disable

  db-b:
    # 結構化格式（含自動失效）
    _silent_mode:
      target: "all"                   # warning / critical / all
      expires: "2026-04-01T00:00:00Z" # ISO 8601，到期自動解除
      reason: "計畫性維護"
```

**Maintenance Mode** (`_state_maintenance`) — 抑制所有告警：

```yaml
tenants:
  db-a:
    # 結構化格式（含自動失效 + 排程式維護）
    _state_maintenance:
      target: "enable"                # enable / disable（預設 enable）
      expires: "2026-04-01T00:00:00Z" # 到期自動解除
      reason: "資料庫升級"
      recurring:                      # v1.11.0: 排程式維護窗口
        - cron: "0 2 * * 0"          # 每週日 02:00 UTC
          duration: "4h"              # 持續 4 小時
          reason: "Weekly backup"
```

> **排程式維護**：`recurring` 欄位由 Go exporter 儲存但不執行——由 `da-tools maintenance-scheduler` CronJob 在 runtime 讀取 conf.d/ 並建立 Alertmanager silence。

**Severity Dedup** (`_severity_dedup`) — Critical 觸發時自動抑制 Warning 通知：

```yaml
tenants:
  db-a:
    _severity_dedup: true             # 啟用 severity dedup inhibit rule
```

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

# Tenant metadata info — Dynamic Runbook Injection (v1.11.0)
# HELP tenant_metadata_info Tenant metadata labels for group_left join
# TYPE tenant_metadata_info gauge
tenant_metadata_info{tenant="db-a",runbook_url="https://wiki.example.com/runbooks/db-a",owner="dba-team",tier="gold"} 1

# Silent mode — per-tenant 通知靜音 (v1.2.0)
# HELP user_silent_mode Silent mode flag (1=active)
# TYPE user_silent_mode gauge
user_silent_mode{tenant="db-b",target_severity="warning"} 1
user_silent_mode{tenant="db-c",target_severity="warning"} 1
user_silent_mode{tenant="db-c",target_severity="critical"} 1
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

## K8s 部署與配置管理

### 部署 (Helm)

```bash
# 首次安裝 (OCI registry — 推薦)
helm install threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.7.0 \
  -n monitoring --create-namespace \
  -f values-override.yaml

# 升級 (含 config 變更)
helm upgrade threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.7.0 \
  -n monitoring \
  -f values-override.yaml
```

> **已 clone 專案？** 也可指向本地 chart 目錄：
> ```bash
> helm install threshold-exporter ./helm/threshold-exporter \
>   -n monitoring --create-namespace -f values-override.yaml
> ```

Helm chart 會自動建立：Deployment (2 replicas + PDB)、Service (含 Prometheus scrape annotations)、ConfigMap (`threshold-config`)。

### 將 da-tools 產出注入 K8s

`da-tools scaffold` 和 `da-tools migrate` 產出的 tenant config 需注入 `threshold-config` ConfigMap，exporter 才能讀取。有三種方式：

**方式 A (推薦)：Helm values 覆寫**

將產出的 `<tenant>.yaml` 內容合併至 `values.yaml` 的 `thresholdConfig.tenants`，再 `helm upgrade`：

```bash
# 1. da-tools 產出 tenant config
docker run --rm -v $(pwd):/data ghcr.io/vencil/da-tools:v2.7.0 \
  scaffold --tenant db-c --db mariadb,redis --non-interactive -o /data/output

# 2. 將產出的 tenant config 合併至 values override file
#    (手動或用 yq 工具將 output/db-c.yaml 合併至 values-override.yaml)

# 3. Helm upgrade — ConfigMap 自動更新，exporter hot-reload
helm upgrade threshold-exporter \
  oci://ghcr.io/vencil/charts/threshold-exporter --version 2.7.0 \
  -n monitoring -f values-override.yaml
```

**方式 B：kubectl patch ConfigMap**

直接 patch 既有 ConfigMap，不需 Helm：

```bash
# 將 da-tools 產出的 tenant YAML 注入 ConfigMap
kubectl create configmap threshold-config \
  --from-file=_defaults.yaml=conf.d/_defaults.yaml \
  --from-file=db-a.yaml=conf.d/db-a.yaml \
  --from-file=db-c.yaml=output/db-c.yaml \
  -n monitoring --dry-run=client -o yaml | kubectl apply -f -
```

**方式 C：GitOps (生產環境推薦)**

將 `conf.d/` 目錄納入 Git repo，CI/CD pipeline 組裝為 ConfigMap 並 apply。詳見 [GitOps 部署指南](../../docs/integration/gitops-deployment.md)。

> **Hot-reload**：無論哪種方式，ConfigMap 變更後 K8s 會在 1-2 分鐘內 propagate 新內容至 Pod volume，exporter 的 SHA-256 watcher 在下一個 reload-interval (預設 30s) 自動偵測並載入。不需重啟 Pod。

### 驗證部署

```bash
# Pod 狀態
kubectl get pods -n monitoring -l app=threshold-exporter

# 閾值輸出
kubectl port-forward svc/threshold-exporter 8080:8080 -n monitoring &
curl -s http://localhost:8080/metrics | grep user_threshold

# 完整 config (debug)
curl -s http://localhost:8080/api/v1/config | python3 -m json.tool
```

### 在 K8s 內執行 da-tools

當 threshold-exporter 運行在 K8s 叢集內時，da-tools 也可以作為 K8s Job 執行，直接透過 K8s Service 存取 Prometheus，不需 port-forward：

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: check-alert
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: da-tools
          image: ghcr.io/vencil/da-tools:v2.7.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

> **叢集內網路**：da-tools 容器可直接使用 `http://prometheus.monitoring.svc.cluster.local:9090`，無需 `--network=host` 或 port-forward。

---

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
