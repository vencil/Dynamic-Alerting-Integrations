---
title: "測試覆蓋矩陣"
tags: [testing, maintenance, internal]
audience: [platform-engineer, sre]
version: v2.9.0
lang: zh
---
# 測試覆蓋矩陣

> **Language / 語言：** | **中文（當前）**

> **內部 QA 紀錄**：本文件為維護者用的測試清單、數量、CI 指令與 benchmark 對照。**對外的場景行為說明、設計證明與生命週期時序圖見公開 [驗證場景與平台行為](../scenarios/verified-scenarios.md)**。
>
> 相關文件：[Testing Playbook](testing-playbook.md) · [Test Map](test-map.md) · [Benchmark Playbook](benchmark-playbook.md)

---

## 企業級測試覆蓋矩陣 (Enterprise Test Coverage Matrix)

場景 A–F 的對外行為說明、每項保證由哪個測試或 lint 守、以及缺口，見公開 [驗證場景](../scenarios/verified-scenarios.md)（SSOT，本檔不複製）。

### 端到端生命週期（live 叢集，無自動化覆蓋）

live 叢集上「真實負載 → alert firing → cleanup → resolved」**沒有自動化測試、也沒有斷言**；缺口已列入公開 [驗證場景](../scenarios/verified-scenarios.md) 的缺口清單（SSOT）。手動觀察入口：`make load-composite TENANT=<tenant>` / `make load-cleanup`。原 `make demo-full`（`scripts/demo.sh`）已於 [#1989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1989) 刪除——它沒有斷言、跳過的步驟仍 rc 0，且自腳本搬遷後 Step 1 即 rc 2。

### Unit/Integration Tests（`make test` / `pytest`）

v1.7.0–v2.0.0 新增大量企業功能，其測試覆蓋集中在 unit/integration 層：

| 功能域 | 企業防護需求 | 覆蓋範圍 |
|--------|-------------|----------|
| **Silent Mode** | 靜音通知但保留 TSDB 紀錄 | sentinel metric emit、inhibit rule 產出、三態交互 |
| **Severity Dedup** | warning/critical 去重 | per-tenant inhibit rules、metric_group 配對、sentinel metric |
| **Config-driven Routing** | 6 種 receiver + guardrails | receiver 結構驗證、timing clamp、domain allowlist |
| **Per-rule Overrides** | 特定 alert 走不同 receiver | expand_routing_overrides、子路由產出、驗證互斥 |
| **Platform Enforced Routing** | NOC 必收 + tenant 也收 | `_routing_enforced` 合併、`continue: true` 插入 |
| **Expires Auto-expiry** | 防止 silent/maintenance 忘記關 | `time.Now().After(expires)` 邏輯、`da_config_event` emit |
| **Cardinality Guard** | 防止 tenant 配置爆炸 | `max_metrics_per_tenant` truncate、ERROR log |
| **Schema Validation** | 偵測 typo/unknown key | Go + Python 雙端一致、warning 回報 |
| **Onboard/Migration** | 企業無縫遷移 | AST engine、triage CSV、shadow mapping、prefix injection |
| **N:1 Namespace Mapping** | 多 NS → 單 tenant | relabel snippet 產出、`_namespaces` 元資料 |
| **Shadow Monitoring Cutover** | 一鍵自動化切換 | readiness 消費、5 步驟執行、dry-run、timeout 處理 |
| **Blind Spot Discovery** | 叢集盲區偵測 | targets 解析、segment 匹配、wrapped YAML 格式 |
| **Config Diff** | 配置差異 blast radius | wrapped/flat 格式載入、變更分類、Markdown 產出 |
| **AM GitOps ConfigMap** | 完整 ConfigMap 產出 | base-config 載入、互斥驗證、YAML 結構 |
| **Recurring Maintenance** | 排程式維護窗口自動化 | parse_duration（含 `d`）、is_in_window cron 判定、silence CRUD + extend、Pushgateway 指標推送 |
| **Alert Quality Scoring** | 告警品質四維評估 | noise/stale/latency/suppression 四指標計算、三級評分、tenant 報告、Markdown 產出 |
| **Policy-as-Code** | 配置策略引擎 | 10 運算子驗證、when 條件篩選、tenant 排除、severity 分級、違規報告 |
| **Cardinality Forecasting** | 基數趨勢預測 | 線性回歸、風險分級、觸頂天數計算、Markdown/JSON 報告 |
| **SAST Compliance** | 靜態安全分析合規 | Go G112、Python CWE-276、B602、encoding 規範、全倉庫掃描 |
| **Migration Engine v3** | AST 遷移引擎 | PromQL 解析、prefix injection、triage 分類、shadow mapping |
| **Offboard & Deprecate** | 租戶下架與規則下架 | 清理流程、審計日誌、deprecation 標記 |

> 完整測試套件：`make test`（Go）+ `pytest tests/`（Python）。CI pipeline `.github/workflows/validate.yaml` 在每次 PR 自動執行。完整測試架構導覽見 [Test Map](test-map.md)。

### Tier 2 — Performance Benchmarks（`go test -bench`）

Performance benchmarks 與 unit tests 分離記錄。Tier 2 量測 production hot-path 在不同 tenant 規模下的延遲、記憶體與 goroutine 行為，為 SLO 與 sharding 決策提供 empirical 依據（**非 unit-level 正確性驗證**）。完整方法論與基線數據見 [Benchmark Playbook](benchmark-playbook.md)。

#### Phase .b 1000+ tenant hierarchical baseline (B-1 Phase 1 + B-8, v2.8.0)

新增於 PR #59，檔案 `components/threshold-exporter/app/config_hierarchy_bench_test.go`。覆蓋 post-A-10 production hot path：`WatchLoop → diffAndReload → scanDirTree`（#1568 起兩平面共用一個 walker；`ScanDirTree_*` 系列改名自 `ScanDirHierarchical_*`，冷掃含 tenant 宣告 parse，數字與舊系列不可直接比較）。

| Benchmark | Tier | 量測對象（Coverage Target） | Last Verified |
|-----------|------|---------------------------|---------------|
| `BenchmarkScanDirTree_Hierarchical_1000_Cold` | 2 | `scanDirTree` 無 prior：directory walk + per-file SHA-256 hash + tenant 宣告 parse (1000 tenants) | v2.9.0+ |
| `BenchmarkScanDirTree_Hierarchical_2000_Cold` | 2 | 同上，2000 tenants（scaling characterization） | v2.9.0+ |
| `BenchmarkScanDirTree_Hierarchical_5000_Cold` | 2 | 同上，5000 tenants（scaling characterization） | v2.9.0+ |
| `BenchmarkFullDirLoad_Hierarchical_1000` | 2 | `fullDirLoad`：cold-load YAML parse + L0/L1/L2/L3 hierarchical merge (1000 tenants) | v2.8.0 |
| `BenchmarkFullDirLoad_Hierarchical_2000` | 2 | 同上，2000 tenants | v2.8.0 |
| `BenchmarkFullDirLoad_Hierarchical_5000` | 2 | 同上，5000 tenants | v2.8.0 |
| `BenchmarkDiffAndReload_Hierarchical_1000_NoChange` | 2 | `diffAndReload` steady-state WatchLoop tick：hash diff → no-op fast path (1000 tenants) | v2.8.0 |
| `BenchmarkDiffAndReload_Hierarchical_2000_NoChange` | 2 | 同上，2000 tenants | v2.8.0 |
| `BenchmarkDiffAndReload_Hierarchical_5000_NoChange` | 2 | 同上，5000 tenants | v2.8.0 |
| `BenchmarkDiffAndReload_Hierarchical_1000_NoChange_Warm` | 2 | 同 1000_NoChange，但 mtime 先設為一小時前、固定走 mtime fast-path。1000_NoChange 本身是雙峰（fixture 剛寫、2s mtime guard 內整棵重讀），此變體與下一列把兩種模式分開量（#1982） | v2.9.0+ |
| `BenchmarkDiffAndReload_Hierarchical_1000_NoChange_Reread` | 2 | 同上，mtime 設在未來、固定整棵重讀（same-hash carry，不重 parse）（#1982） | v2.9.0+ |
| `BenchmarkDiffAndReload_Hierarchical_1000_OneTenantChanged` | 2 | `diffAndReload` 單一 tenant YAML 變更 → diff + targeted reload tail（fresh-dir variant） | v2.8.0 |
| `BenchmarkBlastRadius_DefaultsChange_Hierarchical_1000` | 2 | B-8：region-level `_defaults.yaml` 變更 → affected-tenants count via `b.ReportMetric` (1000 tenants) | v2.8.0 |
| `BenchmarkBlastRadius_DefaultsChange_Hierarchical_2000` | 2 | 同上，2000 tenants | v2.8.0 |
| `BenchmarkBlastRadius_DefaultsChange_Hierarchical_5000` | 2 | 同上，5000 tenants | v2.8.0 |

**共用 helpers**（同檔案，非獨立 benchmark）：

- `buildDirConfigHierarchical(b, N)` — Pure Go fixture writer，鏡射 `generate_tenant_fixture.py` 結構（8 domains × 6 regions × 3 envs + L0/L1/L2/L3 `_defaults.yaml`）；`sync.Once` cached for read-only benchmarks，fresh-dir variant for mutating benchmarks
- `reportResourceMetrics(b)` — `runtime.GC()` ×2 reap finalizers 後 emit `MB-heap-after-gc` / `MB-sys` / `goroutines` via `b.ReportMetric`
- 共享驅動函式 `benchScanDirTreeHierarchicalAtSize` / `benchFullDirLoadAtSize` / `benchDiffAndReloadHierarchicalAtSizeNoChange` / `benchBlastRadiusDefaultsChangeAtSize` — 由各 size variant 呼叫，DRY 化 1000/2000/5000 三組量測

**執行方式**：完整 `bench_wrapper.sh` 重跑指令見 [Benchmark Playbook §重跑本 baseline 指令](benchmark-playbook.md#重跑本-baseline-指令)；快速跑全 Go bench 用 `make go-bench` 或 `make go-bench-clean`（後者經 `bench_wrapper.sh` 過濾 stdout）。

> **Phase 1 baseline disclaimer**：以上 benchmarks 量測 synthetic fixture，**非 definitive SLO 承諾**。Customer anonymized sample 校準排定於 Phase 2（B-2，blocked on customer data per planning §11.1）。下游文件引用須附「Phase 1 synthetic baseline」前綴。

---

> 本文件從 [`architecture-and-design.md`](../architecture-and-design.md) 獨立拆分（v2.6.0 doc-quality-improvement Phase 2）。v2.9.0 起對外場景行為再拆出公開 [驗證場景](../scenarios/verified-scenarios.md)，本文件聚焦內部測試清單。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| [驗證場景與平台行為（公開）](../scenarios/verified-scenarios.md) | ⭐⭐⭐ |
| [Testing Playbook](testing-playbook.md) | ⭐⭐⭐ |
| [Test Map](test-map.md) | ⭐⭐⭐ |
| [Benchmark Playbook](benchmark-playbook.md) | ⭐⭐ |
| [性能分析與基準測試](../benchmarks.md) | ⭐⭐ |
