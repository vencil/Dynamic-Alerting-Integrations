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
make test-alert     # 或 make test-alert NS=db-b

# 5. 存取 UI
make port-forward
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000 (admin/admin)
```

## Makefile Targets

```
make setup          # 部署全部資源 (Helm + Monitoring)
make reset          # 清除後重新部署
make verify         # 驗證 Prometheus 指標
make test-alert     # 觸發 db-a 故障測試 (NS=db-b 可指定)
make status         # 顯示所有 Pod 狀態
make port-forward   # 啟動所有 port-forward
make shell-db-a     # 進入 db-a MariaDB CLI
make clean          # 清除 K8s 資源 (保留 cluster)
make destroy        # 清除資源 + 刪除 cluster
make helm-template  # 預覽 Helm 產生的 YAML
make help           # 顯示所有可用 targets
```

## Project Structure

```
.
├── .devcontainer/
│   └── devcontainer.json       # Dev Container 配置 (Kind + kubectl + helm)
├── helm/
│   ├── mariadb-instance/       # Helm chart: MariaDB + exporter sidecar
│   │   ├── Chart.yaml
│   │   ├── values.yaml         # 預設值
│   │   └── templates/          # deployment, service, pvc, secret, configmaps
│   ├── values-db-a.yaml        # db-a instance overrides
│   └── values-db-b.yaml        # db-b instance overrides (不同 seed data)
├── k8s/
│   ├── 00-namespaces/          # db-a, db-b, monitoring
│   └── 03-monitoring/          # Prometheus, Grafana, Alertmanager
├── scripts/
│   ├── _lib.sh                 # 共用函式庫 (跨平台相容)
│   ├── setup.sh                # 一鍵部署
│   ├── verify.sh               # 驗證 Prometheus 指標
│   ├── test-alert.sh           # 觸發 DB 故障測試 Alert
│   └── cleanup.sh              # 清除所有資源
├── Makefile                    # 操作入口
├── .gitignore
├── CLAUDE.md                   # AI Agent 接續開發指引
└── README.md
```

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
