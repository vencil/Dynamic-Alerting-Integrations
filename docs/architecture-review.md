# Dynamic Alerting Integrations 架構評估與改進建議

> **注意**：本文件為 Week 0 時的評估快照。部分建議已在 Week 1 實作並進一步修正。
> 主要差異：專案已重命名、metric 統一為 `user_threshold`、Prometheus 已切換為 kubernetes_sd_configs、
> recording rules 已加入 `sum by(tenant)` 聚合。最新狀態請參考 `CLAUDE.md`。

## Executive Summary

這是一份針對本專案的深度評估，涵蓋：
1. 當前實作的優缺點分析
2. Skill 建立策略（提升 AI Agent 效率）
3. 目錄結構重構方案（支援 multi-repo 開發）

---

## 1. 實作檢視與評估

### ✅ 優點（保持並發揚）

#### 1.1 環境一致性設計 ★★★★★
- **Dev Container First**: 消除 "Works on my machine" 問題
- **明確的工具鏈版本**: kubectl, helm, kind 都在容器內，版本鎖定
- **適合 AI Agent 操作**: Claude Code 可以在穩定環境下執行命令

#### 1.2 操作封裝與可維護性 ★★★★★
- **Makefile 設計**: 清晰的生命週期管理（setup → verify → test-alert → clean）
- **Script 模塊化**: `_lib.sh` 提供可重用函式，遵循 DRY 原則
- **跨平台兼容**: `kill_port`, `url_encode` 的 fallback 機制很到位

#### 1.3 監控架構選擇 ★★★★☆
- **Sidecar Pattern**: mysqld_exporter 與 MariaDB 同 Pod，簡化網路拓撲
- **Static Scrape Config**: 對測試環境來說，比 ServiceMonitor CRD 更直觀
- **Alert Rules 分層**: Down/Absent/High/Restart 四類 severity 覆蓋基本場景

#### 1.4 文檔完善度 ★★★★★
- `CLAUDE.md`: 提供 AI Agent 的 context（這是關鍵！）
- `README.md`: 清晰的架構圖 + Quick Start
- 兩份文檔互補，不冗余

---

### ⚠️ 潛在問題與改進空間

#### 1.1 【Critical】缺少 Dynamic Alerting 的核心機制

**問題描述**:
- 目前所有 Alert Rules 都是 **靜態閾值**（如 `threads_connected > 80`）
- Spec.md 的核心訴求是 **Config-as-Metric**（使用者可動態調整閾值）
- 但環境中沒有任何「將配置轉換為 Prometheus Metric」的 exporter

**影響**:
- 四個 Scenario（A/B/C/D）都無法在當前環境驗證
- 這是整個專案的 **blocking issue**

**建議**:
```bash
# 需要新增 component:
1. threshold-exporter (HTTP endpoint 接收配置 → 轉成 metrics)
2. Recording rules (Normalization Layer)
3. Dynamic alert rules (使用 group_left join)
```

#### 1.2 【High】Prometheus Config 的擴展性不足

**問題描述**:
- 使用 `static_configs` 硬編碼兩個 target
- 未來新增 db-c, db-d 時，需要修改 ConfigMap → 重啟 Prometheus

**不完全同意 Gemini 的建議**:
Gemini 建議立即引入 `kubernetes_sd_configs`，但這會增加複雜度。我的替代方案：

```yaml
# 階段性策略：
Phase 1 (current): static_configs  ✓ 適合 2-3 instances
Phase 2 (3-10 instances): Prometheus Operator + ServiceMonitor
Phase 3 (10+ instances): VictoriaMetrics + vmagent (更適合 multi-tenant)
```

**當前可做的改進**:
```yaml
# 使用 relabel_configs 讓配置更模塊化
scrape_configs:
  - job_name: 'mysqld-exporter'
    kubernetes_sd_configs:
      - role: service
        namespaces:
          names: ['db-a', 'db-b']
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
        action: keep
        regex: true
      - source_labels: [__meta_kubernetes_namespace]
        target_label: tenant
```

#### 1.3 【High】缺少 kube-state-metrics

**問題**:
- Scenario C 需要 pod phase, container status 等 K8s 原生指標
- 目前環境只有 MySQL 指標

**建議**:
```bash
# 立即加入（非常輕量）
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
```

#### 1.4 【Medium】Alert 測試策略不完整

**問題**:
- `test-alert.sh` 只測試 "MariaDB Down"
- 沒有測試 **閾值變化時 Alert 的反應**

**應該測試的場景**:
```bash
# Scenario A 測試流程：
1. 設定 user_cpu_threshold=70% (透過 threshold-exporter)
2. 驗證 Prometheus 抓到 metric
3. 製造 CPU=75% 的負載
4. 驗證 Alert firing
5. 調整閾值為 80%
6. 驗證 Alert 自動解除
```

#### 1.5 【Medium】Recording Rules 架構缺失

**問題**:
- 根據 spec.md，需要 Normalization Layer (recording rules)
- 當前環境直接在 alert rules 計算，不符合 best practice

**應該的架構**:
```yaml
# Recording Rules (每 15s 計算一次)
groups:
  - name: normalization
    interval: 15s
    rules:
      - record: tenant:mysql_cpu_usage:rate5m
        expr: rate(mysql_global_status_threads_running[5m])

      - record: tenant:alert_threshold:cpu
        expr: user_cpu_threshold{component="mysql"}

# Alert Rules (使用 normalized metrics)
groups:
  - name: alerts
    rules:
      - alert: MySQLHighCPU
        expr: |
          tenant:mysql_cpu_usage:rate5m
          > on(tenant) group_left
          tenant:alert_threshold:cpu
```

#### 1.6 【Low】密碼管理

**問題**:
- Helm values 中密碼是明文 `stringData`
- 雖然標注「正式環境應改用 sealed-secrets」，但測試環境也該展示最佳實踐

**建議**:
```bash
# 使用 SOPS 加密 values files
# 在 Dev Container 加入 sops, age
sops -e -i helm/values-db-a.yaml
```

---

## 2. Skill 建立策略

### 為什麼需要 Skills？

當前的 `CLAUDE.md` 提供了靜態 context，但 AI Agent 還需要：
- **動作模板**: 避免每次都要推理「該執行哪些指令」
- **語意化介面**: 把「檢查 db-a 健康度」轉譯為一系列 kubectl/curl 操作
- **驗證邏輯**: 確保執行結果符合預期

### 建議的 Skills

#### Skill 1: `inspect-tenant`
```yaml
name: inspect-tenant
description: 全面檢查指定 tenant 的健康狀態
parameters:
  - name: tenant
    type: string
    required: true
    example: db-a

actions:
  - name: check-pods
    command: kubectl get pods -n {tenant} -o wide

  - name: check-db-logs
    command: kubectl logs -n {tenant} -l app=mariadb -c mariadb --tail=20

  - name: check-exporter-logs
    command: kubectl logs -n {tenant} -l app=mariadb -c exporter --tail=20

  - name: verify-metrics
    command: |
      curl -s http://localhost:9090/api/v1/query \
        --data-urlencode 'query=mysql_up{instance="{tenant}"}' | \
      python3 -c "import sys,json; print(json.load(sys.stdin)['data']['result'])"

output:
  format: summary
  include:
    - pod_status
    - recent_errors
    - metric_availability
```

**使用案例**:
```
User: "db-a 怎麼了？"
Claude: [執行 inspect-tenant db-a]
        → Pod Running ✓
        → 最近 20 條日誌無 ERROR
        → mysql_up=1, uptime=3600s
        → 結論：健康
```

#### Skill 2: `verify-dynamic-threshold`
```yaml
name: verify-dynamic-threshold
description: 驗證動態閾值是否正確生效
parameters:
  - name: tenant
    type: string
  - name: metric
    type: string
    example: cpu_usage

actions:
  - name: get-current-value
    command: |
      curl -s http://localhost:9090/api/v1/query \
        --data-urlencode 'query=tenant:mysql_{metric}:rate5m{tenant="{tenant}"}'

  - name: get-threshold
    command: |
      curl -s http://localhost:9090/api/v1/query \
        --data-urlencode 'query=user_{metric}_threshold{tenant="{tenant}"}'

  - name: check-alert-status
    command: |
      curl -s http://localhost:9090/api/v1/alerts | \
      python3 -c "import sys,json; alerts=[a for a in json.load(sys.stdin)['data']['alerts'] if '{tenant}' in str(a)]; print(alerts)"

validation:
  - rule: "current_value < threshold => alert should be inactive"
  - rule: "current_value > threshold => alert should be firing"
```

**這是最重要的 Skill**，因為它驗證了整個 Dynamic Alerting 架構的核心邏輯。

#### Skill 3: `simulate-scenario`
```yaml
name: simulate-scenario
description: 模擬 spec.md 中定義的四種 Scenario
parameters:
  - name: scenario
    type: enum
    values: [A, B, C, D]

scenario_A:  # Dynamic Thresholds
  steps:
    - update_threshold: "POST http://threshold-exporter/api/v1/threshold"
    - wait: 30s
    - verify: "check if alert rule uses new threshold"

scenario_B:  # Weakest Link
  steps:
    - inject_load: "target random container in pod"
    - verify: "alert should fire for that specific container"

scenario_C:  # State Matching
  steps:
    - break_pod: "set invalid image"
    - verify: "alert should fire for ImagePullBackOff state"

scenario_D:  # Composite Priority
  steps:
    - set_multiple_conditions: "VIP tenant + High Severity"
    - verify: "correct alert route triggered"
```

#### Skill 4: `deploy-component`
```yaml
name: deploy-component
description: 部署一個 sub-component 到 Kind cluster
parameters:
  - name: component
    type: string
    example: threshold-exporter
  - name: mode
    type: enum
    values: [local, helm]
    default: local

actions:
  - name: build-image
    when: mode == local
    command: |
      cd ../vibe-{component}
      docker build -t {component}:dev .
      kind load docker-image {component}:dev --name vibe-cluster

  - name: deploy
    command: |
      kubectl apply -f k8s/components/{component}/

  - name: wait-ready
    command: kubectl wait --for=condition=ready pod -l app={component} -n monitoring --timeout=60s

  - name: verify
    command: curl http://localhost:{port}/health
```

---

## 3. 目錄結構重構建議

### 當前問題

```
vibe-k8s-lab/   (Monorepo)
├── helm/mariadb-instance/    ← 測試用的 MariaDB
├── k8s/03-monitoring/         ← Monitoring stack
└── scripts/                   ← 測試腳本

未來需要：
threshold-exporter/  (獨立 Repo)
vibe-kube-alert-router/   (獨立 Repo)
vibe-config-api/          (獨立 Repo)
```

### 不同意 Gemini 的部分

**Gemini 建議**: Git Submodule
**我的觀點**: **強烈不推薦**

原因：
1. Submodule 的 detached HEAD 問題常讓新手困惑
2. 多人協作時容易 out-of-sync
3. CI/CD 配置複雜

**更好的替代方案**: 使用 **Helm Dependencies + Local Override**

---

### 建議的新目錄結構

```
vibe-k8s-lab/  (Integration Repo)
│
├── .devcontainer/
├── Makefile
├── CLAUDE.md
│
├── components/            ← 新增：component manifests
│   ├── threshold-exporter/
│   │   ├── Chart.yaml
│   │   ├── values.yaml
│   │   └── deployment.yaml
│   ├── config-api/
│   └── alert-router/
│
├── environments/          ← 新增：環境配置
│   ├── local/
│   │   └── values.yaml   (指向 local builds)
│   └── ci/
│       └── values.yaml   (指向 image registry)
│
├── tests/                 ← 新增：整合測試
│   ├── scenario-a.sh
│   ├── scenario-b.sh
│   └── verify-all.sh
│
├── helm/                  ← 保留：測試資料
│   └── mariadb-instance/
│
└── scripts/               ← 保留：操作腳本
    └── ...
```

---

### Component 開發工作流

#### Phase 1: 獨立開發 (在 component repo)

```bash
cd ~/projects/threshold-exporter
├── cmd/exporter/main.go
├── Dockerfile
├── helm/                    ← Component 自己的 Helm chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
├── tests/
└── README.md
```

#### Phase 2: 本地整合測試 (在 lab repo)

```bash
# 方案 A: 使用 local build (開發階段)
cd ~/projects/vibe-k8s-lab

# 1. Build component image
make component-build COMP=threshold-exporter

# 內部實作：
# docker build -t threshold-exporter:dev ../threshold-exporter
# kind load docker-image threshold-exporter:dev --name vibe-cluster

# 2. 部署到 Kind
make component-deploy COMP=threshold-exporter ENV=local

# 內部實作：
# helm install threshold-exporter ./components/threshold-exporter \
#   -f environments/local/threshold-exporter.yaml
```

**environments/local/threshold-exporter.yaml**:
```yaml
image:
  repository: threshold-exporter
  tag: dev
  pullPolicy: Never  # 使用 local image

# Override for local testing
config:
  logLevel: debug
  storage: memory  # 不需要真的 DB
```

#### Phase 3: CI/CD 整合 (使用發布的 images)

```yaml
# environments/ci/threshold-exporter.yaml
image:
  repository: ghcr.io/vencil/threshold-exporter
  tag: v0.1.0
  pullPolicy: IfNotPresent
```

---

### Makefile 擴充

```makefile
# ============================================================
# Component Management
# ============================================================
COMP ?= threshold-exporter
ENV  ?= local

.PHONY: component-build
component-build: ## Build component image and load into Kind
	@echo "Building $(COMP)..."
	@if [ ! -d "../vibe-$(COMP)" ]; then \
		echo "Error: ../vibe-$(COMP) not found"; exit 1; \
	fi
	cd ../vibe-$(COMP) && docker build -t $(COMP):dev .
	kind load docker-image $(COMP):dev --name $(CLUSTER)
	@echo "✓ $(COMP):dev loaded into Kind cluster"

.PHONY: component-deploy
component-deploy: ## Deploy component to cluster
	@if [ ! -f "components/$(COMP)/Chart.yaml" ]; then \
		echo "Error: Component $(COMP) not found"; exit 1; \
	fi
	helm upgrade --install $(COMP) ./components/$(COMP) \
		-n monitoring --create-namespace \
		-f environments/$(ENV)/$(COMP).yaml
	kubectl wait --for=condition=ready pod -l app=$(COMP) -n monitoring --timeout=60s
	@echo "✓ $(COMP) deployed ($(ENV) environment)"

.PHONY: component-test
component-test: ## Run integration test for component
	@./tests/verify-$(COMP).sh

.PHONY: component-logs
component-logs: ## View component logs
	@kubectl logs -n monitoring -l app=$(COMP) -f

# Example usage:
# make component-build COMP=threshold-exporter
# make component-deploy COMP=threshold-exporter ENV=local
# make component-test COMP=threshold-exporter
```

---

### 關於 Skaffold 的看法

**Gemini 建議**: 使用 Skaffold
**我的觀點**: **過於重量級**

原因：
1. Skaffold 適合微服務架構（10+ services）
2. 這個專案只有 3-4 個 components
3. 學習曲線增加了協作成本

**更輕量的替代方案**:

#### 方案 A: 使用 `make watch`（推薦）

```makefile
.PHONY: dev-watch
dev-watch: ## Watch for changes and auto-rebuild (requires entr)
	@echo "Watching ../vibe-$(COMP) for changes..."
	@find ../vibe-$(COMP) -name '*.go' | entr -r make component-build component-deploy COMP=$(COMP)
```

```bash
# Terminal 1: Auto rebuild on file change
make dev-watch COMP=threshold-exporter

# Terminal 2: Watch logs
make component-logs COMP=threshold-exporter
```

#### 方案 B: 使用 Tilt（如果真的需要視覺化）

```python
# Tiltfile
load('ext://helm_resource', 'helm_resource')

docker_build('threshold-exporter:dev', '../threshold-exporter')

helm_resource(
    'threshold-exporter',
    'components/threshold-exporter',
    flags=['--values=environments/local/threshold-exporter.yaml']
)
```

Tilt 的優勢：
- 有 Web UI (http://localhost:10350)
- 更直觀的 log streaming
- 但依然比 Skaffold 輕量

---

## 4. 遷移路徑 (Migration Plan)

### Step 1: 準備 Component 結構 (Week 1-2)

```bash
# 1. 在 lab repo 建立 components/ 目錄
mkdir -p components/threshold-exporter
cd components/threshold-exporter

# 2. 建立基本 Helm chart
helm create threshold-exporter
# 清理不需要的 template，只保留 deployment, service, configmap

# 3. 建立環境配置
mkdir -p ../../environments/{local,ci}
```

### Step 2: 獨立 Component Repos (Week 3-4)

```bash
# 1. 建立 threshold-exporter repo
cd ~/projects
mkdir threshold-exporter
cd threshold-exporter

# 2. 實作 exporter
go mod init github.com/vencil/threshold-exporter
# ... 實作 HTTP server, metrics endpoint ...

# 3. 本地測試
cd ~/projects/vibe-k8s-lab
make component-build COMP=threshold-exporter
make component-deploy COMP=threshold-exporter ENV=local
make verify  # 驗證 Prometheus 能抓到 metric
```

### Step 3: 整合測試 (Week 5)

```bash
# 1. 建立 Scenario A 測試
cat > tests/scenario-a.sh <<'EOF'
#!/bin/bash
# Test Dynamic Threshold

# 1. 部署 threshold-exporter
make component-deploy COMP=threshold-exporter

# 2. 設定初始閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a", "metric":"cpu", "value":70}'

# 3. 等待 Prometheus scrape
sleep 30

# 4. 驗證 metric 存在
verify_metric 'user_cpu_threshold{tenant="db-a"}' == 70

# 5. 製造高負載
kubectl exec -n db-a deploy/mariadb -- \
  mysqlslap --concurrency=100 --iterations=1000

# 6. 驗證 Alert firing
verify_alert 'MySQLHighCPU{tenant="db-a"}' == firing

# 7. 調高閾值
curl -X POST http://localhost:8080/api/v1/threshold \
  -d '{"tenant":"db-a", "metric":"cpu", "value":90}'

# 8. 驗證 Alert 解除
sleep 60
verify_alert 'MySQLHighCPU{tenant="db-a"}' == resolved
EOF
```

---

## 5. Skills 實作範例

為了實際展示如何整合 Skills，這裡提供一個完整的實作範例：

### 建立 `.claude/skills/` 目錄結構

```bash
.claude/
└── skills/
    ├── inspect-tenant/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── inspect.sh
    ├── verify-dynamic-threshold/
    │   ├── SKILL.md
    │   └── scripts/
    │       └── verify.sh
    └── simulate-scenario/
        ├── SKILL.md
        └── scenarios/
            ├── scenario-a.sh
            ├── scenario-b.sh
            ├── scenario-c.sh
            └── scenario-d.sh
```

### Skill 範例：`inspect-tenant`

**`.claude/skills/inspect-tenant/SKILL.md`**:
```markdown
# Skill: inspect-tenant

## Purpose
全面檢查指定 tenant (db-a, db-b 等) 的健康狀態，包含：
- K8s Pod 狀態
- MariaDB 日誌
- Exporter 狀態
- Prometheus Metrics 可用性

## Usage
當使用者詢問類似問題時，自動執行此 skill：
- "db-a 怎麼了？"
- "檢查 db-b 的狀態"
- "為什麼 db-a 的 alert 在 firing？"

## Execution

```bash
# 1. 執行檢查腳本
.claude/skills/inspect-tenant/scripts/inspect.sh <tenant-name>

# 2. 解析輸出
# 腳本會返回 JSON 格式：
{
  "tenant": "db-a",
  "pod_status": "Running",
  "db_healthy": true,
  "exporter_healthy": true,
  "metrics": {
    "mysql_up": 1,
    "uptime": 3600,
    "threads_connected": 5
  },
  "recent_errors": []
}

# 3. 根據結果給出建議
- 如果 pod_status != Running → 檢查 kubectl describe pod
- 如果 db_healthy = false → 檢查 MariaDB logs
- 如果 exporter_healthy = false → 檢查 exporter logs
- 如果 recent_errors 非空 → 分析錯誤訊息
```

## Implementation

參見 `scripts/inspect.sh`
```

**`.claude/skills/inspect-tenant/scripts/inspect.sh`**:
```bash
#!/bin/bash
set -euo pipefail

TENANT=${1:-db-a}
OUTPUT_JSON=$(mktemp)

# 1. 檢查 Pod 狀態
POD_STATUS=$(kubectl get pods -n ${TENANT} -l app=mariadb -o jsonpath='{.items[0].status.phase}' 2>/dev/null || echo "NotFound")

# 2. 檢查 MariaDB 健康度
if [ "$POD_STATUS" = "Running" ]; then
  DB_HEALTHY=$(kubectl exec -n ${TENANT} deploy/mariadb -c mariadb -- mariadb -u root -pchangeme_root_pw -e "SELECT 1" &>/dev/null && echo true || echo false)
else
  DB_HEALTHY=false
fi

# 3. 檢查 Exporter
EXPORTER_UP=$(curl -s http://localhost:9090/api/v1/query --data-urlencode "query=up{job=\"mysqld-exporter-${TENANT}\"}" | python3 -c "import sys,json; r=json.load(sys.stdin)['data']['result']; print(r[0]['value'][1] if r else '0')" 2>/dev/null || echo "0")

# 4. 抓取關鍵 Metrics
METRICS=$(curl -s http://localhost:9090/api/v1/query --data-urlencode "query={instance=\"${TENANT}\"}" | python3 -c "
import sys, json
result = json.load(sys.stdin)['data']['result']
metrics = {}
for m in result:
  name = m['metric']['__name__']
  value = m['value'][1]
  metrics[name] = value
print(json.dumps(metrics))
" 2>/dev/null || echo "{}")

# 5. 檢查最近錯誤
RECENT_ERRORS=$(kubectl logs -n ${TENANT} -l app=mariadb -c mariadb --tail=50 2>/dev/null | grep -i error || echo "")

# 6. 組合 JSON
cat > ${OUTPUT_JSON} <<EOF
{
  "tenant": "${TENANT}",
  "pod_status": "${POD_STATUS}",
  "db_healthy": ${DB_HEALTHY},
  "exporter_healthy": $([ "$EXPORTER_UP" = "1" ] && echo true || echo false),
  "metrics": ${METRICS},
  "recent_errors": $(echo "${RECENT_ERRORS}" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read().strip().split('\n')))")
}
EOF

cat ${OUTPUT_JSON}
rm ${OUTPUT_JSON}
```

### 在 CLAUDE.md 中引用 Skills

更新 `CLAUDE.md`：

```markdown
## Available Skills

### 1. inspect-tenant
**觸發條件**: 使用者詢問某個 tenant 的健康狀態
**執行**: `.claude/skills/inspect-tenant/scripts/inspect.sh <tenant>`
**範例**:
- User: "db-a 怎麼了？"
- Claude: [執行 inspect-tenant db-a] → 解析 JSON → 回報結果

### 2. verify-dynamic-threshold
**觸發條件**: 驗證動態閾值是否正確設定
**執行**: `.claude/skills/verify-dynamic-threshold/scripts/verify.sh <tenant> <metric>`

### 3. simulate-scenario
**觸發條件**: 使用者要測試 spec.md 的 Scenario A/B/C/D
**執行**: `.claude/skills/simulate-scenario/scenarios/scenario-<x>.sh`
```

---

## 6. 總結與優先級

### 🔴 Critical (立即執行)

1. **實作 threshold-exporter**
   - 這是 Dynamic Alerting 的核心
   - 建議時程：Week 1-2

2. **加入 kube-state-metrics**
   - `helm install kube-state-metrics ...` (30 分鐘內完成)
   - 沒有它就無法實作 Scenario C

3. **重構 Alert Rules → Recording Rules**
   - 建立 Normalization Layer
   - 讓 alert rules 引用 recording rules

### 🟡 High (2-4 週內)

4. **建立 Skills**
   - 從 `inspect-tenant` 開始（最實用）
   - 然後是 `verify-dynamic-threshold`（驗證核心邏輯）

5. **調整目錄結構**
   - 建立 `components/` 和 `environments/`
   - 準備拆分 component repos

6. **整合測試框架**
   - 實作 Scenario A 的完整測試流程
   - 驗證「閾值變化 → Alert 狀態變化」

### 🟢 Medium (1-2 個月內)

7. **完善 Component 開發工作流**
   - 實作 `make component-build/deploy/test`
   - 考慮引入 Tilt（視團隊需求）

8. **CI/CD Pipeline**
   - GitHub Actions: 自動測試 4 個 Scenarios
   - 自動發布 component images

9. **Production-ready 改進**
   - SOPS 加密敏感資料
   - Helm Chart 發布到 GitHub Pages
   - Grafana Dashboard 匯出

---

## 附錄：與 Gemini 建議的比較

| 項目 | Gemini 建議 | 我的建議 | 理由 |
|------|------------|----------|------|
| Prometheus Config | 立即引入 kubernetes_sd_configs | 保持 static_configs，但加入 relabel | 測試階段保持簡單，避免過早優化 |
| Component 整合方式 | Git Submodule | Helm Dependencies + Local Override | Submodule 協作成本高，容易出錯 |
| 開發工作流工具 | Skaffold | Make + entr (或 Tilt) | Skaffold 對小專案太重，學習曲線陡 |
| Skills 設計 | 語意化指令 | 語意化 + 驗證邏輯 + JSON 輸出 | AI Agent 需要結構化輸出來判斷健康度 |
| Alert 測試 | 提到需要測試閾值變化 | 提供完整測試腳本範例 | 具體實作比概念更有價值 |

### Gemini 的優秀建議（我完全同意）

✅ DevContainer 是最強優勢
✅ 需要驗證「閾值變化時 Alert 的反應」
✅ 需要 Service Discovery（但時機要對）
✅ Skills 的語意化介面（如 `inspect_tenant`）

### 我的額外貢獻

✅ 指出 Recording Rules 架構缺失（這是 Gemini 沒提到的）
✅ 提供完整的 Skill 實作範例（shell script + JSON 輸出）
✅ 詳細的目錄結構調整方案（包含 Makefile 實作）
✅ 明確的遷移路徑（Week 1-2-3-4 的具體行動）
✅ 對 Skaffold/Submodule 提出反對意見（並給出替代方案）

---

## 下一步行動 (Next Actions)

建議按照以下順序執行：

```bash
# Week 1: 基礎架構
1. 部署 kube-state-metrics
2. 重構 Prometheus config (加入 recording rules)
3. 建立 components/ 目錄結構

# Week 2: 實作 threshold-exporter (在獨立 repo)
1. 實作 HTTP API (POST /api/v1/threshold)
2. 實作 Prometheus /metrics endpoint
3. 本地測試

# Week 3: 整合測試
1. 在 lab repo 建立 component deployment manifests
2. 實作 make component-build/deploy
3. 驗證 Prometheus 能抓到 dynamic threshold metrics

# Week 4: Scenario A 驗證
1. 實作 Scenario A 測試腳本
2. 建立 inspect-tenant skill
3. 建立 verify-dynamic-threshold skill
4. 文檔更新
```

需要我協助實作任何部分嗎？
