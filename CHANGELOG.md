---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.0.0
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v2.0.0] — Alert Intelligence + Full-Stack DX Overhaul (2026-03-15)

v2.0.0 正式版。自 v1.11.0 起的全量升級：76 個 commits、346 個檔案變更（+73,057 / -12,023）。涵蓋 Go Exporter 增強、Rule Pack 擴展、告警智能化、互動工具生態、文件全面重構、測試工程化、專案結構正規化。

> **版號說明**：v1.12.0 / v1.13.0 / v2.0.0-preview 系列皆為開發中版本（無 Git tag / GitHub Release），統一於 v2.0.0 正式釋出。

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

## [v1.11.0] — Dynamic Runbook Injection + Recurring Maintenance + Config Drift CI (2026-03-08)

PromQL 層級 Runbook 注入（`tenant_metadata_info` info metric + Rule Pack `group_left` join）、排程式維護窗口自動 Alertmanager silence、Config Drift CI 模板。

### 🏷️ Dynamic Runbook Injection

- **Go schema `_metadata`**: 新增 `TenantMetadata` struct（`runbook_url` / `owner` / `tier`），透過 `ResolveMetadata()` 解析
- **`tenant_metadata_info` info metric**: Exporter 無條件輸出所有 tenant 的 metadata labels（值永遠為 1），保證 `group_left` join 不會漏掉任何 tenant
- **11 個 Rule Pack 自動注入**: 所有含 `unless on(tenant)` 的 alert expr 加上 `* on(tenant) group_left(runbook_url, owner, tier) tenant_metadata_info`，annotations 新增 `runbook_url` / `owner` / `tier`
- **Python metadata extraction**: `load_tenant_configs()` 回傳 5-tuple（新增 `metadata_configs`），支援 `{{tenant}}` 佔位符替換

### 🔧 Recurring Maintenance Schedules

- **Go schema `RecurringSchedule`**: `_state_maintenance.recurring[]` 欄位（`cron` / `duration` / `reason`）
- **`maintenance_scheduler.py`**: CronJob 工具，每 5 分鐘評估排程式維護窗口
  * 讀取 conf.d/ 的 `_state_maintenance.recurring` 配置
  * `croniter` 計算 cron 視窗，`parse_duration()` 支援 Go-style 時間格式（`1d` / `4h` / `2h30m` / `1d12h`）
  * Alertmanager API `/api/v2/silences` 冪等建立 silence（相同 tenant+reason 不重複建立）
  * **Silence 自動延展**：既有 silence 到期時間早於視窗結束時，自動 extend（`POST` with existing ID）；解析失敗時安全 skip
  * HTTP retry with exponential backoff（1s→2s→4s），僅重試 5xx + 連線錯誤
  * **Pushgateway 可觀測性**：`--pushgateway` flag 推送 5 個 gauge metrics（`# TYPE` 註解符合 Prometheus exposition format），CronJob 運行狀態可被 Prometheus 主動抓取
  * CLI：`--config-dir`、`--alertmanager`、`--pushgateway`、`--dry-run`、`--json-output`
- **K8s CronJob manifest**: `cronjob-maintenance-scheduler.yaml`，`*/5 * * * *` 排程（含預留 `--pushgateway` 參數註解）

### 🔄 Config Drift CI

- **`config_diff.py` exit codes**: 0=無變更、1=有變更（CI signal）、2=錯誤
- **CI 模板**: `.github/workflows/config-diff.yaml`（GitHub Actions）、`.gitlab/ci/config-diff.gitlab-ci.yml`（GitLab CI）
- **`gitops-deployment.md`**: 新增 CI 模板對照表 + exit code 表 + Data-Driven Threshold Review 雙引擎概念

### 📦 版號

- threshold-exporter: 1.8.0 → 1.9.0（新增 `tenant_metadata_info` metric + `_metadata` reserved key + `RecurringSchedule` schema）
- da-tools: 1.10.0 → 1.11.0（新增 `maintenance-scheduler` 命令，COMMAND_MAP 19→20）

### 📊 測試

| 項目 | v1.10.0 | v1.11.0 | 變化 |
|------|---------|---------|------|
| Python tests | 750+ | 790+ | +40+ |
| Go tests | +6 | +6 | metadata + schema |
| 新增測試檔 | — | `test_maintenance_scheduler.py` | 1 file |

### 📄 文件

- architecture-and-design.md §11 roadmap: Runbook Injection / Recurring Maintenance / Config Drift CI 移入「已完成」
- CLAUDE.md 工具表 +1（maintenance_scheduler）、長期展望移除已完成項
- README 工具數 19→20
- CronJob manifest 新增

---

## [v1.10.0] — Shadow Monitoring 自動化 + AM GitOps 閉環 + 盲區掃描 + 配置差異比對 (Shadow Monitoring Automation + AM GitOps + Blind Spot Discovery + Config Diff) (2026-03-08)

Shadow Monitoring 工具鏈完善（One-command 切換 + Grafana 儀表板）、Alertmanager 配置 GitOps 閉環、Cluster 監控盲區掃描、Directory-level 配置差異比對。

### 🎯 Shadow Monitoring 自動化

- **`cutover_tenant.py`**: 一鍵執行 shadow-monitoring-sop.md §7.1 所有切換步驟
  * 消費 `cutover-readiness.json`（由 validate_migration.py 自動生成）
  * 順序執行：停止 Shadow Monitor Job → 移除舊 Recording Rules → 移除 migration_status:shadow label → 移除 Alertmanager shadow route → check-alert + diagnose 驗證
  * 支援 `--dry-run` 預覽、`--force` 跳過 readiness 檢查
  * da-tools CLI 新增 `cutover` 命令

### 📊 Shadow Monitoring 可視化

- **`shadow-monitoring-dashboard.json`**: Grafana 儀表板
  * 5 個面板：Shadow Rules Active、Per-Tenant Status Table、Old vs New Comparison、Delta Trend、Inhibited Shadow Alerts
  * Prometheus 數據源（Recording Rule 直接查詢，不依賴 CSV）
  * Template Variables：`$tenant`（auto-discover）、`$old_metric` / `$new_metric`（手動輸入配對）

### 🔧 AM GitOps 閉環

- **`generate_alertmanager_routes.py --output-configmap`**: 產出完整 Alertmanager ConfigMap YAML（不只 fragment），供 Git PR flow 使用
  * `--base-config <path>` 載入基礎配置（global + default route/receiver），缺失時使用內建預設
  * 與 `--apply` 互斥（`argparse` mutually_exclusive_group）
  * 支援 `--dry-run`（僅印不寫）和 `-o <file>`（寫入檔案）
  * ConfigMap 結構：`apiVersion: v1` / `kind: ConfigMap` / `data.alertmanager.yml`

### 🔍 Blind Spot Discovery

- **`blind_spot_discovery.py`**: 掃描 Prometheus cluster targets 與 tenant config 交叉比對，找出未被監控的 DB instance
  * 呼叫 `/api/v1/targets?state=active` 擷取活躍目標
  * `JOB_DB_MAP` 對齊 rule-packs/ 目錄名（mariadb / postgresql / redis / mongodb / kafka / rabbitmq / elasticsearch / oracle / clickhouse / db2）
  * 三種狀態：`covered`（有 tenant config）、`blind_spot`（叢集有但未納管）、`unrecognized`（job 無法推斷 DB type）
  * CLI：`--prometheus`、`--config-dir`、`--json-output`、`--exclude-jobs`

### 📊 Directory-level Config Diff

- **`config_diff.py`**: 比較兩個 `conf.d/` 目錄，產出 per-tenant blast radius 報告
  * 變更分類：`tighter`（閾值下降）/ `looser`（閾值上升）/ `added` / `removed` / `toggled`（enable↔disable）/ `modified`
  * 啟發式推斷受影響 alert name（metric key → CamelCase pattern）
  * Markdown 表格 + Summary 輸出，適用於 PR review comment
  * CLI：`--old-dir`、`--new-dir`、`--json-output`

### 🔧 Bug Fix

- **da-tools `build.sh`**: TOOL_FILES 從 10 補齊至 19（修正自 v1.8.0 起缺少的 7 個工具 + `_lib_python.py` 共用函式庫）

### 📦 da-tools CLI

- 新增命令: `cutover`, `blind-spot`, `config-diff`
- COMMAND_MAP: 16 → 19 個命令
- da-tools 版號: 1.9.0 → 1.10.0

### 📊 測試

| 項目 | v1.9.0 | v1.10.0 | 變化 |
|------|--------|---------|------|
| Python tests | 632 | 750+ | +120+ |
| 新增測試檔 | — | `test_cutover_tenant.py`, `test_blind_spot_discovery.py`, `test_config_diff.py` | 3 files |

### 📄 文件

- shadow-monitoring-sop.md §8: 移除「尚在規劃中」，Shadow Dashboard + cutover 標記為已實現
- §9 快速參考卡更新 cutover 指令
- architecture-and-design.md §11 roadmap: §11.3/11.5/11.6 移入「v1.10.0 已完成」，P2 剩餘 3 項
- CLAUDE.md 工具表 +3、長期展望移除已完成項
- README 工具數 17→19

---


---

> **歷史版本 (v0.1.0–v1.9.0)：** 詳見 repo 根目錄的 CHANGELOG-archive.md
