# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
**Multi-Tenant Dynamic Alerting** 平台。
**當前進度**: Phase 2D 完成 — Migration Tooling 驗證 + Migration Guide 全面重寫。
**核心機制**: Config-driven (ConfigMap 掛載), Hot-reload (SHA-256 hash 比對), 支援單檔與目錄兩種模式。

## Phase 1 完成摘要 (Week 1-4)
- **Week 1-2**: Kind 叢集、MariaDB sidecar、Prometheus Recording Rules 正規化層、threshold-exporter (Go) 三態邏輯 + Helm chart。
- **Week 3**: Scenario B (Weakest Link — cAdvisor 容器資源)、Scenario C (State Matching — kube-state-metrics 狀態乘法)。
- **Week 4**: Scenario D (維護模式 `unless`、複合警報 `and`、多層嚴重度 `_critical` 後綴降級)、Tech Debt 清理、工具轉正 (`patch_config.py`, `check_alert.py`, `diagnose.py`)。

## Phase 2 規劃 (Roadmap)
### 2A — Migration Guide ✅
- `docs/migration-guide.md`: 完整遷移指南，含 Percona MariaDB 五種場景範例、Alertmanager routing 遷移、驗證流程、LLM 輔助批量轉換 Prompt。

### 2B — 多 DB 支援擴展 (待開發)
- 目標: 支援 MongoDB, Redis, Elasticsearch 等 DB 類型。
- 挑戰: 多維度指標 (Index/Queue 級別閾值)、字串狀態 (cluster health green/yellow/red)。
- 設計方向: 擴充 ConfigMap 語法支援標籤選擇器；沿用 Scenario C state_filter 處理狀態型指標。
- 需修改: threshold-exporter Go 程式碼 (config parser)。

### 2C — GitOps Self-Service ✅
- **Directory Scanner**: ConfigMap 拆分為 `_defaults.yaml` + 每租戶 `<tenant>.yaml`，排序合併。
- **邊界規則**: `state_filters` / `defaults` 僅允許在 `_` 前綴檔案，租戶檔僅含 `tenants` 區塊，違規自動忽略 + WARN log。
- **雙模式**: `-config` (單檔) / `-config-dir` (目錄)，自動偵測，向下相容。
- **Hot-reload**: SHA-256 hash 比對 (取代 ModTime，K8s symlink rotation 更可靠)。
- **工具適配**: `patch_config.py` 雙模式自動偵測；`_lib.sh` 共用 `get_cm_value()`。
- **測試**: 20 單元測試通過 + `tests/integration-2c.sh` 整合驗證 (15/16 PASS，1 個 K8s timing)。
- **待擴展**: GitOps Repo + CI/CD pipeline。

### 2D — Migration Tooling ✅
- **`migrate_rule.py`**: 80/20 自動轉換工具，三種情境 (完美解析 / 複雜表達式+TODO / LLM Fallback)。
- **Bug Fix**: `base_key` 提取跳過 PromQL 函式名 (`rate`→metric)；`absent()` 等語義不同函式歸入 LLM Fallback。
- **測試**: `tests/legacy-dummy.yml` (4 條規則覆蓋 3 種情境) + `tests/test-migrate-tool.sh` (13 assertions PASS)。
- **Migration Guide 重寫**: 以正規化層、聚合模式選擇 (max vs sum)、工具核心流程為骨架，保留五種場景範例。

## 核心組件與架構 (Architecture)
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (`monitoring` ns, port 8080): YAML → Prometheus Metrics。三態邏輯 + `_critical` 多層嚴重度 + `default_state` 控制。支援單檔 (`-config`) 與目錄 (`-config-dir /etc/threshold-exporter/conf.d`) 兩種模式。
- **kube-state-metrics**: K8s 狀態指標 (Scenario C)。
- **Prometheus Normalization Layer**: `tenant:<component>_<metric>:<function>` 格式。
- **Scenario D 機制**: 維護模式 (`unless`)、複合警報 (`and`)、多層嚴重度降級。

## 開發與操作規範 (Strict Rules)
1. **ConfigMap 修改**: 禁止 `cat <<EOF` 覆寫。用 `kubectl patch` / `helm upgrade` / `patch_config.py`。
2. **Tenant-agnostic**: Go 與 PromQL 中禁止 Hardcode Tenant ID。
3. **三態邏輯**: Custom / Default (省略) / Disable (`"disable"`)。
4. **Doc-as-Code**: 功能完成後同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`。
5. **Makefile**: `make setup` (一鍵部署), `make port-forward` (9090/3000/8080)。

## 專案工具 (scripts/tools/)
- `patch_config.py <tenant> <metric_key> <value>`: 安全局部更新 ConfigMap (三態，自動偵測單檔/目錄模式)。
- `check_alert.py <alert_name> <tenant>`: JSON 回傳 alert 狀態 (firing/pending/inactive)。
- `diagnose.py <tenant>`: Exception-based 健康檢查。
- `migrate_rule.py <legacy-rules.yml>`: 傳統 alert rules → 動態多租戶三件套 (Tenant Config + Recording Rule + Alert Rule)。

## AI Agent 環境與排錯指南 (MCP & Troubleshooting)
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`。
- **Windows-MCP (Dev Container)**: 必須透過 `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>` 執行指令。切勿使用管線 (`|`) 抓輸出，請用 `Start-Process` 重定向檔案。
- 🚨 **重要排錯手冊 (Playbooks)**:
  為了節省 Token，詳細的踩坑紀錄與最佳實踐已抽離。當你遇到以下情況時，**必須先讀取對應文件**：
  1. 遇到 Windows/PowerShell 指令卡住、無輸出：請讀取 `docs/windows-mcp-playbook.md`。
  2. 遇到 K8s ConfigMap 延遲、測試腳本報錯、環境不乾淨：請讀取 `docs/testing-playbook.md`。
