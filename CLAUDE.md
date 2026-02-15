# CLAUDE.md — AI Agent 接續開發指引

## 專案概述

這是一個基於 Kind (Kubernetes in Docker) 的本地測試環境，用來驗證 **Multi-Tenant Dynamic Alerting** 架構。
設計規格請參考：https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md

## 當前環境狀態（已驗證可運作）

### 叢集架構

```
Kind Cluster: vibe-cluster (K8s v1.27.3, 單 control-plane node)
│
├─ namespace: db-a
│  └─ Deployment: mariadb (2 containers)
│     ├─ mariadb:11 — port 3306, PVC 1Gi (local-path)
│     └─ prom/mysqld-exporter:v0.15.1 — port 9104 (sidecar)
│
├─ namespace: db-b
│  └─ Deployment: mariadb (同上結構，不同 seed data)
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
3. Kind cluster `vibe-cluster` 由 `postCreateCommand` 自動建立

### 操作指令

```bash
./scripts/setup.sh          # 部署所有資源
./scripts/setup.sh --reset  # 清除重建
./scripts/verify.sh         # 驗證 Prometheus 指標
./scripts/test-alert.sh db-a  # 觸發故障測試
./scripts/cleanup.sh        # 清除所有 K8s 資源
```

### 存取 UI

```bash
kubectl port-forward -n monitoring svc/prometheus 9090:9090
kubectl port-forward -n monitoring svc/grafana 3000:3000
kubectl port-forward -n monitoring svc/alertmanager 9093:9093
# Grafana login: admin / admin
```

## Spec 核心需求（待實作）

參考 spec.md，這個測試環境的最終目標是驗證以下 Dynamic Alerting 模式：

### Scenario A: Dynamic Thresholds（動態閾值）

- 需要建立 **Config Metric**：`user_cpu_threshold{user_name, target_component, severity}`
- 透過 pushgateway 或 custom exporter 將使用者設定的閾值推成 Prometheus metric
- 用 `group_left` many-to-one join 讓 alert rule 動態比對閾值
- **目前狀態**：尚未實作，環境已準備好

### Scenario B: Weakest Link Detection（最弱環節偵測）

- 監控 Pod 內個別 container 的資源使用
- 保留 container dimension 做聚合
- 當任一 container 超標即觸發
- **目前狀態**：尚未實作

### Scenario C: State/String Matching（狀態字串比對）

- 比對 K8s pod phase（CrashLoopBackOff, ImagePullBackOff 等）
- 用乘法運算做交集邏輯
- **目前狀態**：尚未實作

### Scenario D: Composite Priority Logic（組合優先級邏輯）

- 支援 condition-specific rules + fallback defaults
- 使用 `unless` 排除已匹配條件，`or` 做聯集
- **目前狀態**：尚未實作

## 下一步建議

1. **實作 Scenario A**：建立一個 pushgateway（或簡單 HTTP exporter）把 user config 轉成 metric，在 Prometheus 寫 recording rule + alert rule 驗證 `group_left` join
2. **擴充 MariaDB 指標**：啟用更多 mysqld_exporter collector（`--collect.perf_schema.*`），為 Scenario B 的 container-level 監控做準備
3. **加入 kube-state-metrics**：部署到 monitoring namespace，提供 pod phase / container status 等 metric，支援 Scenario C
4. **Alert Rule 模板化**：依 spec 的 Normalization Layer + Logic Layer 設計，把 alert rules 從靜態配置改為 recording rule 階層式架構

## 技術限制與注意事項

- Kind 是單 node cluster，不支援真實的 node affinity / pod anti-affinity 測試
- PVC 使用 `local-path-provisioner`（Kind 預設），無需額外安裝 CSI driver
- MariaDB 密碼目前寫在 Secret 的 `stringData`（明文 YAML），正式環境應改用 sealed-secrets 或 external-secrets
- Alertmanager 的 webhook receiver 指向 `http://localhost:5001/alerts`（不存在），僅用於測試 routing；正式環境需替換為實際通知端點
- Windows 環境下 Docker Desktop 的記憶體限制可能影響所有 Pod 同時運行，建議分配 ≥ 4GB 給 Docker Desktop

## 檔案結構

```
.
├── .devcontainer/devcontainer.json   # Dev Container 配置
├── k8s/
│   ├── 00-namespaces/                # Namespace 定義
│   ├── 01-db-a/                      # MariaDB A (Secret, ConfigMap, Deployment, PVC, Service)
│   ├── 02-db-b/                      # MariaDB B (同上)
│   └── 03-monitoring/                # Prometheus + Grafana + Alertmanager
├── scripts/
│   ├── setup.sh                      # 一鍵部署
│   ├── verify.sh                     # 指標驗證
│   ├── test-alert.sh                 # 故障測試
│   └── cleanup.sh                    # 清除資源
├── .env.example                      # 環境變數範本
├── .gitignore
├── CLAUDE.md                         # ← 你正在讀的這份
├── README.md
└── LICENSE
```

## Coding Style

- K8s manifests 使用純 YAML（不用 Helm），每個資源獨立一個檔案
- Shell scripts 使用 `set -euo pipefail`，帶顏色 log 函式
- 所有 MariaDB 相關的 ConfigMap / Secret 依 namespace 隔離，同名但內容獨立
- Prometheus scrape config 使用 static_configs + relabel，不依賴 ServiceMonitor CRD
