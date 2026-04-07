# CLAUDE.md — AI 開發上下文指引

## 專案概覽 (v2.6.0)

Multi-Tenant Dynamic Alerting 平台。Config-driven, Hot-reload (SHA-256), Directory Scanner (`-config-dir`)。

- **Cluster**: Kind (`dynamic-alerting-cluster`) | **NS**: `db-a`, `db-b` (Tenants), `monitoring` (Infra)
- **threshold-exporter** ×2 HA (port 8080): YAML → Prometheus Metrics。三態 + `_critical` 多層嚴重度 + 維度標籤
- **Prometheus**: 15 個 Rule Pack（14 個 optional Projected Volume + 1 個 platform ConfigMap）。**⚠️ 總數以 `platform-data.json` 為準（15），不是 `rule-packs/` yaml 檔案數（14）**
- **Alertmanager**: 動態 route/receiver/inhibit 產生 + `configmap-reload` sidecar 自動 reload
- **三態運營模式**: Normal / Silent (`_silent_mode`) / Maintenance (`_state_maintenance`)，均支援 `expires` 自動失效
- **Distribution**: OCI registry + Docker images (`ghcr.io/vencil/threshold-exporter`, `ghcr.io/vencil/da-tools`, `ghcr.io/vencil/da-portal`)

版本歷程詳見 `CHANGELOG.md`。

## 架構速查

完整概念索引見 `docs/architecture-and-design.md`。以下僅列最易踩坑 / 最不直覺的設計：

| 概念 | 關鍵機制 | 詳見 |
|------|---------|------|
| Severity Dedup | **Alertmanager inhibit**（非 PromQL），TSDB 永遠完整 | §2.8 |
| Sentinel Alert 模式 | exporter flag metric → sentinel alert → inhibit。新 flag 一律走此模式 | §2.7, §2.8 |
| Routing Guardrails | group_wait 5s–5m, group_interval 5s–5m, repeat_interval 1m–72h。**Go + Python 兩端必須一致** | §2.9 |
| Schema Validation | Go `ValidateTenantKeys()` + Python `validate_tenant_keys()` 雙端驗證 | `governance-security.md` §2 |
| Cardinality Guard | per-tenant 500 上限，超限 truncate + log ERROR | Go `ResolveAt()` |
| 三態 + Bilingual | 三態（Normal/Silent/Maintenance）、`*_zh` 雙語 annotation、CLI i18n — 三層各自獨立 | §2.7, §3.2 |
| Dual-Perspective | `platform_summary` + `summary` 雙視角 → `_routing_enforced` NOC 通知 | §2.11 |
| 四層路由合併 | `_routing_defaults` → profile → tenant `_routing` → `_routing_enforced`，ADR-007 | §2.12 |
| Tenant API 架構 | commit-on-write + `atomic.Value` RBAC 熱更新 + Portal 降級安全，ADR-009 | §2.14 |

## 開發規範

1. **ConfigMap**: 禁止 `cat <<EOF`。用 `kubectl patch` / `helm upgrade` / `patch_config.py`
2. **Tenant-agnostic**: Go/PromQL 禁止 Hardcode Tenant ID
3. **三態**: Custom / Default (省略) / Disable (`"disable"`)
4. **Doc-as-Code**: 同步更新 `CHANGELOG.md`, `CLAUDE.md`, `README.md`。變更連動規則見 `docs/internal/doc-map.md` § Change Impact Matrix
5. **SAST**: 7 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions + stderr routing）。詳見 `docs/governance-security.md`
6. **推銷語言不進 repo**: README 保持客觀工程語言
7. **版號治理**: `make version-check` → `make bump-docs` → 四線 tag（`v*` platform / `exporter/v*` / `tools/v*` / `portal/v*`）
8. **Sentinel Alert 模式**: 新 flag metric 一律用 sentinel → Alertmanager inhibit
9. **i18n 三層架構**: JSX 用 `window.__t(zh, en)` + Rule Pack 用 `*_zh` 後綴 annotation + Python CLI 用 `detect_cli_lang()` 切換 argparse help
10. **雙語政策**: `docs/internal/` 及工具性檔案（CHANGELOG、tags、includes）**不需要英文版**，僅外部面向文件需維持 ZH/EN 雙語對。pre-commit hook 已設定 `BILINGUAL_EXEMPT_PATHS` 自動豁免。**Agent 不需詢問是否補 internal docs 英文版 — 答案一律是不用。**
11. **檔案衛生**：禁止對掛載路徑的檔案使用 `sed -i`（會截斷缺少 EOF 換行的檔案）。批量文字替換用 `git show HEAD:file | sed | tr -d '\0' > file` pipe 模式，或用 Read + Edit 工具。`file-hygiene` pre-commit hook 會自動修復 null bytes 與缺失的 EOF 換行。

## 互動工具生態（39 JSX tools）

**Source of Truth 檔案**：`docs/assets/tool-registry.yaml`（工具 metadata）、`docs/assets/platform-data.json`（Rule Pack 數據）、`docs/assets/flows.json`（Guided Flow）、`docs/assets/jsx-loader.html`（載入器）、`docs/interactive/index.html`（Hub）。

**變更 SOP**：
- **互動工具**：更新 `tool-registry.yaml` → `make sync-tools` → `make lint-docs`
- **Rule Pack**：`make platform-data` → 新增 `*_zh` 雙語 annotation → `check_bilingual_annotations.py --check`
- **Guided Flow**：編輯 `flows.json`（tool key 須存在於 registry）→ 新工具需同步 jsx-loader `CUSTOM_FLOW_MAP` → `make lint-docs`

## Pre-commit 品質閘門

30 個 auto-run hooks（每次 commit，含 `file-hygiene`）+ 13 個 manual-stage hooks（含 orphan-doc-check、glossary-coverage-check、md-yaml-drift-check）。Hook 清單與觸發規則見 `.pre-commit-config.yaml`。

```bash
pre-commit run --all-files                              # 全跑 auto hooks
pre-commit run --hook-stage manual --all-files           # manual-stage（schema / translation / flow E2E / jsx-babel / i18n）
```

## 文件導覽

完整文件對照表（143 個文件，含受眾與內容摘要）見 [`docs/internal/doc-map.md`](docs/internal/doc-map.md)。

快速入口：`docs/getting-started/` (3 角色入門) | `docs/scenarios/` (9 場景，含 [README 導覽](docs/scenarios/README.md)) | `docs/internal/` (Playbook + doc-map + test-map) | `docs/adr/` (11 ADRs，含[快速導讀](docs/adr/README.md))

**近期結構變更**（doc-quality-improvement-plan Phase 1–3）：
- `README.md` / `README.en.md`：採用「5s → 30s → 5min」漸進式揭露，367 → ~190 行
- `docs/index.md`：精簡為 MkDocs site 導航入口，326 → ~140 行
- `docs/federation-integration.md`：清除「場景 A/B」代號 → 中央評估/邊緣評估描述性名稱
- `docs/scenarios/shadow-audit.md`：已合併至 `shadow-monitoring-cutover.md` Phase 0（redirect stub）
- `docs/vcs-integration-guide.md`：新建 VCS 整合指南（GitHub/GitLab/自託管）
- `docs/internal/ssot-language-evaluation.md`：恢復為 `status: draft` 活文件（v2.7.0 EN-first 遷移參考）
- 改善計畫詳見 [`docs/internal/doc-quality-improvement-plan.md`](docs/internal/doc-quality-improvement-plan.md)

## 工具 (scripts/tools/)

96 個 Python 工具（不含共用函式庫，ops+dx+lint=96），依職責分三子目錄：

| 子目錄 | 用途 | 數量 |
|--------|------|------|
| `ops/` | 運維工具（scaffold, diagnose, migrate, validate, alert-quality, alert-correlate, drift-detect, policy, forecast, notification-test, threshold-recommend, tenant-mapping, explain-route, discover-mappings, init, config-history, gitops-check, operator-generate, operator-check, rule-pack-split, policy-opa-bridge...） | 45 |
| `dx/` | DX 自動化（generate_*, bump_docs, sync_*, coverage_gap_analysis, generate_tenant_metadata...） | 20 |
| `lint/` | 文件 CI lint（check_*, validate_docs_*, lint_*, check_cli_coverage, check_bilingual_content, check_frontmatter_versions, check_routing_profiles, check_doc_template, check_portal_i18n...） | 31 |
| root | 共用（`validate_all.py`）+ 函式庫（`_lib_python.py` facade + 4 子模組）+ 資料（`metric-dictionary.yaml`） | 1 + 5 lib |

完整工具表見 [`docs/internal/tool-map.md`](docs/internal/tool-map.md)。常用工具速查：`da-tools <cmd> --help` | CLI 完整參考見 [`docs/cli-reference.md`](docs/cli-reference.md)

## Makefile 速查

| 目標 | 用途 |
|------|------|
| `make demo-full` | 端對端展演（scaffold → load → alert → cleanup） |
| `make demo-showcase` | 5-tenant 展演腳本（scaffold → validate → routes → diff → 三態） |
| `make lint-docs` | 一站式文件 lint，支援 `ARGS="--parallel"` |
| `make platform-data` | 重新產生 `platform-data.json`（Rule Pack 數據） |
| `make version-check` / `bump-docs` | 版號治理 |
| `make pre-tag` | ⛔ Pre-tag 品質閘門（version-check + lint-docs，打 tag 前必跑） |
| `make benchmark` | 效能基準（idle + routing + alertmanager + reload） |
| `make validate-config` | 一站式配置驗證 |
| `make test-e2e` | Portal Playwright E2E 煙霧測試（需 Node.js ≥ 20） |
| `make portal-image` / `portal-run` | Self-Hosted Portal Docker image |

完整目標見 `make help`。

## Release 流程

五線版號（`v*` platform / `exporter/v*` / `tools/v*` / `portal/v*` / `tenant-api/v*`）。完整步驟與踩坑記錄見 `docs/internal/github-release-playbook.md`。

## AI Agent 環境

- **Dev Container**: `docker exec -w /workspaces/vibe-k8s-lab vibe-dev-container <cmd>`
- **K8s MCP**: 常 timeout → fallback docker exec
- **Prometheus/Alertmanager**: `port-forward` + `localhost:9090/9093`
- **Python tests**: Cowork VM 可直接跑；Go tests 需在 Dev Container 內
- **檔案清理**: `docker exec ... rm -f`（Cowork VM 無法直接 rm 掛載路徑）
- **Dev Container 重啟**: 系統重開機後 `docker start vibe-dev-container`

### Playbook 體系（必讀）

每個操作領域都有對應的 Playbook，記錄累積的經驗、已知陷阱和標準做法。**開始任何非純程式碼的操作前，先讀對應 Playbook。**

| Playbook / Map | 涵蓋領域 | 何時讀 |
|----------------|---------|--------|
| `docs/internal/testing-playbook.md` | K8s 排錯、負載注入、程式碼品質、SAST | 跑測試、場景驗證、新增工具 |
| `docs/internal/benchmark-playbook.md` | Benchmark 方法論、執行環境、踩坑記錄 | 跑 benchmark、效能分析 |
| `docs/internal/test-map.md` | 測試架構：factories、markers、檔案對照、snapshot 工作流 | 新增/修改測試、理解測試基礎設施 |
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
5. **Commit**：`git commit` → pre-commit hooks 自動執行 13 個品質檢查（platform-data drift, tool consistency, versions...）+ 6 個 manual-stage hooks
6. **Lesson Learned**：回寫 Playbook + CLAUDE.md

## 長期展望
