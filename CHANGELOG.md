---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.2.0
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v2.2.0] — 採用管線 + UX 升級 + 運維工具 (2026-03-17)

v2.2.0 聚焦三大主題：降低採用門檻的 Adoption Pipeline、Portal 互動體驗全面升級、配置運維新工具。新增 2 個 CLI 工具、3 個互動工具、Portal 三大 Tab 重構、24 個 Template Gallery 模板、5-tenant 展演腳本與 Hands-on Lab。

### 採用管線（Phase A — Adoption Pipeline）

- **`da-tools init`** — 專案骨架一鍵產生：CI/CD pipeline（GitHub Actions / GitLab CI）、`conf.d/` 目錄（含 `_defaults.yaml` + tenant YAML）、Kustomize overlays、`.pre-commit-config.da.yaml`，支援 `--non-interactive` 自動模式
- **GitOps CI/CD 整合指南** (`docs/scenarios/gitops-ci-integration.md`) — 三階段管線（Validate → Generate → Apply）、ArgoCD / Flux 整合、PR Comment Bot 工作流
- **Kustomize Overlays** — `configMapGenerator` 模式產生 threshold-config ConfigMap

### UX 升級（Phase B — Portal & Templates）

**Self-Service Portal 重構（3 Tab）**
- **Tab 1 (YAML Validation)**: Rule Pack 多選 → metric autocomplete → 動態 sample YAML 產生 → 即時驗證（含 pack-aware metric key 交叉檢查）
- **Tab 2 (Alert Preview)**: Pack-grouped 滑桿、視覺化閾值條、disabled/no-threshold 狀態顯示、severity dedup 說明
- **Tab 3 (Routing Trace)**: Metric+severity 輸入 → Alert origin → Inhibit check → 四層合併 → Domain Policy check → 通知派送 → NOC 副本

**Template Gallery 擴充（6 → 24 模板）**
- 7 場景模板：ecommerce、iot-pipeline、saas-backend、analytics、enterprise-db、event-driven、search-platform
- 13 Quick Start 模板：每個可選 Rule Pack 各一
- 4 特殊模板：maintenance、routing-profile、finance-compliance、minimal
- View mode 切換（All / Scenarios / Quick Start）+ Pack filter chips + Coverage summary

**新增互動工具**
- **CI/CD Setup Wizard** (`cicd-setup-wizard.jsx`) — 5 步精靈產生 `da-tools init` 命令：CI Platform → Deploy Mode → Rule Packs → Tenants → Review & Generate（第 27 個 JSX 工具）
- **Notification Template Previewer** (`notification-previewer.jsx`) — 6 種 receiver 通知預覽（Slack / Email / PagerDuty / Webhook / Teams / Rocket.Chat）+ Dual-Perspective annotation 展示 + Severity Dedup 說明（第 28 個）
- **Platform Health Dashboard** (`platform-health.jsx`) — 平台健康儀表板：元件狀態、租戶概覽、Rule Pack 使用分佈、Reload 事件時間線（第 29 個）

**展演與教學**
- **Demo Showcase** (`scripts/demo-showcase.sh`) — 5-tenant 完整展演腳本（prod-mariadb / prod-redis / prod-kafka / staging-pg / prod-oracle），7 步驟自動執行，支援 `--quick` 模式
- **Hands-on Lab** (`docs/scenarios/hands-on-lab.md`) — 30–45 分鐘 Docker-based 實戰教程，8 個練習覆蓋 init → validate → routes → routing trace → blast radius → three-state → domain policy

### 運維工具（Phase C — Operations）

- **`da-tools config-history`** — 配置快照與歷史追蹤：`snapshot` / `log` / `show` / `diff` 子命令，`.da-history/` 存儲，SHA-256 變更偵測，git-independent 輕量級版本控制

### 漸進式遷移 Playbook

- **`docs/scenarios/incremental-migration-playbook.md`** — 四階段雙軌並行遷移法（Strangler Fig Pattern）：Phase 0 Audit（`onboard` + `blind-spot`）→ Phase 1 Pilot（單一 domain 影子部署）→ Phase 2 Dual-Run（`shadow-verify` 品質比對）→ Phase 3 Cutover（逐 domain 切換）→ Phase 4 Cleanup。每步有 CLI 指令、預期輸出、回退方式
- **`architecture-and-design.md` §2.13** — 新增效能架構說明：Pre-computed Recording Rule vs Runtime Aggregation 的 PromQL 對比，解釋為什麼 tenant 增加不會導致 Prometheus CPU/Memory 暴增

### GitOps Native Mode

- **`da-tools init --config-source git`** — 產生 git-sync sidecar Kustomize overlay，threshold-exporter 直接從 Git 倉庫讀取配置，省去 ConfigMap 中間層。支援 SSH / HTTPS 認證、自訂分支與路徑。git-sync sidecar 寫入 emptyDir shared volume，threshold-exporter 的既有 Directory Scanner + SHA-256 hot-reload 機制無縫復用
- **`da-tools gitops-check`** — GitOps Native Mode 就緒度驗證工具，三個子命令：`repo`（Git 倉庫可達性 + 分支驗證）、`local`（本地 conf.d/ 結構驗證）、`sidecar`（K8s git-sync 部署狀態檢查），支援 `--json` 和 `--ci` 模式
- **Container Image Security Hardening** — 三層防護：base pin + build-time upgrade + attack surface reduction
  - threshold-exporter：`alpine` → `distroless/static-debian12:nonroot`（零 CVE，無 shell/apk/openssl）
  - da-tools：`python:3.13-alpine` → `python:3.13.3-alpine3.22` multi-stage build（修復 CVE-2025-48174, CVE-2025-15467）
  - da-portal：`nginx:1.28-alpine` → `nginx:1.28.0-alpine3.22` + `apk del libavif gd libxml2`（移除未使用 library，消除掃描器 false positive）

### 數字

| 項目 | v2.1.0 | v2.2.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 73 | 77 | +4 |
| da-tools CLI 命令 | 27 | 36 | +9 |
| JSX 互動工具 | 26 (+1 wizard) | 29 | +3 |
| Template Gallery 模板 | 6 | 24 | +18 |
| 場景文件 | 6 | 9 | +3 |
| Makefile targets | — | +1 (`demo-showcase`) | NEW |

---

## [v2.1.0] — 運維自助 + 告警智能化 + 性能優化 + 跨域路由 (2026-03-16)

v2.1.0 自 v2.0.0 起的全量升級。涵蓋 Go Exporter 增量熱載入、告警關聯分析、跨域路由架構 (ADR-006/007)、生態整合 (Backstage Plugin)、5 個新 CLI 工具、3 個互動工具、測試 +75%（1,759 → 3,070）、文件治理與正確性全面校正。

### Go Exporter 核心

**Incremental Hot-Reload (§5.6)**
- per-file SHA-256 index + parsed config cache，WatchLoop 增量重載路徑
- `ConfigManager` 新增 `fileHashes` / `fileConfigs` / `fileMtimes` 欄位
- `scanDirFileHashes()` — mtime guard + 輕量 hash check（mtime 未變直接跳過 I/O）
- `IncrementalLoad()` — 比對 per-file hash → 只重新解析 changed/added files → `mergePartialConfigs()`
- `fullDirLoad()` — 完整載入並初始化 cache（首次載入或 fallback）
- `applyBoundaryRules()` — 提取為獨立函式供共用
- **效能優化**：logConfigStats 取代 Resolve()、mtime guard、incremental merge（tenant 檔變動直接 patch）、byte cache（scan 快取復用，免除重複 I/O）
- 15 個 Go tests + 5 個 benchmarks（含 NoChange / OneFileChanged / ScanHashes / MergePartials）

**程式碼品質**
- 4 處 error print 修正為 `stderr` 輸出
- `parsePromDuration` / `isDisabled` / `clampDuration` 新增單元測試
- Go test 增加 config_test.go（801 行）+ config_bench_test.go（268 行）+ main_test.go（97 行）

### 跨域路由架構（ADR-006 + ADR-007）

**ADR-006: Tenant Mapping Topologies (1:1, N:1, 1:N)**
- 資料面映射方案：Prometheus Recording Rules 實現 1:N 映射（exporter 零修改）
- `generate_tenant_mapping_rules.py` — 讀取 `_instance_mapping.yaml`，產出 Recording Rules（36 tests）
- `scaffold_tenant.py` 新增 `--topology=1:N`、`--mapping-instance`、`--mapping-filter` 參數（9 tests）
- 範例設定檔 `_instance_mapping.yaml`

**ADR-007: Cross-Domain Routing Profiles**
- 四層合併管線：`_routing_defaults` → `routing_profiles[ref]` → tenant `_routing` → `_routing_enforced`
- `generate_alertmanager_routes.py` 擴展：profile 解析 + `check_domain_policies()` 驗證（21 tests）
- `scaffold_tenant.py` 新增 `--routing-profile` 參數
- 重構 `_parse_config_files()` → `_parse_platform_config()` + `_parse_tenant_overrides()` 子函式
- 範例設定檔 `_routing_profiles.yaml`、`_domain_policy.yaml`

**ADR-007 工具生態**
- `explain_route.py` — 路由合併管線除錯器：四層展開、`--show-profile-expansion`、`--json`、da-tools CLI 整合（25 tests）
- `check_routing_profiles.py` — CI lint 工具：未知 profile ref、孤立 profile、格式錯誤 constraints、`--strict` 模式（28 tests + pre-commit hook）

### 新增 CLI 工具

- **`da-tools test-notification`** — 6 種 receiver 連通性測試（webhook/slack/email/teams/pagerduty/rocketchat），Dry-run / CI gate / per-tenant 批次。57 tests，97% 覆蓋率
- **`da-tools threshold-recommend`** — 基於歷史 P50/P95/P99 的閾值推薦引擎，純 Python 統計，信心等級分級。54 tests，96% 覆蓋率
- **`da-tools alert-correlate`** — 告警關聯分析：時間窗口聚類 + 關聯分數 + 根因推斷，支援線上/離線模式。95% 覆蓋率
- **`da-tools drift-detect`** — 跨叢集配置漂移偵測：SHA-256 manifest 比對，pairwise 多目錄分析 + 修復建議。99% 覆蓋率
- **`da-tools explain-route`** — 路由合併管線除錯器（ADR-007），25 tests

### 生態整合

- **Backstage Plugin**：`components/backstage-plugin/` TypeScript/React plugin
  - `DynamicAlertingPage` + `DynamicAlertingEntityContent`
  - `PrometheusClient` API 層：via Backstage proxy 查詢 threshold / silent_mode / ALERTS
  - Entity 整合：`dynamic-alerting.io/tenant` annotation → 自動對應租戶

### 互動工具

- **Multi-Tenant Comparison** (`multi-tenant-comparison.jsx`)：Heatmap 色彩矩陣 + Outlier detection + Divergence Ranking（第 25 個 JSX 工具）
- **Alert Noise Analyzer** (`alert-noise-analyzer.jsx`)：MTTR 計算、震盪偵測、去重空間分析、Top noisy alerts（第 26 個）
- **ROI Calculator** (`roi-calculator.jsx`)：Rule 維護 / Alert Storm / Time-to-Market 三模型成本分析（第 27 個）

### DX Tooling

- **`check_frontmatter_versions.py`** — frontmatter version 全域掃描 + `--fix` 自動修復（29 tests）
- **`coverage_gap_analysis.py`** — per-file 覆蓋率排行報表（22 tests）
- **`check_bilingual_content.py`** — 雙語內容 CJK 比例 lint
- **`check_doc_links.py`** — 跨語言對應檔案驗證
- **`validate_all.py`** 增強：`--notify`（桌面通知）、`--diff-report`（CI 失敗自動 diff）
- **`generate_rule_pack_stats.py --format summary`** — Badge 風格單行輸出
- **Snapshot tests v2** — alert_correlate、drift_detect、bilingual_content 快照測試

### 安全加固

- SAST 規則擴充：6 rules 自動掃描（encoding + shell + chmod + yaml.safe_load + credentials + dangerous functions），189 patterns
- NetworkPolicy 精細化、container security context 強化
- 憑證掃描 + `.env` 防護 + `os.chmod 0o600` 補齊
- **CVE 緩解**：CVE-2025-15467 (openssl CVSS 9.8 pre-auth RCE) + CVE-2025-48174 (libavif buffer overflow)
  - 所有 Dockerfile 加入 `apk --no-cache upgrade` 拉取安全修補
  - `da-tools` base image pin 從 `python:3.13-alpine` → `python:3.13.2-alpine3.21`
- **CI Image Scanning**：release workflow 三個 image 均加入 Trivy 掃描（CRITICAL + HIGH 阻斷）

### 品質閘門

- Pre-commit hooks：12 → **13** 個 auto-run（新增 `routing-profiles-check`）
- `build.sh` 修補：新增遺漏的 `alert_correlate`、`notification_tester`、`threshold_recommend` 打包

### 測試覆蓋率

Python 測試總數從 v2.0.0 的 1,759 提升至 **3,070**（+75%）。v2.1.0 新增工具均達 95%+ 覆蓋率，5 個既有工具從 41–74% 提升至 63–99%。Coverage gate 維持 `fail_under=64`，實際整體覆蓋率高於此基線。

### 數字

| 項目 | v2.0.0 | v2.1.0 | 變化 |
|------|--------|--------|------|
| Python 工具 | 62 | 73 | +11 |
| da-tools CLI 命令 | 23 | 27 | +4 |
| JSX 互動工具 | 24 | 26 (+1 wizard) | +3 |
| ADRs | 5 | 7（006/007 Accepted） | +2 |
| Python 測試 | 1,759 | 3,070 | +1,311 |
| Pre-commit hooks | 12 + 5 manual | 13 + 5 manual | +1 |
| Go tests (new files) | — | +3 files (1,166 lines) | NEW |

### Benchmark — Incremental Hot-Reload（Go, `-count=3` median）

| Benchmark | ns/op | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| IncrementalLoad_NoChange_10 | 165,700 | 34,272 | 176 |
| IncrementalLoad_NoChange_1000 | 1,528,000 | 2,027,264 | 13,085 |
| IncrementalLoad_OneFileChanged_10 | 220,600 | 73,280 | 241 |
| IncrementalLoad_OneFileChanged_1000 | 6,908,000 | 6,652,880 | 22,211 |
| ScanDirFileHashes_1000 | 1,206,000 | 1,985,200 | 13,012 |

### 文件治理與正確性

**Root README (zh/en) 增強**
- 開頭改為問題導向定位（規則膨脹 + 變更瓶頸），新增「適用場景」聲明與版本 badge
- 「關鍵設計決策」表新增 ADR 連結欄 + Sentinel 三態控制、四層路由合併兩行
- Quick Start 下方新增「生產部署」指引，文件導覽新增 Day-2 Operations 路徑

**ADR 生命週期更新**
- ADR-006/007：`📋 Proposed` → `✅ Accepted (v2.1.0)`，checklist 改為實作摘要 + 後續方向
- ADR-004：「現況與後續方向」替代舊 Roadmap 段落
- ADR-001/003：新增 v2.1.0 living-doc 狀態行
- ADR-002/004：新增「相關決策」交叉引用（ADR-005/006）

**architecture-and-design.md**
- 新增 §2.12 Routing Profiles 與 Domain Policies（四層合併管線 Mermaid 圖）
- 「本文涵蓋內容」補上三態模式、Dedup、路由系統
- 拆分文件導覽表移除過時 §N 前綴
- ADR-006 工具引用、Rule Pack 數量修正、雙語 annotation 章節翻譯

**benchmarks.md 重構**
- §8（Alertmanager Idle-State）合併至 §10（Under-Load）作為 baseline 比較表
- §13（pytest-benchmark）去重：移除與 §7 重複的 route generation 行
- 傳統方案估算加註推算基礎（per-rule ~0.3ms / ~60KB）
- 自引用修正、相關資源連結格式修正

**docs/README.md (zh/en) 去重**
- 移除與 root README 重複的「工具速查」22 行表 → 精簡為摘要 + 連結
- 移除重複的「快速命令」和「版本與維護」段落

**Component README 修正**
- threshold-exporter：斷裂 §11.1 引用 → 指向 `gitops-deployment.md`
- da-tools：版號表 `v2.0.0` → `v2.1.0`，移除過時措辭
- da-portal：`24 JSX tools` → `26`，image tag `v2.0.0` → `v2.1.0`
- backstage-plugin：移除不存在的 `(§5.13)` 引用

**交叉引用修正**
- ADR-006 (zh/en)：`§2.6` → `§2.3`（Tenant-Namespace 映射模式）
- ADR README (zh/en)：ADR-006/007 badge 更新為 ✅ Accepted

### 🐛 Bug Fixes

- 修復 `entrypoint.py` help text 遺漏 `validate-config` 命令
- 4 處 Python error output 修正為 stderr
- `da-tools build.sh` TOOL_FILES 補齊遺漏工具

---

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 v2.1.0 正式釋出。

### 🔧 Go Exporter 增強

**Tenant Profiles（四層繼承）**
- Go schema 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- `applyProfiles()` fill-in pattern：Load 階段展開 profile 至 tenant overrides（僅填入未設定的 key）
- `_profiles.yaml` boundary enforcement：LoadDir 限制 profiles 只能從該檔載入
- `ValidateTenantKeys()` 擴展：`_profile` 引用不存在的 profile → WARN
- 繼承順序：Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- 13 個新 Go 測試案例

**Dual-Perspective Annotation**
- `platform_summary` annotation：Alert 同時攜帶 Platform 視角（NOC）和 Tenant 視角 summary
- 與 `_routing_enforced` 整合：NOC 收到 `platform_summary`，tenant 收到原始 `summary`

### 📦 Rule Pack 擴展（13 → 15）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`)：GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`)：active connections、request rate、connection backlog — 6 alert rules
- Projected Volume 13 → 15 ConfigMap sources，scaffold_tenant / metric-dictionary 同步更新

### 🚀 告警智能化（3 個新工具 + 1 個 Self-Service Portal）

**Alert Quality Scoring (`da-tools alert-quality`)**
- 4 項品質指標：Noise（震盪偵測）、Stale（閒置 14 天）、Resolution Latency（flapping 警告）、Suppression Ratio
- 三級評分（GOOD/WARN/BAD）+ per-tenant 加權分數（0–100）
- 輸出：text / `--json` / `--markdown`，CI gate：`--ci --min-score 60`
- 57 個測試，89.8% 覆蓋率

**Policy-as-Code (`da-tools evaluate-policy`)**
- 宣告式 DSL：10 種運算子（required / forbidden / gte / lte / matches / one_of ...）
- `when` 條件式、萬用字元目標（`*_cpu`）、dot-path 嵌套（`_routing.receiver.type`）
- Duration 比較、tenant 排除、error/warning 雙嚴重度
- CI gate：`--ci` 有 error 違規 exit 1
- 106 個測試，94.0% 覆蓋率

**Cardinality Forecasting (`da-tools cardinality-forecast`)**
- 純 Python 線性回歸（無 numpy）：趨勢分類（growing/stable/declining）+ 風險等級（critical/warning/safe）
- 觸頂天數預測 + 預計日期，可設基數上限（`--limit`）和預警天數（`--warn-days`）
- CI gate：`--ci` 有 critical 風險 exit 1
- 61 個測試，93.5% 覆蓋率

**Tenant Self-Service Portal (`self-service-portal.jsx`)**
- 三分頁 SPA：YAML 驗證（schema + routing guardrails）、告警預覽（滑桿模擬）、路由視覺化（樹狀圖）
- 瀏覽器端執行，零後端依賴，雙語支援（zh/en）

**Self-Hosted Portal (`da-portal` Docker image)**
- `ghcr.io/vencil/da-portal` — nginx:alpine 靜態 image，打包 24 JSX tools + Hub + Guided Flows + vendor JS
- 企業內網 / air-gapped 部署：`docker run -p 8080:80`，免 build step
- Volume mount 客製化：`platform-data.json`、`flows.json`、`nginx.conf`（含 Prometheus reverse proxy placeholder 解決 CORS）
- CI/CD：`portal/v*` tag 觸發 `release.yaml` 自動 build + push GHCR

### 🛠️ DX 自動化工具（+8 個新工具）

**Operations**
- **`shadow_verify.py`**：Shadow Monitoring 三階段驗證（preflight / runtime / convergence）
- **`byo_check.py`**：BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq）
- **`grafana_import.py`**：Grafana Dashboard ConfigMap 匯入（sidecar 掛載 + verify + dry-run）
- **`federation_check.py`**：多叢集 Federation 整合驗證（edge / central / e2e 三模式）

**Scalable Configuration Governance**
- **`assemble_config_dir.py`**：Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest
- **`da_assembler.py`**：ThresholdConfig CRD → YAML 輕量 controller（Watch / One-shot / 離線渲染 / Dry-run）
- **ThresholdConfig CRD**（`dynamicalerting.io/v1alpha1`）：namespace-scoped + RBAC + printer columns

**DX 工具迭代**
- `validate_all.py`：`--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- `bump_docs.py`：`--what-if`（全 238 rules 審計）
- `generate_cheat_sheet.py` / `generate_rule_pack_stats.py`：`--lang zh/en/all` 雙語
- `check_doc_freshness.py`：false-positive 修正 + `--fix`
- `check_translation.py`：cross-dir + lang fix
- `check_includes_sync.py`：`--fix`（自動建立缺失 .en.md stub）

### 🎯 互動工具生態（0 → 24 JSX tools）

**工具矩陣**：23 個位於 `docs/interactive/tools/` + 1 個 `docs/getting-started/wizard.jsx`
- Config：Playground、Lint、Diff、Schema Explorer、Template Gallery
- Rule Pack：Selector、Matrix、Detail、PromQL Tester
- 運維：Alert Simulator/Timeline、Health Dashboard、Capacity Planner、Threshold Calculator
- 學習：Architecture Quiz、Glossary、Dependency Graph、Runbook Viewer、Onboarding Checklist
- 展示：Platform Demo、Migration Simulator、CLI Playground、Self-Service Portal

**基礎設施**
- **tool-registry.yaml**（單一真相源）→ `sync_tool_registry.py`（`make sync-tools`）自動同步 Hub 卡片 + TOOL_META + JSX frontmatter
- **platform-data.json**（共用資料源）：從 Rule Pack YAML 萃取（15 packs, 139R + 99A），JSX 工具 fetch 共用
- **jsx-loader.html**：瀏覽器端 JSX transpiler + `TOOL_META`（related footer）+ `__PLATFORM_DATA` 預載 + Guided Flow 模式
- **tool-consistency-check**（pre-commit）：Registry ↔ Hub ↔ TOOL_META ↔ JSX ↔ MD 一致性驗證

**Guided Flows**
- `flows.json` 多步引導流程（onboarding / tenant-setup / alert-deep-dive），`?flow=onboarding` 啟動
- Cross-step data（`__FLOW_STATE` + sessionStorage）、progress persistence、completion tracking
- Conditional steps + checkpoint validation（`__checkFlowGate()` Next 按鈕閘門）
- Custom flow builder：`?flow=custom&tools=...` Hub 互動式 builder，24 工具全覆蓋
- Flow analytics：進度條、完成率、drop-off 步驟偵測

### 🌐 Bilingual Annotations (i18n)

- **Rule Pack 雙語 annotation**：`summary_zh` / `description_zh` / `platform_summary_zh` — 三個 Pilot Pack（MariaDB, PostgreSQL, Kubernetes）
- **Alertmanager template fallback**：Go `or` function 優先中文、自動 fallback 英文（所有 receiver 類型）
- **CLI i18n**：`detect_cli_lang()` 偵測 `DA_LANG`/`LANG` → argparse help 雙語切換（23 個 CLI 命令）
- **check_bilingual_annotations.py**：Rule Pack 雙語覆蓋率驗證（pre-commit manual stage）

### 📄 文件全面重構

**結構重組**
- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）zh/en
- 全面雙語化：33 → 46 對 `.en.md` 文件
- MkDocs Material 站點：CJK 搜尋、tags、i18n 切換、abbreviation tooltips
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）

**內容修訂**
- 根 README (zh/en) 重寫：角色導向痛點敘事（Platform / Tenant / Domain / Enterprise）
- architecture-and-design.en.md：補 §2.3 Tenant-Namespace Mapping、修 §3.1（15 packs + `prometheus-rules-*` 命名）、補 Bilingual Annotations
- Benchmarks 重寫：5 輪實測數據統一採集（idle + under-load + routing + alertmanager + reload）
- 6 份文件精簡（avg -23%）：移除過時內容、手動 curl 改為 da-tools CLI 引用
- Scenario CLI 修正：`tenant-lifecycle.md` (zh/en) 修正 4 個不存在的 CLI flags
- Tool-map 重生成：62 個工具完整覆蓋（之前僅 18 個）

**文件 CI 工具鏈（13 tools）**
- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `validate_all.py`：統一驗證入口

### 🔒 Security Audit & Hardening

- **程式碼安全**：ReDoS 防護（regex 長度限制）、URL 注入白名單、SSRF scheme 白名單（http/https only）、Prototype pollution 過濾（`__proto__`/`constructor`）、YAML 100KB 上限、`os.chmod` 補齊
- **文件安全加固**：HTTP→HTTPS 範例、webhook 驗證升為 error、`--web.enable-lifecycle` 安全註解、Grafana 密碼警告、新增「生產環境安全加固」章節

### 🏗️ 專案結構正規化

- **scripts/tools/ 三層子目錄化**：62 個工具分入 `ops/`（30）、`dx/`（18）、`lint/`（13）+ root（1 + 1 lib）
  * Docker flat layout 相容（dual sys.path + build.sh 自動 strip）
- **JSX 工具搬遷**：22 個工具 `docs/` → `docs/interactive/tools/`，registry/flows/loader/hub 路徑同步
- **測試歸位**：`test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **generate_tool_map.py 重寫**：自動掃描 ops/dx/lint/root 子目錄

### 🧪 測試工程化（14 輪系統化重構）

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| 測試檔案 | 5 | 40 | +35 |
| 測試數量 | ~790 | 1,759 | +969 |
| Go 測試 | 97 | 110 | +13 |
| Coverage gate | 無 | 64%（`setup.cfg`） | NEW |
| Test markers | 無 | 5（slow/integration/benchmark/regression/snapshot） | NEW |
| Factories | 無 | 12（`factories.py` + `PipelineBuilder`） | NEW |

**關鍵里程碑**：
- Wave 5-6：pytest 遷移、SAST 掃描器（189 rules）、整合測試
- Wave 7-8：property-based tests（Hypothesis）、snapshot tests（18 JSON）、coverage gate
- Wave 9-10：factories 拆分、domain policy、deepdiff structured diff
- Wave 11-12：unittest→pytest batch migration、metric_dictionary fixture
- Wave 13：conftest re-export cleanup、duplicate removal、factory docstrings
- Wave 14-16：parametrize、scaffold snapshots、benchmark baseline、validate_all coverage
- Wave 17：coverage attack — baseline_discovery（31→55%）、backtest_threshold（32→70%）、batch_diagnose（49→71%）
- Wave 18：parametrize sweep — 合併重複測試方法

### 🛡️ 品質閘門

- **Pre-commit hooks**：0 → 12 個 auto-run + 5 個 manual-stage（schema / translation / flow E2E / jsx-babel / i18n coverage）
- **新增 hooks**：`tool-map-check`、`doc-map-check`、`rule-pack-stats-check`、`glossary-check`、`changelog-lint`、`version-consistency`、`includes-sync`、`platform-data-check`、`repo-name-check`、`tool-consistency-check`、`structure-check`、`doc-links-check`
- **Docker CI 修正**：build.sh 自動 strip sys.path hack + 觸發路徑 `**/*.py` + 3 個遺漏工具打包修正
- **Conventional Commits** + `generate_changelog.py` 自動化

### 📦 Dependency Upgrades

- **Prometheus**: v2.53.0 → v3.10.0（PromQL 相容性已驗證，15 個 Rule Pack 無影響）
- **Alertmanager**: v0.27.0 → v0.31.1
- **configmap-reload**: v0.14.0 → v0.15.0
- **Grafana**: 11.1.0 → 12.4.1
- **kube-state-metrics**: v2.10.0 → v2.18.0
- **Go**: 1.22 → 1.26.1（go.mod + Dockerfile + CI）
- **Frontend CDN**: React 18.2.0 → 18.3.1、Babel 7.23.9 → 7.26.4、Lucide 0.383.0 → 0.436.0

### 📊 Numbers

| 項目 | v1.11.0 | v2.0.0 | 變化 |
|------|---------|--------|------|
| Rule Packs | 13 | 15 | +2 |
| Python 工具 | ~20 | 62 | +42 |
| da-tools CLI 命令 | 20 | 23 | +3 |
| JSX 互動工具 | 0 | 24 | +24 |
| 文件（docs/ .md） | ~20 | 68 | +48 |
| 雙語文件對 | 0 | 46 | +46 |
| Python 測試 | ~790 | 1,759 | +969 |
| 測試檔案 | 5 | 40 | +35 |
| Pre-commit hooks | 0 | 12 + 5 manual | +17 |
| Docker images | 2 | 3 | +1 (da-portal) |

### 📈 Benchmark（v2.0.0，15 Rule Packs，Kind 叢集）

**Idle-State（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v2.0.0 (15 packs) | 變化 |
|------|-------|-------|------|
| Total Rules | 141 | 237 | +96 |
| Rule Groups | 27 | 43 | +16 |
| Eval Time / Cycle | 20.3ms | 23.2ms | +2.9ms |
| p50 per-group | 1.23ms | 0.39ms | 改善 |
| p99 per-group | 6.89ms | 4.89ms | 改善 |
| Prometheus CPU | 0.014 cores | 0.004 cores | — |
| Prometheus Memory | 142.7MB | 112.6MB | — |
| Exporter Heap (×2 HA) | 2.4MB | 2.2MB | — |
| Active Series | ~6,037 | 6,239 | +202 |

**Go Micro-Benchmark（Intel Core 7 240H，`-count=5` median）：**

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 12,209 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 100,400 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 1,951,206 | 3,848,574 | 5,039 |
| ResolveAt_10Tenants_Mixed | 34,048 | 40,052 | 271 |
| ResolveAt_100Tenants_Mixed | 405,797 | 462,636 | 2,622 |
| ResolveAt_1000Tenants_Mixed | 5,337,575 | 5,258,548 | 26,056 |
| ResolveAt_NightWindow_1000 | 5,404,213 | 5,223,925 | 25,056 |
| ResolveSilentModes_1000 | 86,700 | 186,086 | 10 |

**Route Generation Scaling（Python `generate_alertmanager_routes.py`）：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

---

> **歷史版本 (v0.1.0–v1.11.0)：** 詳見 [`CHANGELOG-archive.md`](CHANGELOG-archive.md)
