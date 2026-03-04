# Changelog

All notable changes to the **Dynamic Alerting Integrations** project will be documented in this file.

## [v1.4.0] - Routing Defaults, 6 Receiver Types & Auto-Reload (2026-03-04)

三態 Routing Defaults + Rocket.Chat / PagerDuty receiver + `--apply` 一站式部署 + ConfigMap Watcher Sidecar 自動 reload。

### 🎯 Routing Defaults (三態延伸)

* **`_routing_defaults` in `_defaults.yaml`**：Domain expert 設定預設 receiver + timing，tenant 三態（繼承/覆寫/disable）。
* **`{{tenant}}` Template Substitution**：receiver 欄位支援 `{{tenant}}` 佔位符，merge 時自動替換為 tenant 名稱（如 `#alerts-{{tenant}}` → `#alerts-db-a`）。
* **Default receiver 允許**：無 `_routing` 的 tenant 自動繼承 `_routing_defaults`，不需每個 tenant 都寫 receiver。

### 📡 Receiver 類型擴充

* **Rocket.Chat**：以 `webhook_configs` 包裝，`channel`/`username`/`icon_url` 作為 metadata 記錄。
* **PagerDuty**：原生 `pagerduty_configs` 映射，`service_key` 為必要欄位。
* **v1.4.0 支援 6 種 receiver**：webhook / email / slack / teams / rocketchat / pagerduty。

### 🚀 generate-routes --apply

* **一站式部署**：`--apply` flag 自動讀取現有 ConfigMap → merge fragment → kubectl apply → curl reload。
* **安全機制**：需明確 `--namespace` + `--configmap` + 互動確認（`--yes` 跳過）。

### 🔄 ConfigMap Watcher Sidecar

* **`configmap-reload` sidecar**：Alertmanager deployment 新增 `ghcr.io/jimmidyson/configmap-reload:v0.14.0` sidecar，偵測 ConfigMap 變更後自動 POST `/-/reload`。
* **Volume mount 調整**：從 `subPath` 改為目錄掛載，支援 ConfigMap live update。

### 🧪 Testing

* **12 個新 Python 測試**：`TestRoutingDefaults`（7 tests，三態合併 + `{{tenant}}` 替換 + boundary warning）+ `TestNewReceiverTypes`（5 tests，rocketchat/pagerduty 驗證）。
* **Go `validReceiverTypes` 更新**：新增 rocketchat + pagerduty。

---

## [v1.3.0] - Alertmanager 動態化 & Receiver 擴充 (2026-03-04)

Alertmanager 動態 Reload + 多類型 Receiver 支援 + Routing CI 驗證，回應 user feedback「最後一哩路」。

### 🔄 Alertmanager 動態 Reload

* **`--web.enable-lifecycle`**：Alertmanager deployment 新增 lifecycle API flag，支援 `curl -X POST /-/reload` 免重啟更新配置。
* **`_lib.sh` 新增 `reload_alertmanager()`**：共用函式庫新增 Alertmanager reload helper。

### 📡 Receiver 類型擴充（Breaking Change）

* **結構化 `receiver` 物件**：`_routing.receiver` 從純 URL 字串改為結構化物件（含 `type` 欄位）。**不向後相容** v1.2.0 格式。
* **四種 Receiver 類型**：`webhook`（既有）、`email`（SMTP）、`slack`（Incoming Webhook）、`teams`（MS Teams v0.27.0+）。
* **訊息模板（Go Template）**：Slack/Teams/Email 的 `title` / `text` / `html` 欄位支援 Alertmanager Go template 語法，可引用 `.CommonLabels`、`.Annotations`、`.Status` 等變數。
* **`build_receiver_config()`**：新增 Python 函式，依 type 驗證必要欄位並產出對應 Alertmanager receiver 結構。
* **Go `RoutingConfig` 更新**：`Receiver string` → `ReceiverType string` + `ReceiverConfig map[string]interface{}`，含 `validReceiverTypes` 驗證。

### ✅ Routing CI Validation

* **`generate_alertmanager_routes.py --validate`**：新增驗證模式，exit code 0/1 供 CI pipeline 使用。
* **`make validate-routes`**：Makefile target，一鍵驗證 conf.d/ 所有 tenant routing config。

### 🛠️ Tooling

* **`scaffold_tenant.py`**：互動模式新增 receiver type 選擇（webhook/email/slack/teams）；非互動模式新增 `--routing-receiver-type` + `--routing-smarthost` 參數。
* **`generate_alertmanager_routes.py`**：`RECEIVER_TYPES` 常數定義四種 type 的必要/選填欄位 + AM config key 映射。

### 📄 Documentation

* **`byo-alertmanager-integration.md`**：從 v1.2.0 藍圖框架升級為完整整合指引（6 步驟 + 動態 reload + 4 種 receiver 範例 + 驗證 checklist + Operator appendix）。

### 🧪 Testing

* **16 個新 Python 測試**（`TestBuildReceiverConfig`）：四種 type 基本/選填欄位、缺少 type、未知 type、缺少必要欄位、舊格式拒絕、case insensitive。
* **Go test 更新**：所有 `TestResolveRouting_*` + `TestScheduledValue_RoutingMapRoundTrip` 遷移至結構化 receiver 格式。

---

## [v1.2.0] - Silent Mode, Severity Dedup & Alert Routing (2026-03-03)

三態運營模式 + Severity Dedup 可選化 + Config-Driven Alert Routing，回應 user feedback「還給 tenant 合理的自理權」。

### 🔇 Silent Mode (核心功能)

* **`_silent_mode` tenant 配置**：新增 `"warning"` / `"critical"` / `"all"` / `"disable"` 四個值。Alert 照常觸發（TSDB 有 ALERTS 紀錄），但 Alertmanager 攔截通知。
* **`user_silent_mode` 新指標**：`user_silent_mode{tenant, target_severity}` flag gauge，由 threshold-exporter 輸出。
* **`rule-pack-operational.yaml` 新 Rule Pack**：`TenantSilentWarning` + `TenantSilentCritical` + `TenantSeverityDedupEnabled` sentinel alerts。
* **Alertmanager inhibit_rules**：Silent Mode inhibit（sentinel-based）+ Severity Dedup inhibit（per-tenant generated）。

### 🔀 Severity Dedup (F2a — Auto-Suppression 可選化)

* **問題**：v1.1.0 的 PromQL `unless critical` 在 critical 觸發時消滅 warning 的 TSDB 紀錄，TSDB 完全無紀錄。
* **解法**：Dedup 從 PromQL 層移到 Alertmanager 層。TSDB 永遠完整，dedup 只控制通知行為。
* **Per-Tenant 控制**：`generate_alertmanager_routes.py` 掃描所有 tenant 的 `_severity_dedup` 設定，為每個 enabled tenant 產出專屬 inhibit rule（帶 `tenant="<name>"` + `metric_group=~".+"` matcher）。`_severity_dedup: "disable"` 的 tenant 不產出 rule → 兩種通知都收到。
* **`_severity_dedup` tenant 配置**：`"enable"`（預設）/ `"disable"`。
* **`user_severity_dedup` 新指標**：`user_severity_dedup{tenant, mode}` flag gauge，Sentinel `TenantSeverityDedupEnabled` 供 Grafana 面板顯示。
* **`metric_group` label**：Alert rule 新增 `metric_group` label，讓 per-tenant inhibit rules 正確配對 warning/critical（因 alertname 不同）。
* **Rule Pack 更新**：MariaDB rule pack 移除 PromQL `unless critical` 子句，改為 `metric_group` label 配對。

### 🔔 Alert Routing (F3 — Config-Driven Routing)

* **`_routing` tenant 配置**：receiver (webhook URL) + group_by + group_wait + group_interval + repeat_interval。
* **Timing Guardrails**：group_wait 5s–5m、group_interval 5s–5m、repeat_interval 1m–72h，超限自動 clamp。
* **`generate_alertmanager_routes.py` 新工具**：讀取 conf.d/ 所有 tenant YAML，產出 Alertmanager route + receiver + per-tenant severity dedup inhibit_rules YAML fragment。
* **Silent Mode bypass**：inhibit_rules 在 route evaluation 之前攔截，silent tenant 的 routing 自動 bypass。

### 🛠️ Tooling

* **`scaffold_tenant.py`**：互動模式新增靜音模式、severity dedup、alert routing 選項；非互動模式新增 `--silent-mode`、`--severity-dedup`、`--routing-receiver` 等參數。
* **`migrate_rule.py`**：Auto-Suppression 改為 Alertmanager-based dedup（不再注入 PromQL `unless`，改加 `metric_group` label）。
* **`diagnose.py`**：健康檢查新增 `operational_mode` 欄位（normal / silent:warning / silent:all / maintenance）。
* **da-tools v1.2.0**：
  * 新增 `generate-routes` 命令（`generate_alertmanager_routes.py` 收錄至容器 image）
  * 移除 `bump-docs` 命令（開發者內部工具，對使用者無意義）

### 📄 Documentation

* **`architecture-and-design.md`**：
  * 新增 §2.3 Tenant-Namespace 映射模式（1:1 / N:1 / 1:N + relabel_configs 範例）(F1)
  * 新增 §2.8 Severity Dedup（行為矩陣、metric_group 配對機制）(F2a)
  * 新增 §2.9 Alert Routing 客製化（Schema、Guardrails、工具鏈）(F3)
  * §2.7 三態運營模式（行為矩陣、資料流、配置範例、Alertmanager 範本）
  * §3.1 Rule Pack 表格更新（9→10 packs，新增 Operational）
* **`byo-prometheus-integration.md`**：新增彈性 Tenant-Namespace 映射 section (F1)
* **`byo-alertmanager-integration.md`**：新增 Alertmanager 整合藍圖（框架），含動態 reload 方向、receiver 擴充規劃、generate-routes 工具使用概要
* **`migration-guide.md`**：§5 新增 Config-Driven Routing 說明 (F3)

---

## [v1.1.0] - OCI Chart Publishing, da-tools Priority & Doc Polish (2026-03-01)

Helm chart OCI 發佈、da-tools 容器優先敘述、Config 分離、Auto-Suppression、全 repo 文件一致性修正。

### 📦 Helm Chart OCI Publishing

* **OCI Registry 發佈**: Helm chart 推送至 `oci://ghcr.io/vencil/charts/threshold-exporter`，用戶不需 clone repo 即可 `helm install`。
* **`.helmignore`**: 排除 Go source (`app/`)、dev config (`config/`)、README.md，確保 chart .tgz 乾淨。
* **`.github/workflows/release.yaml`**: 統一 CI pipeline，`v*` tag 觸發 exporter image + chart push，`tools/v*` tag 觸發 da-tools image push。含 Chart.yaml version verification gate。
* **Makefile**: 新增 `chart-package` / `chart-push` targets + `OCI_REGISTRY` 變數。
* **`bump_docs.py` 重構**: Chart.yaml `version` 歸入 exporter 版號線（chart version = appVersion = exporter version）；新增 OCI `--version` 追蹤規則（migration-guide + exporter README）；platform 版號來源改為 CLAUDE.md。

### 🔄 da-tools 優先敘述

* **`docs/migration-guide.md`**: helm 指令全面改為 OCI registry 路徑；da-tools 容器為主要敘述，python3 降為 blockquote fallback。
* **`docs/shadow-monitoring-sop.md`**: 6 處 python3 命令改為 da-tools（validate、check-alert、deprecate），保留 diagnose.py（需 kubectl）。prose 引用同步更新。
* **`components/threshold-exporter/README.md`**: §部署 + §方式A 改為 OCI install，local chart 降為 blockquote。

### 🏗️ Config 分離

* **`values.yaml`**: 清空為 `tenants: {}`，移除內嵌的 tenant 設定範例。
* **`environments/local/`**: tenant-specific 設定移至 environment overlay，對齊生產 helm upgrade -f 流程。

### 🔧 migrate_rule.py Enhancements

* **Auto-Suppression (warning ↔ critical 配對)**：自動為 warning alert 注入第二層 `unless on(tenant)` 子句，critical 觸發時抑制 warning。
* **Threshold naming fix**：critical threshold recording rule 加上 `_critical` 後綴，對齊 Rule Pack 慣例。
* **`MigrationResult.op` 欄位**：新增比較運算子儲存，供 Auto-Suppression 引用。

### 📄 Documentation Polish

* **`architecture-and-design.md` (中/英)**：§4.7 K8s 使用注意事項（Pod scheduling 與 ConfigMap 生命週期）、§8.3 threshold vs data normalization 語義釐清、§10.4 tolerance rationale 補充、§2.3 Auto-Suppression `unless` 範例修正。
* **全 repo `:sum` → `:max` 掃描**：修正 12 處殘留引用，零殘留。
* **`README.md`**：Rule Pack 計數表修正（85R + 56A = 141）。
* **Mermaid `\n` → `<br/>`**：6 個文件共 135 處替換，修正 GitHub 渲染。
* **`CLAUDE.md`**：精簡重寫 + Release 流程 + chart-package/chart-push 語義。
* **`.gitignore`**：新增 `.build/`（Helm package 輸出目錄）。

### 🧪 Testing

* **13 個新測試案例** (`test_migrate_ast.py::TestAutoSuppression`)：基本配對、critical 不被修改、單一嚴重度不配對、不同 metric 不配對、多組配對、運算子保留、備註新增、unparseable/golden 跳過、write_outputs 端到端、dry-run 路徑、無前綴配對。

---

## [v1.0.0] - GA Release (2026-03-01)

首個正式穩定版本。文件大重構、版號統一。

### 📄 Documentation Overhaul

* **`architecture-and-design.md` (中/英) 大重構**：
  * §10 Future Roadmap 中已完成功能歸位至核心章節：§2.4 Regex 維度閾值、§2.5 排程式閾值、§4.7 Under-Load 基準測試
  * 新增 §10 AST 遷移引擎架構（獨立章節）
  * §11 Future Roadmap 精簡為 3 項未實現方向（治理演進、Prometheus 聯邦、生態系擴展）
  * 英文版同步補齊 v0.12.0→v0.13.0 差異（Mermaid 圖、Rule Pack 表格 6→9）
* **`README.md` / `README.en.md` 重寫**：
  * 痛點對比表：新增「舊規則遷移風險」(AST 引擎) + 「維度精細控制」(Regex) 兩大段落
  * 企業價值主張表：新增 Multi-DB 生態系行、更新遷移安全數據
  * 全文 Rule Pack 數量 6→9，含 Oracle、DB2、ClickHouse
* **其餘文件更新**：
  * `migration-guide.md`：§9 擴展 DB 類型說明、Rule Pack 數量 6→9
  * `byo-prometheus-integration.md`：Rule Pack 表格新增 3 個 DB 類型
  * `custom-rule-governance.md` / `.en.md`：版號→v1.0.0
  * `components/threshold-exporter/README.md`：版號→v1.0.0

### 🏷️ Version Governance

* 全 repo 版號統一至 v1.0.0
* `CLAUDE.md`：Phase 13 歸位至版本歷程

---

## [v0.13.0] - Enterprise DB Rule Packs & Benchmark (2026-02-28)

Oracle + DB2 + ClickHouse Rule Pack 擴展，benchmark `--under-load` 模式，Go micro-benchmark。

### 🗃️ Enterprise DB Rule Packs (B3)

* **Oracle Rule Pack** (`rule-packs/rule-pack-oracle.yaml` + `configmap-rules-oracle.yaml`)
  * 6 normalization recording rules + 5 threshold normalization + 7 alert rules
  * 涵蓋: sessions_active, tablespace_used_percent, wait_time rate, process_count, PGA, session_utilization
  * Regex 維度範例: `oracle_tablespace_used_percent{tablespace_name=~"USERS|DATA.*"}`
* **DB2 Rule Pack** (`rule-packs/rule-pack-db2.yaml` + `configmap-rules-db2.yaml`)
  * 7 normalization recording rules + 5 threshold normalization + 7 alert rules
  * 涵蓋: connections_active, bufferpool_hit_ratio (< 反轉), log_usage, deadlocks, tablespace, lock_wait, sort_overflow
  * Regex 維度範例: `db2_bufferpool_hit_ratio{bufferpool_name=~"IBMDEFAULT.*"}`
* **ClickHouse Rule Pack** (`rule-packs/rule-pack-clickhouse.yaml` + `configmap-rules-clickhouse.yaml`)
  * 7 normalization recording rules + 5 threshold normalization + 7 alert rules
  * 涵蓋: queries rate, TCP connections, max_part_count (merge 壓力), replication queue, memory tracking, merge rate, failed queries
  * Regex 維度範例: `clickhouse_max_part_count{database=~"prod_.*"}`
* **Rule Packs 總數**: 6 → 9 (Projected Volume `optional: true`)

### 📊 Benchmark 強化 (B2)

* **`benchmark.sh --under-load [--tenants N]`**: 合成 N 個 synthetic tenants → patch ConfigMap → 量測 reload latency / memory delta / scrape duration / eval time
* **Scrape Duration**: idle-state 基準也納入 `scrape_duration_seconds{job="threshold-exporter"}`
* **JSON 輸出**: `--json` 模式包含 `under_load` 區段，供 CI pipeline 自動化比對
* **Go micro-benchmark** (`config_bench_test.go`): 7 個 `testing.B` 函數，覆蓋 10/100/1000 tenants × scalar/mixed/night-window

### 🔧 Tooling

* **`scaffold_tenant.py`**: 新增 `oracle` + `db2` + `clickhouse` 至 `RULE_PACKS` catalogue (含 dimensional_example)
* **`metric-dictionary.yaml`**: 新增 Oracle 5 + DB2 5 + ClickHouse 5 = 15 個指標對照條目

### 🧪 Testing

* **51 個新 Python 測試** (`test_scaffold_db.py`): RULE_PACKS catalogue / non-interactive generation / metric dictionary / rule pack YAML 結構驗證 (Oracle + DB2 + ClickHouse)
* **19 個新 Shell 測試** (test-scaffold.sh): Oracle/DB2/ClickHouse/composite scaffold + catalog

### 📄 Documentation

* `rule-packs/README.md`: 6 → 9 packs，新增 Oracle + DB2 + ClickHouse rows + exporter links
* `deployment-prometheus.yaml`: Projected Volume 新增 3 個 ConfigMap (oracle, db2, clickhouse)
* `architecture-and-design.md`: §3.1 表格 + Mermaid 圖更新至 9 個 Rule Pack，版本 → v0.13.0
* `CLAUDE.md`: Phase 12 → version history, 9 Rule Packs
* `CHANGELOG.md`: v0.13.0 entry

---

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

### 🧪 Testing: v0.6.0 Self-Review (R1)
* **`test_migrate_v3.py`** (38 tests): migrate_rule v3 核心邏輯 — guess_aggregation 6-rule heuristic、lookup_dictionary、parse_expr regex path、MigrationResult 資料結構、process_rule (perfect/complex/unparseable/golden/shadow)、write_triage_csv、write_prefix_mapping、收斂率計算修正驗證、load_metric_dictionary
* **`test_offboard_deprecate.py`** (34 tests): offboard/deprecate/validate 三工具純邏輯 — find_config_file、check_cross_references、get_tenant_metrics、run_precheck、scan_for_metric (含 dimensional key)、disable_in_defaults (preview/execute)、remove_from_tenants、extract_value_map、compare_vectors (8 status 分支)

### 🐛 Fixes
* **收斂率計算 Bug (A1)**: `write_outputs()` 中 `golden_matches` 包含 unparseable 規則，導致 `convertible` 被過度扣減。修正為 `golden_parseable` 排除 `status == "unparseable"` 的規則。

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
