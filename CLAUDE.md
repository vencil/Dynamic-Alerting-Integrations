# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v1.7.0)
Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。

- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: Projected Volume 掛載 10 個 Rule Pack (`optional: true`)。Threshold normalization 用 `max by(tenant)` 防 HA 翻倍；Data normalization 依語義選擇聚合方式（connections 用 `max`，rate/ratio 用 `sum`）
- **三態運營模式**: Normal（預設）/ Silent（`_silent_mode`，TSDB 有紀錄但不通知）/ Maintenance（`_state_maintenance`，完全不觸發）
- **Severity Dedup**: Per-tenant Alertmanager inhibit（非 PromQL unless）。`metric_group` label 配對 warning/critical，`generate_alertmanager_routes.py` 產出 per-tenant inhibit rules。Sentinel `TenantSeverityDedupEnabled` 供 Grafana 面板顯示。TSDB 永遠完整
- **Alert Routing**: Tenant YAML `_routing` section → `generate_alertmanager_routes.py` 產出 Alertmanager route + receiver + inhibit_rules fragment。Timing guardrails 平台強制。v1.4.0 支援 webhook / email / slack / teams / rocketchat / pagerduty 六種 receiver
- **Enterprise**: Prefix 隔離 (`custom_`)、Metric Dictionary、Triage Mode、Shadow Monitoring
- **Distribution**: OCI registry (`oci://ghcr.io/vencil/charts/threshold-exporter`) + Docker images (`ghcr.io/vencil/threshold-exporter`, `ghcr.io/vencil/da-tools`)
- **Load Injection**: `run_load.sh` 支援 connections / cpu / stress-ng / composite 四種負載類型，整合進 demo + scenario

版本歷程詳見 `CHANGELOG.md`。v1.0.0 為 GA Release，後續版本視社群/客戶回饋決定。

## 關鍵架構決策（累積）

以下為跨版本需長期參照的架構決策。完整變更詳見 `CHANGELOG.md`。

1. **Severity Dedup 架構**：dedup 從 PromQL 移到 Alertmanager，TSDB 永遠有完整 warning+critical 紀錄。`generate_alertmanager_routes.py` 掃描 `_severity_dedup` → 產出 per-tenant inhibit rules（`tenant="<name>"` + `metric_group=~".+"` matcher）
2. **Sentinel Alert 模式**：exporter flag metric → Prometheus fires sentinel → Alertmanager inhibit。已用於 silent mode (`TenantSilentMode`) 和 severity dedup (`TenantSeverityDedupEnabled`)
3. **Receiver 結構化物件**：`_routing.receiver` 為 `{type, ...fields}` 物件（v1.3.0 起，不向後相容純 URL）。`RECEIVER_TYPES` 常數 Python + Go 兩端一致
4. **Routing Defaults 三態延伸**：`_routing_defaults` in `_defaults.yaml`，tenant 三態（繼承/覆寫/disable）。`{{tenant}}` 佔位符 merge 時替換
5. **Routing Guardrails**：`group_wait` 5s–5m、`group_interval` 5s–5m、`repeat_interval` 1m–72h，Go + Python 兩端一致
6. **Alertmanager 動態化**：`--web.enable-lifecycle` + `configmap-reload` sidecar 自動 reload。`--apply` 一站式 ConfigMap merge + reload
7. **Webhook Domain Allowlist**：`--policy` 載入 `allowed_domains`，`--validate` 時檢查 receiver URL host（fnmatch 萬用字元）。空清單 = 不限制（向後相容）
8. **Tenant Config Schema Validation**：Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` 警告未知/typo key。`load_tenant_configs()` 回傳 4-tuple（routing, dedup, schema_warnings, enforced_routing）
9. **Cardinality Guard**：Go `ResolveAt()` per-tenant metric 計數上限（`max_metrics_per_tenant`，default 500）。超限 truncate + log ERROR
10. **共用 Python 函式庫**：`scripts/tools/_lib_python.py` 提供 `parse_duration_seconds()`、`is_disabled()`、`load_yaml_file()`，消除 Python 工具間重複實作
11. **GitOps RBAC**：CODEOWNERS 檔案級權限 + CI pipeline 自動驗證 + `make configmap-assemble` 組裝。ArgoCD/Flux 部署指南含三層變更流程（Standard / Break-Glass / Drift Reconciliation）
12. **Platform Dashboard**：Grafana JSON — 11 面板涵蓋租戶三態狀態、配置分佈、cardinality 監控、reload 活動追蹤。獨立 ConfigMap 部署

## v1.7.0：企業防護網 (Enterprise Guardrails) ✅

### [P0] Silent/Maintenance Mode 自動失效 ✅
- `_silent_mode` / `_state_maintenance` 升級為結構化物件（向後相容純量字串）
- Schema：`{target, expires (ISO8601), reason}`，Go 引擎 `time.Now().After(expires)` 過期即停止 emit sentinel metric
- 失效通知：瞬時 gauge `da_config_event{event="silence_expired"}` + TTL（emit 數個 scrape 週期後停止），搭配 `TenantConfigEvent` alert rule（`for: 0s`）
- `_state_maintenance` 同步支援 `expires`（比 silent mode 更需要防失憶——它在 PromQL 層消滅 alert，TSDB 無紀錄）

### [P0] Platform Enforced Routing ✅
- `_defaults.yaml` 新增 `_routing_enforced` section（`enabled`, `receiver`, `match`）
- `generate_alertmanager_routes.py` 在 tenant route 之前插入 platform route，帶 `continue: true` 實現「NOC 必收 + tenant 也收」雙軌通知
- 預設不啟用，Platform Team 按需開啟

### [P0] PostgreSQL Rule Pack ✅
- 標準三件式（Normalization → Threshold Normalization → Alerts），含 connections/replication_lag/deadlocks/rollback_ratio
- `scaffold_tenant.py` catalog 同步新增 PostgreSQL 選項
- metric-dictionary.yaml 新增 7 筆 PostgreSQL 指標對照

### [P1] `validate-config` 一站式檢查 ✅
- `validate_config.py`：封裝 YAML syntax + schema + routes + policy + custom rules + versions
- 輸出統一報告（pass/warn/fail），`--json` 供 CI 直接消費
- `make validate-config` Makefile 目標

### [P2] Federation 架構文件 ✅
- `docs/federation-integration.md`：場景 A（中央 threshold-exporter + 多邊緣 Prometheus scrape）的架構藍圖
- 涵蓋 federation vs remote-write 方案比較、邊緣/中央配置範例、驗證 checklist、效能考量
- Helm chart 支援 `externalLabels.cluster` 注入

## v1.8.0 規劃：生態系擴張與無縫遷移 (Onboarding & Ecosystem)

### [P0] 既有配置反向分析工具 (`onboard_platform.py`)
- Phase 1：Alertmanager config 反向分析 — 攤平巢狀 route tree → per-tenant `_routing` YAML
- Phase 2：Rule file 批次 AST 分析 → migration plan（CSV + `_defaults.yaml` + tenant overlays）
- Phase 3：Prometheus scrape config 分析 → relabel_configs 建議片段
- 支援 `--tenant-label` 參數適應各企業不同的 label 命名慣例

### [P1] N:1 Tenant Mapping 自動化
- `scaffold_tenant.py --namespaces ns1,ns2` 自動產出正確的 Prometheus relabel_configs snippet
- tenant YAML `_namespaces` 元資料欄位（工具參考，不影響 metric 邏輯）

### [P2] Per-rule Routing Overrides
- `_routing.overrides[]` 支援 per-alertname 或 per-metric_group 指定不同 receiver
- `generate_alertmanager_routes.py` 產出 Alertmanager 子路由

### [P2] Kafka / RabbitMQ Rule Pack
- 按客戶需求排序，接入模式同既有 DB Rule Pack

長期展望（Federation 場景 B Rule Pack 拆分、CRD/Operator、Config Diff Preview、PR 回測 Bot）見 `docs/architecture-and-design.md` §11。

## 開發規範
1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go 必須 `ReadHeaderTimeout`; Python 寫檔必須 `os.chmod(path, 0o600)`; `subprocess` 禁止 `shell=True`
6. **推銷語言不進 repo**: README 保持客觀工程語言；Pitch Deck 獨立產出
7. **版號治理**: 打 tag 前必須 `make version-check`；更新版號用 `make bump-docs`
8. **Sentinel Alert 模式**: 新 flag metric 一律用 sentinel alert → Alertmanager inhibit，不在 PromQL 層做行為控制

## 文件架構
| 文件 | 受眾 | 備註 |
|------|------|------|
| `README.md` / `README.en.md` | 技術主管、初訪者 | 含痛點對比 + 企業價值主張表 |
| `docs/architecture-and-design.md` | Platform Engineers | §2.3 Tenant-NS 映射、§2.8 Severity Dedup、§2.9 Alert Routing、§4.1–4.2 Benchmark |
| `docs/migration-guide.md` | Tenants, DevOps | §5 含 config-driven routing 說明 |
| `docs/byo-prometheus-integration.md` | Platform Engineers, SREs | BYOP 最小整合 + Tenant-NS mapping patterns |
| `docs/byo-alertmanager-integration.md` | Platform Engineers, SREs | Alertmanager 整合指引（動態 reload + 6 種 receiver + 驗證 checklist） |
| `docs/custom-rule-governance.md` | Platform Leads, Domain Experts, Tenant Tech Leads | 三層治理模型 + RnR 權責 + SLA 切割 + CI Linting |
| `components/da-tools/README.md` | All | 可攜帶 CLI 容器：驗證整合、遷移規則、scaffold tenant |
| `docs/shadow-monitoring-sop.md` | SRE, Platform Engineers | Shadow Monitoring 完整 SOP runbook |
| `docs/gitops-deployment.md` | Platform Engineers, DevOps, SREs | GitOps 工作流指南（ArgoCD/Flux 範例 + 三層變更流程 + CODEOWNERS RBAC） |
| `docs/federation-integration.md` | Platform Engineers, SREs | Federation 場景 A 架構藍圖（中央 threshold-exporter + 多邊緣 Prometheus） |
| `docs/internal/testing-playbook.md` | Contributors (AI Agent) | K8s 環境 + shell 陷阱 |
| `docs/internal/windows-mcp-playbook.md` | Contributors (AI Agent) | Dev Container 操作手冊 |
| `rule-packs/README.md` | All | 含 `optional: true` 卸載文件 |
| `components/threshold-exporter/README.md` | Developers | |
| `.github/CODEOWNERS` | Platform Team, Tenant Teams | 檔案級 RBAC — PR 自動指派 reviewer |
| `.github/workflows/validate.yaml` | Platform Team | PR CI pipeline（pytest + go test + schema/routing/policy + lint + version） |
| `k8s/03-monitoring/dynamic-alerting-overview.json` | Platform Engineers, SREs | Grafana Dashboard — 平台級租戶狀態總覽 |

## 工具 (scripts/tools/)
- `patch_config.py <tenant> <key> <value>`: ConfigMap 局部更新
- `check_alert.py <alert> <tenant> [--prometheus URL]`: Alert 狀態 JSON
- `diagnose.py <tenant> [--prometheus URL]`: 健康檢查 JSON
- `migrate_rule.py <rules.yml> [--triage] [--dry-run] [--no-prefix] [--no-ast]`: 傳統→動態 (Triage CSV + Prefix + Dictionary + AST Engine + Auto-Suppression)
- `scaffold_tenant.py [--tenant NAME --db TYPE,...] [--catalog] [--routing-receiver URL --routing-receiver-type TYPE]`: 互動式 Tenant 配置產生器（含 routing + severity_dedup 選項，支援 webhook/email/slack/teams/rocketchat/pagerduty）
- `validate_migration.py [--mapping FILE | --old Q --new Q] --prometheus URL`: Shadow Monitoring 數值 diff
- `offboard_tenant.py <tenant> [--execute]`: Tenant 下架 (Pre-check + 移除)
- `deprecate_rule.py <metric_key...> [--execute]`: Rule/Metric 下架 (三步自動化)
- `baseline_discovery.py <--tenant NAME> [--duration S --interval S --metrics LIST]`: 負載觀測 + 閾值建議
- `bump_docs.py [--platform VER] [--exporter VER] [--tools VER] [--check]`: 版號一致性管理 (三條版號線批次更新 + CI lint)
- `lint_custom_rules.py <path...> [--policy FILE] [--ci]`: Custom Rule deny-list linter (治理合規檢查)
- `generate_alertmanager_routes.py --config-dir <dir> [-o FILE] [--dry-run] [--validate] [--apply] [--policy FILE]`: Tenant YAML → Alertmanager route+receiver+inhibit_rules fragment（含 per-tenant severity dedup inhibit rules）。`--validate` 供 CI pipeline 驗證（exit 0/1）。`--policy` 載入 `allowed_domains` 做 webhook URL 檢查
- `validate_config.py --config-dir <dir> [--policy FILE] [--rule-packs DIR] [--version-check] [--json]`: 一站式配置驗證（YAML syntax + schema + routes + policy + custom rules + versions）。CI 可直接消費 JSON 輸出
- `metric-dictionary.yaml`: 啟發式指標對照字典

## 共用函式庫 (scripts/_lib.sh)
Scenario / benchmark 腳本透過 `source scripts/_lib.sh` 共用 port-forward 管理、PromQL 查詢、alert 等待、exporter metric 讀取等函式。demo.sh 有自己的 `_demo_` helpers 不引用 _lib.sh。

## Makefile 語義區分
- `make test-alert`: **硬體故障/服務中斷測試** — Kill process 模擬 Hard Outage
- `make demo-full`: **動態負載展演** — Composite Load (conn+cpu) → alert 觸發 → 清除 → 恢復
- `make demo`: 快速模式 (scaffold + migrate + diagnose + baseline_discovery，不含負載)
- `make validate-config`: 一站式配置驗證（YAML + schema + routes + custom rules + versions）
- `make chart-package` / `make chart-push`: Helm chart 打包 + 推送至 OCI registry (`ghcr.io/vencil/charts`)
- 其餘目標見 `make help`

## Release 流程
1. `make bump-docs EXPORTER=X.Y.Z` → 更新 Chart.yaml (version + appVersion) + 文件版號
2. `make version-check` → 驗證版號一致性
3. `make release-tag` → 從 Chart.yaml 推導 tag（**禁止手動 `git tag`，防止版號不一致**）
4. `git push origin v<VERSION>` → GitHub Actions 自動 build image + push chart

## AI Agent 環境
- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **Kubernetes MCP**: Context `kind-dynamic-alerting-cluster`（複雜操作常 timeout → fallback docker exec）
- **Prometheus API**: 開發環境 `port-forward` + `localhost`；生產環境 K8s Service (`prometheus.monitoring.svc.cluster.local:9090`)
- **檔案清理**: mounted workspace 無法從 VM 直接 rm → 用 `docker exec ... rm -f`（Cowork 環境需 `allow_cowork_file_delete`）
- Playbooks: Windows/MCP → `docs/internal/windows-mcp-playbook.md` | K8s/測試 → `docs/internal/testing-playbook.md`
