# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v2.0.0-preview.3)

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
| Bilingual Annotations | `summary_zh` / `description_zh` / `platform_summary_zh` 雙語告警摘要，Alertmanager template fallback | `docs/architecture-and-design.md` §3.3 + `k8s/03-monitoring/configmap-alertmanager.yaml` |
| CLI i18n | `detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換 | `_lib_python.py`, `entrypoint.py` |
| Benchmark | idle / scaling-curve / under-load / routing / alertmanager / reload | `docs/benchmarks.md` |
| Federation | 場景 A 藍圖（中央 exporter + 邊緣 Prometheus） | `docs/federation-integration.md` |

## 開發規範

1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`。變更連動規則見 `docs/internal/doc-map.md` § Change Impact Matrix
5. **SAST**: Go `ReadHeaderTimeout`; Python `os.chmod(path, 0o600)` + `encoding="utf-8"`; `subprocess` 禁止 `shell=True`
6. **推銷語言不進 repo**: README 保持客觀工程語言
7. **版號治理**: `make version-check` → `make bump-docs` → 三線 tag（`v*` platform / `exporter/v*` / `tools/v*`）
8. **Sentinel Alert 模式**: 新 flag metric 一律用 sentinel → Alertmanager inhibit
9. **i18n 三層架構**: JSX 用 `window.__t(zh, en)` + Rule Pack 用 `*_zh` 後綴 annotation + Python CLI 用 `detect_cli_lang()` 切換 argparse help

## 互動工具生態（23 JSX tools）

- **單一真相源**: `docs/assets/tool-registry.yaml` — 所有工具 metadata（key, audience, related, appears_in）
- **共用資料源**: `docs/assets/platform-data.json` — Rule Pack 數據（從 YAML 權威來源萃取），JSX 工具 fetch 共用
- **Hub 頁面**: `docs/interactive/index.html` — 角色篩選（Platform / Domain / Tenant）+ 22 張卡片
- **jsx-loader**: `docs/assets/jsx-loader.html` — 瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **Guided Flows**: `docs/assets/flows.json` — 多步引導流程定義（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
  - **Cross-step data**: `window.__FLOW_STATE` + `window.__flowSave(data)` — JSX 元件間 opt-in 資料傳遞（sessionStorage 持久化）
  - **Progress persistence**: `sessionStorage __da_flow_progress_<name>` — Hub 顯示進度 badge + resume 按鈕
  - **Custom flow builder**: `?flow=custom&tools=wizard,playground,...` — Hub 互動式 builder UI，23 工具全覆蓋
  - **Completion tracking**: `sessionStorage __da_flow_completed_<name>` — Hub 顯示 ✓ 完成 badge + 時間戳
  - **Conditional steps**: `condition` 欄位（如 `{"role": ["platform"]}`）→ jsx-loader `filterSteps()` 動態跳過不符步驟
  - **Checkpoint validation**: `validation` 欄位 + `__checkFlowGate()` — Next 按鈕閘門，缺少前置資料顯示 toast 警告
  - **Flow analytics**: Hub 摺疊式分析面板 — 進度條、完成率、drop-off 步驟偵測
  - **Flow lint**: `lint_tool_consistency.py check_flow_components()` — 驗證 flows.json tool key / JSX 路徑 / 必填欄位
  - **Flow E2E test**: `test_flows_e2e.py` — 4 類自動化煙霧測試（schema + CUSTOM_FLOW_MAP + infra + Hub）
- **離線支援**: `make vendor-download` → `docs/assets/vendor/`，jsx-loader 自動偵測

變更互動工具時：先更新 `tool-registry.yaml` → `make sync-tools`（自動同步 Hub + TOOL_META）→ 依 `doc-map.md` § Change Impact Matrix 連動 .md callout → `make lint-docs` 驗證。
變更 Rule Pack（新增/修改規則）時：`make platform-data` → 重新產生 platform-data.json → JSX 工具自動取得最新數據。新增告警規則時須同步加上 `*_zh` 雙語 annotation（`check_bilingual_annotations.py --check` 驗證）。
變更 Guided Flow 時：編輯 `flows.json`（tool key 須存在於 registry）→ `make lint-docs` 驗證 JSX 路徑 + 必填欄位。Custom flow 新增工具需同步 jsx-loader `CUSTOM_FLOW_MAP`。

## Pre-commit 品質閘門

安裝：`pip install pre-commit && pre-commit install`。每次 `git commit` 自動執行 11 個快速檢查（<5s each），攔截 drift 問題。

| Hook ID | 偵測內容 | 觸發檔案 |
|---------|---------|----------|
| `tool-map-check` | Tool map 覆蓋率 | `scripts/tools/*.py` |
| `doc-map-check` | Doc map 覆蓋率 | `docs/**/*.{md,jsx}` |
| `rule-pack-stats-check` | Rule Pack 統計 drift | `rule-packs/*.yaml`, `k8s/03-monitoring/*.yaml` |
| `glossary-check` | 術語表 ↔ abbreviation 同步 | `docs/**/*.md` |
| `changelog-lint` | CHANGELOG 格式 | `CHANGELOG*.md` |
| `version-consistency` | 版號/計數一致性 | `*.{md,yaml,jsx}` |
| `includes-sync` | Include snippet 中英同步 | `docs/includes/` |
| `platform-data-check` | platform-data.json ↔ YAML 源 drift | `rule-packs/`, `k8s/03-monitoring/`, `docs/assets/platform-data.json` |
| `repo-name-check` | GitHub URL repo name 防護（禁止 vibe-k8s-lab） | `*.{md,yaml,py,jsx,html,json}` |
| `tool-consistency-check` | Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 連結 | `docs/assets/`, `docs/**/*.{jsx,md}` |
| `structure-check` | 專案結構正規化（工具/JSX/測試位置） | `*.{py,jsx,yaml}` |

手動全跑：`pre-commit run --all-files`。Manual-stage 額外檢查：`pre-commit run --hook-stage manual --all-files`（schema + translation + flow E2E + jsx-babel + i18n coverage）。

## 文件導覽

完整文件對照表（44 個文件，含受眾與內容摘要）見 [`docs/internal/doc-map.md`](docs/internal/doc-map.md)。

快速入口：`docs/getting-started/` (3 角色入門) | `docs/scenarios/` (5 場景) | `docs/internal/` (Playbook + doc-map) | `docs/adr/` (5 ADRs)

## 工具 (scripts/tools/)

59 個 Python 工具，依職責分三子目錄：

| 子目錄 | 用途 | 數量 |
|--------|------|------|
| `ops/` | 運維工具（scaffold, diagnose, migrate, validate...） | 27 |
| `dx/` | DX 自動化（generate_*, bump_docs, sync_*...） | 18 |
| `lint/` | 文件 CI lint（check_*, validate_docs_*, lint_*...） | 13 |
| root | 共用（`_lib_python.py`, `validate_all.py`, `metric-dictionary.yaml`） | 4 |

完整工具表見 [`docs/internal/tool-map.md`](docs/internal/tool-map.md)。常用工具速查：`da-tools <cmd> --help` | CLI 完整參考見 [`docs/cli-reference.md`](docs/cli-reference.md)

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
| `make lint-docs` | 一站式文件 lint（versions + drift + platform-data + tool consistency），支援 `ARGS="--parallel"` |
| `make platform-data` | 產生 `docs/assets/platform-data.json`（Rule Pack 共用資料源） |
| `make serve-docs` | 啟動本地文件伺服器（含互動工具 `localhost:8080`） |
| `make vendor-download` / `vendor-check` | 下載 / 檢查離線 CDN 資源 |
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
5. **Commit**：`git commit` → pre-commit hooks 自動執行 9 個品質檢查（platform-data drift, tool consistency, versions...）
6. **Lesson Learned**：回寫 Playbook + CLAUDE.md

## 長期展望

已完成項目詳見 `CHANGELOG.md` 及 `docs/internal/dx-tooling-backlog.md`。

近期：Federation B（Rule Pack 分層）、1:N Mapping、Alert Quality Scoring。
中期：Policy-as-Code、Cross-Cluster Drift Detection、Incremental Reload。
遠期：Tenant Self-Service Portal、Cardinality Forecasting、Log-to-Metric Bridge。
詳見 `docs/architecture-and-design.md` §5。
