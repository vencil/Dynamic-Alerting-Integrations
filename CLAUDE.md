# CLAUDE.md — AI 開發上下文指引

## 專案 (v0.7.0)
Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。
- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: Projected Volume 掛載 6 個 Rule Pack (`optional: true`)。Recording rules 用 `max by(tenant)` (非 `sum`)
- **Enterprise**: Prefix 隔離 (`custom_`)、Metric Dictionary、Triage Mode、Shadow Monitoring

## 開發規範
1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go 必須 `ReadHeaderTimeout`; Python 寫檔必須 `os.chmod(path, 0o600)`; `subprocess` 禁止 `shell=True`

## 文件架構
| 文件 | 受眾 |
|------|------|
| `README.md` | 技術主管、初訪者 |
| `docs/architecture-and-design.md` | Platform Engineers |
| `docs/migration-guide.md` | Tenants, DevOps |
| `rule-packs/README.md` | All |
| `components/threshold-exporter/README.md` | Developers |

## 工具 (scripts/tools/)
- `patch_config.py <tenant> <key> <value>`: ConfigMap 局部更新
- `check_alert.py <alert> <tenant> [--prometheus URL]`: Alert 狀態 JSON
- `diagnose.py <tenant> [--prometheus URL]`: 健康檢查 JSON
- `migrate_rule.py <rules.yml> [--triage] [--dry-run] [--no-prefix]`: 傳統→動態 (Triage CSV + Prefix + Dictionary)
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog]`: 互動式 Tenant 配置產生器
- `validate_migration.py [--mapping FILE | --old Q --new Q] --prometheus URL`: Shadow Monitoring 數值 diff
- `offboard_tenant.py <tenant> [--execute]`: Tenant 下架 (Pre-check + 移除)
- `deprecate_rule.py <metric_key...> [--execute]`: Rule/Metric 下架 (三步自動化)
- `metric-dictionary.yaml`: 啟發式指標對照字典

## 共用函式庫 (scripts/_lib.sh)
Scenario / demo / benchmark 腳本透過 `source scripts/_lib.sh` 共用以下函式：

| 類別 | 函式 | 用途 |
|------|------|------|
| 日誌 | `log`, `warn`, `err`, `info` | 彩色輸出 |
| Port-forward | `setup_port_forwards [ns]` | 建立 Prometheus:9090 + Exporter:8080，PID 自動追蹤 |
| | `cleanup_port_forwards` | 清除所有已追蹤的 port-forward |
| Prometheus | `prom_query_value <promql> [default]` | 查詢單一數值 |
| | `get_alert_status <alertname> <tenant>` | 回傳 firing/pending/inactive/unknown |
| | `wait_for_alert <name> <tenant> <state> [timeout]` | 輪詢等待 alert 達到預期狀態 |
| Exporter | `get_exporter_metric <pattern>` | grep exporter /metrics 取值 |
| | `wait_exporter <pattern> <expected> [timeout]` | 等待 metric 出現/消失/達到特定值 |
| 環境 | `require_services [labels...]` | 確認 K8s 服務 Running |
| | `kill_port <port>` | 殺掉佔用端口的程序 |
| ConfigMap | `get_cm_value <tenant> <key>` | 讀取 threshold-config 的當前值 |

## AI Agent 環境
- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`（複雜操作常 timeout → fallback docker exec）
- **Prometheus API**: 開發環境 `port-forward` + `localhost`；生產環境 K8s Service (`prometheus.monitoring.svc.cluster.local:9090`)
- **檔案清理**: mounted workspace 無法從 VM 直接 rm → 用 `docker exec ... rm -f`
- 🚨 **Playbooks**: Windows/MCP → `docs/windows-mcp-playbook.md` | K8s/測試 → `docs/testing-playbook.md`
