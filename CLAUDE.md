# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v2.0.0-preview.2)

Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。

- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: Projected Volume 掛載 15 個 Rule Pack (`optional: true`)
- **Alertmanager**: 動態 route/receiver/inhibit 產生 + `configmap-reload` sidecar 自動 reload
- **三態運營模式**: Normal / Silent (`_silent_mode`) / Maintenance (`_state_maintenance`)，均支援 `expires` 自動失效
- **Distribution**: OCI registry + Docker images (`ghcr.io/vencil/threshold-exporter`, `ghcr.io/vencil/da-tools`)

版本歷程詳見 `CHANGELOG.md`。

## 架構速查

以下列出核心架構概念與對應的深入文件位置。

| 概念 | 關鍵機制 | 詳見 |
|------|---------|------|
| Severity Dedup | Alertmanager inhibit（非 PromQL），TSDB 永遠完整 | `docs/architecture-and-design.md` §2.8 |
| Sentinel Alert 模式 | exporter flag metric → sentinel alert → inhibit | §2.7 三態、§2.8 dedup |
| Alert Routing | Tenant YAML → `generate_alertmanager_routes.py` → route + receiver + inhibit | §2.9 |
| Per-rule Routing Overrides | `_routing.overrides[]` per-alertname/metric_group | §2.9 |
| Platform Enforced Routing | `_routing_enforced` 雙軌通知（NOC + tenant）+ `{{tenant}}` per-tenant channel | §2.11 |
| Routing Defaults 三態 | `_routing_defaults` 繼承/覆寫/disable + `{{tenant}}` 佔位符 | §2.9 |
| Routing Guardrails | group_wait 5s–5m, group_interval 5s–5m, repeat_interval 1m–72h | Go + Python 兩端一致 |
| Webhook Domain Allowlist | `--policy` fnmatch 檢查，空清單=不限制 | `generate_alertmanager_routes.py` |
| Receiver 類型 | webhook / email / slack / teams / rocketchat / pagerduty | `RECEIVER_TYPES` 常數 |
| Schema Validation | Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` | §2.6 |
| Cardinality Guard | per-tenant 500 上限，超限 truncate + log ERROR | Go `ResolveAt()` |
| N:1 Tenant Mapping | `scaffold_tenant.py --namespaces` + relabel_configs snippet | §2.3 |
| Regex 維度閾值 | `=~` 運算子，`_re` label 後綴 | §2.4 |
| 排程式閾值 | `ScheduledValue` + `ResolveAt(now)` 跨午夜 | §2.5 |
| Dynamic Runbook Injection | `_metadata` → `tenant_metadata_info` info metric → Rule Pack `group_left` 自動繼承 | `docs/getting-started/for-tenants.md` |
| Recurring Maintenance | `_state_maintenance.recurring[]` cron+duration → `maintenance_scheduler.py` CronJob → AM silence | `docs/architecture-and-design.md` §2.7 |
| Config Drift CI | `config_diff.py` exit codes → GitHub Actions / GitLab CI 模板 | `docs/gitops-deployment.md` |
| Dual-Perspective Annotation | `platform_summary` + `summary` 雙視角 annotation → `_routing_enforced` NOC 通知 | `docs/scenarios/alert-routing-split.md` |
| Benchmark | idle / scaling-curve / under-load / routing / alertmanager / reload | `docs/benchmarks.md` |
| Federation | 場景 A 藍圖（中央 exporter + 邊緣 Prometheus） | `docs/federation-integration.md` |

## 開發規範

1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`
5. **SAST**: Go `ReadHeaderTimeout`; Python `os.chmod(path, 0o600)` + `encoding="utf-8"`; `subprocess` 禁止 `shell=True`
6. **推銷語言不進 repo**: README 保持客觀工程語言
7. **版號治理**: `make version-check` → `make bump-docs` → 三線 tag（`v*` platform / `exporter/v*` / `tools/v*`）
8. **Sentinel Alert 模式**: 新 flag metric 一律用 sentinel → Alertmanager inhibit

## 文件導覽

完整文件對照表（44 個文件，含受眾與內容摘要）見 [`docs/internal/doc-map.md`](docs/internal/doc-map.md)。

快速入口：`docs/getting-started/` (3 角色入門) | `docs/scenarios/` (5 場景) | `docs/internal/` (Playbook + doc-map) | `docs/adr/` (5 ADRs)

## 工具 (scripts/tools/)

完整工具表（50 個 Python 工具，分三類：運維 / DX Automation / 文件 CI）見 [`docs/internal/tool-map.md`](docs/internal/tool-map.md)。

常用工具速查：`da-tools <cmd> --help` | CLI 完整參考見 [`docs/cli-reference.md`](docs/cli-reference.md)

## Makefile 速查

| 目標 | 用途 |
|------|------|
| `make demo` | 快速展演（scaffold → migrate → diagnose → baseline） |
| `make demo-full` | 完整展演（含 composite load → alert → cleanup） |
| `make test-alert` | 硬體故障測試（kill process） |
| `make benchmark` | 效能基準（`ARGS="--under-load --scaling-curve --routing-bench --alertmanager-bench --reload-bench --json"`） |
| `make validate-config` | 一站式配置驗證 |
| `make chart-package` / `chart-push` | Helm OCI 打包推送 |
| `make version-check` / `bump-docs` | 版號治理 |
| `make lint-docs` | 一站式文件 lint（versions + drift checks），支援 `ARGS="--parallel"` |
| `make release-tag-exporter` | 從 Chart.yaml 推導 `exporter/v*` tag |

完整目標見 `make help`。

## Release 流程（三線版號）

1. `make bump-docs PLATFORM=X.Y.Z EXPORTER=X.Y.Z TOOLS=X.Y.Z` → 更新版號（只傳有變更的 flag）
2. `make version-check` → 驗證一致性
3. 建立 tag（依實際變更決定哪些要推）：
   - `git tag v<PLATFORM>` → GitHub Release 錨點（不觸發 build）
   - `make release-tag-exporter` → exporter image + Helm chart（僅 exporter 有 code change 時）
   - `git tag tools/v<TOOLS>` → da-tools image（僅 da-tools 有 code change 時）
4. `git push origin <tag>` → CI 自動 build + push

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Dev Container 重啟**: 系統重開機後 `docker start vibe-dev-container`

### Playbook 體系（必讀）

每個操作領域都有對應的 Playbook，記錄累積的經驗、已知陷阱和標準做法。**開始任何非純程式碼的操作前，先讀對應 Playbook。**

| Playbook | 涵蓋領域 | 何時讀 |
|----------|---------|--------|
| `docs/internal/testing-playbook.md` | K8s 排錯、負載注入、Benchmark 方法論、程式碼品質、SAST | 跑測試、benchmark、場景驗證、新增工具 |
| `docs/internal/windows-mcp-playbook.md` | Docker exec 模式、Shell 陷阱、Port-forward、Helm 防衝突 | 任何 docker exec / K8s / Windows MCP 操作 |
| `docs/internal/github-release-playbook.md` | Git push、Tag、GitHub Release、CI 觸發 | Release 流程 |

### Playbook 維護原則

Playbook 是 **living documents**，跟隨專案演進持續更新：

1. **Lesson Learned 回寫**：每次遇到新陷阱或發現更好做法，立即更新對應 Playbook（不是下次再說）
2. **新領域擴展**：專案新增技術領域（如新的 Rule Pack 類型、新的部署目標、新的 CI 工具）時，評估是否需要新 Playbook 或在既有 Playbook 新增章節
3. **交叉引用**：Playbook 之間用相對連結互相引用，避免重複內容。每個 Playbook 頂部有 `相關文件` 導航
4. **全局 vs 領域**：CLAUDE.md 只放指引級摘要（指向哪個 Playbook），詳細步驟和陷阱清單放 Playbook 內
5. **驗證更新**：Playbook 內的數字（Rule Pack 數量、工具數量等）在版本升級時一併更新

### 開發 Session 標準工作流

1. **起手式**：`docker ps` 確認 Dev Container 運行 → 讀相關 Playbook
2. **開發**：程式碼修改 → Go test / Python test → 場景驗證
3. **Benchmark**：完整 benchmark（idle + routing + Go micro-bench）→ 記錄到 CHANGELOG + architecture docs
4. **文件同步**：`bump_docs.py --check` → 更新 CLAUDE.md / README / CHANGELOG 的計數
5. **Lesson Learned**：回寫 Playbook + CLAUDE.md

## 長期展望

已完成項目詳見 `CHANGELOG.md` 及 `docs/internal/dx-tooling-backlog.md`。

近期：Federation B（Rule Pack 分層）、1:N Mapping、Alert Quality Scoring。
中期：Policy-as-Code、Cross-Cluster Drift Detection、Incremental Reload。
遠期：Tenant Self-Service Portal、Cardinality Forecasting、Log-to-Metric Bridge。
詳見 `docs/architecture-and-design.md` §5。
