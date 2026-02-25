# CLAUDE.md — AI 開發上下文指引

## 專案概述 (Current Status)
**Multi-Tenant Dynamic Alerting** 平台。
**當前版本**: v0.4.0 — Ease of Adoption (Phase 3 進行中)。
**核心機制**: Config-driven (ConfigMap 掛載), Hot-reload (SHA-256 hash 比對), 支援單檔與目錄兩種模式。

## 專案里程碑 (Milestones)
- **v0.1.0 (Phase 1)**: 動態閾值 threshold-exporter (Go)、三態邏輯、cAdvisor/KSM 整合 (Scenario B/C)、Scenario D (維護模式 `unless`、複合警報 `and`、多層嚴重度 `_critical` 降級)。
- **v0.2.0 (Phase 2A/C/D)**: GitOps 目錄掃描模式 (`-config-dir`、SHA-256 hash)、`migrate_rule.py` 80/20 自動轉換工具 (三種情境)、`docs/migration-guide.md` 完整遷移指南。
- **v0.3.0 (Phase 2B - Current)**: Dimensional Metrics — `"metric{label=\"value\"}"` 維度標籤 (Redis/ES/MongoDB)、Unchecked Collector 動態 Descriptor、`extract_label_matchers()` PromQL 維度偵測、權威範本 (`conf.d/examples/`)。
- **設計約束**: 維度 key 不支援 `_critical` 後綴（改用 `"value:critical"`）；維度 key 為 tenant-only，不繼承 defaults。
- **v0.4.0 (Phase 3 - Current)**: Ease of Adoption — 降低導入難度。
  - **3A** ✅: SAST 修復 (`shell=True` 移除、Go test 權限縮緊) + `migrate_rule.py` UX 大升級 (智能聚合猜測 heuristics、檔案化輸出 `migration_output/`、`--dry-run`/`--interactive` flags)。
  - **3B** ✅: Rule Packs 模組包架構 — `rule-packs/` 目錄含 5 個 pack (kubernetes/mariadb 預設、redis/mongodb/elasticsearch 選配)。每個 pack 含三件套 (normalization + threshold + alert rules)。Helm values overlay 掛載方式。
  - **3C** ✅: `scaffold_tenant.py` 互動式 tenant config 產生器 (含 `--catalog` exporter 清單、`--non-interactive` 模式) + `make demo` 端對端示範 + `test-scaffold.sh` 測試。

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
- `migrate_rule.py <legacy-rules.yml> [-o DIR] [--dry-run] [--interactive]`: 傳統 alert rules → 動態多租戶三件套。智能猜測聚合模式 (sum/max)，檔案化輸出至 `migration_output/` (tenant-config.yaml、platform-recording-rules.yaml、platform-alert-rules.yaml、migration-report.txt)。
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog] [-o DIR]`: 互動式 tenant config 產生器。支援 5 種 DB 類型 (kubernetes/mariadb/redis/mongodb/elasticsearch)，自動生成 _defaults.yaml + tenant.yaml + scaffold-report.txt (含 Helm 部署指令)。

## AI Agent 環境與排錯指南 (MCP & Troubleshooting)
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`。
- **Windows-MCP (Dev Container)**: 必須透過 `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>` 執行指令。切勿使用管線 (`|`) 抓輸出，請用 `Start-Process` 重定向檔案。
- 🚨 **重要排錯手冊 (Playbooks)**:
  為了節省 Token，詳細的踩坑紀錄與最佳實踐已抽離。當你遇到以下情況時，**必須先讀取對應文件**：
  1. 遇到 Windows/PowerShell 指令卡住、無輸出：請讀取 `docs/windows-mcp-playbook.md`。
  2. 遇到 K8s ConfigMap 延遲、測試腳本報錯、環境不乾淨：請讀取 `docs/testing-playbook.md`。
