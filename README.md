# Dynamic Alerting Integrations

Kubernetes 本地測試環境，用於驗證 **Multi-Tenant Dynamic Alerting** 架構（參見 [spec.md](https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md)）。

基於 **Kind** (Kubernetes in Docker) 搭建，包含兩組 MariaDB 實例 + mysqld_exporter，以及完整的 Prometheus / Grafana / Alertmanager 監控堆疊。

## Architecture

```
Kind Cluster (dynamic-alerting-cluster)
│
├─ namespace: db-a
│  └─ Pod: mariadb:11 + prom/mysqld-exporter (sidecar)
│     └─ PVC: 1Gi (local-path, Docker VM 內部)
│
├─ namespace: db-b
│  └─ Pod: mariadb:11 + prom/mysqld-exporter (sidecar)
│     └─ PVC: 1Gi (local-path, Docker VM 內部)
│
└─ namespace: monitoring
   ├─ Prometheus  ─ scrape db-a:9104, db-b:9104
   ├─ Grafana     ─ MariaDB Overview dashboard
   └─ Alertmanager
```

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS)
- [VS Code](https://code.visualstudio.com/) + [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

## Quick Start

```bash
# 1. Open in VS Code → "Reopen in Container"
#    (自動安裝 kubectl, helm, kind 並建立 dynamic-alerting-cluster)

# 2. 一鍵部署
make setup

# 3. 驗證指標
make verify

# 4. 測試 Alert
make test-alert     # 或 make test-alert TENANT=db-b

# 5. 存取 UI
make port-forward
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000 (admin/admin)
```

## Makefile Targets

```
make setup              # 部署全部資源 (Kind cluster + DB + Monitoring)
make reset              # 清除後重新部署
make verify             # 驗證 Prometheus 指標抓取
make test-alert         # 觸發故障測試 (TENANT=db-b 可指定)
make test-scenario-a    # Scenario A: 動態閾值
make test-scenario-b    # Scenario B: 弱環節檢測
make test-scenario-c    # Scenario C: 狀態字串比對
make demo               # 端對端示範 (scaffold + migrate + diagnose)
make component-build    # Build component image (COMP=threshold-exporter)
make component-deploy   # Deploy component (COMP=threshold-exporter ENV=local)
make status             # 顯示所有 Pod 狀態
make port-forward       # 啟動所有 port-forward
make shell              # 進入 DB CLI (TENANT=db-a)
make inspect-tenant     # AI Agent: 檢查 Tenant 健康 (TENANT=db-a)
make clean              # 清除 K8s 資源 (保留 cluster)
make destroy            # 清除資源 + 刪除 cluster
make help               # 顯示所有可用 targets
```

## Project Structure

```
.
├── components/
│   ├── threshold-exporter/     # 動態閾值 exporter (Helm chart + Go app)
│   └── (kube-state-metrics 已整合至 k8s/03-monitoring/)
├── environments/
│   ├── local/                  # 本地開發 Helm values
│   └── ci/                     # CI/CD Helm values
├── helm/
│   └── mariadb-instance/       # Helm chart: MariaDB + exporter sidecar
├── k8s/
│   ├── 00-namespaces/          # db-a, db-b, monitoring
│   └── 03-monitoring/          # Prometheus, Grafana, Alertmanager
├── rule-packs/                 # 模組化 Prometheus 規則包
│   ├── rule-pack-kubernetes.yaml   # cAdvisor + KSM (預設啟用)
│   ├── rule-pack-mariadb.yaml      # mysqld_exporter (預設啟用)
│   ├── rule-pack-redis.yaml        # Redis (選配)
│   ├── rule-pack-mongodb.yaml      # MongoDB (選配)
│   ├── rule-pack-elasticsearch.yaml # Elasticsearch (選配)
│   └── README.md
├── scripts/                    # 操作腳本 (_lib.sh, setup, verify, cleanup...)
│   └── tools/                  # 自動化工具 (patch_config, check_alert, diagnose, migrate_rule)
├── tests/                      # 整合測試 (scenario-a/b/c/d.sh, test-migrate-*.sh)
├── .devcontainer/              # Dev Container 配置
├── Makefile                    # 操作入口 (make help 查看所有 targets)
├── CLAUDE.md                   # AI Agent 開發上下文指引
└── README.md
```

## Migration & Adoption (快速導入指南)

已經有傳統 Prometheus 警報想遷移？或者是新租戶想快速接入？

**[docs/migration-guide.md](docs/migration-guide.md)** 提供完整的遷移路徑，包含：自動轉換工具 (`migrate_rule.py`)、五種 Percona MariaDB 實戰場景、Recording Rule 聚合模式選擇指南、多 DB 維度標籤 (Redis/ES/MongoDB) 配置範例，以及 threshold-exporter 部署教學。

```bash
# 新租戶: 互動式產生 tenant config
python3 scripts/tools/scaffold_tenant.py

# 既有 alert rules: 自動轉換工具
python3 scripts/tools/migrate_rule.py <your-legacy-rules.yml>

# 端對端示範
make demo
```

## Rule Packs (模組化 Prometheus 規則)

預先建置的 Normalization + Alert Rules 模組包，掛載即可使用：

| Rule Pack | Exporter | 狀態 | Recording Rules | Alert Rules |
|-----------|----------|------|----------------|-------------|
| **kubernetes** | cAdvisor + kube-state-metrics | 預設啟用 | 5 | 4 |
| **mariadb** | mysqld_exporter (Percona) | 預設啟用 | 7 | 8 |
| **redis** | oliver006/redis_exporter | 選配 | 7 | 6 |
| **mongodb** | percona/mongodb_exporter | 選配 | 7 | 6 |
| **elasticsearch** | elasticsearch_exporter | 選配 | 7 | 7 |

```bash
# 啟用選配 rule pack (Helm values overlay)
helm upgrade --install threshold-exporter ./components/threshold-exporter \
  -n monitoring \
  -f environments/local/threshold-exporter.yaml \
  -f rule-packs/rule-pack-redis.yaml
```

詳見 [rule-packs/README.md](rule-packs/README.md)。

## Alert Rules & Thresholds

Alert rules are now **dynamically managed** via the `threshold-exporter`.
Instead of static values, rules are configured via ConfigMap and support:

1.  **Dynamic Updates**: Hot-reload without restarting pods.
2.  **Three-State Logic**: Custom value / Default / Disable.
3.  **Per-Tenant Isolation**: Different thresholds for `db-a` vs `db-b`.

See [components/threshold-exporter/README.md](components/threshold-exporter/README.md) for configuration details.

## Key Design Decisions

- **PVC (not emptyDir)**: MariaDB 資料使用 Kind 內建的 `standard` StorageClass (local-path-provisioner)，資料存在 Docker VM 內部，避免 Windows I/O 效能問題，且 Pod 重啟後資料保留。
- **Sidecar pattern**: mysqld_exporter 與 MariaDB 在同一 Pod，透過 `localhost:3306` 連線，無需額外 Service。
- **Static scrape config**: Prometheus 使用靜態配置而非 ServiceMonitor CRD，簡單易讀、不需安裝 Prometheus Operator。
- **Helm chart**: 兩組 DB instance 共用一個 chart template，僅透過 values 檔區分 seed data，消除重複 YAML。
- **Cross-platform scripts**: `_lib.sh` 提供跨平台工具函式 (kill_port/url_encode fallback)，所有 script 可在 Linux/macOS/Dev Container 環境運行。

## License

MIT
