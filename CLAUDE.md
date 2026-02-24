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

## AI Agent 環境 (MCP Connectivity)

### Kubernetes MCP Server
- Context: `kind-dynamic-alerting-cluster`。全功能 kubectl 操作。
- Prometheus 查詢: `exec_in_pod` → `wget -qO- "http://localhost:9090/api/v1/query?query=<PromQL>"`
- Exporter 查詢: `exec_in_pod` → `wget -qO- "http://threshold-exporter.monitoring.svc:8080/metrics"`
- **注意**: Kubernetes MCP 直接連 Kind 叢集，若 Kind 未啟動會 timeout。先確認叢集存在再操作。

### Windows-MCP (Dev Container)
- **注意**: kubectl/kind/go 僅在 Dev Container 內可用，Windows Shell 無法直接執行。
- **執行指令**: 必須透過 `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`。
- **PowerShell 陷阱**: 切勿使用管線 (`|`) 直接抓 docker 輸出。請使用 `Start-Process` 將輸出 Redirect 到檔案再讀取。
- **詳細排錯與語法**: 若遇 timeout、無輸出或執行錯誤，請務必先讀取 `docs/windows-mcp-playbook.md` 參考最佳實踐。

## 測試注意事項 (Testing Caveats)
- **前置準備**: 確保 Dev Container、Kind 叢集、kubeconfig 狀態正常，並隨時用 `pkill -f port-forward` 清理殘留。
- **已知雷區**: K8s ConfigMap volume 傳播有 30-90s 延遲；注意 grep metrics 時的 label 順序；留意中斷測試可能造成的 ConfigMap 髒資料殘留。
- **詳細排錯**: 若遇測試腳本失敗、環境異常或狀態不同步，**請務必優先查閱 `docs/testing-playbook.md`** 獲取完整的已知問題 (Known Issues) 與修復指令。
