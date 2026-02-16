# CLAUDE.md — AI Agent 接續開發指引

## 專案概述

**Dynamic Alerting Integrations** 是一個基於 Kind (Kubernetes in Docker) 的本地測試環境，用來驗證 **Multi-Tenant Dynamic Alerting** 架構。
設計規格請參考：https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md

## 當前環境狀態（已驗證可運作）

### 叢集架構

```
Kind Cluster: dynamic-alerting-cluster (K8s v1.27.3, 單 control-plane node)
│
├─ namespace: db-a
│  └─ Deployment: mariadb (2 containers, via Helm)
│     ├─ mariadb:11 — port 3306, PVC 1Gi (local-path)
│     └─ prom/mysqld-exporter:v0.15.1 — port 9104 (sidecar)
│
├─ namespace: db-b
│  └─ Deployment: mariadb (同上結構，不同 seed data, via Helm)
│
└─ namespace: monitoring
   ├─ Deployment: prometheus (prom/prometheus:v2.53.0) — port 9090
   ├─ Deployment: grafana (grafana/grafana:11.1.0) — port 3000 (NodePort 30300)
   └─ Deployment: alertmanager (prom/alertmanager:v0.27.0) — port 9093
```

### 已驗證的指標

| Metric | db-a | db-b | 說明 |
|--------|------|------|------|
| `mysql_up` | 1 | 1 | DB 存活狀態 |
| `mysql_global_status_uptime` | ✓ | ✓ | 運行秒數 |
| `mysql_global_status_threads_connected` | ✓ | ✓ | 活躍連線數 |
| `mysql_slave_status_slave_io_running` | 無 | 無 | 未配置 replication（預期） |

### 已驗證的 Alert 流程

- 關閉 db-a 的 MariaDB → K8s liveness probe 偵測失敗 → 容器自動重啟
- Prometheus 偵測到 `mysql_global_status_uptime < 300` → `MariaDBRecentRestart` alert **firing**
- Alert 成功送達 Alertmanager（`[active]` 狀態確認）

## 開發環境

### 使用 Dev Container

1. VS Code → "Reopen in Container"（`.devcontainer/devcontainer.json` 自動配置）
2. 容器內已有：kubectl, helm, kind, docker (Docker-in-Docker)
3. Kind cluster `dynamic-alerting-cluster` 由 `postCreateCommand` 自動建立

### 操作指令 (Makefile)

```bash
make setup          # 部署所有資源 (Helm + Monitoring)
make reset          # 清除重建
make verify         # 驗證 Prometheus 指標
make test-alert     # 觸發 db-a 故障測試 (NS=db-b 可指定)
make status         # 顯示所有 Pod 狀態
make port-forward   # 啟動所有 port-forward
make shell-db-a     # 進入 db-a MariaDB CLI
make clean          # 清除 K8s 資源
make destroy        # 清除 + 刪除 cluster
make helm-template  # 預覽 Helm YAML
make help           # 顯示所有 targets
```

### 存取 UI

```bash
make port-forward
# Prometheus:   http://localhost:9090
# Grafana:      http://localhost:3000 (admin / admin)
# Alertmanager: http://localhost:9093
```

## 部署架構

MariaDB 透過 Helm chart 部署：`helm/mariadb-instance/` chart + `helm/values-db-{a,b}.yaml`。兩個 DB instance 共用 template，僅 seed data 不同。Monitoring stack 使用純 YAML（`k8s/03-monitoring/`）。

## Spec 核心需求（待實作）

參考 spec.md，這個測試環境的最終目標是驗證以下 Dynamic Alerting 模式：

### Scenario A: Dynamic Thresholds（動態閾值）

- Config Metric: `user_threshold{tenant, component, metric, severity}`（統一 gauge）
- threshold-exporter 將使用者設定的閾值暴露為 Prometheus metric
- Recording rules 產生 `tenant:alert_threshold:cpu` / `tenant:alert_threshold:connections`
- Alert rules 使用 `group_left on(tenant)` join normalized metrics 與 thresholds
- **目前狀態**：Recording rules + Alert rules 已就緒，threshold-exporter 待實作

### Scenario B: Weakest Link Detection（最弱環節偵測）

- 監控 Pod 內個別 container 的資源使用
- 保留 container dimension 做聚合
- 當任一 container 超標即觸發
- **目前狀態**：尚未實作

### Scenario C: State/String Matching（狀態字串比對）

- 比對 K8s pod phase（CrashLoopBackOff, ImagePullBackOff 等）
- 用乘法運算做交集邏輯
- **目前狀態**：kube-state-metrics 已部署，提供 pod phase / container status 指標

### Scenario D: Composite Priority Logic（組合優先級邏輯）

- 支援 condition-specific rules + fallback defaults
- 使用 `unless` 排除已匹配條件，`or` 做聯集
- **目前狀態**：尚未實作

## 下一步建議

1. **實作 threshold-exporter**：在獨立 repo 開發 Go HTTP server，暴露 `user_threshold` gauge metric
2. **整合驗證 Scenario A**：部署 threshold-exporter → 設定閾值 → 驗證 recording rules 傳遞 → alert 觸發/解除
3. **擴充 MariaDB 指標**：啟用更多 mysqld_exporter collector（`--collect.perf_schema.*`），為 Scenario B 做準備
4. **實作 Scenario C alert rules**：利用已部署的 kube-state-metrics，建立 pod phase 比對規則

## 技術限制與注意事項

- Kind 是單 node cluster，不支援真實的 node affinity / pod anti-affinity 測試
- PVC 使用 `local-path-provisioner`（Kind 預設），無需額外安裝 CSI driver
- MariaDB 密碼目前寫在 Helm values 的 `stringData`（明文），正式環境應改用 sealed-secrets 或 external-secrets
- Alertmanager 的 webhook receiver 指向 `http://localhost:5001/alerts`（不存在），僅用於測試 routing；正式環境需替換為實際通知端點
- Windows 環境下 Docker Desktop 的記憶體限制可能影響所有 Pod 同時運行，建議分配 ≥ 4GB 給 Docker Desktop

## 檔案結構

```
.
├── .devcontainer/devcontainer.json   # Dev Container 配置
├── .claude/skills/                   # AI Agent skills
│   └── inspect-tenant/              # Tenant 健康檢查
├── components/                       # Sub-component Helm charts
│   ├── threshold-exporter/          # Scenario A (Helm chart)
│   ├── config-api/                  # 待實作
│   └── alert-router/                # 待實作
├── environments/                     # 環境配置分離
│   ├── local/                       # 本地開發 (pullPolicy: Never)
│   └── ci/                          # CI/CD (image registry)
├── helm/
│   ├── mariadb-instance/            # Helm chart (MariaDB + exporter)
│   ├── values-db-a.yaml             # Instance A overrides
│   └── values-db-b.yaml            # Instance B overrides
├── k8s/
│   ├── 00-namespaces/               # Namespace 定義
│   └── 03-monitoring/               # Prometheus + Grafana + Alertmanager + RBAC
├── scripts/
│   ├── _lib.sh                      # 共用函式庫
│   ├── setup.sh                     # 一鍵部署
│   ├── verify.sh                    # 指標驗證
│   ├── test-alert.sh                # 故障測試
│   ├── deploy-kube-state-metrics.sh # kube-state-metrics 部署
│   └── cleanup.sh                   # 清除資源
├── tests/                            # 整合測試
│   ├── scenario-a.sh                # Dynamic Thresholds 測試
│   └── verify-threshold-exporter.sh # Exporter 驗證
├── docs/                             # 文檔
├── Makefile                          # 操作入口 (make help 查看)
├── CLAUDE.md                         # ← 你正在讀的這份
└── README.md
```

## Coding Style

- MariaDB 透過 Helm chart（helm/ 目錄）部署，每個資源獨立一個 template
- Monitoring 使用純 YAML（k8s/03-monitoring/），每個資源獨立一個檔案
- Shell scripts 使用 `set -euo pipefail`，source `_lib.sh` 取得共用函式
- `_lib.sh` 提供跨平台函式：`kill_port`（lsof→fuser→ss fallback）、`url_encode`（python3→sed fallback）、`preflight_check`
- Prometheus scrape config 使用 kubernetes_sd_configs + annotation-based discovery（`prometheus.io/scrape: "true"`）
- 新增 tenant/component 不需要修改 Prometheus ConfigMap
- Makefile targets 對應每個常用操作，`make help` 查看完整列表

## Week 1 更新 (完成)

### 新增功能

1. **模塊化目錄結構**
   - `components/` - Sub-component manifests (threshold-exporter, config-api, alert-router)
   - `environments/` - 環境配置 (local vs ci)
   - `tests/` - 整合測試腳本
   - `.claude/skills/` - AI Agent skills

2. **Component 管理系統**
   ```bash
   make component-build COMP=threshold-exporter   # Build & load to Kind
   make component-deploy COMP=threshold-exporter  # Deploy to cluster
   make component-test COMP=threshold-exporter    # Run integration test
   ```

3. **inspect-tenant Skill**
   - 一鍵檢查 tenant 健康狀態（Pod + DB + Exporter + Metrics）
   - 輸出 JSON 格式供程式化處理
   - 使用: `make inspect-tenant TENANT=db-a`

4. **Prometheus Recording Rules + Normalization Layer**
   - MySQL metrics: `tenant:mysql_cpu_usage:rate5m`, `tenant:mysql_threads_connected:sum`, `tenant:mysql_connection_usage:ratio`
   - Dynamic Thresholds: `tenant:alert_threshold:cpu`, `tenant:alert_threshold:connections`
   - 所有 recording rules 使用 `sum/max/min by(tenant)` 聚合，確保 `group_left on(tenant)` join 正確
   - 統一 threshold metric 名稱為 `user_threshold{tenant, metric, component, severity}`

5. **Prometheus Service Discovery**
   - 從 static_configs 遷移至 kubernetes_sd_configs + annotation-based discovery
   - 新增 RBAC (ServiceAccount + ClusterRole) 讓 Prometheus 能跨 namespace 發現 Service
   - MariaDB Service 加上 `prometheus.io/*` annotations
   - 新增 tenant/component 不需要修改 Prometheus ConfigMap

6. **kube-state-metrics 整合**
   - 提供 K8s 原生指標（pod phase, container status）
   - 支援 Scenario C (State Matching)
   - 部署: `./scripts/deploy-kube-state-metrics.sh`

### 下一步

- **Week 2**: 實作 threshold-exporter (獨立 repo)，Scenario A 端到端驗證
- **Week 3**: Scenario B/C alert rules，引入 Tilt 開發工具
- **Week 4**: Scenario D + 整合測試自動化

詳細說明請參考：[docs/deployment-guide.md](docs/deployment-guide.md)
