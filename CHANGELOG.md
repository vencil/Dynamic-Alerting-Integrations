# Changelog

## [Phase 2D] - Migration Tooling (2026-02-24)

### migrate_rule.py 驗證與修復
- **Bug Fix — base_key 提取**: 新增 PromQL 函式名過濾表 (rate, absent, sum, avg 等 40+ 函式)，確保 `rate(mysql_global_status_slow_queries[5m])` 正確提取 metric 名稱而非函式名。
- **語義不可轉換偵測**: `absent()`, `predict_linear()`, `vector()` 等改變向量語義的函式自動歸入「無法解析」情境，交由 LLM Fallback 處理。
- **測試**: 新增 `tests/legacy-dummy.yml` (4 條規則覆蓋 3 種情境) + `tests/test-migrate-tool.sh` (13 assertions, 全部 PASS)。

### Migration Guide 全面重寫 (`docs/migration-guide.md`)
- **正規化層 (Step 0)**: 新增章節說明為何遷移前必須先建立 `tenant:` 正規化層，以抹平單節點 vs. 叢集差異。
- **聚合模式選擇 (Step 2)**: 新增 Max (最弱環節) vs. Sum (叢集總量) 架構決策指南，附決策矩陣。
- **工具核心流程 (Step 1)**: 以 `migrate_rule.py` 作為遷移入口，說明三種處理情境與「三件套」輸出。
- **保留精華**: 五種場景範例 (連線數、多層嚴重度、Replication Lag、慢查詢 Rate、Buffer Pool 百分比)、Alertmanager routing 遷移、驗證 Checklist、LLM System Prompt、目錄模式注意事項。

### 文件同步
- **CLAUDE.md**: Phase 2D 標記 ✅，新增 `migrate_rule.py` 工具說明。
- **CHANGELOG.md**: 新增 Phase 2D 完整記錄。

## [Phase 2C] - GitOps Directory Scanner (2026-02-24)

### 整合測試與修復 (Integration Test Fixes)
- **_lib.sh `get_cm_value()`**: 修復 JSON 嵌入問題 — 改用 `json.load(sys.stdin)` 管道取代 `'''${var}'''` 字串嵌入，避免多行 YAML 值破壞 Python string。
- **integration-2c.sh**: 新增測試前置清理 (port-forward 殘留、ConfigMap 值恢復)；修正 metrics label 順序 grep (`metric.*tenant` 而非 `tenant.*metric`)；hot-reload 等待從 20s 調至 45s。
- **Helm ConfigMap 遷移**: 記錄 `helm upgrade` 不會清理舊 `config.yaml` key 問題，需 `kubectl delete cm` + 重新 deploy。
- **CLAUDE.md**: 新增 AI Agent MCP 操作最佳實踐 (Windows-MCP Start-Process 模式、7 個常見陷阱)、測試前置準備 Checklist、5 個已知問題與修復方案。

### threshold-exporter 目錄模式
- **config.go**: `loadDir()` 目錄掃描器 — 排序合併 `_defaults.yaml` + 租戶檔。邊界規則強制 (`state_filters`/`defaults` 僅 `_` 前綴檔)。SHA-256 hash 取代 ModTime 偵測變更。
- **main.go**: 新增 `-config-dir` flag，`resolveConfigPath()` 自動偵測單檔/目錄，向下相容 `-config`。
- **config_test.go**: 新增 6 組目錄模式測試 (BasicMerge, BoundaryEnforcement, HashChange, EmptyDir, SkipsHidden, CriticalSuffix)。

### Helm Chart
- **configmap.yaml**: 單 key → 多 key (`_defaults.yaml` + `<tenant>.yaml`)，由 `values.yaml` range 生成。
- **deployment.yaml**: args 改用 `-config-dir`，mount path `/etc/threshold-exporter/conf.d`。
- **config/conf.d/**: 新增範例目錄 (`_defaults.yaml`, `db-a.yaml`, `db-b.yaml`)。

### 工具與測試
- **patch_config.py**: 重寫，`detect_mode()` 自動偵測 ConfigMap 格式，雙模式 patching。
- **_lib.sh**: 新增共用 `get_cm_value()` 支援雙模式。
- **Makefile**: 補 `test-scenario-d` target。

### 文件
- **migration-guide.md**: 新增 Section 8「目錄模式架構注意事項」(邊界規則表、相容性、hot-reload)。

## [Phase 2A] - Migration Guide (2026-02-24)
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