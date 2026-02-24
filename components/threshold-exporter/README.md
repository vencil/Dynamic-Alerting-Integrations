# Threshold Exporter

**核心 Component** — 集中式、config-driven 的 Prometheus metric exporter，將使用者設定的動態閾值轉換為 Prometheus metrics，實現 Scenario A–D。

## 架構

- **單一 Pod** 在 monitoring namespace，服務所有 tenant
- **Directory Scanner 模式** (`-config-dir`): ConfigMap 拆分為多檔，掛載至 `/etc/threshold-exporter/conf.d/`，按檔名排序合併
- **三態設計**: custom value / default / disable
- **多層嚴重度**: `"40:critical"` 後綴覆寫 severity
- **Hot-reload**: SHA-256 hash 比對，自動偵測 K8s ConfigMap symlink rotation

> 也支援舊版單檔模式 (`-config`)，但新部署請使用 `-config-dir`。

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
```

## Prometheus 整合

Recording rules 直接透傳 exporter 的 resolved values（無 fallback 邏輯）：

```yaml
- record: tenant:alert_threshold:connections
  expr: sum by(tenant) (user_threshold{metric="connections"})
```

Service Discovery 透過 `prometheus.io/scrape: "true"` annotation 自動發現。

## 開發

```bash
# Build & load to Kind
make component-build COMP=threshold-exporter

# Deploy
make component-deploy COMP=threshold-exporter ENV=local

# Verify
make component-test COMP=threshold-exporter

# View metrics
curl http://localhost:8080/metrics | grep user_threshold

# View resolved config
curl http://localhost:8080/api/v1/config
```

## 修改閾值

**強烈建議使用專案標準工具**，它會自動偵測單檔/多檔模式並安全更新：

```bash
# 修改 db-a 的 mysql_connections 閾值為 50
python3 scripts/tools/patch_config.py db-a mysql_connections 50

# 停用 db-b 的 container_cpu 監控
python3 scripts/tools/patch_config.py db-b container_cpu disable
```

Exporter 會在 reload-interval 內自動載入新設定 (SHA-256 hash 變更觸發)。
