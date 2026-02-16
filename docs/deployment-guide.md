# Dynamic Alerting Integrations — 部署指南

## 概述

本專案已完成 Week 1 重構，新增以下功能：
- ✅ 專案重命名為 `dynamic-alerting-integrations`
- ✅ 建立模塊化目錄結構 (components/, environments/, tests/, .claude/skills/)
- ✅ 新增 `inspect-tenant` skill（AI Agent 檢查工具）
- ✅ 更新 Prometheus config 加入 Recording Rules
- ✅ 準備 kube-state-metrics 部署腳本
- ✅ 準備 threshold-exporter 配置模板

## 快速開始

### 1. 進入 Dev Container

```bash
# 在 VS Code 中打開專案
code .

# 按 F1 → 選擇 "Dev Containers: Reopen in Container"
# 等待容器啟動 (會自動建立 dynamic-alerting-cluster)
```

### 2. 部署基礎環境

```bash
# 一鍵部署 (MariaDB + Monitoring)
make setup

# 驗證部署
make status

# 啟動 port-forward
make port-forward
```

### 3. 部署 kube-state-metrics

```bash
./scripts/deploy-kube-state-metrics.sh

# 或手動部署
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
```

### 4. 驗證功能

```bash
# 驗證 Prometheus 指標
make verify

# 測試 inspect-tenant skill
make inspect-tenant TENANT=db-a

# 查看所有 components
make component-list
```

## 新功能說明

### Component 管理

用於開發和部署 sub-components (threshold-exporter, config-api 等)。

```bash
# 1. Build component image (從 ../threshold-exporter)
make component-build COMP=threshold-exporter

# 2. 部署到 Kind cluster
make component-deploy COMP=threshold-exporter ENV=local

# 3. 查看日誌
make component-logs COMP=threshold-exporter

# 4. 執行整合測試
make component-test COMP=threshold-exporter
```

### Skills (AI Agent 工具)

#### inspect-tenant

檢查 tenant 的完整健康狀態：

```bash
make inspect-tenant TENANT=db-a
```

輸出範例：
```
=== Checking Tenant: db-a ===
✓ Pod Status: Running
✓ Database: Healthy
✓ Exporter: Up (mysql_up=1)
✓ Metrics: uptime=3600s, connections=5
✓ No recent errors

=== JSON Output ===
{
  "tenant": "db-a",
  "pod_status": "Running",
  "db_healthy": true,
  "exporter_healthy": "1",
  "metrics": {...}
}
```

### Recording Rules

Prometheus 現已包含 Normalization Layer：

```yaml
# 標準化 metrics
tenant:mysql_cpu_usage:rate5m
tenant:mysql_connection_usage:ratio
tenant:mysql_uptime:hours

# 動態閾值（預設值）
tenant:alert_threshold:cpu
tenant:alert_threshold:connections
```

查詢範例：
```promql
# 查看標準化的 CPU 使用率
tenant:mysql_cpu_usage:rate5m{tenant="db-a"}

# 查看動態閾值（目前使用預設值 80）
tenant:alert_threshold:connections{tenant="db-a"}
```

## 目錄結構

```
dynamic-alerting-integrations/
├── components/                    # Sub-component manifests
│   ├── threshold-exporter/        # 動態閾值 exporter (待實作)
│   ├── config-api/                # 配置 API (待實作)
│   ├── alert-router/              # Alert 路由 (待實作)
│   └── kube-state-metrics/        # K8s 狀態 metrics
│
├── environments/                  # 環境配置
│   ├── local/                     # 本地開發 (使用 :dev images)
│   │   └── threshold-exporter.yaml
│   └── ci/                        # CI/CD (使用 registry images)
│       └── threshold-exporter.yaml
│
├── tests/                         # 整合測試
│   └── (待建立 scenario-a.sh)
│
├── .claude/skills/                # AI Agent skills
│   └── inspect-tenant/
│       ├── SKILL.md
│       └── scripts/inspect.sh
│
├── helm/                          # Helm charts
│   └── mariadb-instance/
│
├── k8s/                           # K8s manifests
│   ├── 00-namespaces/
│   └── 03-monitoring/
│       └── configmap-prometheus.yaml  # 已更新 (recording rules)
│
└── scripts/                       # 操作腳本
    ├── deploy-kube-state-metrics.sh
    └── ...
```

## 已實作的改進

### 1. Prometheus Config 增強

**變更**：
- ✅ 新增 `tenant` label 到所有 scrape configs
- ✅ 新增 kube-state-metrics scrape config
- ✅ 新增 threshold-exporter scrape config（預留）
- ✅ 新增 Recording Rules (Normalization Layer)
- ✅ 更新 Alert Rules 使用動態閾值

**影響**：
- Alert rules 現在支援 `group_left` join
- 為 Scenario A (Dynamic Thresholds) 做好準備

### 2. Makefile 增強

**新增 Targets**：
```makefile
make component-build        # Build component image
make component-deploy       # Deploy component
make component-test         # Run integration test
make component-logs         # View logs
make component-list         # List all components
make inspect-tenant         # Run inspect-tenant skill
```

### 3. 環境分離

**local/** (開發環境)：
- 使用 `image: threshold-exporter:dev`
- `pullPolicy: Never` (使用 kind load 的本地 image)
- 記憶體內 storage
- Debug log level

**ci/** (CI/CD 環境)：
- 使用 `image: ghcr.io/vencil/threshold-exporter:v0.1.0`
- `pullPolicy: IfNotPresent`
- Redis storage
- Info log level
- 2 replicas + health checks

## 下一步

### Week 2-3: 實作 threshold-exporter

```bash
# 1. 建立獨立 repo
cd ~/projects
git clone https://github.com/vencil/threshold-exporter
cd threshold-exporter

# 2. 實作 Go exporter
# - HTTP API: POST /api/v1/threshold
# - Prometheus endpoint: GET /metrics

# 3. 本地測試
cd ~/projects/dynamic-alerting-integrations
make component-build COMP=threshold-exporter
make component-deploy COMP=threshold-exporter
```

### Week 4: Scenario A 驗證

```bash
# 1. 建立測試腳本
cat > tests/scenario-a.sh <<'EOF'
#!/bin/bash
# 測試動態閾值功能

# 1. 設定閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a","metric":"connections","value":70}'

# 2. 等待 Prometheus scrape
sleep 30

# 3. 製造高負載
# ...

# 4. 驗證 Alert firing
# ...

# 5. 調整閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a","metric":"connections","value":90}'

# 6. 驗證 Alert 解除
# ...
EOF

# 2. 執行測試
make component-test COMP=threshold-exporter
```

## 疑難排解

### 問題: Cluster 名稱錯誤

```bash
# 如果 Kind cluster 名稱還是 vibe-cluster
kind delete cluster --name vibe-cluster
kind create cluster --name dynamic-alerting-cluster

# 重新設定 kubeconfig
kind get kubeconfig --name dynamic-alerting-cluster > ~/.kube/config
```

### 問題: Component 找不到

```bash
# 確認 component repo 位置
ls -la ../threshold-exporter

# 如果不存在，建立 placeholder
mkdir -p ../threshold-exporter
cat > ../threshold-exporter/Dockerfile <<'EOF'
FROM busybox
CMD ["sleep", "3600"]
EOF
```

### 問題: Port-forward 失敗

```bash
# 殺掉佔用的 process
lsof -ti :9090 | xargs kill -9
lsof -ti :3000 | xargs kill -9

# 重新啟動
make port-forward
```

## 驗證清單

部署完成後，請確認：

- [ ] Kind cluster 名稱為 `dynamic-alerting-cluster`
- [ ] 所有 Pods 在 Running 狀態 (`make status`)
- [ ] Prometheus 可以抓到 mysql_up metrics (`make verify`)
- [ ] kube-state-metrics 部署成功
- [ ] Recording rules 有資料 (查詢 `tenant:mysql_cpu_usage:rate5m`)
- [ ] inspect-tenant skill 可以執行 (`make inspect-tenant`)
- [ ] Component management 指令可用 (`make component-list`)

## 參考文件

- [Architecture Review](./architecture-review.md) - 詳細評估與設計決策
- [CLAUDE.md](../CLAUDE.md) - AI Agent 開發指引
- [README.md](../README.md) - 快速開始指南
- [Spec.md](https://github.com/vencil/FunctionPlan/blob/main/AP_Alerts/spec.md) - 設計規格
