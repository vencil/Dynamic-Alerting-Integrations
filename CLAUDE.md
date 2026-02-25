# CLAUDE.md — AI 開發上下文指引

## 專案概述
**Multi-Tenant Dynamic Alerting** 平台 (v0.4.1)。
Config-driven (ConfigMap 掛載), Hot-reload (SHA-256 hash), Directory Scanner 模式 (`-config-dir`)。
5 個 Rule Pack 透過 Projected Volume 預載 (MariaDB, Kubernetes, Redis, MongoDB, Elasticsearch)。

## 核心架構
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤。
- **Prometheus**: Projected Volume 掛載 5 個 `configmap-rules-*.yaml` → `/etc/prometheus/rules/`
- **Normalization**: `tenant:<component>_<metric>:<function>` 格式
- **Scenario D**: 維護模式 (`unless`)、複合警報 (`and`)、嚴重度降級

## 開發規範
1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go 必須 `ReadHeaderTimeout`; Python 寫檔必須 `os.chmod(path, 0o600)`

## 工具 (scripts/tools/)
- `patch_config.py <tenant> <metric_key> <value>`: 安全局部更新 ConfigMap
- `check_alert.py <alert_name> <tenant>`: JSON alert 狀態
- `diagnose.py <tenant>`: Exception-based 健康檢查
- `migrate_rule.py <rules.yml> [-o DIR] [--dry-run] [--interactive]`: 傳統 → 動態三件套
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog] [-o DIR]`: 互動式 tenant config 產生器

## AI Agent 環境
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`
- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- 🚨 **Playbooks** (遇到問題時讀取):
  1. Windows/PowerShell 問題 → `docs/windows-mcp-playbook.md`
  2. K8s/測試問題 → `docs/testing-playbook.md`
