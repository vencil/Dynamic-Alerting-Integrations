# da-tools — Dynamic Alerting CLI Toolkit

> **受眾**：Platform Engineers、SREs、Tenants (DevOps)
> **Image**：`ghcr.io/vencil/da-tools`
> **版本**：2.1.0（獨立版號，與 threshold-exporter 脫鉤）

---

## 概述

`da-tools` 是一個可攜式 CLI 容器，打包了 Dynamic Alerting 平台的驗證與遷移工具。**不需要 clone 整個專案、不需安裝 Python 依賴**——`docker pull` 即可使用，將整合驗證與規則遷移從小時級縮短到分鐘級：

- 驗證 BYOP 整合是否正確（防止 tenant label 不匹配、threshold-exporter unreachable 等靜默失敗）
- 觀測現有指標並取得閾值建議（baseline discovery）
- 將既有 Prometheus 規則自動轉換為動態格式（AST 引擎，非 regex 替換）
- 產生新 tenant 配置、下架 tenant 或棄用指標（全生命週期）

**Image 大小**：~60 MB（Python 3.12 Alpine + PyYAML + promql-parser），秒級拉取

---

## 快速開始

```bash
# 本地建構（見下方「本地建構」章節）
cd components/da-tools/app && ./build.sh 2.1.0

# 或從 registry 拉取（需 CI/CD 已推送）
docker pull ghcr.io/vencil/da-tools:v2.3.0

# 查看說明
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --help

# 查看版本
docker run --rm ghcr.io/vencil/da-tools:v2.3.0 --version
```

---

## 命令總覽

### Prometheus API 工具（可攜帶，只需 HTTP 存取）

這些工具只需要能連到 Prometheus HTTP API，可從任何位置執行。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `check-alert` | 查詢特定 tenant 的 alert 狀態 | `<alert_name> <tenant>` |
| `diagnose` | Tenant 健康檢查（config + metric + alert 狀態） | `<tenant>` |
| `baseline` | 觀測指標 + 推薦閾值 | `--tenant <name>` |
| `validate` | Shadow Monitoring 雙軌比對（含 auto-convergence） | `--mapping <file>` 或 `--old <query> --new <query>` |
| `batch-diagnose` | 批次租戶健康檢查（auto-discover + 並行診斷） | （自動探索 tenants） |
| `backtest` | PR threshold 變更歷史回測 | `--git-diff` 或 `--config-dir <dir> --baseline <dir>` |
| `cutover` | Shadow Monitoring 一鍵切換（§7.1 全步驟自動化） | `--readiness-json <file> --tenant <name>` |
| `blind-spot` | 掃描 cluster targets 與 tenant config 交叉比對盲區 | `--config-dir <dir>` |
| `maintenance-scheduler` | 評估排程式維護窗口，自動建立 Alertmanager silence | `--config-dir <dir>` |
| `alert-quality` | 警報品質評估（噪音/陳腐/延遲/壓制四指標，三級評分） | `--tenant <name>` 或 `--all` |

### Config 產出工具（讀取 tenant YAML，產出 Alertmanager fragment）

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `generate-routes` | Tenant YAML → Alertmanager route + receiver + inhibit_rules fragment | `--config-dir <dir>` |
| `patch-config` | ConfigMap 局部更新（含 `--diff` preview 模式） | `<tenant> <metric> <value>` 或 `--diff` |

### 檔案系統工具（離線可用，不需網路）

這些工具操作本地 YAML 檔案，透過 Volume Mount 傳入。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `migrate` | 傳統規則 → 動態格式轉換 (AST + regex 雙引擎) | `<input_file>` |
| `scaffold` | 產生 tenant 配置 | `--tenant <name> --db <types>` |
| `offboard` | 下架 tenant 配置 | `<tenant>` |
| `deprecate` | 標記指標為 disabled | `<metric_keys...>` |
| `lint` | 檢查 Custom Rule 治理合規性 | `<path...>` |
| `onboard` | 分析既有 Alertmanager/Prometheus 配置進行遷移 | `<config_file>` 或 `--alertmanager-config <file>` |
| `validate-config` | 一站式配置驗證（YAML + schema + routes + policy） | `--config-dir <dir>` |
| `analyze-gaps` | Custom Rule 對應 Rule Pack 缺口分析 | `--config <path>` |
| `config-diff` | 兩目錄配置差異比對（GitOps PR review） | `--old-dir <dir> --new-dir <dir>` |
| `evaluate-policy` | Policy-as-Code 策略評估（宣告式 DSL，10 運算子） | `--config-dir <dir>` |
| `cardinality-forecast` | 基數趨勢預測（線性回歸，三級風險，觸頂天數） | `--tenant <name>` 或 `--all` |

---

## 使用範例

### 場景一：BYOP 整合後驗證

完成 [BYOP 整合指南](../../docs/byo-prometheus-integration.md) 三個步驟後，用 `da-tools` 驗證：

```bash
# 設定 Prometheus 位址（避免每次都打 --prometheus）
export PROM=http://prometheus.monitoring.svc.cluster.local:9090

# 1. 確認 alert 狀態
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  check-alert MariaDBHighConnections db-a

# 2. 觀測指標並取得閾值建議
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  baseline --tenant db-a --duration 300

# 3. Shadow Monitoring 雙軌比對
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  validate --mapping /data/mapping.csv --watch --rounds 5
```

### 場景二：規則遷移（離線）

```bash
# 轉換既有規則（Dry Run + Triage 報告）
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/my-rules.yml \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  migrate /data/my-rules.yml -o /data/output --dry-run --triage

# 產出：
#   /data/output/migration_output/  ← 轉換後的規則
#   /data/output/triage.csv         ← 需人工審閱的規則清單
```

### 場景三：產生新 tenant 配置

```bash
# 非互動式產生 tenant 配置
docker run --rm \
  -v $(pwd)/configs:/data/configs \
  ghcr.io/vencil/da-tools:v2.3.0 \
  scaffold --tenant db-c --db mariadb,redis --non-interactive -o /data/configs
```

### 場景四：產出 Alertmanager Route Fragment

```bash
# 從 tenant YAML 產出 Alertmanager route + receiver + inhibit_rules fragment
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /data/conf.d --dry-run

# 寫入檔案
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /data/conf.d -o /data/output/alertmanager-routes.yaml
```

### 場景五：GitOps 完整 ConfigMap 產出（v1.10.0）

與場景四的 fragment 模式不同，`--output-configmap` 產出可直接 `kubectl apply` 的完整 Alertmanager ConfigMap YAML，適合 Git PR flow：

```bash
# Dry run — 在 stdout 預覽完整 ConfigMap（含 global + route + receivers + inhibit_rules）
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /data/conf.d --output-configmap --dry-run

# 寫入檔案，供 Git commit + PR review
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /data/conf.d --output-configmap \
    -o /data/output/alertmanager-configmap.yaml

# 搭配自訂基礎配置（global 設定、default receiver 等）
docker run --rm \
  -v $(pwd)/conf.d:/data/conf.d \
  -v $(pwd)/base-alertmanager.yaml:/data/base.yaml \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:v2.3.0 \
  generate-routes --config-dir /data/conf.d --output-configmap \
    --base-config /data/base.yaml -o /data/output/alertmanager-configmap.yaml
```

產出結構：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: alertmanager-config
  namespace: monitoring
data:
  alertmanager.yml: |
    global:
      resolve_timeout: 5m
    route:
      group_by: [alertname, tenant]
      receiver: default
      routes:          # ← 自動產出的 per-tenant routes
        - match: {tenant: db-a}
          receiver: db-a-webhook
          ...
    receivers:         # ← default + per-tenant receivers
      - name: default
      - name: db-a-webhook
        ...
    inhibit_rules: ... # ← per-tenant severity dedup
```

**`--apply` vs `--output-configmap` 選擇指南：**

| 場景 | 推薦模式 | 原因 |
|------|---------|------|
| P0 緊急修復 | `--apply --yes` | 立即生效，跳過 PR flow |
| GitOps 正常流程 | `--output-configmap` | 產出檔案進 Git，走 review + CI |
| CI 驗證 | `--validate` | 只驗不寫，exit code 0/1 |

### 場景六：Shadow Monitoring 一鍵切換（v1.10.0）

完成 Shadow Monitoring 收斂確認後，執行自動化切換：

```bash
# Dry run — 預覽切換步驟，不做任何變更
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --readiness-json /data/cutover-readiness.json \
    --tenant db-a --dry-run

# 執行切換（需 readiness 檢查通過）
docker run --rm --network=host \
  -v $(pwd)/validation_output:/data \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --readiness-json /data/cutover-readiness.json --tenant db-a

# 強制切換（跳過 readiness 檢查，僅限確認安全後使用）
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  cutover --tenant db-a --force
```

`cutover` 自動執行：停止 Shadow Monitor Job → 移除舊 Recording Rules → 移除 `migration_status: shadow` label → 移除 Alertmanager shadow route → `check-alert` + `diagnose` 驗證。完整流程對應 [Shadow Monitoring SOP §7.1](../../docs/shadow-monitoring-sop.md#71-切換步驟)。

**何時用 `--force`：** `cutover-readiness.json` 是由 `validate --auto-detect-convergence` 自動產出的收斂證明。如果你已透過手動方式（如 CSV 報告分析）確認收斂，但沒有 readiness JSON，可用 `--force` 跳過此檢查。注意：`--force` 不會跳過切換後的 `check-alert` / `diagnose` 健康驗證。

### 場景七：監控盲區掃描（v1.10.0）

掃描 Prometheus 叢集內的活躍 targets，與 `conf.d/` tenant 配置交叉比對，找出有 exporter 在跑、但未被本平台閾值管理涵蓋的 DB instance：

```bash
# 掃描叢集盲區
docker run --rm --network=host \
  -v $(pwd)/conf.d:/data/conf.d \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /data/conf.d

# 排除不需要納管的 job（如 node-exporter、kube-state-metrics）
docker run --rm --network=host \
  -v $(pwd)/conf.d:/data/conf.d \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /data/conf.d --exclude-jobs node-exporter,kube-state-metrics

# JSON 結構化輸出（供 CI 或其他工具消費）
docker run --rm --network=host \
  -v $(pwd)/conf.d:/data/conf.d \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:v2.3.0 \
  blind-spot --config-dir /data/conf.d --json-output
```

範例輸出：

```
=== Blind Spot Discovery Report ===

Covered (2 instances):
  ✓ db-a  mariadb   job=mysqld-exporter

Blind Spots (3 instances):
  ✗ db-c  postgresql  job=postgres-exporter   ← 叢集有 exporter 但無 tenant 配置
  ✗ db-d  redis       job=redis-exporter
  ✗ db-e  mongodb     job=mongodb-exporter

Unrecognized (1 job):
  ? job=custom-app-metrics                    ← 無法推斷 DB type

Summary: 2 covered, 3 blind spots, 1 unrecognized
```

**使用場景：** 遷移上線後的定期健檢、新增 exporter 後確認已有對應 tenant 配置、CI 中 gate check（blind spot > 0 → 警告）。

**與 `analyze-gaps` 的差異：** `blind-spot` 比對的是「叢集基礎設施 vs tenant 配置」（有 exporter 但沒配置 = 盲區）；`analyze-gaps` 比對的是「custom rule vs Rule Pack」（有自訂規則但 Rule Pack 已涵蓋 = 可替代）。兩者互補，建議在遷移完成後同時執行。

### 場景八：配置目錄級差異比對（v1.10.0）

比較兩個 `conf.d/` 目錄（例如 Git PR 中的 base branch vs feature branch），產出 blast radius 報告：

```bash
# 比對兩個目錄
docker run --rm \
  -v $(pwd)/conf.d-old:/data/old \
  -v $(pwd)/conf.d-new:/data/new \
  ghcr.io/vencil/da-tools:v2.3.0 \
  config-diff --old-dir /data/old --new-dir /data/new

# JSON 結構化輸出
docker run --rm \
  -v $(pwd)/conf.d-old:/data/old \
  -v $(pwd)/conf.d-new:/data/new \
  ghcr.io/vencil/da-tools:v2.3.0 \
  config-diff --old-dir /data/old --new-dir /data/new --json-output
```

範例輸出：

```markdown
## Config Diff Report

### db-a (3 changes, est. 3 alerts affected)

| Metric Key | Change | Old | New | Est. Alert |
|------------|--------|-----|-----|------------|
| mysql_connections | tighter | 100 | 80 | MariaDBHighConnections |
| mysql_connections_critical | tighter | 150 | 120 | MariaDBHighConnections |
| mysql_replication_lag | looser | 5 | 10 | MariaDBReplicationLag |

### db-b (1 change, est. 1 alert affected)

| Metric Key | Change | Old | New | Est. Alert |
|------------|--------|-----|-----|------------|
| redis_memory_usage | added | — | 80 | RedisHighMemoryUsage |

### Summary
- Tenants affected: 2 / 5
- Total changes: 4 (2 tighter, 1 looser, 1 added)
```

**變更分類說明：**

| 分類 | 含義 | 影響 |
|------|------|------|
| `tighter` | 閾值下降（更容易觸發 alert） | 可能增加告警 |
| `looser` | 閾值上升（更不容易觸發 alert） | 可能減少告警 |
| `added` | 新增 metric key | 新增 alert 覆蓋 |
| `removed` | 移除 metric key | 失去 alert 覆蓋 |
| `toggled` | enable ↔ disable 切換 | 開啟或關閉 alert |
| `modified` | 複雜值變更（如 routing 物件） | 需人工審閱 |

**與 `patch-config --diff` 的差異：** `patch-config --diff` 是**單一 metric 的 live ConfigMap 預覽**（類似 `terraform plan`，顯示 apply 前後的即時差異）；`config-diff` 是**目錄級的靜態比對**（兩個 conf.d/ 目錄的完整差異，適合 PR review）。兩者分別用於營運階段和 review 階段。

---

## 環境變數

| 變數 | 用途 | 預設值 |
|------|------|--------|
| `PROMETHEUS_URL` | Prometheus 端點 URL（作為 `--prometheus` 的 fallback） | `http://localhost:9090` |

> **提示**：容器內的 `localhost` 是容器自己。請使用：
> - K8s 內部：`http://prometheus.monitoring.svc.cluster.local:9090`
> - Docker Desktop：`http://host.docker.internal:9090`
> - Linux Docker：`--network=host` 搭配 `http://localhost:9090`

---

## 本地建構

```bash
cd components/da-tools/app

# 建構 dev image
./build.sh

# 建構指定版本
./build.sh 2.1.0

# 載入到 Kind cluster（如需要在 K8s Job 中使用）
kind load docker-image da-tools:dev --name dynamic-alerting-cluster
```

---

## 作為 Kubernetes Job 執行

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: byop-validation
  namespace: monitoring
spec:
  template:
    spec:
      containers:
        - name: da-tools
          image: ghcr.io/vencil/da-tools:v2.3.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

---

## 版號策略

`da-tools` 採用**獨立版號**，與平台版本（v2.0.0）和 threshold-exporter 版號脫鉤：

| 元件 | 版號 | Git Tag | 說明 |
|------|------|---------|------|
| 平台文件 | v2.1.0 | `v2.1.0` | ADR-007 Routing Profiles + Portal 強化 + 文件治理 |
| threshold-exporter | v2.1.0 | `exporter/v2.1.0` | Go binary |
| **da-tools** | **v2.1.0** | **`tools/v2.1.0`** | **Python CLI 工具集（23 命令）** |

CI/CD 透過 `tools/v*` tag 觸發，不會被平台文件更新或 exporter 變更影響。

---

## 收錄的工具

| 工具腳本 | 對應命令 | 原始位置 |
|----------|----------|----------|
| `check_alert.py` | `check-alert` | `scripts/tools/` |
| `diagnose.py` | `diagnose` | `scripts/tools/` |
| `baseline_discovery.py` | `baseline` | `scripts/tools/` |
| `validate_migration.py` | `validate` | `scripts/tools/` |
| `migrate_rule.py` | `migrate` | `scripts/tools/` |
| `scaffold_tenant.py` | `scaffold` | `scripts/tools/` |
| `offboard_tenant.py` | `offboard` | `scripts/tools/` |
| `deprecate_rule.py` | `deprecate` | `scripts/tools/` |
| `lint_custom_rules.py` | `lint` | `scripts/tools/` |
| `generate_alertmanager_routes.py` | `generate-routes` | `scripts/tools/` |
| `onboard_platform.py` | `onboard` | `scripts/tools/` |
| `batch_diagnose.py` | `batch-diagnose` | `scripts/tools/` |
| `backtest_threshold.py` | `backtest` | `scripts/tools/` |
| `analyze_rule_pack_gaps.py` | `analyze-gaps` | `scripts/tools/` |
| `patch_config.py` | `patch-config` | `scripts/tools/` |
| `validate_config.py` | `validate-config` | `scripts/tools/` |
| `cutover_tenant.py` | `cutover` | `scripts/tools/` |
| `blind_spot_discovery.py` | `blind-spot` | `scripts/tools/` |
| `maintenance_scheduler.py` | `maintenance-scheduler` | `scripts/tools/` |
| `config_diff.py` | `config-diff` | `scripts/tools/` |
| `alert_quality.py` | `alert-quality` | `scripts/tools/` |
| `policy_engine.py` | `evaluate-policy` | `scripts/tools/` |
| `cardinality_forecasting.py` | `cardinality-forecast` | `scripts/tools/` |
| `metric-dictionary.yaml` | （migrate 內部參照） | `scripts/tools/` |
