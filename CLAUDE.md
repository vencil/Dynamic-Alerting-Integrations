# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
驗證 **Multi-Tenant Dynamic Alerting** 架構。
**當前進度**: Week 4 - Scenario D (Composite Priority Logic).
**核心機制**: Config-driven (ConfigMap `threshold-config` 掛載), Hot-reload (無需重啟 Exporter Pod)。

## 核心組件與架構 (Architecture)
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (`monitoring` ns, port 8080): 將 YAML 配置轉換為 Prometheus Metrics。支援三態邏輯 (Custom/Default/Disable)。支援 `_critical` 後綴產生多層級嚴重度 threshold。`default_state` 欄位控制 state_filter 預設行為。
- **kube-state-metrics**: 提供 K8s 狀態指標 (Scenario C 依賴)。
- **Prometheus Normalization Layer**: 統一指標命名格式 `tenant:<component>_<metric>:<function>`。
- **Scenario D 機制**:
  - 維護模式: `_state_maintenance: enable` → `user_state_filter{filter="maintenance"}=1` → `unless` 抑制所有常規 alert。
  - 複合警報: `MariaDBSystemBottleneck` = `connections > threshold AND cpu > threshold`。
  - 多層級嚴重度: `mysql_connections_critical: "90"` → 額外 `severity="critical"` threshold；Warning alert 含 `unless` 降級。

## 開發與操作規範 (Strict Rules)
1. **ConfigMap 修改規範 (重要)**：絕對**禁止**在測試腳本或指令中使用 `cat <<EOF` 整包覆寫 `threshold-config`。必須使用 `kubectl patch`、`helm upgrade`，或呼叫 `update-config` skill 進行局部更新，以免洗掉其他設定。
2. **多租戶隔離 (Tenant-agnostic)**：Go 程式碼與 PromQL Recording Rules 中禁止 Hardcode Tenant ID。
3. **三態邏輯**: Custom 數值 / Default (省略 Key) / Disable (設定為 `"disable"`)。
4. **文件同步規範 (Doc-as-Code)**：每次完成功能開發或重構後，**必須**同步更新以下文件再回報完成：
   - `CHANGELOG.md`: 記錄變更摘要 (放在對應的 Week/Unreleased 區塊)。
   - `CLAUDE.md`: 若涉及新 Skill、新規範、架構變動，更新對應區段。
   - `README.md`: 若涉及 Project Structure 或使用方式變動。
5. **Makefile 操作**:
   - `make setup`: 一鍵部署 (包含 Kind, DB, Monitoring, Exporter)。
   - `make port-forward`: 開啟 9090 (Prometheus), 3000 (Grafana), 8080 (Exporter)。

## 專案工具 (scripts/tools/)
標準化的自動化工具，供測試腳本與 AI Agent 共用：
- `patch_config.py`: `python3 scripts/tools/patch_config.py <tenant> <metric_key> <value>`
  - 安全局部更新 `threshold-config` ConfigMap。支援三態: 自訂數值 / `"default"` (刪除 key，恢復預設) / `"disable"`。
  - 所有測試腳本 (scenario-a/b/c/d.sh) 均使用此工具，禁止 `cat <<EOF` 覆寫。
- `check_alert.py`: `python3 scripts/tools/check_alert.py <alert_name> <tenant>`
  - 回傳 JSON: `{alert, tenant, state: "firing"|"pending"|"inactive"}`。需 port-forward 9090。
- `diagnose.py`: `python3 scripts/tools/diagnose.py <tenant>`
  - Exception-based reporting：正常回傳 `{"status":"healthy"}`，異常時附帶錯誤與日誌。

## AI Agent 環境 (MCP Connectivity)
- **Kubernetes MCP Server**: 可用。Context: `kind-dynamic-alerting-cluster`。
  - 支援: `kubectl_get`, `kubectl_apply`, `exec_in_pod`, `kubectl_logs`, `kubectl_scale`, `kubectl_patch` 等全功能。
  - **動態測試路徑**: exec 進 Prometheus Pod → `wget -qO-` 查詢 API (無需 port-forward)。
  - **Prometheus 查詢**: `exec_in_pod` → `wget -qO- "http://localhost:9090/api/v1/query?query=<PromQL>"`
  - **Exporter 查詢**: `exec_in_pod` → `wget -qO- "http://threshold-exporter.monitoring.svc:8080/metrics"`
- **Windows-MCP**: 可用，但 kubectl/kind 不在 Windows PATH (僅在 Dev Container 內)。
  - 用途限於: 檔案操作、PowerShell 指令、Docker Desktop 狀態。
- **注意**: kubeconfig 需從 Dev Container 匯出至 Windows `%USERPROFILE%\.kube\config` 才能讓 Kubernetes MCP 連線。