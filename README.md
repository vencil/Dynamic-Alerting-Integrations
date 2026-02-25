# Dynamic Alerting Integrations

> **Enterprise-grade Multi-Tenant Dynamic Alerting** — Config-driven thresholds, GitOps-ready directory mode, 5 pre-loaded Rule Packs via Projected Volume.

Kubernetes 本地測試環境，用於驗證 **Multi-Tenant Dynamic Alerting** 架構（參見 [spec.md](https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md)）。

基於 **Kind** (Kubernetes in Docker) 搭建，包含兩組 MariaDB 實例 + mysqld_exporter，以及完整的 Prometheus / Grafana / Alertmanager 監控堆疊。

## Architecture

```
Kind Cluster (dynamic-alerting-cluster)
│
├─ namespace: db-a
│  └─ Pod: mariadb:11 + prom/mysqld-exporter (sidecar)
│
├─ namespace: db-b
│  └─ Pod: mariadb:11 + prom/mysqld-exporter (sidecar)
│
└─ namespace: monitoring
   ├─ Prometheus  ─ scrape db-a:9104, db-b:9104
   │  └─ Projected Volume: 5 Rule Pack ConfigMaps → /etc/prometheus/rules/
   ├─ threshold-exporter ─ YAML → Prometheus metrics (Directory Scanner mode)
   ├─ Grafana     ─ MariaDB Overview dashboard
   └─ Alertmanager
```

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Windows/macOS)
- [VS Code](https://code.visualstudio.com/) + [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

## Quick Start

```bash
# 1. Open in VS Code → "Reopen in Container"

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
│       ├── configmap-rules-*.yaml  # 5 個獨立 Rule Pack ConfigMaps
│       └── deployment-prometheus.yaml  # Projected Volume 架構
├── rule-packs/                 # 模組化 Prometheus 規則包 (權威參考)
├── scripts/                    # 操作腳本 (_lib.sh, setup, verify, cleanup...)
│   └── tools/                  # 自動化工具 (patch_config, check_alert, diagnose, migrate_rule, scaffold_tenant)
├── tests/                      # 整合測試 (scenario-a/b/c/d.sh, test-migrate-*.sh, test-scaffold.sh)
├── docs/
│   ├── migration-guide.md      # 完整遷移指南
│   ├── windows-mcp-playbook.md # Dev Container 操作手冊
│   └── testing-playbook.md     # 測試排錯手冊
├── .devcontainer/              # Dev Container 配置
├── Makefile                    # 操作入口 (make help 查看所有 targets)
├── CLAUDE.md                   # AI Agent 開發上下文指引
└── README.md
```

## Migration & Adoption (快速導入指南)

已有傳統 Prometheus 警報？或是新租戶想快速接入？

**[docs/migration-guide.md](docs/migration-guide.md)** 提供 Zero-Friction 遷移路徑：5 個 Rule Pack 已預載、scaffold_tenant.py 互動式產生器、migrate_rule.py 自動轉換工具、五種實戰場景、維度標籤配置範例。

```bash
# 新租戶: 互動式產生 tenant config
python3 scripts/tools/scaffold_tenant.py

# 既有 alert rules: 自動轉換
python3 scripts/tools/migrate_rule.py <your-legacy-rules.yml>

# 端對端示範
make demo
```

## Rule Packs (模組化 Prometheus 規則)

5 個 Rule Pack 透過 Kubernetes **Projected Volume** 預載於 Prometheus 中，各自擁有獨立 ConfigMap，由不同團隊獨立維護：

| Rule Pack | Exporter | Recording Rules | Alert Rules |
|-----------|----------|----------------|-------------|
| **kubernetes** | cAdvisor + kube-state-metrics | 5 | 4 |
| **mariadb** | mysqld_exporter (Percona) | 7 | 8 |
| **redis** | oliver006/redis_exporter | 7 | 6 |
| **mongodb** | percona/mongodb_exporter | 7 | 6 |
| **elasticsearch** | elasticsearch_exporter | 7 | 7 |

未部署 exporter 的 Rule Pack 不會產生 metrics，alert 也不會誤觸發 (near-zero cost)。新增 exporter 後只需配置 `_defaults.yaml` + tenant YAML。

詳見 [rule-packs/README.md](rule-packs/README.md)。

## Alert Rules & Thresholds

Alert rules are **dynamically managed** via the `threshold-exporter` (Directory Scanner mode). Instead of static values, rules are configured via ConfigMap and support:

1. **Dynamic Updates**: Hot-reload via SHA-256 hash comparison.
2. **Three-State Logic**: Custom value / Default / Disable.
3. **Per-Tenant Isolation**: Different thresholds for `db-a` vs `db-b`.
4. **Dimensional Labels**: Per-queue/index/database thresholds for Redis/ES/MongoDB.

See [components/threshold-exporter/README.md](components/threshold-exporter/README.md) for details.

## Key Design Decisions

- **Projected Volume**: 5 個 Rule Pack ConfigMap 透過 projected volume 合併掛載至 `/etc/prometheus/rules/`，各團隊獨立維護、零 PR 衝突。
- **GitOps Directory Mode**: threshold-exporter 使用 `-config-dir` 掃描 `conf.d/`，支援 `_defaults.yaml` + per-tenant YAML 拆分。
- **PVC (not emptyDir)**: MariaDB 資料使用 Kind 內建 StorageClass，Pod 重啟後資料保留。
- **Sidecar pattern**: mysqld_exporter 與 MariaDB 在同一 Pod，透過 `localhost:3306` 連線。
- **Annotation-based SD**: `prometheus.io/scrape: "true"` 自動發現，新增組件不需修改 Prometheus 設定。
- **Cross-platform scripts**: `_lib.sh` 提供跨平台工具函式，所有 script 可在 Linux/macOS/Dev Container 環境運行。

## License

MIT
