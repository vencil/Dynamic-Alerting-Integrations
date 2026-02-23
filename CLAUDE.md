# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
**Multi-Tenant Dynamic Alerting** 平台。
**當前進度**: Phase 2 起步 — Migration Guide 完成，準備進入擴展支援與 GitOps 設計。
**核心機制**: Config-driven (ConfigMap `threshold-config` 掛載), Hot-reload (無需重啟 Exporter Pod)。

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

### 2C — GitOps Self-Service (待開發)
- 目標: 租戶透過 Git PR 管理自己的閾值配置。
- 設計方向: 新建 `tenant-alert-configs` Repo (純 YAML)；CI/CD 自動 apply 至 K8s ConfigMap；exporter 改為監聽多個帶 label 的 ConfigMap。
- 關鍵原則: **租戶只寫 Threshold YAML，不寫 PromQL**。

### 2D — Migration Tooling (待開發)
- 目標: 自動化遷移工具，讀取傳統 `rules.yml` 並產生 `threshold-config.yaml`。
- 可結合 LLM Prompt (已在 migration-guide.md 中定義)。

## 核心組件與架構 (Architecture)
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (`monitoring` ns, port 8080): YAML → Prometheus Metrics。三態邏輯 + `_critical` 多層嚴重度 + `default_state` 控制。
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
- `patch_config.py <tenant> <metric_key> <value>`: 安全局部更新 ConfigMap (三態)。
- `check_alert.py <alert_name> <tenant>`: JSON 回傳 alert 狀態 (firing/pending/inactive)。
- `diagnose.py <tenant>`: Exception-based 健康檢查。

## AI Agent 環境 (MCP Connectivity)
- **Kubernetes MCP Server**: Context `kind-dynamic-alerting-cluster`。全功能 kubectl 操作。
  - Prometheus 查詢: `exec_in_pod` → `wget -qO- "http://localhost:9090/api/v1/query?query=<PromQL>"`
  - Exporter 查詢: `exec_in_pod` → `wget -qO- "http://threshold-exporter.monitoring.svc:8080/metrics"`
- **Windows-MCP**: 限檔案操作與 PowerShell。kubectl/kind 僅在 Dev Container 內可用。
