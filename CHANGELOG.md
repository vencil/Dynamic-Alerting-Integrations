# Changelog

## [Phase 2] - Migration & Extensibility (2026-02-24)
### Migration Guide (`docs/migration-guide.md`)
- 完整遷移指南：從傳統 Prometheus 警報遷移至動態閾值架構。
- 五種 Percona MariaDB 場景範例：基本數值比較、多層嚴重度、Slave Lag、Slow Queries (rate)、Buffer Pool (百分比)。
- 遷移前評估 Checklist、Alertmanager routing 遷移 (instance → tenant)、三態操作範例。
- 遷移後驗證流程：整合 `check_alert.py` / `diagnose.py` 工具鏈。
- LLM 輔助批量轉換 System Prompt，支援小型模型自動抽取閾值。

### CLAUDE.md 重構
- 簡化 Week 1-4 為 Phase 1 摘要，新增 Phase 2 Roadmap (2B 多 DB 支援、2C GitOps、2D Migration Tooling)。

## [Week 4 — Final Polish] - Tech Debt Cleanup (2026-02-23)
### Dependencies & Dockerfile
- **Go 1.23**: `go.mod` 升級至 `go 1.23.0`，`client_golang` 升級至 `v1.20.0`，執行 `go mod tidy` 更新所有間接依賴。
- **Dockerfile 精簡**: Builder `golang:1.23-alpine`，Runtime `alpine:3.21`。移除冗餘 `CMD` 指令（由 K8s `deployment.yaml` 的 `args` 控制）。
- **devcontainer.json**: Go feature 升級至 `1.23`。

### Setup Optimization
- **devcontainer.json**: 移除 `postCreateCommand` 中的 `kind create cluster` 迴圈（與 `setup.sh` 職責重疊），僅保留工具安裝。
- **setup.sh**: 消除 `/tmp/threshold-exporter.tar` 的 Disk I/O 浪費，改用 `kind load docker-image` 直接 memory stream 載入。

### Tools Promotion
- **scripts/tools/**: 將 `.claude/skills/` 中的 `patch_cm.py`、`check_alert.py`、`diagnose.py` 轉正為標準專案工具 (`patch_config.py`、`check_alert.py`、`diagnose.py`)。
- 更新 `tests/scenario-a/b/c/d.sh`、`Makefile`、`.claude/skills/*/SKILL.md` 中的路徑引用。

### Cleanup
- **移除 `docs/architecture-review.md`**: Week 0 架構評估快照，所有 Critical/High 項目 (threshold-exporter、kube-state-metrics、Recording Rules、Skills) 已於 Week 1-4 全部完成，文件已完成歷史使命。

## [Week 4] - Composite Priority Logic (2026-02-22)
### Scenario D: Alert Fatigue 解法
- **Phase 1 — 維護模式**: Go `StateFilter` 新增 `default_state` 欄位 (opt-in model)。`maintenance` filter 預設停用，租戶設 `_state_maintenance: enable` 啟用。PromQL 5 條 alert rules 加 `unless on(tenant) (user_state_filter{filter="maintenance"} == 1)` 抑制。Hot-reload 驗證通過。
- **Phase 2 — 複合警報**: 新增 `MariaDBSystemBottleneck` alert，使用 PromQL `and` 要求高連線數**且**高 CPU 同時觸發，severity=critical。含 maintenance 抑制。
- **Phase 3 — 多層級嚴重度**: Go `Resolve()` 支援 `<metric>_critical` 後綴，產生 `severity="critical"` 的獨立 threshold metric。Recording rules 新增 `tenant:alert_threshold:connections_critical` / `cpu_critical`。新增 `MariaDBHighConnectionsCritical` alert。Warning alert 含 `unless` 降級邏輯（critical 觸發時自動抑制 warning）。

### Pre-Scenario D Refactoring (2026-02-22)
#### Test Scripts — ConfigMap 覆寫技術債清理
- **scenario-a/b/c.sh**: 移除所有 `cat <<EOF` 整包覆寫 `threshold-config` 的寫法，全部改用 `patch_cm.py` 局部更新。
- **scenario-b/a.sh**: 新增 `get_cm_value()` helper，測試前保存原始值、結束後精確恢復，真正做到 tenant-agnostic。
- **scenario-c.sh**: cleanup 改用 `patch_cm.py default` 刪除 `_state_container_imagepull` key（三態恢復）。

#### patch_cm.py 增強
- 新增 `"default"` 值支援：傳入 `default` 時刪除 key（恢復三態中的 Default 狀態），若 tenant 無自訂值則移除整個 tenant 區塊。

#### 錯誤修正 & 過時內容清理
- **configmap-alertmanager.yaml**: 移除 `localhost:5001` dead webhook receiver，消除 Alertmanager `Connection refused` 噪音日誌。
- **scenario-c.sh**: 將 kube-state-metrics 部署提示從 `./scripts/deploy-kube-state-metrics.sh` 更新為 `make setup`。
- **README.md**: 更新 Project Structure，標註 kube-state-metrics 已整合至 `k8s/03-monitoring/`。

#### Token 優化
- 新增 `.claudeignore`：排除 `.git/`、`go.sum`、`vendor/`、`__pycache__/`、`charts/`、`*.tgz` 等非必要檔案，減少 AI Agent token 消耗。

## [Week 3] - State Matching & Weakest Link (2025-02-23)
### Features
- **Scenario C (State Matching)**:
  - Implemented `user_state_filter` metric (1.0 = enabled).
  - Alert Logic: `count * flag > 0` (Multiplication pattern).
  - Config: Added `state_filters` section and `_state_` prefix for per-tenant disable.
- **Scenario B (Weakest Link)**:
  - Integrated `kubelet-cadvisor` for container metrics.
  - Implemented `tenant:pod_weakest_cpu_percent:max` recording rules.
  - Added container-level thresholds to `threshold-exporter`.

### Infrastructure
- **kube-state-metrics**: 整合至 `k8s/03-monitoring/deployment-kube-state-metrics.yaml` (v2.10.0)，隨 `make setup` 自動部署。
- **Deprecated**: `scripts/deploy-kube-state-metrics.sh` (改用標準部署流程)。
- **setup.sh**: 新增 kube-state-metrics rollout status 等待。

### Verification (Dynamic — via MCP exec_in_pod)
- **Scenario B**: 端對端驗證通過 — cAdvisor → kube-state-metrics limits → recording rules → alert comparison。db-a CPU 3.1%, Memory 21%; db-b CPU 3.1%, Memory 23%。Alerts 正確保持 inactive (低於閾值)。
- **Scenario C**: 端對端驗證通過 — 建立 invalid image Pod → ImagePullBackOff → `ContainerImagePullFailure` alert 觸發 (db-a)。刪除 Pod 後 alert 正確解除。Disable 邏輯驗證: db-b 無 `container_crashloop` filter → `ContainerCrashLoop` alert 不觸發。

## [Week 2] - Config-Driven Architecture (2025-02-16)
### Refactor
- **Threshold Exporter**:
  - Moved from HTTP API to **YAML ConfigMap + Hot-reload**.
  - Implemented **Three-State Logic**: Custom Value / Default / Disable.
  - Removed per-tenant sidecars to avoid scalability issues.
- **Helm**: Refactored `threshold-exporter` into a full Helm chart with `checksum/config` auto-restart.

## [Week 1] - Foundation (2025-02-09)
### Setup
- **Renaming**: Project renamed to `dynamic-alerting-integrations`.
- **Normalization**: Established Prometheus Recording Rules layer (e.g., `tenant:mysql_cpu_usage:rate5m`).
- **Skills**: Created `diagnose-tenant` script for automated health checks.
- **Infrastructure**: Setup Kind cluster, MariaDB sidecars, and basic Monitoring stack.