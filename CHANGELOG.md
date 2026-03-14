---
title: "Changelog"
tags: [changelog, releases]
audience: [all]
version: v2.0.0-preview.3
lang: zh
---
# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v2.0.0-preview.3] — Project Structure Normalisation (2026-03-14)

v2.0.0 release polish：專案結構正規化、DX 改善、品質閘門強化。

### 🏗️ Project Structure Reorganisation

- **scripts/tools/ 三層子目錄化**: 58 個 Python 工具依職責分入 `ops/`（27）、`dx/`（18）、`lint/`（12）+ root 共用（4）
  * Docker flat layout 相容：dual sys.path + build.sh 自動 strip repo-layout hack
  * 跨子目錄 import 相容（dx→ops: `generate_platform_data` → `scaffold_tenant`）
- **JSX 工具搬遷**: 22 個互動工具從 `docs/` 搬至 `docs/interactive/tools/`
  * `tool-registry.yaml`、`flows.json`、`jsx-loader.html`（CUSTOM_FLOW_MAP + TOOL_META）、`index.html` 路徑同步更新
- **測試歸位**: `test_assemble_config_dir.py`、`test_da_assembler.py`、`test_flows_e2e.py` 統一搬入 `tests/`
- **CRD 範例歸位**: `example-thresholdconfig.yaml` → `k8s/crd/examples/`
- **inject_metadata_join.py** 從 `scripts/internal/` 搬入 `scripts/tools/ops/`

### 🛡️ Quality Gates

- **`check_structure.py`** (NEW): 專案結構正規化 pre-commit hook — 檢查 scripts/tools/ root 清潔度、JSX 位置、測試檔位置、禁追蹤目錄
- **`check_repo_name.py`** (NEW): GitHub URL repo name 防護 hook — 掃描 `vibe-k8s-lab` 誤用（`--ci` / `--fix`）
- **Pre-commit hooks**: 10 → 12 auto-run hooks（新增 `structure-check` + `repo-name-check`）
- **`tests/conftest.py`**: 統一 sys.path 設定，從 24 個測試檔移除重複 boilerplate

### 🐳 Docker & CI

- **build.sh**: 組裝時自動 `sed` 移除 repo-layout sys.path hack，Docker image 內程式碼更乾淨
- **docs-ci.yaml**: 修正觸發路徑 `scripts/tools/*.py` → `scripts/tools/**/*.py`（子目錄化後 CI 不漏觸發）
- **Dockerfile**: 修正 `org.opencontainers.image.source` repo name

### 📄 Documentation

- **GitLab CI**: 標記 `.gitlab/ci/` 為 deprecated（新增 README.md + YAML deprecated 標頭）
- **tool-map.md** (zh/en): 補齊 `check_structure.py`、`fix_doc_links.py`、`check_i18n_coverage.py`、`sync_tool_registry.py` 等遺漏條目
- **CLAUDE.md**: hook 數量 → 12、lint 工具 → 12、總工具 → 58
- **.gitignore**: 新增 `.pytest_cache/`、`*.pyc`、`tests/_test_output/`

### 🧹 Cleanup

- 移除 git 追蹤的測試產出（`tests/_test_output/`、`tests/_test_multidb_output/`）
- 移除 `.gitignore` 重複條目

## [v2.0.0-preview.2] — Scalable Config Governance + DX Tooling (2026-03-14)

Roadmap §5.3 全面實作（Sharded GitOps + Assembler Controller + CRD）、9 輪 DX 工具迭代、GitHub Pages Interactive Tools、Roadmap 重寫。

### 🏗️ Scalable Configuration Governance (§5.3)

- **`assemble_config_dir.py`**: Sharded GitOps 組裝工具 — 多來源 conf.d/ 合併、SHA-256 衝突偵測、assembly manifest、YAML 驗證
  * CLI: `da-tools sharded-assemble --sources <dirs> --output <dir> [--check] [--validate] [--manifest]`
- **`da_assembler.py`**: ThresholdConfig CRD → YAML 輕量 controller（非 Operator）
  * Watch 模式（即時 reconcile）、One-shot 模式（`--once`）、離線渲染（`--render-cr`）、Dry-run 預覽
  * Status subresource 更新（phase / lastRenderedHash / tenantCount）
  * CLI: `da-tools assembler --once` / `da-tools assembler --render-cr <file>`
- **ThresholdConfig CRD** (`k8s/crd/thresholdconfig-crd.yaml`): `dynamicalerting.io/v1alpha1`，namespace-scoped
  * `x-kubernetes-preserve-unknown-fields` 支援彈性 tenant 閾值結構
  * Printer columns: Phase, Tenants, Last Rendered, Age
  * Short names: `tc`, `tconfig`
- **RBAC** (`k8s/crd/assembler-rbac.yaml`): ServiceAccount + ClusterRole + ClusterRoleBinding
- **Makefile**: `make sharded-assemble` / `sharded-check` / `assembler-render` / `assembler-install-crd`

### 🛠️ DX Tooling (Rounds 7–9, 11 improvements)

- **`generate_doc_map.py`**: `--include-adr`（ADR 納入 doc-map，H1 title 萃取）
- **`validate_docs_versions.py`**: doc-file-count 自動驗證 + auto-fix
- **`bump_docs.py`**: `--what-if`（全 238 rules 審計）
- **`generate_cheat_sheet.py`**: `--lang zh/en/all` 雙語速查表
- **`check_doc_freshness.py`**: false-positive 修正（code-block-only + stopword）、`--fix`（`.doc-freshness-ignore` 自動產生）
- **`check_translation.py`**: cross-dir + lang fix（full-path pairing + empty-lang guard）
- **`validate_all.py`**: `--profile` + `--watch`（CSV timing trend）、`--smart`（git diff → affected-check 自動跳過）
- **`generate_rule_pack_stats.py`**: `--lang zh/en/all` 雙語統計表
- **`check_includes_sync.py`**: `--fix`（自動建立缺失 .en.md stub）

### 🌐 GitHub Pages Interactive Tools

- **`docs/interactive/index.html`**: Landing page（Dark mode、4 card navigation）
- **`docs/assets/jsx-loader.html`**: 瀏覽器端 JSX 載入器改寫
  * Front matter 剝離、ES import → global reference 轉換、lucide-react CDN + SVG fallback
  * `export default function` → auto-render

### 📄 Roadmap Rewrite (§5)

- 移除已完成項目（搬至 `docs/internal/dx-tooling-backlog.md`）
- 新增 4 個方向：Alert Quality Scoring、Policy-as-Code、Cross-Cluster Drift Detection、Incremental Reload
- 重新分類：近期（設計基礎已有）/ 中期（需客戶驗證）/ 遠期（探索方向）

### 📊 Numbers

- Python 工具：50 個（+4）
- 文件：44 個（+1）
- 單元測試：31 個新增（14 assemble + 17 assembler）
- 驗證 pipeline：11 checks pass

---

## [v2.0.0-preview] — DX Automation + Documentation Overhaul (2026-03-13)

Major documentation quality overhaul + 4 new DX automation tools. Breaking: version jump from v1.13.0 to v2.0.0 reflects the scope of documentation restructuring and new tooling.

### 🛠️ DX Automation Tools (4 new)

- **`shadow_verify.py`**: Shadow Monitoring 就緒度與收斂性三階段驗證（preflight / runtime / convergence）
  * Preflight: mapping file, recording rules loaded, AM interception route
  * Runtime: mismatch count, tenant coverage, three-state mode consistency
  * Convergence: cutover-readiness assessment, 7-day zero-mismatch check
  * CLI: `da-tools shadow-verify <phase> --mapping <file> --report-csv <file> --json`
- **`byo_check.py`**: BYO Prometheus & Alertmanager 整合驗證（取代手動 curl + jq 步驟）
  * Prometheus: connection, tenant label injection, threshold-exporter scrape, Rule Pack loading, vector matching
  * Alertmanager: connection, tenant routing, inhibit_rules, active alerts, silences
  * CLI: `da-tools byo-check <prometheus|alertmanager|all> --json`
- **`grafana_import.py`**: Grafana Dashboard ConfigMap 匯入（sidecar 自動掛載）
  * Single/batch import + verify mode + dry-run
  * CLI: `da-tools grafana-import --dashboard <file> --verify --namespace <ns>`
- **`federation_check.py`**: 多叢集 Federation 整合驗證（edge / central / e2e 三模式）
  * Edge: external_labels, federate endpoint; Central: edge metrics reception, recording rules
  * CLI: `da-tools federation-check <edge|central|e2e> --edge-urls <urls> --json`

### 📄 Documentation Overhaul

**Content Correctness & Trimming（6 document pairs, avg -23%）：**

| Document Pair | Before | After | Reduction |
|---------------|--------|-------|-----------|
| migration-guide | ~997 | ~768 | -23% |
| byo-prometheus-integration | ~558 | ~483 | -14% |
| byo-alertmanager-integration | ~510 | ~468 | -8% |
| grafana-dashboards | ~598 | ~385 | -36% |
| tenant-lifecycle | ~706 | ~497 | -30% |
| multi-cluster-federation | ~640 | ~468 | -27% |

**Key Changes：**
- 移除捏造/過時內容，確保所有文件描述與實際程式碼行為一致
- 手動 `curl + jq` 驗證步驟全面替換為 `da-tools` CLI 工具引用
- 冗長 `docker run --rm -v ...` 範例精簡為 `da-tools <cmd>` 短格式
- 移除重複的 Python script fallback 區塊（「已 clone 專案」模式）
- 冗長 K8s Job YAML 區塊替換為精簡描述 + cross-reference
- 多處 verbose subsection 合併為 compact reference table

**CLI Reference & Cheat Sheet：**
- `docs/cli-reference.md` (.en.md): 新增 4 個 DX 工具完整命令文件（shadow-verify / byo-check / federation-check / grafana-import）
- `docs/cheat-sheet.md` (.en.md): 新增 4 個工具速查行 + 快速提示分類
- Version Compatibility 表：補上 v1.10.0–v1.13.0 版號

**CLAUDE.md 瘦身：**
- 「文件導覽」表格（46 行）提取至 `docs/internal/doc-map.md`，CLAUDE.md 僅保留 cross-reference

### 📦 版號

- da-tools: 1.12.0 → 2.0.0-preview（新增 4 個 DX 命令，COMMAND_MAP 20→24）

---

## [v1.13.0] — Dual-Perspective Annotation + Documentation Infrastructure (2026-03-12)

Dual-Perspective Alert Annotation（`platform_summary`）、文件大重構、全面雙語化、MkDocs Material 站點配置、文件 CI 工具鏈、互動式元件、Conventional Commits、API 健康監控。

### 🏷️ Dual-Perspective Annotation

- **`platform_summary` annotation**: Alert 同時攜帶 Platform 視角（NOC 用）和 Tenant 視角的 summary
- **`_routing_enforced` 整合**: 雙視角 annotation 搭配 enforced routing，NOC 收到 platform_summary，tenant 收到原始 summary

### 📄 文件大重構

- architecture-and-design.md 拆分為 6 個專題文件（benchmarks / governance-security / troubleshooting / migration-engine / federation-integration / byo-prometheus-integration）
- 3 個角色入門指南（for-platform-engineers / for-domain-experts / for-tenants）
- Context Diagram（context-diagram.md）
- 全面雙語化：33 對 `.en.md` 文件

### 🌐 MkDocs Material 站點

- CJK 搜尋最佳化 + tags plugin + i18n 切換 + abbreviation tooltips
- YAML front matter（title / tags / audience / version / lang）全文件覆蓋

### 🔧 文件 CI 工具鏈（12 tools）

- `validate_mermaid.py` / `check_doc_links.py` / `check_doc_freshness.py` / `doc_coverage.py`
- `add_frontmatter.py` / `doc_impact.py` / `check_translation.py` / `check_includes_sync.py`
- `sync_glossary_abbr.py` / `sync_schema.py` / `generate_cheat_sheet.py` / `inject_related_docs.py`
- `generate_rule_pack_readme.py` / `generate_alert_reference.py` / `generate_nav.py` / `generate_changelog.py`
- `validate_all.py`：統一驗證入口（11 項檢查）

### 🎨 互動式元件

- `docs/getting-started/wizard.jsx`：角色導向入門精靈
- `docs/interactive/tools/playground.jsx`：Tenant YAML 驗證 Playground
- `docs/interactive/tools/rule-pack-selector.jsx`：Rule Pack 選擇器
- `docs/interactive/tools/cli-playground.jsx`：CLI 指令建構器

### 📦 其他

- Conventional Commits + `generate_changelog.py`
- API 端點健康監控（Blackbox Exporter）+ README badges
- Glossary（30+ 術語）+ 5 ADRs + JSON Schema（VS Code 自動補全）
- Doc Include 片段（docker-usage-pattern / prometheus-url-config / verify-checklist / three-state-summary）

---

## [v1.12.0] — Tenant Profiles + JVM/Nginx Rule Packs (2026-03-12)

Tenant Profiles 四層繼承鏈（defaults → profile → tenant override）、JVM + Nginx 兩個新 Rule Pack（13→15 ConfigMaps）、Python 工具鏈全面整合。

### 🏷️ Tenant Profiles（§11.4 Phase 1）

- **Go schema `_profile`**: Config struct 新增 `Profiles map[string]map[string]ScheduledValue` 欄位
- **`applyProfiles()` fill-in pattern**: 在 Load 階段將 profile 值展開至 tenant overrides map（僅填入 tenant 未設定的 key），所有 Resolve* 函式無需修改
- **`_profiles.yaml` boundary enforcement**: LoadDir 限制 profiles 只能從 `_profiles.yaml` 載入，其他 tenant 檔案含 `profiles:` → WARN + 忽略
- **`ValidateTenantKeys()` 擴展**: `_profile` 引用不存在的 profile name → WARN
- **四層繼承順序**: Global Defaults → Profile → Tenant Override（tenant 永遠勝出）
- **13 個新 Go 測試案例**: Profile 基本繼承、tenant 覆寫、disable、routing/silent/metadata 繼承、ScheduledValue、LoadDir boundary 等

### 📦 Rule Pack 擴展（§11.1）

- **JVM Rule Pack** (`rule-pack-jvm.yaml`): GC pause rate、heap memory usage、thread pool — 7 alert rules（含 composite `JVMPerformanceDegraded`）
- **Nginx Rule Pack** (`rule-pack-nginx.yaml`): active connections、request rate、connection backlog — 6 alert rules
- **Projected Volume**: 13→15 個 ConfigMap sources
- **scaffold_tenant.py**: 新增 `jvm` + `nginx` RULE_PACKS 條目
- **metric-dictionary.yaml**: 新增 6 個 metric 對照條目

### 🔧 Python 工具鏈整合

- **`scaffold_tenant.py`**: 新增 `--profile` CLI 參數 + 互動模式 profile 提示
- **`config_diff.py`**: Profile 變更爆炸半徑計算（掃描引用該 profile 的 tenant 清單），JSON 輸出新增 `profile_diffs`
- **`validate_config.py`**: 新增 Check 6 (profile references validation)，驗證所有 `_profile` 引用指向已定義的 profile
- **`diagnose.py`**: 新增 `--config-dir` 參數，健康報告顯示 tenant 使用的 profile name
- **`_lib_python.py`**: `VALID_RESERVED_KEYS` 新增 `_profile`

### 📊 測試

| 項目 | v1.11.0 | v1.12.0 | 變化 |
|------|---------|---------|------|
| Go tests | 97 | 110 | +13 (profile 測試) |
| Rule Packs | 13 | 15 | +2 (JVM, Nginx) |

### 📈 Benchmark（15 Rule Packs，Kind 叢集）

**Idle-State 量測（2 tenant，237 rules，43 rule groups）：**

| 指標 | v1.11.0 (13 packs) | v1.12.0 (15 packs) | 變化 |
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

**Route Generation Scaling：**

| Tenants | Wall Time | Routes | Inhibit Rules |
|---------|-----------|--------|---------------|
| 2 | 181ms | 3 | 2 |
| 10 | 196ms | 8 | 10 |
| 50 | 248ms | 41 | 50 |
| 100 | 327ms | 80 | 100 |

### 📄 文件

- CLAUDE.md: Rule Pack 數量 13→15
- README.md / README.en.md: 數量同步 + Mermaid 圖新增 jvm/nginx
- architecture-and-design.md (中/英): 數量同步 + Mermaid 圖更新
- byo-prometheus-integration.md: 數量同步
- rule-packs/README.md: 新增 JVM + Nginx 列
- docs/internal/v1.12-development-plan.md: 完整開發計畫

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
