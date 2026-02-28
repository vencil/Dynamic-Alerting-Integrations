# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v0.13.0)
Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。

- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: Projected Volume 掛載 9 個 Rule Pack (`optional: true`)。Recording rules 用 `max by(tenant)` (非 `sum`)
- **Enterprise**: Prefix 隔離 (`custom_`)、Metric Dictionary、Triage Mode、Shadow Monitoring
- **Load Injection**: `run_load.sh` 支援 connections / cpu / stress-ng / composite 四種負載類型，整合進 demo + scenario

## 版本歷程
| Phase | 版本 | 核心內容 |
|-------|------|---------|
| 1 | v0.1.0 | Scenario A~D (動態閾值/弱環節/狀態比對/維護模式+複合+多層) |
| 2 | v0.2.0~v0.3.0 | Directory Scanner, SHA-256 Hot-reload, 維度標籤, migrate_rule v1~v2 |
| 3 | v0.4.0 | Projected Volume 5 Rule Packs, scaffold_tenant, SAST 修復 |
| 4 | v0.5.0 | HA ×2, PDB, Anti-Affinity, Platform Self-Monitoring (第 6 個 Rule Pack) |
| 5 | v0.6.0 | migrate_rule v3 (Triage/Prefix/Dictionary), Shadow Monitoring, offboard/deprecate 工具, 72 測試案例 |
| 6 | v0.7.0 | Load Injection Toolkit, _lib.sh 模組化, demo-full, 文件 + 企業價值主張更新, 34 測試案例 |
| 7 | v0.8.0 | Composite Load, Scenario E/F, Shadow Monitoring SOP, Baseline Discovery, 版本統一, 28 測試案例 |
| 8 | v0.9.0 | BYOP 整合指南, da-tools CLI 容器, CI/CD 版號治理, 測試矩陣 + Mermaid 流程圖, 15 測試案例 |
| 9 | v0.10.0 | 三層治理模型 + RnR + CI deny-list linting + 文件重整, 51 測試案例 |
| 10 | **v0.11.0** | AST 遷移引擎 (promql-parser) — migrate_rule v4, tenant label 注入, 54 測試案例 |
| 11 | **v0.12.0** | Exporter 核心擴展 — B1 Regex 維度閾值 (`=~` + `_re` label) + B4 排程式閾值 (ScheduledValue + ResolveAt), 56 測試案例 |
| 12 | **v0.13.0** | Enterprise DB Rule Packs (Oracle + DB2 + ClickHouse) + benchmark `--under-load` + Go micro-benchmark, 50 測試案例 |

## 規劃中 Phases

### Phase 13 (v0.14.0) — 敘事重寫
- README 痛點對比表更新：納入治理維度 (從「自動化工具」升級到「多租戶監控治理平台」)
- 商業價值主張重寫：整合 RnR 框架、三層治理、AST 遷移、多 DB 支援
- `README.en.md` 同步更新
- 確保所有文件用語風格一致 (客觀工程語言，不含推銷用語)

## Backlog (不在近期 Phase 規劃內)
- B5: Log-based 錯誤偵測 (ORA-600) — 非 metrics 路線，建議作為獨立 companion project
  - 生態系解法：引導客戶用 grok_exporter / mtail 將 log 轉為 Prometheus metric，再由本平台接管閾值管理。可在 BYOP 整合指南補充附錄段落

## 開發規範
1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go 必須 `ReadHeaderTimeout`; Python 寫檔必須 `os.chmod(path, 0o600)`; `subprocess` 禁止 `shell=True`
6. **推銷語言不進 repo**: README 保持客觀工程語言；Pitch Deck 獨立產出
7. **版號治理**: 打 tag 前必須 `make version-check`；更新版號用 `make bump-docs`

## 文件架構
| 文件 | 受眾 | 備註 |
|------|------|------|
| `README.md` / `README.en.md` | 技術主管、初訪者 | 含痛點對比 + 企業價值主張表 |
| `docs/architecture-and-design.md` | Platform Engineers | O(M) 推導 + Benchmark 在 §4.1–4.2 |
| `docs/migration-guide.md` | Tenants, DevOps | 含遷移安全保證陳述 |
| `docs/byo-prometheus-integration.md` | Platform Engineers, SREs | BYOP 最小整合 (tenant label、scrape、rule mount) + Operator 附錄 |
| `docs/custom-rule-governance.md` | Platform Leads, Domain Experts, Tenant Tech Leads | 三層治理模型 + RnR 權責 + SLA 切割 + CI Linting |
| `components/da-tools/README.md` | All | 可攜帶 CLI 容器：驗證整合、遷移規則、scaffold tenant |
| `docs/shadow-monitoring-sop.md` | SRE, Platform Engineers | Shadow Monitoring 完整 SOP runbook |
| `docs/internal/testing-playbook.md` | Contributors (AI Agent) | K8s 環境 + shell 陷阱 |
| `docs/internal/windows-mcp-playbook.md` | Contributors (AI Agent) | Dev Container 操作手冊 |
| `rule-packs/README.md` | All | 含 `optional: true` 卸載文件 |
| `components/threshold-exporter/README.md` | Developers | |

## 工具 (scripts/tools/)
- `patch_config.py <tenant> <key> <value>`: ConfigMap 局部更新
- `check_alert.py <alert> <tenant> [--prometheus URL]`: Alert 狀態 JSON
- `diagnose.py <tenant> [--prometheus URL]`: 健康檢查 JSON
- `migrate_rule.py <rules.yml> [--triage] [--dry-run] [--no-prefix] [--no-ast]`: 傳統→動態 (Triage CSV + Prefix + Dictionary + AST Engine)
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog]`: 互動式 Tenant 配置產生器
- `validate_migration.py [--mapping FILE | --old Q --new Q] --prometheus URL`: Shadow Monitoring 數值 diff
- `offboard_tenant.py <tenant> [--execute]`: Tenant 下架 (Pre-check + 移除)
- `deprecate_rule.py <metric_key...> [--execute]`: Rule/Metric 下架 (三步自動化)
- `baseline_discovery.py <--tenant NAME> [--duration S --interval S --metrics LIST]`: 負載觀測 + 閾值建議
- `bump_docs.py [--platform VER] [--exporter VER] [--tools VER] [--check]`: 版號一致性管理 (三條版號線批次更新 + CI lint)
- `lint_custom_rules.py <path...> [--policy FILE] [--ci]`: Custom Rule deny-list linter (治理合規檢查)
- `metric-dictionary.yaml`: 啟發式指標對照字典

## 共用函式庫 (scripts/_lib.sh)
Scenario / benchmark 腳本透過 `source scripts/_lib.sh` 共用（demo.sh 有自己的 `_demo_` helpers 不引用 _lib.sh）：

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

## Makefile 語義區分
- `make test-alert`: **硬體故障/服務中斷測試** — Kill process 模擬 Hard Outage
- `make demo-full`: **動態負載展演** — Composite Load (conn+cpu) → alert 觸發 → 清除 → 恢復
- `make demo`: 快速模式 (scaffold + migrate + diagnose + baseline_discovery，不含負載)
- `make test-scenario-{a,b,e} ARGS=--with-load`: Scenario 真實負載模式
- `make test-scenario-e`: Multi-tenant 隔離測試
- `make test-scenario-f`: HA 故障切換測試
- `make load-composite TENANT=db-a`: 複合負載 (connections + cpu)
- `make baseline-discovery TENANT=db-a`: 觀測指標 + 閾值建議
- `make version-check`: 版號一致性 CI lint
- `make version-show`: 顯示三條版號線現狀
- `make bump-docs PLATFORM=x EXPORTER=x TOOLS=x`: 批次更新版號引用

## AI Agent 環境
- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`（複雜操作常 timeout → fallback docker exec）
- **Prometheus API**: 開發環境 `port-forward` + `localhost`；生產環境 K8s Service (`prometheus.monitoring.svc.cluster.local:9090`)
- **檔案清理**: mounted workspace 無法從 VM 直接 rm → 用 `docker exec ... rm -f`（Cowork 環境需 `allow_cowork_file_delete`）
- 🚨 **Playbooks**: Windows/MCP → `docs/internal/windows-mcp-playbook.md` | K8s/測試 → `docs/internal/testing-playbook.md`
