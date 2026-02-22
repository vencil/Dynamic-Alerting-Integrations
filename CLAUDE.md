# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
驗證 **Multi-Tenant Dynamic Alerting** 架構。
**當前進度**: Week 4 - Scenario D (Composite Priority Logic).
**核心機制**: Config-driven (ConfigMap `threshold-config` 掛載), Hot-reload (無需重啟 Exporter Pod)。

## 核心組件與架構 (Architecture)
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (`monitoring` ns, port 8080): 將 YAML 配置轉換為 Prometheus Metrics。支援三態邏輯 (Custom/Default/Disable)。
- **kube-state-metrics**: 提供 K8s 狀態指標 (Scenario C 依賴)。
- **Prometheus Normalization Layer**: 統一指標命名格式 `tenant:<component>_<metric>:<function>`。

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

## AI Skills (MCP 工具箱)
我們提供了專屬腳本來節省 Token 與驗證時間：
- `diagnose-tenant`: `python3 .claude/skills/diagnose-tenant/scripts/diagnose.py`
- `update-config`: `python3 .claude/skills/update-config/scripts/patch_cm.py <tenant> <metric_key> <value>`
  - 支援三態: 自訂數值 / `"default"` (刪除 key，恢復預設) / `"disable"`
  - 所有測試腳本 (scenario-a/b/c.sh) 均已改用此工具，禁止 `cat <<EOF` 覆寫。

## AI Agent 環境 (MCP Connectivity)
- **Kubernetes MCP Server**: 可用。Context: `kind-dynamic-alerting-cluster`。
  - 支援: `kubectl_get`, `kubectl_apply`, `exec_in_pod`, `kubectl_logs`, `kubectl_scale`, `kubectl_patch` 等全功能。
  - **動態測試路徑**: exec 進 Prometheus Pod → `wget -qO-` 查詢 API (無需 port-forward)。
  - **Prometheus 查詢**: `exec_in_pod` → `wget -qO- "http://localhost:9090/api/v1/query?query=<PromQL>"`
  - **Exporter 查詢**: `exec_in_pod` → `wget -qO- "http://threshold-exporter.monitoring.svc:8080/metrics"`
- **Windows-MCP**: 可用，但 kubectl/kind 不在 Windows PATH (僅在 Dev Container 內)。
  - 用途限於: 檔案操作、PowerShell 指令、Docker Desktop 狀態。
- **注意**: kubeconfig 需從 Dev Container 匯出至 Windows `%USERPROFILE%\.kube\config` 才能讓 Kubernetes MCP 連線。