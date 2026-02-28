# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v0.12.0] - Exporter Core Expansion: B1 + B4 (2026-02-28)

threshold-exporter Go 核心重構：支援 regex 維度閾值與排程式閾值覆蓋。

### 🔧 B1: Regex 維度閾值

* **`parseLabelsStringWithOp()`**: Config key 支援 `=~` 運算子（如 `oracle_tablespace{tablespace=~"SYS.*"}`）
* **`RegexLabels` field**: `ResolvedThreshold` 新增 regex label map，collector 以 `_re` 後綴輸出
* **PromQL 匹配策略**: Exporter 輸出 regex pattern 為 label value，recording rules 透過 `label_replace` + `=~` 匹配
* **混合模式**: 同一 key 可同時包含 exact (`=`) 和 regex (`=~`) label matcher

### ⏰ B4: 排程式閾值 (Time-Window Overrides)

* **`ScheduledValue` 型別**: 自訂 `UnmarshalYAML`，支援雙格式：
  * 純量字串 `"70"` — 完全向後相容
  * 結構化 `{default: "70", overrides: [{window: "01:00-09:00", value: "1000"}]}`
* **`ResolveAt(now time.Time)`**: 時間感知解析，取代原本的 `Resolve()` 作為核心方法
* **跨午夜支援**: `matchTimeWindow()` 正確處理 `22:00-06:00` 等跨日窗口
* **UTC-only 設計**: 窗口時間統一為 UTC，時區轉換由 Tenant 自行處理
* **三態相容**: 窗口內 `value: "disable"` 可在特定時段停用告警

### 🏗️ Tenants 型別重構

* **型別變更**: `Tenants` 從 `map[string]map[string]string` 升級為 `map[string]map[string]ScheduledValue`
* **向後相容**: 所有現有 YAML 配置透過 `UnmarshalYAML` 自動轉換為 `ScheduledValue`
* **`loadDir` 合併邏輯**: Directory mode deep-merge 更新為新型別
* **`configViewHandler`**: `/api/v1/config` 端點顯示 time override 數量，支援 `?at=<RFC3339>` 查詢參數以 debug 排程式閾值

### 🧪 測試套件

* **56 個測試函數** (26 個既有 Go 測試更新為 ScheduledValue 型別 + 30 個新增)：
  * `ScheduledValue` YAML 解析 (scalar / structured / mixed)
  * `ResolveValue` 時間窗口匹配 (same-day / cross-midnight / boundary / first-match-wins)
  * `ResolveAt` 整合測試 (scheduled override / scheduled disable / scheduled critical)
  * `matchTimeWindow` 邊界條件 (minute precision / non-UTC input conversion)
  * `parseHHMM` 輸入驗證
  * `parseLabelsStringWithOp` regex 解析 (pure regex / mixed / multiple)
  * Regex dimensional 解析 + B1+B4 組合測試 + 負面案例 (regex+_critical 不支援)
  * HTTP handler 測試 (healthHandler / configViewHandler regex 顯示 / 排程 override 計數 / `?at=` 時間覆寫 / readyHandler 狀態)
  * Collector Prometheus 整合測試 (_re suffix / mixed exact+regex / state filter)
  * Directory mode ScheduledValue 合併測試

---

## [v0.11.0] - AST Migration Engine (2026-02-28)

`migrate_rule.py` 核心升級：以 AST 取代 regex 進行 PromQL 解析，實現精準 metric 辨識與安全改寫。

### 🧬 AST Engine (promql-parser Rust/PyO3)

* **`migrate_rule.py` v4**: 引入 `promql-parser` 0.7.0 (Rust/PyO3 binding) 作為 PromQL 解析核心
  * AST-Informed String Surgery: 先用 AST 精準定位 VectorSelector 節點，再用字串操作改寫
  * Metric name 辨識不再依賴 function blacklist (`PROMQL_FUNCS`)，直接由 AST 提取
  * 支援巢狀 `and/or/unless`、`offset`、subquery 等複雜 PromQL 結構
* **Prefix injection**: AST 驗證的 word-boundary 替換，不誤改子字串或 label name
* **Tenant label injection**: 自動注入 `tenant=~".+"` matcher 到所有 VectorSelector
* **Reparse validation**: 每次改寫後 reparse 驗證，失敗則回退原始表達式
* **Graceful degradation**: `promql-parser` 未安裝時自動降級為 regex (`--no-ast` 可強制)

### 🧪 測試套件

* **`tests/test_migrate_ast.py`**: 54 個測試案例，涵蓋:
  * AST metric 提取 (簡單/巢狀/複合/histogram_quantile)
  * Prefix injection (含子字串安全/複合表達式)
  * Tenant label injection (有/無現有 labels/巢狀函式/同 metric 多次出現)
  * 「Regex Killer」案例: compound and、regex labels、aggregation+offset
  * 語義中斷偵測 (含巢狀 Call 節點: absent(rate())、predict_linear in sum)
  * Metric Dictionary 載入與查找測試
  * write_outputs / write_triage_csv 整合測試
  * `parse_expr` all_metrics 欄位驗證 (simple / compound / no-ast)
  * AST 路徑端到端 write_outputs 整合 (tenant label 注入驗證)
  * 降級行為、端到端 process_rule 整合

### 🐳 da-tools Container

* **Dockerfile**: 新增 `promql-parser==0.7.0` 依賴 (Alpine pre-built wheel)

---

## [v0.10.0] - Governance, Documentation Restructure & CI Linting (2026-02-28)

本版本建立多租戶客製化規則治理框架，重整文件架構，並新增 CI 護欄工具。

### 📋 三層治理模型 (Custom Rule Governance)

* **`docs/custom-rule-governance.md`**: 全新治理規範文件，定義三層客製化規則模型：
  * Tier 1 (Standard): Config-driven 三態控制，覆蓋 ~80% 需求
  * Tier 2 (Pre-packaged Scenarios): 平台預製複合場景，Tenant 僅控制啟停
  * Tier 3 (True Custom): 嚴格治理，獨立 Rule Group 隔離，帶 expiry date
* **RnR 權責定義**: Platform Engineering / Domain Experts / Tenant Teams 三角責任歸屬
* **SLA 切割**: Tier 1-2 由平台保證，Tier 3 不保證 SLA，平台有權強制下架
* **收編週期 (Assimilation Cycle)**: 季度 review，將具共性的 Tier 3 晉升為 Tier 2

### 🛡️ CI Deny-list Linting

* **`scripts/tools/lint_custom_rules.py`**: Custom Rule 治理合規 linter
  * 禁止高成本函式 (`holt_winters`, `predict_linear`)
  * 禁止危險 regex (`=~".*"`) 和 tenant 隔離破壞 (`without(tenant)`) — whitespace-tolerant 比對
  * 強制 `tenant` label、限制 range vector duration
  * 支援自訂 policy 檔 (`--policy`) 和 CI 模式 (`--ci`)
* **`.github/custom-rule-policy.yaml`**: 預設 deny-list 規則定義檔

### 🧪 測試套件

* **`tests/test_lint_custom_rules.py`**: 40 個測試案例，涵蓋:
  * Duration 解析、denied function 偵測 (含子字串安全)
  * Denied pattern 偵測 (whitespace 變體: `=~ ".*"`, `without (tenant)`)
  * Range vector duration 超限、required label 檢查
  * Tier 3 governance labels (expiry / owner)
  * 完整檔案 lint (直接格式 + ConfigMap wrapper + 空檔 + 不存在)
  * Policy 載入合併、group interval 檢查、檔案收集
* **`tests/test_bump_docs.py`**: 11 個測試案例，涵蓋:
  * `_build_rules()` 結構完整性 (三條版號線 + 必要 key)
  * `apply_rules()` check-only / 寫入模式 / whole_file 模式
  * 邊界案例 (檔案不存在、pattern 無匹配)
  * `read_current_versions()` 真實 repo 讀取

### 📄 文件重整

* **Playbook 搬移**: `testing-playbook.md` / `windows-mcp-playbook.md` 移至 `docs/internal/`，與 user-facing 文件分離
* **文件導覽重排**: 按讀者旅程排序 (架構→部署→整合→遷移→治理→SOP)
* **前置需求改寫**: 必要條件僅列 Docker Engine + kubectl；Dev Container 降為建議選項
* **README.en.md**: 同步更新所有上述變更

---

## [v0.9.0] - Ecosystem Integration, CI/CD Decoupling & Test Visibility (2026-02-27)

本版本聚焦於企業生態系整合、版號治理與測試透明度，不涉及 Go 核心程式碼變更。

### 🔌 BYOP 整合指南 (Bring Your Own Prometheus)

* **`docs/byo-prometheus-integration.md`**: 全新獨立文件，指引 Platform Engineer 以 3 個最小步驟將現有 Prometheus / Thanos 叢集接入動態閾值引擎：
  1. 透過 `relabel_configs` 注入 `tenant` 標籤
  2. 設定 `scrape_configs` 抓取 `threshold-exporter`
  3. 掛載黃金規則包 (Projected Volume / PrometheusRule CRD)
* 每個步驟附完整的 curl/jq 驗證命令 + 端到端 Checklist。
* **Appendix**: Prometheus Operator (kube-prometheus-stack) 的 ServiceMonitor / PrometheusRule 等價設定。

### 🧰 da-tools CLI 容器

* **`components/da-tools/`**: 可攜帶 CLI 驗證工具容器 (`ghcr.io/vencil/da-tools`)，打包 7 個 Python 工具 + metric-dictionary.yaml：
  * Prometheus API 工具：`check-alert`、`baseline`、`validate`
  * 檔案系統工具：`migrate`、`scaffold`、`offboard`、`deprecate`
* **設計理念**: 不需 clone 專案，`docker pull` 即可驗證整合或遷移規則。
* 支援 `PROMETHEUS_URL` 環境變數，可直接在 K8s Job 中執行。
* 獨立版號 `tools/v0.1.0`，與平台和 exporter 版號脫鉤。
* `docs/byo-prometheus-integration.md` 和 `docs/migration-guide.md` 均新增 `da-tools` docker run 範例。

### 🏗️ CI/CD 版號治理

* **`release-exporter.yaml`**: 觸發條件從 `v*` 改為 `exporter/v*`，避免文件更新誤觸發 Docker image 重建。
* **`release-tools.yaml`**: 新增 da-tools CI/CD workflow，`tools/v*` tag 觸發。
* **Helm Chart 雙版號分離**: `Chart.yaml` 的 `version` (0.9.0) 與 `appVersion` (0.5.0) 正式脫鉤，Chart 結構升級不再連帶 Go binary 版號。
* 三條版號線互不干擾：`v*` (平台文件) / `exporter/v*` (Go binary) / `tools/v*` (Python CLI)。

### 📊 測試透明度

* **Enterprise Test Coverage Matrix** (`docs/architecture-and-design.md` §9.2): 矩陣表格對應 scenario-a~f + demo-full 到企業防護場景與斷言邏輯。中英文版同步。
* **Mermaid 流程圖** (`docs/architecture-and-design.md` §9.3–9.5):
  * §9.3 demo-full 時序圖：composite load → alert firing → cleanup → resolved 完整生命週期
  * §9.4 Scenario E 流程圖：雙維度隔離驗證 (閾值修改 + disable metric)
  * §9.5 Scenario F 流程圖：HA Kill Pod → PDB 保護 → `max by(tenant)` 防翻倍證明

### 🔧 版號治理工具

* **`scripts/tools/bump_docs.py`**: 三條版號線批次更新工具 (`--platform` / `--exporter` / `--tools`)，含 `--check` 模式供 CI lint。
* **Makefile**: 新增 `make version-check`、`make version-show`、`make bump-docs` targets。

### 📖 文件更新

* **README.md / README.en.md**: 文件導覽表新增 BYOP 整合指南、da-tools CLI 入口。
* **CLAUDE.md**: 文件架構表 + 工具清單同步更新。
* **`docs/architecture-and-design.en.md`**: 補齊 §9.2–9.5 (矩陣 + 三張 Mermaid 流程圖)，與中文版完整對齊。

### 🔧 Self-Review 修正

* **`release-tools.yaml`**: CI TOOLS array 補齊 `lint_custom_rules.py`，與 `build.sh` 和 `entrypoint.py` 對齊
* **`entrypoint.py`**: `open()` 補上 `encoding='utf-8'`
* **`da-tools/README.md`**: 版本 header 修正 0.2.0 → 0.3.0 (與 VERSION 檔對齊)
* **`bump_docs.py`**: 新增 da-tools README version header 更新 rule，防止未來版號 drift

### 🧪 測試套件

* **`tests/test_entrypoint.py`** (15 tests): CLI dispatcher 完整測試
  * `TestCommandMapConsistency` (3): COMMAND_MAP 覆蓋所有 build.sh 工具、值格式、PROMETHEUS_COMMANDS 子集
  * `TestInjectPrometheusEnv` (4): 環境變數注入 / 已有 flag 不重複 / 未設定不注入 / 回傳同 list
  * `TestVersionDisplay` (2): VERSION 檔存在 + semver 格式
  * `TestRunToolErrors` (1): 缺失腳本 exit(1)
  * `TestPrintUsage` (1): usage exit(0)
  * `TestCIWorkflowSync` (1): release-tools.yaml ⊇ build.sh 工具一致性
  * `TestBumpDocsToolsRuleCoverage` (1): bump_docs 涵蓋 README header rule
  * `TestMainRouting` (2): unknown command exit(1) + help exit(0)

---

## [v0.8.0] - Testing Coverage, SRE Runbook & Baseline Discovery (2026-02-27)

本版本為 Phase 7 測試覆蓋強化 + B6/B7 交付

### 🧪 Testing Coverage
* **`run_load.sh --type composite`**: 複合負載 — connections + cpu 同時啟動，驗證 `MariaDBSystemBottleneck` 複合警報。
* **`tests/scenario-e.sh`**: Multi-Tenant 隔離測試 — 修改 tenant A 不影響 tenant B。支援 `--with-load`。
* **`tests/scenario-f.sh`**: HA 故障切換測試 — Kill Pod → alert 持續 → 恢復 → 閾值不翻倍 (max by)。

### 📋 SRE Runbook & Discovery Tooling
* **`docs/shadow-monitoring-sop.md`**: Shadow Monitoring SRE SOP — 啟動/巡檢/異常處理/收斂判定/退出完整 runbook。
* **`scripts/tools/baseline_discovery.py`**: Baseline Discovery — 觀測 p50~p99 統計，建議 warning (p95×1.2) / critical (p99×1.5) 閾值。

### 🎭 Demo 強化
* **`make demo`**: Step 5d 新增 `baseline_discovery.py` 快速觀測（15s 取樣 + 閾值建議），展示完整工具鏈。
* **`make demo-full`**: Step 6 改用 `--type composite` 一次啟動 connections + stress-ng（取代原本分開注入），步驟從 6a–6j 精簡為 6a–6i。

### 📖 文件與版本
* **Migration Guide**: 開頭加入「遷移安全保證」陳述；Phase C 的「99.9%」修正為準確工程描述。
* **README.md / README.en.md**: 文件導覽表新增 Shadow Monitoring SOP；工具表新增 `baseline_discovery.py`；Makefile 目標與專案結構補齊 Scenario E/F、composite、baseline。
* **全域版本一致性**: Helm Chart 0.8.0、CI image tag v0.8.0、所有文件統一 v0.8.0。
* **清理**: 刪除根目錄殘留的 `test-legacy-rules.yaml`（測試輸入已收斂至 `tests/legacy-dummy.yml`）。

### 🧪 測試套件

* **`tests/test_baseline_discovery.py`** (28 tests): baseline_discovery.py 純邏輯測試
  * `TestExtractScalar` (8): valid/empty/None/NaN/Inf/non-numeric/missing-key/zero
  * `TestPercentile` (7): p50 odd/even, p0, p100, single, empty, p95 interpolation
  * `TestComputeStats` (5): normal/None-filter/all-None/empty/single
  * `TestSuggestThreshold` (5): sufficient/insufficient/connections-ceil/zero-p95/note
  * `TestDefaultMetrics` (3): required keys/tenant placeholder/known keys

---

## [v0.7.0] - Live Observability & Load Injection (Phase 6) (2026-02-27)

本版本為 Phase 6 真實負載注入與動態展演，讓系統價值「肉眼可見」，徹底解決「改設定觸發警報像作弊」的痛點。

### 🔥 Load Injection Toolkit
* **`scripts/run_load.sh`**: 統一負載注入入口腳本，支援三個展演劇本：
  * **Connection Storm** (`--type connections`): 使用 PyMySQL 持有 95 個 idle 連線，觸發 `MariaDBHighConnections`（保留 exporter 連線槽位，確保 Prometheus 能持續回報指標）。
  * **CPU & Slow Query Burn** (`--type cpu`): 使用 `sysbench oltp_read_write` 執行高密度 OLTP 查詢（16 threads, 300s），觸發 `MariaDBHighSlowQueries` 與 `MariaDBSystemBottleneck` 複合警報。
  * **Container Weakest Link** (`--type stress-ng`): Alpine CPU burn Pod（CPU limit: 100m），故意造成 CPU throttling，驗證 `PodContainerHighCPU` 弱環節偵測精準度（實測 97.3%）。
* **`--dry-run` 模式**: 預覽 K8s manifest 而不實際 apply，方便審查與教學。
* **`--cleanup` 模式**: 一鍵清除所有負載注入資源，trap 確保異常退出也能清理。

### 🏗️ Testing 模組化重構
* **`scripts/_lib.sh` 擴充**: 新增 `setup_port_forwards`, `cleanup_port_forwards`, `prom_query_value`, `get_alert_status`, `wait_for_alert`, `get_exporter_metric`, `wait_exporter`, `require_services` 共 8 個共用函式，取代 4 個 scenario + demo.sh 中重複的 inline Python + port-forward 管理程式碼。
* **Scenario A/B/C/D 重構**: 移除各腳本中重複的 alert polling、port-forward 建立、exporter metric 查詢邏輯，統一透過 `_lib.sh` 提供。
* **清除 7 個 debug 暫存腳本**: 刪除 `_check_alerts.sh`, `_check_alerts2.sh`, `_check_load.sh`, `_final_check.sh`, `_retest_load.sh`, `_test_conn.sh`, `_test_conn95.sh` — 已被正式工具取代。
* **淨減 ~580 行**: 正式腳本總行數從 ~2,200 降至 ~1,625 行（含 _lib.sh 從 94 行擴充至 260 行）。

### 🎭 Demo & Testing 整合
* **`make demo-full`**: 完整 demo 含 Live Load Injection — stress-ng + connection storm → 等待 alerts FIRING → 清除 → alerts 自動消失，展示「負載→觸發→清除→恢復」完整循環。
* **`make demo`**: 保持原始快速模式（`--skip-load`），僅展示工具鏈。
* **`make load-demo`**: 單獨啟動 stress-ng + connections 壓測，手動觀察 alerts。
* **Scenario A (`--with-load`)**: 保持原始閾值(70)，真實 95 connections > 70 → alert fires → 清除 → resolves。不再需要人為壓低閾值。
* **Scenario B (`--with-load`)**: 保持原始閾值(70)，stress-ng 97.3% > 70% → alert fires → 清除 → resolves。
* 所有 load 路徑加入 `trap cleanup EXIT`，確保 Ctrl+C / 錯誤退出時自動清除 load-generator 資源。

### 📋 SRE Runbook & Discovery Tooling
* **`docs/shadow-monitoring-sop.md`**: Shadow Monitoring SRE SOP — 完整 runbook 涵蓋：啟動（本地 / K8s Job）、日常巡檢流程與頻率、異常處理 Playbook（mismatch / missing / 工具故障）、收斂判定標準（7 天 0 mismatch + 覆蓋業務高低峰）、退出與回退步驟。
* **`scripts/tools/baseline_discovery.py`**: Baseline Discovery 工具 — 在負載注入環境下持續觀測指標（connections / cpu / slow_queries / memory / disk_io），計算 p50/p90/p95/p99/max 統計摘要，自動建議 warning (p95×1.2) / critical (p99×1.5) 閾值。產出時間序列 CSV + 統計摘要 CSV + patch_config.py 建議指令。
* **`make baseline-discovery TENANT=db-a`**: Makefile target 快捷入口。

### 🧪 Testing Coverage Expansion (Phase 7)
* **`run_load.sh --type composite`**: 複合負載 — 同時啟動 connections + cpu 負載，用於驗證 `MariaDBSystemBottleneck` 複合警報在真實負載下觸發。
* **`tests/scenario-e.sh`**: Scenario E — Multi-Tenant 隔離測試。修改 tenant A 的閾值/disable metric，驗證 tenant B 完全不受影響。支援 `--with-load` 真實負載模式。
* **`tests/scenario-f.sh`**: Scenario F — HA 故障切換測試。殺掉一個 threshold-exporter Pod → 驗證 alert 持續 → Pod 恢復 → 驗證閾值不翻倍（max by vs sum by）。
* **Migration Guide**: 開頭加入「遷移安全保證」定心丸陳述；Phase C 的「99.9% 一致」修正為準確的工程描述。
* **全域版本一致性**: 統一 6 個文件的 v0.5.0 → v0.7.0 標示。

### 📖 文件更新
* **README.md / README.en.md**: Quick Start 加入 `make demo-full`（動態負載展演）與 `make test-alert`（硬體故障測試）的語義區分。新增「企業級價值主張」表格（Risk-Free Migration, Zero-Crash Opt-Out, Full Lifecycle, Live Verifiability）融入痛點與解決方案區塊。
* **rule-packs/README.md**: 補充「動態卸載 (optional: true)」文件 — 說明 Projected Volume 的 `optional: true` 機制，含卸載/恢復操作範例。
* **Makefile**: `test-alert` 重新定義為「硬體故障/服務中斷測試 (Hard Outage Test)」；`demo-full` 定義為「動態負載展演 (Live Load Demo)」。

### 🎯 Makefile Targets
* `make load-connections TENANT=db-a` — 連線數風暴
* `make load-cpu TENANT=db-a` — CPU 與慢查詢
* `make load-stress TENANT=db-a` — 容器 CPU 極限
* `make load-composite TENANT=db-a` — 複合負載 (connections + cpu)
* `make load-cleanup` — 清除所有壓測資源
* `make load-demo TENANT=db-a` — 壓測 Demo（啟動 → 觀察 → 手動 cleanup）
* `make demo-full` — 完整端對端 Demo（含 Live Load）
* `make test-scenario-a ARGS=--with-load` — Scenario A 真實負載模式
* `make test-scenario-b ARGS=--with-load` — Scenario B 真實負載模式
* `make test-scenario-e ARGS=--with-load` — Scenario E 多租戶隔離（可選真實負載）
* `make test-scenario-f TENANT=db-a` — Scenario F HA 故障切換

### 🧪 測試套件

* **`tests/test_lib_helpers.py`** (34 tests): _lib.sh Python snippet 邏輯測試
  * `TestUrlEncode` (6): simple/spaces/braces/single-quote/empty/complex-PromQL
  * `TestPromQueryValueParsing` (6): normal/empty/malformed/missing-key/custom-default/float
  * `TestGetAlertStatusParsing` (6): firing/pending/inactive/precedence/empty/malformed
  * `TestGetCmValueParsing` (4): per-tenant-yaml/config-fallback/missing-key/empty
  * `TestGetExporterMetricRegex` (5): integer/float/none/zero/large
  * `TestLibShStructure` (4): file-exists/shebang/functions-present/stdin-pattern
  * `TestScenarioScriptsSourceLib` (3): source-lib/set-pipefail/trap-cleanup

---

## [v0.6.0] - Enterprise Governance (Phase 5) (2026-02-27)

本版本為 Phase 5 企業級治理，針對大型客戶（1500+ 條規則）的遷移場景提供完整的工具鏈與安全機制。

### 🏗️ Architecture: Rule Pack 動態開關
* **Projected Volume `optional: true`**: 所有 6 個 Rule Pack ConfigMap 加上 `optional: true`，允許客戶透過 `kubectl delete cm prometheus-rules-<type>` 卸載不需要的黃金標準 Rule Pack，Prometheus 不會 Crash。大型客戶可關閉黃金標準，改用自訂規則包。

### 🔧 Tooling: migrate_rule.py v3 (企業級遷移)
* **Triage Mode (`--triage`)**: 大規模遷移前的分析報告，輸出 CSV 檔案可在 Excel 中批次決策。自動將規則分為 auto / review / skip / use_golden 四桶。
* **Prefix 隔離 (預設 `custom_`)**: 遷移產出的 Recording Rule 自動加上 `custom_` 前綴，在命名空間層面與黃金標準徹底隔離，避免 `multiple matches for labels` 錯誤。
* **Prefix Mapping Table**: 自動產出 `prefix-mapping.yaml`，記錄 custom_ 前綴與黃金標準的對應關係，方便未來收斂。
* **Metric Heuristic Dictionary**: 外部 `metric-dictionary.yaml` 啟發式比對，自動建議使用者改用黃金標準。平台團隊可直接維護字典，不需改 Python code。
* **收斂率統計**: 報告中顯示壓縮率，讓客戶看到規則收斂的成效。
* **Shadow Labels**: 遷移產出的 Alert Rule 自動帶上 `source: legacy` 與 `migration_status: shadow` label，支援 Alertmanager 雙軌並行。

### 🔍 Tooling: Shadow Monitoring 驗證
* **`validate_migration.py`**: 透過 Prometheus API 比對新舊 Recording Rule 的數值輸出（而非 Alert 狀態），精準度 100%。支援批次比對（讀取 prefix-mapping.yaml）、持續監控模式（`--watch`）、CSV 報告輸出。

### 🗑️ Tooling: 下架工具
* **`offboard_tenant.py`**: 安全 Tenant 下架工具，含 Pre-check（檔案存在、跨引用掃描）+ 執行模式。
* **`deprecate_rule.py`**: 規則/指標三步下架工具 — (1) _defaults.yaml 設 disable (2) 掃描清除 tenant 殘留 (3) 產出 ConfigMap 清理指引。支援批次處理多個 metric。

---

## [v0.5.0] - Enterprise High Availability (Phase 4) (2026-02-26)

本版本為 Phase 4 企業級高可用性 (HA) 架構的重大升級。系統現在具備了容錯轉移能力、避免閾值重複計算的底層防護，以及專屬的平台自我監控網。

### 🚀 Architecture & High Availability
* **預設 2 Replicas**: `threshold-exporter` 的預設副本數提升至 2，消除單點故障 (SPOF) 風險。
* **Pod Anti-Affinity**: 引入軟性反親和性調度 (`preferredDuringSchedulingIgnoredDuringExecution`)，確保 Pod 盡可能分散於不同節點，同時相容本地 Kind 單節點叢集。
* **Pod Disruption Budget (PDB)**: 新增 PDB 確保在 K8s Node 維護期間，至少有 1 個 Exporter Pod (`minAvailable: 1`) 存活提供服務。
* **Platform Self-Monitoring (平台自我監控)**: 新增專門監控 Exporter 自身健康的第 6 個 Rule Pack (`configmap-rules-platform.yaml`)，並已透過 Projected Volume 預載入 Prometheus。包含 `ThresholdExporterDown`、`ThresholdExporterAbsent`、`ThresholdExporterTooFewReplicas` 與 `ThresholdExporterHighRestarts` 等防護警報。

### 🛠️ Fixes & Documentation
* **修復 Double Counting 數學陷阱**: 將所有 Rule Packs 內的 Threshold Normalization Recording Rules 聚合函數由 `sum by(tenant)` 全面修正為 **`max by(tenant)`**。徹底解決了當 Replica > 1 時，Prometheus 抓取多個 Pod 導致閾值翻倍的致命問題。
* **文件對齊**: 更新 `README.md`、`migration-guide.md` 與 `rule-packs/README.md`，明確標示 HA 架構與 6 個預載 Rule Packs，並同步更新測試斷言以符合最新輸出格式。

---

## [v0.4.0] - Ease of Adoption & Zero-Friction (Phase 3) (2026-02-25)

本版本為 Phase 3 的集大成之作！系統全面轉向「開箱即用」與「零阻力導入」，並大幅重構了底層 ConfigMap 掛載架構與安全性。

### 🚀 Features & Enhancements
* **Rule Packs 解耦與預載 (Projected Volumes)**: 
  * 將龐大的單一 Prometheus ConfigMap 拆解為 5 個獨立的 `configmap-rules-*.yaml` (MariaDB, Kubernetes, Redis, MongoDB, Elasticsearch)，不同維運團隊可獨立維護自己的領域。
  * 透過 Kubernetes Projected Volume 將所有 ConfigMap 無縫投射至 Prometheus 中。
  * **100% 預載入**: 平台預設載入所有 5 大權威 Rule Packs。受惠於 Prometheus 的空集合 (Empty Vector) 運算特性，未部署的 DB 不耗費效能。租戶只需寫入閾值即刻生效，不需再做 Helm 掛載設定。
* **Scaffold 工具 (`scaffold_tenant.py`)**: 互動式租戶設定精靈，一鍵產生新租戶的 ConfigMap 架構 (`_defaults.yaml` 與 `<tenant>.yaml`)。
* **遷移工具 UX 終極進化 (`migrate_rule.py` v2)**:
  * **智能聚合猜測 (Heuristics)**: 自動根據 PromQL 語法 (如 `rate`, `percent`) 猜測聚合方式 (`sum` vs `max`)。
  * **視覺化防呆 (ASCII Warnings)**: 當套用 AI 猜測時，自動在生成的 YAML 中插入醒目的 ASCII 警告區塊，強制人工 Double Check。
  * **檔案化輸出與 Boilerplate**: 工具輸出至 `migration_output/`，自帶合法 YAML 縮排結構，並自動對重複的 Recording Rule 進行去重 (Deduplication)。

### 🛡️ Proactive Security (SAST Fixes)
* **OS Command Injection**: 全面移除 Python 工具中的 `shell=True`，改用 List 安全傳遞參數。
* **Gosec G112 (Slowloris)**: 於 Go exporter 的 HTTP Server 中補齊 `ReadHeaderTimeout: 3 * time.Second` 防護。
* **CWE-276 (File Permissions)**: Python 自動寫檔與 Go 測試建立假目錄時，嚴格限制權限為 `0600`/`0700`。
* **SSRF False Positive**: 為 `check_alert.py` 增加 `# nosec B310` 排除本機 API 誤判。

---

## [v0.3.0] - Dimensional Metrics Milestone (Phase 2B) (2026-02-25)

系統現在具備了處理 Redis、Elasticsearch、MongoDB 等多維度指標的能力。

### 🚀 Features
* **Label Selector Syntax**: 租戶現在可以透過 PromQL 風格的標籤選擇器來設定特定維度的閾值 (例如 `"redis_queue_length{queue='tasks'}": "500"`)。
* **Unchecked Collector Refactor**: `threshold-exporter` Go 核心升級為動態 Descriptor 模式，能將解析出的自訂維度標籤直接輸出為 Prometheus metric 標籤。
* **Authoritative Templates**: 新增業界標準的設定範本 (`config/conf.d/examples/`)，涵蓋 Redis (Oliver006)、Elasticsearch (Prometheus Community) 與 MongoDB (Percona) 的最佳實踐。
* **Smart Dimension Hints**: `migrate_rule.py` 現在能偵測傳統 PromQL 中的維度標籤，並在終端機輸出對應的 YAML 設定提示。

---

## [v0.2.0] - GitOps Directory Scanner & Migration Tooling (Phase 2A/C/D) (2026-02-24)

大幅提升擴展性，徹底解耦 ConfigMap，為 GitOps 鋪平道路。

### 🚀 Features
* **Directory Mode (`-config-dir`)**: `threshold-exporter` 支援掃描並深度合併 `conf.d/` 目錄下的多個 YAML 檔案 (`_defaults.yaml` + `<tenant>.yaml`)，完美解決單一 ConfigMap 的合併衝突問題。
* **Robust Hot-Reloading**: 捨棄 ModTime，改用 **SHA-256 Hash 比對**，完美解決 Kubernetes ConfigMap volume symlink 輪轉時的熱重載延遲與漏抓問題。
* **Boundary Enforcement**: 實作嚴格邊界規則，禁止租戶檔案覆寫平台級設定 (`state_filters`, `defaults`)。
* **Automated Migration Tooling (`migrate_rule.py` v1)**: 首個版本的傳統 PromQL 警報轉換工具，支援 80/20 法則自動拆解三件套，複雜語義優雅降級為 LLM Prompt。
* **Migration Guide**: 釋出第一版完整的架構遷移指南。

---

## [v0.1.0] - The Composite Priority Milestone (Phase 1) (2026-02-23)

首個正式版本。完成了所有基礎場景的驗證，確立了 Config-driven 與 Hot-reload 的動態警報架構。

### 🚀 Features
* **Dynamic Thresholds (Scenario A)**: 實作 Go `threshold-exporter`，支援三態邏輯 (Custom Value / Default / Disable)。
* **Weakest Link Detection (Scenario B)**: 整合 `kubelet-cadvisor`，實現容器層級資源 (CPU/Memory) 的最大值 (Max) 瓶頸監控。
* **State Matching (Scenario C)**: 透過乘法邏輯 (`count * flag > 0`) 結合 `kube-state-metrics`，實現 Kubernetes 狀態 (如 CrashLoopBackOff) 的動態開關。
* **Composite Priority Logic (Scenario D)**:
  * **Maintenance Mode**: 使用 `unless` 邏輯全域抑制特定租戶的常規警報。
  * **Composite Alerts**: 結合 `and` 邏輯，僅在多重症狀同時發生時觸發警報 (如高連線數 + 高 CPU)。
  * **Multi-tier Severity**: 支援 `_critical` 後綴配置，具備 Critical 觸發時自動降級 Warning 警報的功能。
