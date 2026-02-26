# CLAUDE.md — AI 開發上下文指引

## 專案概述
**Multi-Tenant Dynamic Alerting** 平台 (v0.6.0)。
Config-driven (ConfigMap 掛載), Hot-reload (SHA-256 hash), Directory Scanner 模式 (`-config-dir`)。
6 個 Rule Pack 透過 Projected Volume 預載 (MariaDB, Kubernetes, Redis, MongoDB, Elasticsearch, Platform)，`optional: true` 支援選擇性卸載。
HA 架構: 2 Replicas + PodAntiAffinity + PDB + `max by(tenant)` 防 Double Counting。
Enterprise Governance: Prefix 隔離 (`custom_`)、Metric Dictionary、Triage Mode、Shadow Monitoring 驗證。

## 核心架構
- **Cluster**: Kind (`dynamic-alerting-cluster`)
- **Namespaces**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** (port 8080, ×2 HA): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤。
- **Prometheus**: Projected Volume 掛載 6 個 `configmap-rules-*.yaml` → `/etc/prometheus/rules/` (`optional: true`)
- **Normalization**: `tenant:<component>_<metric>:<function>` 格式
- **Scenario D**: 維護模式 (`unless`)、複合警報 (`and`)、嚴重度降級
- **HA 關鍵**: threshold recording rules 使用 `max by(tenant)` 聚合 `user_threshold` (非 `sum`)

## 開發規範
1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go 必須 `ReadHeaderTimeout`; Python 寫檔必須 `os.chmod(path, 0o600)`

## 文件架構
| 文件 | 內容 | 受眾 |
|------|------|------|
| `README.md` | 痛點/解決方案 + 架構圖 + Quick Start | 技術主管、初訪者 |
| `docs/architecture-and-design.md` | 效能分析、HA 設計、治理、SAST | Platform Engineers |
| `docs/migration-guide.md` | scaffold/migrate 工具 + 5 場景 | Tenants, DevOps |
| `rule-packs/README.md` | 6 Rule Pack 規格與範本 | All |
| `components/threshold-exporter/README.md` | 元件架構、API、Config | Developers |

## 工具 (scripts/tools/)
- `patch_config.py <tenant> <metric_key> <value>`: 安全局部更新 ConfigMap
- `check_alert.py <alert_name> <tenant> [--prometheus URL]`: JSON alert 狀態
- `diagnose.py <tenant> [--prometheus URL]`: Exception-based 健康檢查
- `migrate_rule.py <rules.yml> [--triage] [--dry-run] [--interactive] [--no-prefix]`: 傳統 → 動態三件套 (v3: Triage CSV + Prefix 隔離 + Metric Dictionary)
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog] [-o DIR]`: 互動式 tenant config 產生器
- `validate_migration.py [--mapping FILE | --old Q --new Q] --prometheus URL`: Shadow Monitoring 驗證 (Recording Rule 數值 diff)
- `offboard_tenant.py <tenant> [--execute]`: 安全 Tenant 下架 (Pre-check + 移除)
- `deprecate_rule.py <metric_key...> [--execute]`: 規則/指標下架 (三步自動化)
- `metric-dictionary.yaml`: 啟發式指標對照字典 (外部 YAML，平台團隊可直接維護)

## AI Agent 環境
- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`（簡單查詢可用，複雜操作常 timeout → fallback docker exec）
- **Prometheus API**: 開發環境透過 `port-forward` + `localhost`；生產環境用 K8s Service (`prometheus.monitoring.svc.cluster.local:9090`)
- **檔案清理**: mounted workspace 無法從 VM 直接 rm → 用 `docker exec ... rm -f`
- 🚨 **Playbooks** (遇到問題時讀取):
  1. Windows/PowerShell/MCP 問題 → `docs/windows-mcp-playbook.md`
  2. K8s/測試/Benchmark 問題 → `docs/testing-playbook.md`
