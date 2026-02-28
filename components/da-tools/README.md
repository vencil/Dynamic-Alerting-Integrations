# da-tools — Dynamic Alerting CLI Toolkit

> **受眾**：Platform Engineers、SREs、Tenants (DevOps)
> **Image**：`ghcr.io/vencil/da-tools`
> **版本**：0.3.0（獨立版號，與 threshold-exporter 脫鉤）

---

## 概述

`da-tools` 是一個輕量的 CLI 容器工具，打包了 Dynamic Alerting 平台的驗證與遷移工具。它讓你**不需要 clone 整個專案**，就能完成以下工作：

- 驗證 BYOP 整合是否正確（alert 狀態、閾值查詢、向量匹配）
- 觀測現有指標並取得閾值建議（baseline discovery）
- 將既有 Prometheus 規則轉換為動態格式
- 產生新 tenant 配置、下架 tenant 或棄用指標

**Image 大小**：~60 MB（Python 3.12 Alpine + PyYAML + promql-parser）

---

## 快速開始

```bash
# 拉取 image
docker pull ghcr.io/vencil/da-tools:0.3.0

# 查看說明
docker run --rm ghcr.io/vencil/da-tools:0.3.0 --help

# 查看版本
docker run --rm ghcr.io/vencil/da-tools:0.3.0 --version
```

---

## 命令總覽

### Prometheus API 工具（可攜帶，只需 HTTP 存取）

這些工具只需要能連到 Prometheus HTTP API，可從任何位置執行。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `check-alert` | 查詢特定 tenant 的 alert 狀態 | `<alert_name> <tenant>` |
| `baseline` | 觀測指標 + 推薦閾值 | `--tenant <name>` |
| `validate` | Shadow Monitoring 雙軌比對 | `--mapping <file>` 或 `--old <query> --new <query>` |

### 檔案系統工具（離線可用，不需網路）

這些工具操作本地 YAML 檔案，透過 Volume Mount 傳入。

| 命令 | 用途 | 最小參數 |
|------|------|----------|
| `migrate` | 傳統規則 → 動態格式轉換 (AST + regex 雙引擎) | `<input_file>` |
| `scaffold` | 產生 tenant 配置 | `--tenant <name> --db <types>` |
| `offboard` | 下架 tenant 配置 | `<tenant>` |
| `deprecate` | 標記指標為 disabled | `<metric_keys...>` |
| `lint` | 檢查 Custom Rule 治理合規性 | `<path...>` |

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
  ghcr.io/vencil/da-tools:0.3.0 \
  check-alert MariaDBHighConnections db-a

# 2. 觀測指標並取得閾值建議
docker run --rm --network=host \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:0.3.0 \
  baseline --tenant db-a --duration 300

# 3. Shadow Monitoring 雙軌比對
docker run --rm --network=host \
  -v $(pwd)/mapping.csv:/data/mapping.csv \
  -e PROMETHEUS_URL=$PROM \
  ghcr.io/vencil/da-tools:0.3.0 \
  validate --mapping /data/mapping.csv --watch --rounds 5
```

### 場景二：規則遷移（離線）

```bash
# 轉換既有規則（Dry Run + Triage 報告）
docker run --rm \
  -v $(pwd)/my-rules.yml:/data/my-rules.yml \
  -v $(pwd)/output:/data/output \
  ghcr.io/vencil/da-tools:0.3.0 \
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
  ghcr.io/vencil/da-tools:0.3.0 \
  scaffold --tenant db-c --db mariadb,redis --non-interactive -o /data/configs
```

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
./build.sh 0.1.0

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
          image: ghcr.io/vencil/da-tools:0.3.0
          env:
            - name: PROMETHEUS_URL
              value: "http://prometheus.monitoring.svc.cluster.local:9090"
          args: ["check-alert", "MariaDBHighConnections", "db-a"]
      restartPolicy: Never
  backoffLimit: 0
```

---

## 版號策略

`da-tools` 採用**獨立版號**，與平台版本（v0.12.0+）和 threshold-exporter 版號脫鉤：

| 元件 | 版號 | Git Tag | 說明 |
|------|------|---------|------|
| 平台文件 | v0.12.0 | `v0.11.0` | AST 遷移引擎 + 治理模型 + 流程圖 |
| threshold-exporter | v0.12.0 | `exporter/v0.12.0` | Go binary |
| **da-tools** | **v0.3.0** | **`tools/v0.3.0`** | **Python CLI 工具集** |

CI/CD 透過 `tools/v*` tag 觸發，不會被平台文件更新或 exporter 變更影響。

---

## 收錄的工具

| 工具腳本 | 對應命令 | 原始位置 |
|----------|----------|----------|
| `check_alert.py` | `check-alert` | `scripts/tools/` |
| `baseline_discovery.py` | `baseline` | `scripts/tools/` |
| `validate_migration.py` | `validate` | `scripts/tools/` |
| `migrate_rule.py` | `migrate` | `scripts/tools/` |
| `scaffold_tenant.py` | `scaffold` | `scripts/tools/` |
| `offboard_tenant.py` | `offboard` | `scripts/tools/` |
| `deprecate_rule.py` | `deprecate` | `scripts/tools/` |
| `lint_custom_rules.py` | `lint` | `scripts/tools/` |
| `metric-dictionary.yaml` | （migrate 內部參照） | `scripts/tools/` |

> **未收錄**：`diagnose.py` 和 `patch_config.py` 需要 kubectl 叢集存取，屬於集群內操作工具，不適合「帶回家驗證」的場景。
