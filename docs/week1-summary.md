# Week 1 實作完成摘要

> **注意**：本文件為 Week 1 初版快照。後續修正包括：recording-rules.yml 掛載修復、
> label 聚合對齊（`sum by(tenant)`）、metric 統一為 `user_threshold`、
> threshold-exporter 轉為完整 Helm chart、Prometheus 遷移至 kubernetes_sd_configs。
> 最新狀態請參考 `CLAUDE.md`。

## 🎯 完成項目

### 1. 專案重命名 ✅

**變更**：
- `vibe-k8s-lab` → `dynamic-alerting-integrations`
- `vibe-cluster` → `dynamic-alerting-cluster`

**影響檔案**：
- `.devcontainer/devcontainer.json`
- `README.md`
- `CLAUDE.md`
- `Makefile`
- `scripts/_lib.sh`

---

### 2. 目錄結構重構 ✅

**新增目錄**：
```
dynamic-alerting-integrations/
├── components/               ← 新增：Sub-component manifests
│   ├── threshold-exporter/
│   ├── config-api/
│   ├── alert-router/
│   └── kube-state-metrics/
│
├── environments/             ← 新增：環境配置
│   ├── local/
│   │   └── threshold-exporter.yaml
│   └── ci/
│       └── threshold-exporter.yaml
│
├── tests/                    ← 新增：整合測試
│
├── .claude/skills/           ← 新增：AI Agent skills
│   └── inspect-tenant/
│       ├── SKILL.md
│       └── scripts/inspect.sh
│
└── docs/                     ← 新增：文檔
    ├── architecture-review.md
    ├── deployment-guide.md
    └── week1-summary.md
```

---

### 3. Component 管理系統 ✅

**新增 Makefile Targets**：
```bash
make component-build COMP=threshold-exporter   # Build & load to Kind
make component-deploy COMP=threshold-exporter  # Deploy with env config
make component-test COMP=threshold-exporter    # Run integration test
make component-logs COMP=threshold-exporter    # View logs
make component-list                            # List components
```

**工作流程**：
1. 在獨立 repo 開發 component（如 `../threshold-exporter`）
2. `make component-build` - Build Docker image & load to Kind
3. `make component-deploy ENV=local` - 使用 local 配置部署
4. `make component-test` - 執行整合測試

---

### 4. inspect-tenant Skill ✅

**功能**：
- 檢查 Pod 狀態
- 驗證 MariaDB 健康度
- 確認 Exporter 運作
- 抓取關鍵 Metrics
- 分析最近錯誤日誌
- 提供 JSON 輸出

**使用**：
```bash
make inspect-tenant TENANT=db-a
```

**輸出範例**：
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

---

### 5. Prometheus 增強 ✅

#### 5.1 Recording Rules (Normalization Layer)

```yaml
# 標準化 CPU 使用率
tenant:mysql_cpu_usage:rate5m

# 標準化連線使用率
tenant:mysql_connection_usage:ratio

# 標準化 uptime
tenant:mysql_uptime:hours

# 動態閾值（預設值 80）
tenant:alert_threshold:cpu
tenant:alert_threshold:connections
```

#### 5.2 更新 Alert Rules

```yaml
# 舊版（靜態閾值）
expr: mysql_global_status_threads_connected > 80

# 新版（動態閾值）
expr: |
  mysql_global_status_threads_connected
  > on(tenant) group_left
  tenant:alert_threshold:connections
```

#### 5.3 新增 Scrape Configs

```yaml
# kube-state-metrics (Scenario C)
- job_name: "kube-state-metrics"
  static_configs:
    - targets: ["kube-state-metrics.monitoring.svc.cluster.local:8080"]

# threshold-exporter (Scenario A - 預留)
- job_name: "threshold-exporter"
  static_configs:
    - targets: ["threshold-exporter.monitoring.svc.cluster.local:8080"]
```

#### 5.4 新增 tenant Label

所有 mysqld-exporter scrape configs 現在都包含 `tenant` label：
```yaml
labels:
  tenant: "db-a"  # 或 "db-b"
  instance: "db-a"
```

---

### 6. kube-state-metrics 整合 ✅

**部署腳本**：
```bash
./scripts/deploy-kube-state-metrics.sh
```

**提供的 Metrics**（用於 Scenario C）：
- `kube_pod_status_phase` - Pod 狀態
- `kube_pod_container_status_waiting_reason` - 等待原因 (CrashLoopBackOff, etc.)
- `kube_deployment_status_replicas`
- `kube_node_status_condition`

**驗證**：
```bash
kubectl get pods -n monitoring -l app.kubernetes.io/name=kube-state-metrics
```

---

### 7. 環境配置分離 ✅

#### Local Environment (`environments/local/`)
```yaml
image:
  repository: threshold-exporter
  tag: dev
  pullPolicy: Never  # 使用 kind load 的 local image

config:
  logLevel: debug
  storage: memory

resources:
  requests:
    cpu: 50m
    memory: 64Mi
```

#### CI Environment (`environments/ci/`)
```yaml
image:
  repository: ghcr.io/vencil/threshold-exporter
  tag: v0.1.0
  pullPolicy: IfNotPresent

config:
  logLevel: info
  storage:
    type: redis
    redis:
      host: redis.monitoring.svc.cluster.local

resources:
  requests:
    cpu: 100m
    memory: 128Mi

replicaCount: 2  # HA
```

---

### 8. 文檔更新 ✅

**新增文檔**：
- `docs/architecture-review.md` - 完整評估（20+ 頁）
- `docs/deployment-guide.md` - 部署指南
- `docs/week1-summary.md` - 本文件
- `CHANGELOG.md` - 變更日誌

**更新文檔**：
- `README.md` - 更新專案名稱和架構圖
- `CLAUDE.md` - 加入 Week 1 更新說明

---

## 📊 與 Gemini 建議的比較

| 項目 | Gemini 建議 | 我的實作 | 狀態 |
|------|------------|----------|------|
| Prometheus Config 擴展 | 立即引入 kubernetes_sd | 保持 static_configs，加入 recording rules | ✅ 更符合測試需求 |
| Component 整合 | Git Submodule | Helm Dependencies + Local Override | ✅ 更靈活 |
| 開發工具 | Skaffold | Make + Component Management | ✅ 更輕量 |
| Skills 設計 | 語意化指令 | 語意化 + JSON 輸出 + 驗證邏輯 | ✅ 更完整 |
| Recording Rules | 未提及 | 完整實作 Normalization Layer | ✅ 我的額外貢獻 |

---

## 🚀 下一步行動

### Week 2-3: 實作 threshold-exporter

#### 1. 建立獨立 Repo
```bash
cd ~/projects
mkdir threshold-exporter
cd threshold-exporter

# 初始化 Go 專案
go mod init github.com/vencil/threshold-exporter
```

#### 2. 實作核心功能
```go
// HTTP API
POST /api/v1/threshold
{
  "tenant": "db-a",
  "component": "mysql",
  "metric": "cpu_usage",
  "value": 70,
  "severity": "warning"
}

// Prometheus endpoint
GET /metrics
# HELP user_cpu_threshold User-defined CPU threshold
# TYPE user_cpu_threshold gauge
user_cpu_threshold{tenant="db-a",component="mysql",severity="warning"} 70
```

#### 3. 本地整合測試
```bash
cd ~/projects/dynamic-alerting-integrations

# Build & Deploy
make component-build COMP=threshold-exporter
make component-deploy COMP=threshold-exporter ENV=local

# 測試 API
curl -X POST http://localhost:8080/api/v1/threshold \
  -H "Content-Type: application/json" \
  -d '{"tenant":"db-a","component":"mysql","metric":"cpu","value":70}'

# 驗證 Prometheus 抓到 metric
curl http://localhost:9090/api/v1/query \
  --data-urlencode 'query=user_cpu_threshold{tenant="db-a"}'
```

---

### Week 4: Scenario A 驗證

#### 1. 建立整合測試腳本
```bash
cat > tests/scenario-a.sh <<'EOF'
#!/bin/bash
# Scenario A: Dynamic Thresholds

# 1. 設定初始閾值 70
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a","metric":"connections","value":70}'

# 2. 等待 Prometheus scrape
sleep 30

# 3. 製造高負載（75 connections）
# ...

# 4. 驗證 Alert firing
if curl -s http://localhost:9090/api/v1/alerts | grep -q "MariaDBHighConnections.*firing"; then
  echo "✓ Alert fired correctly"
else
  echo "✗ Alert should be firing"
  exit 1
fi

# 5. 調高閾值到 80
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a","metric":"connections","value":80}'

# 6. 等待閾值生效
sleep 60

# 7. 驗證 Alert 解除
if ! curl -s http://localhost:9090/api/v1/alerts | grep -q "MariaDBHighConnections.*firing"; then
  echo "✓ Alert resolved correctly"
else
  echo "✗ Alert should be resolved"
  exit 1
fi

echo "✓ Scenario A: Dynamic Thresholds PASSED"
EOF

chmod +x tests/scenario-a.sh
```

#### 2. 執行測試
```bash
make component-test COMP=threshold-exporter
```

---

## 🎓 學到的關鍵點

### 1. Recording Rules 的重要性
- **問題**：Alert rules 直接查詢原始 metrics 會很複雜
- **解決**：使用 recording rules 建立 Normalization Layer
- **好處**：
  - Alert rules 變簡單 (`tenant:mysql_cpu_usage:rate5m > threshold`)
  - 查詢效能更好（預先計算）
  - 更容易維護和理解

### 2. Component 開發工作流
- **問題**：Monorepo 太大，獨立 repo 又難整合
- **解決**：
  - Component 在獨立 repo 開發
  - Lab repo 透過 `make component-*` 整合
  - 環境配置分離（local vs ci）
- **好處**：
  - 清晰的責任邊界
  - 靈活的開發流程
  - 易於 CI/CD

### 3. Skills 的價值
- **問題**：AI Agent 需要重複執行相同檢查
- **解決**：建立標準化 skills（inspect-tenant）
- **好處**：
  - 一致的輸出格式（JSON）
  - 包含診斷邏輯
  - 可重複使用

---

## 📋 驗證清單

請在 Dev Container 中執行以下驗證：

```bash
# 1. 檢查專案名稱
grep -r "vibe-cluster" . --exclude-dir=.git  # 應該沒有結果
grep -r "dynamic-alerting-cluster" CLAUDE.md README.md Makefile  # 應該有結果

# 2. 檢查目錄結構
ls -la components/ environments/ tests/ .claude/skills/

# 3. 檢查 Makefile 新指令
make help | grep component
make help | grep inspect

# 4. 部署並驗證
make setup
make status
./scripts/deploy-kube-state-metrics.sh

# 5. 測試 inspect-tenant skill
make port-forward &
sleep 10
make inspect-tenant TENANT=db-a

# 6. 驗證 Recording Rules
curl -s http://localhost:9090/api/v1/query \
  --data-urlencode 'query=tenant:mysql_cpu_usage:rate5m'
```

---

## 🎉 總結

Week 1 的重構已完成，主要成就：

1. ✅ 建立了清晰的模塊化架構
2. ✅ 準備好 Component 開發工作流
3. ✅ 實作了 Recording Rules (Normalization Layer)
4. ✅ 建立了第一個 Skill (inspect-tenant)
5. ✅ 整合了 kube-state-metrics
6. ✅ 完整的文檔和部署指南

**現在已經為 Scenario A (Dynamic Thresholds) 的實作做好準備！**

下一步：開始實作 threshold-exporter 的 Go 程式。
