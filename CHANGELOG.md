# Changelog

## [Unreleased] - Week 4: Composite Priority Logic
- **Goal**: Implement Scenario D (Composite Priority).
- **Plan**: Support condition-specific rules + fallback using `unless` / `or` logic.

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