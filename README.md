# Vibe K8s Lab

Kubernetes 本地測試環境，用於驗證 **Multi-Tenant Dynamic Alerting** 架構（參見 [spec.md](https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md)）。

基於 **Kind** (Kubernetes in Docker) 搭建，包含兩組 MariaDB 實例 + mysqld_exporter，以及完整的 Prometheus / Grafana / Alertmanager 監控堆疊。

## Architecture

```
Kind Cluster (vibe-cluster)
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
#    (自動安裝 kubectl, helm, kind 並建立 vibe-cluster)

# 2. 部署所有資源
./scripts/setup.sh

# 3. 等待 Prometheus 開始 scraping，驗證指標
sleep 30 && ./scripts/verify.sh

# 4. 測試 Alert — 觸發 db-a 故障
./scripts/test-alert.sh db-a

# 5. 存取 UI (port-forward)
kubectl port-forward -n monitoring svc/prometheus 9090:9090 &
kubectl port-forward -n monitoring svc/grafana 3000:3000 &
# Grafana: http://localhost:3000  (admin / admin)
```

## Project Structure

```
.
├── .devcontainer/
│   └── devcontainer.json       # Dev Container 配置 (Kind + kubectl + helm)
├── k8s/
│   ├── 00-namespaces/          # db-a, db-b, monitoring
│   ├── 01-db-a/                # MariaDB A + mysqld-exporter + PVC
│   ├── 02-db-b/                # MariaDB B + mysqld-exporter + PVC
│   └── 03-monitoring/          # Prometheus, Grafana, Alertmanager
├── scripts/
│   ├── setup.sh                # 一鍵部署 (支援 --reset)
│   ├── verify.sh               # 驗證 Prometheus 指標
│   ├── test-alert.sh           # 觸發 DB 故障測試 Alert
│   └── cleanup.sh              # 清除所有資源
├── .env.example                # 環境變數範本
├── .gitignore
├── CLAUDE.md                   # AI Agent 接續開發指引
└── README.md
```

## Alert Rules

| Alert | Condition | Severity | For |
|---|---|---|---|
| `MariaDBDown` | `mysql_up == 0` | critical | 15s |
| `MariaDBExporterAbsent` | `absent(mysql_up)` | critical | 30s |
| `MariaDBHighConnections` | `threads_connected > 80` | warning | 30s |
| `MariaDBRecentRestart` | `uptime < 300s` | info | 0s |

## Key Design Decisions

- **PVC (not emptyDir)**: MariaDB 資料使用 Kind 內建的 `standard` StorageClass (local-path-provisioner)，資料存在 Docker VM 內部，避免 Windows I/O 效能問題，且 Pod 重啟後資料保留。
- **Sidecar pattern**: mysqld_exporter 與 MariaDB 在同一 Pod，透過 `localhost:3306` 連線，無需額外 Service。
- **Static scrape config**: Prometheus 使用靜態配置而非 ServiceMonitor CRD，簡單易讀、不需安裝 Prometheus Operator。
- **No Helm**: 純 YAML manifests，方便學習和修改。

## Useful Commands

```bash
# 重新部署（清除後再建立）
./scripts/setup.sh --reset

# 查看所有 Pod
kubectl get pods -A

# 查看 MariaDB 日誌
kubectl logs -n db-a -l app=mariadb -c mariadb

# 進入 MariaDB CLI
kubectl exec -it -n db-a deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw

# 查看 Prometheus targets
kubectl port-forward -n monitoring svc/prometheus 9090:9090
# → http://localhost:9090/targets

# 刪除整個環境
./scripts/cleanup.sh
kind delete cluster --name vibe-cluster
```

## License

MIT
