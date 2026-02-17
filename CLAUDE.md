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
   ├─ Deployment: alertmanager (prom/alertmanager:v0.27.0) — port 9093
   └─ Deployment: threshold-exporter — port 8080 (config-driven)
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
make setup              # 部署所有資源 (Helm + Monitoring)
make reset              # 清除重建
make verify             # 驗證 Prometheus 指標
make test-alert         # 觸發 db-a 故障測試 (NS=db-b 可指定)
make test-scenario-a    # Scenario A 端到端測試 (TENANT=db-a)
make test-scenario-b    # Scenario B 端到端測試 (TENANT=db-a)
make test-scenario-c    # Scenario C 端到端測試 (TENANT=db-a)
make status             # 顯示所有 Pod 狀態
make port-forward       # 啟動所有 port-forward (含 threshold-exporter)
make shell-db-a         # 進入 db-a MariaDB CLI
make clean              # 清除 K8s 資源
make destroy            # 清除 + 刪除 cluster
make helm-template      # 預覽 Helm YAML
make help               # 顯示所有 targets

# Component 管理
make component-build COMP=threshold-exporter   # Build & load to Kind
make component-deploy COMP=threshold-exporter  # Deploy to cluster
make component-test COMP=threshold-exporter    # Run integration test
```

### 存取 UI

```bash
make port-forward
# Prometheus:          http://localhost:9090
# Grafana:             http://localhost:3000 (admin / admin)
# Alertmanager:        http://localhost:9093
# Threshold-Exporter:  http://localhost:8080/metrics
```

## 部署架構

MariaDB 透過 Helm chart 部署：`helm/mariadb-instance/` chart + `helm/values-db-{a,b}.yaml`。兩個 DB instance 共用 template，僅 seed data 不同。Monitoring stack 使用純 YAML（`k8s/03-monitoring/`）。

## Threshold-Exporter 架構設計

### 核心設計決策

threshold-exporter 是一個**集中式、config-driven**的 Prometheus metric exporter：

1. **單一 Pod**：部署在 monitoring namespace，不跟隨 tenant 擴增。一個 Pod 服務所有 tenant 的閾值。
2. **YAML config 驅動**：透過 ConfigMap 掛載 YAML 檔，定義 defaults 和 per-tenant overrides。不使用 HTTP API 寫入。
3. **三態設計** (per tenant, per metric)：
   - **Custom value** — 明確設定數值（例：`"70"`）
   - **Default** — 省略不寫，自動套用 `defaults` 區塊的值
   - **Disable** — 設為 `"disable"`，不暴露該 metric，alert 不觸發
4. **Config hot-reload**：exporter 定期檢查 config 檔變更，不需要重啟 Pod。
5. **Default resolution 在 exporter 層**：Prometheus recording rules 不需要 fallback 邏輯。

### Config 格式

```yaml
# components/threshold-exporter/config/threshold-config.yaml
defaults:
  mysql_connections: 80     # Scenario A
  mysql_cpu: 80             # Scenario A
  # container_cpu_percent: 90   # Scenario B (未來)

tenants:
  db-a:
    mysql_connections: "70"       # custom
    # mysql_cpu 省略               # → default 80
  db-b:
    mysql_connections: "disable"  # disabled → no alert
    mysql_cpu: "40"              # custom
```

### Metric key 命名規則

`<component>_<metric>` → 暴露為 `user_threshold{tenant="...", metric="...", component="...", severity="..."}`

例：`mysql_connections` → `component="mysql"`, `metric="connections"`

### 修改閾值的工作流

```
修改 YAML → kubectl apply ConfigMap → exporter reload → Prometheus scrape → recording rule 更新 → alert 觸發/解除
```

不需要重啟任何 Pod。`helm upgrade` 或直接 `kubectl apply` ConfigMap 都可以。

### 為何選擇這個架構

- **不做 per-tenant sidecar**：Pod 數量不隨 tenant 線性增長，避免撞到 node 的 Pod 上限。
- **不做 HTTP API 寫入**：config 即代碼，可版控、可 review、可 GitOps。
- **不做 Prometheus fallback**：exporter 已 resolve 所有 default，recording rules 純粹 pass-through，PromQL 更簡潔。
- **Disable 透過 metric 缺席實現**：`group_left` join 對不存在的 metric 自然產生空結果 = 不觸發 alert。

## Spec 核心需求

參考 spec.md，這個測試環境的最終目標是驗證以下 Dynamic Alerting 模式：

### Scenario A: Dynamic Thresholds（動態閾值）✅ Week 2 實作完成

- Config Metric: `user_threshold{tenant, component, metric, severity}`（統一 gauge）
- threshold-exporter 讀取 ConfigMap YAML，resolve 三態邏輯，暴露 Prometheus metric
- Recording rules 透傳 `tenant:alert_threshold:cpu` / `tenant:alert_threshold:connections`（無 fallback）
- Alert rules 使用 `group_left on(tenant)` join normalized metrics 與 thresholds
- **目前狀態**：Go 實作完成，Helm chart + ConfigMap + tests 就緒

### Scenario B: Weakest Link Detection（最弱環節偵測）✅ Week 3 實作完成

- 監控 Pod 內個別 container 的資源使用（cAdvisor metrics via kubelet API proxy）
- Container CPU/memory 計算為 limit 的百分比
- `max by(tenant, pod)` 取最弱環節 — 任一 container 超標即觸發
- 閾值透過同一個 threshold-exporter 管理（`container_cpu` / `container_memory` keys）
- Recording rules: `tenant:container_cpu_percent:by_container` → `tenant:pod_weakest_cpu_percent:max`
- Alert rules: `PodContainerHighCPU` / `PodContainerHighMemory`
- **目前狀態**：Prometheus rules 完成，kubelet-cadvisor scrape job 已配置，需 deploy 後驗證

### Scenario C: State/String Matching（狀態字串比對）✅ Week 3 實作完成

- 比對 K8s container waiting reason（CrashLoopBackOff, ImagePullBackOff 等）
- 乘法運算做交集邏輯：`count * flag > 0`（flag absent = disabled = no alert）
- Config 新增 `state_filters:` section + per-tenant `_state_<filter>: "disable"` 覆蓋
- 新 metric: `user_state_filter{tenant, filter, severity}` = 1.0（flag gauge）
- Recording rule: `tenant:container_waiting_reason:count` (uses kube-state-metrics)
- Alert rules: `ContainerCrashLoop` / `ContainerImagePullFailure`
- **目前狀態**：Go 實作完成（config.go + collector.go + unit tests），Prometheus rules 就緒

### Scenario D: Composite Priority Logic（組合優先級邏輯）

- 支援 condition-specific rules + fallback defaults
- 使用 `unless` 排除已匹配條件，`or` 做聯集
- **目前狀態**：尚未實作

## 下一步

- **Week 4**: Scenario D (Composite Priority) + 整合測試自動化 + Tilt 引入

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
│   └── threshold-exporter/          # Scenario A
│       ├── Chart.yaml               # Helm chart metadata
│       ├── values.yaml              # Default values (含 thresholdConfig)
│       ├── templates/               # K8s templates (deployment, service, configmap)
│       ├── config/                  # Reference config files
│       │   └── threshold-config.yaml
│       └── app/                     # Go source code
│           ├── main.go              # HTTP server + entrypoint
│           ├── config.go            # YAML config loader + three-state resolver
│           ├── collector.go         # Prometheus collector
│           ├── config_test.go       # Unit tests
│           ├── Dockerfile           # Multi-stage build
│           └── go.mod
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
│   ├── scenario-a.sh                # Dynamic Thresholds 端到端測試
│   ├── scenario-b.sh                # Weakest Link Detection 端到端測試
│   ├── scenario-c.sh                # State/String Matching 端到端測試
│   └── verify-threshold-exporter.sh # Exporter 功能驗證
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
- Go code 使用標準 `flag` + `os.Getenv` 配置，`gopkg.in/yaml.v3` 解析 config
- Makefile targets 對應每個常用操作，`make help` 查看完整列表

## Week 1 更新 (完成)

### 新增功能

1. **模塊化目錄結構**
   - `components/` - Sub-component manifests (threshold-exporter)
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

## Week 2 更新 (完成)

### threshold-exporter 實作

1. **Go 應用程式** (`components/threshold-exporter/app/`)
   - `config.go` — YAML config loader + 三態解析 (custom/default/disable)
   - `collector.go` — Prometheus Collector，每次 scrape 即時 resolve config
   - `main.go` — HTTP server (/metrics, /health, /ready, /api/v1/config)
   - `config_test.go` — 完整 unit tests 覆蓋三態邏輯
   - `Dockerfile` — Multi-stage build (golang:1.21-alpine → alpine:3.19)

2. **Helm chart 重構**
   - 新增 `templates/configmap.yaml` — threshold config 透過 ConfigMap 掛載
   - Deployment 使用 `checksum/config` annotation 確保 config 變更時自動重啟
   - 移除舊的 HTTP API 模式（config.logLevel, config.storage），改為 exporter.* 配置

3. **Prometheus recording rules 簡化**
   - 移除 `or (max by(tenant) (mysql_up) * 80)` fallback 邏輯
   - Recording rules 現在純粹 pass-through exporter 的 resolved values
   - Default resolution 完全在 exporter 層完成

4. **測試腳本更新**
   - `tests/scenario-a.sh` — 改用 `kubectl apply` ConfigMap 動態修改閾值（不再用 HTTP POST）
   - `tests/verify-threshold-exporter.sh` — 驗證三態邏輯、config reload、metrics 暴露

5. **Makefile 更新**
   - `make test-scenario-a` — Scenario A 端到端測試
   - `make component-build` — 支援 in-repo build（`components/*/app/`）
   - `make port-forward` — 含 threshold-exporter (8080)

### 下一步

- **Week 3**: Scenario B (Weakest Link) + Scenario C (State Matching) ← 已完成

## Week 3 更新 (完成)

### Scenario C: State/String Matching 實作

1. **Config 擴充** (`config.go`)
   - 新增 `StateFilter` struct：`Reasons []string` + `Severity string`
   - `ThresholdConfig` 新增 `StateFilters map[string]StateFilter` 欄位
   - 新方法 `ResolveStateFilters()` — 為每個 tenant × filter 產生 flag
   - Per-tenant disable: tenants map 中 `_state_<filter_name>: "disable"`
   - 共用 `isDisabled()` 函式（提取原有的 disable 判斷邏輯）

2. **Collector 擴充** (`collector.go`)
   - 新增 `user_state_filter{tenant, filter, severity}` metric descriptor
   - `Collect()` 同時呼叫 `cfg.Resolve()` 和 `cfg.ResolveStateFilters()`
   - Disabled filters → no metric exposed（與 numeric disable 相同模式）

3. **Config 格式**
   ```yaml
   state_filters:
     container_crashloop:
       reasons: ["CrashLoopBackOff"]
       severity: "critical"
     container_imagepull:
       reasons: ["ImagePullBackOff", "InvalidImageName"]
       severity: "warning"
   tenants:
     db-b:
       _state_container_crashloop: "disable"
   ```

4. **Prometheus Rules**
   - Recording rule: `tenant:container_waiting_reason:count` (uses `label_replace` for namespace→tenant)
   - Alert: `ContainerCrashLoop` — `count * user_state_filter > 0`（乘法模式）
   - Alert: `ContainerImagePullFailure` — 同上

5. **測試**
   - 7 個新 unit tests 覆蓋 state filter 解析、disable variants、backward compat
   - `tests/scenario-c.sh` — E2E 測試（觸發 ImagePullBackOff → alert fires → disable filter → alert resolves）

### Scenario B: Weakest Link Detection 實作

1. **kubelet cAdvisor 整合**
   - 新增 `kubelet-cadvisor` scrape job（使用 K8s API proxy 避免 Kind TLS 問題）
   - `metric_relabel_configs` 過濾只保留 tenant namespace 的 container metrics
   - RBAC 新增 `nodes/proxy` 權限

2. **Container 閾值**
   - `defaults` 新增 `container_cpu: 80` / `container_memory: 85`
   - Per-tenant override: `container_cpu: "70"` 等
   - `parseMetricKey("container_cpu")` → `component="container"`, `metric="cpu"`

3. **Prometheus Rules**
   - Recording: `tenant:container_cpu_percent:by_container` — 每 container CPU 使用率（% of limit）
   - Recording: `tenant:container_memory_percent:by_container` — 同上 memory
   - Recording: `tenant:pod_weakest_cpu_percent:max` — `max by(tenant, pod)` 取最弱環節
   - Threshold: `tenant:alert_threshold:container_cpu` / `container_memory`（pass-through exporter）
   - Alert: `PodContainerHighCPU` / `PodContainerHighMemory` — 用 `group_left` join

4. **測試**
   - `tests/scenario-b.sh` — E2E 測試（驗證 cAdvisor metrics → 設定低閾值 → alert fires → 恢復）
   - 支援 partial test mode（cAdvisor 不可用時只驗證 threshold metrics）

### Makefile 更新

- `make test-scenario-b` — Scenario B 端到端測試
- `make test-scenario-c` — Scenario C 端到端測試

### 下一步

- **Week 4**: Scenario D (Composite Priority) + 整合測試自動化 + Tilt 引入

詳細說明請參考：[docs/deployment-guide.md](docs/deployment-guide.md)
