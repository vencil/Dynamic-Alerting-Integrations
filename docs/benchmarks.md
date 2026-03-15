---
title: "性能分析與基準測試 (Performance Analysis & Benchmarks)"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.0.0
lang: zh
---
# 性能分析與基準測試 (Performance Analysis & Benchmarks)

> **Language / 語言：** | **中文（當前）**

> 相關文件：[Architecture](architecture-and-design.md) · [Benchmark Playbook](internal/benchmark-playbook.md)（方法論、踩坑） · [Test Map § Benchmark 基線](internal/test-map.md#benchmark-基線)

**測試環境：** Kind 單節點叢集（Intel Core 7 240H），2 個租戶，237 個規則（15 Rule Packs），43 個規則群組。所有數據於 v2.0.0 統一採集。

---

## 1. 向量匹配複雜度分析

**傳統方法（多租戶硬編碼）：**
```
N 個租戶 × M 個警報規則 = N×M 個獨立 PromQL 評估
複雜度：O(N×M)

範例：100 個租戶，35 個警報規則
= 3,500 個獨立規則評估
```

**動態方法（向量匹配 `group_left`）：**
```
M 個警報規則 × 1 次向量匹配 = M 個評估
複雜度：O(M)，與租戶數量無關

範例：100 個租戶，35 個警報規則
= 35 個規則評估（不論租戶數量）
```

**實測驗證：**

| 場景 | 租戶數 | 規則數 | Eval Time (median) | 來源 |
|------|--------|--------|-------------------|------|
| Idle-State 基線 | 2 | 237 | 59.1ms | §2（5 輪） |
| Under-Load 注入 | 102 | 237 | 60.6ms | §11（3 輪：65.3 / 29.8 / 86.6ms） |

租戶數從 2 增加至 102（×51 倍），規則評估時間幾乎不變（59.1ms → 60.6ms），實測證實 O(M) 複雜度——eval time 僅受規則數 M 驅動，與租戶數 N 無關。傳統 O(N×M) 方案在同等條件下預計需要 ~2,800ms+（見 §2 擴展性對比）。

## 2. Prometheus Rule Evaluation（Idle-State，5 輪）

**設置：** 2 個租戶，237 個規則（15 Rule Packs），43 個規則群組。

| 指標 | Median | StdDev | 說明 |
|------|--------|--------|------|
| Eval Time / Cycle | 59.1ms | ±18.5ms | 全部規則群組單次評估總耗時 |
| p50 per-group | 1.00ms | ±0.08ms | 單群組評估中位數 |
| p99 per-group | 6.34ms | ±0.10ms | 單群組評估長尾 |

> 5 輪原始值：[20.8, 59.1, 61.0, 67.9, 55.6]ms。第 1 輪較低為 Prometheus 快取冷啟動後的首次快照，後 4 輪穩定在 55-68ms 範圍。

**擴展性對比：**

| 指標 | 現有（2 租戶） | 傳統方案（100 租戶） | 動態方案（100 租戶） |
|------|-------|-------------------|------------------|
| 警報規則數 | 96（固定） | 9,600（96×100） | 96（固定） |
| 記錄規則數 | 141（正規化） | 0（嵌入在警報中） | 141（固定） |
| **規則總數** | **237** | **9,600** | **237** |
| 評估複雜度 | O(M) | O(N×M) | O(M) |
| **估計評估時間** | **~59ms** | **~2,800ms+** | **~59ms** |

15 個 Rule Pack 全掛載，無對應 exporter 的包觸發空向量零成本（見下節），每 group 平均 eval time ~1.0ms。Projected Volume 架構的水平擴展性得到驗證。

## 3. 空向量零成本 (Empty Vector Zero-Cost)

所有 15 個規則包預加載（`optional: true`）。未部署匯出器的包針對空向量評估。

| Rule Pack | 狀態 | 規則數 | 評估時間 | 備註 |
|-----------|------|--------|---------|------|
| MariaDB | ✓ 活躍 | 7 | **2.12ms** | 有匯出器 |
| MongoDB | ✗ 無匯出器 | 7 | **0.64ms** | 空向量 |
| Redis | ✗ 無匯出器 | 7 | **0.41ms** | 空向量 |
| Elasticsearch | ✗ 無匯出器 | 7 | **1.75ms** | 複雜 PromQL，仍低成本 |

空向量操作近似 O(1)，預加載未使用規則包的開銷 < 1ms。新租戶上線時所有規則自動適用，無需重新部署。

## 4. 資源使用基準（Idle-State，5 輪 median）

| 指標 | 元件 | Median | StdDev |
|------|------|--------|--------|
| CPU（5m 均值） | Prometheus | 0.017 cores | ±0.001 |
| RSS Memory | Prometheus | 148.1MB | ±1.3MB |
| Heap Memory | threshold-exporter (×2 HA) | 3.1MB | ±0.5MB |
| Scrape Duration | Prometheus → exporter | 6.1ms | ±3.9ms |
| Active Series | Prometheus | 6,338 | ±10 |
| TSDB Storage | Prometheus | 3.0MB | ±0.1MB |

**記憶體效率：**

```
threshold-exporter ×2 HA：~6.2MB
+ Prometheus RSS：148.1MB
= 叢集開銷：~154MB

vs. 傳統方案 (9,600 規則 @ 100 租戶)：~600MB+
```

**自動化收集：**

```bash
make benchmark          # 完整報告（人類可讀）
make benchmark ARGS=--json  # JSON 輸出（CI/CD 消費）
```

## 5. 儲存與基數分析

Prometheus 的效能瓶頸在於活躍時間序列數（Active Series），而非磁碟空間。每個 series 佔用約 2KB 記憶體。

| 指標 | 數值 (5 輪 median) | 說明 |
|------|-------------------|------|
| TSDB 磁碟用量 | 3.0MB | 含所有規則與指標 |
| 活躍 Series 總數 | 6,338 | 包含所有 exporter + recording rules |
| `user_threshold` Series | 8 | threshold-exporter 輸出的閾值指標 |
| 每租戶 Series 增量 | ~4 | 新增 1 個租戶的邊際成本 |

**擴展估算：**

```
100 租戶：
  user_threshold series = 100 × 4 = 400
  記憶體增量 ≈ (400 - 8) × 2KB ≈ 0.8MB
  總 series ≈ 6,338 - 8 + 400 = 6,730
```

動態架構的 series 增量極小（每租戶 ~4 series），100 個租戶僅增加 ~0.8MB 記憶體。Under-Load 實測（§11）驗證此估算。

## 6. Go Micro-Benchmark（threshold-exporter）

`config_bench_test.go` 量測 threshold-exporter 設定解析效能（`go test -bench=. -benchmem -count=5`，Intel Core 7 240H）：

| Benchmark | ns/op (median) | B/op | allocs/op |
|-----------|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 19,590 | 26,488 | 61 |
| Resolve_100Tenants_Scalar | 163,839 | 202,777 | 520 |
| Resolve_1000Tenants_Scalar | 4,076,536 | 3,848,575 | 5,039 |
| ResolveAt_10Tenants_Mixed | 71,536 | 40,032 | 271 |
| ResolveAt_100Tenants_Mixed | 927,426 | 461,872 | 2,621 |
| ResolveAt_1000Tenants_Mixed | 10,274,749 | 5,244,817 | 26,054 |
| ResolveAt_NightWindow_1000 | 8,438,156 | 5,220,583 | 25,055 |
| ResolveSilentModes_1000 | 156,172 | 187,218 | 10 |

10→100→1000 租戶呈線性增長，1000 租戶完整 ResolveAt（含排程式閾值）在 ~10ms 以內。`ResolveSilentModes_1000` 僅 156µs，flag metric 查詢近乎零成本。

> **與 Rule Evaluation 的關係：** §2 量測 Prometheus 規則評估（O(M)，與租戶數無關），本節量測 threshold-exporter 設定解析（O(N)，線性增長）。兩者互補：最關鍵瓶頸（規則評估）恆定，次要成本（設定解析）1000 租戶仍僅 ~10ms，遠低於 15 秒抓取週期。

## 7. Route Generation Scaling

`generate_alertmanager_routes.py` 將 tenant YAML 轉換為 Alertmanager route + receiver + inhibit_rules。

合成 tenant 規格：6 種 receiver type 輪替、`_severity_dedup` 啟用、每 5 個 tenant 帶 routing override。

**CLI E2E 量測（含 Python 啟動 + YAML 載入 + schema validation，10 輪 median ± stddev）：**

| Tenants | Wall Time | Output Lines |
|---------|-----------|-------------|
| 2       | 468ms ±201ms | 52 |
| 10      | 545ms ±220ms | 175 |
| 50      | 632ms ±237ms | 766 |
| 100     | 963ms ±280ms | 1,519 |
| 200     | 1,051ms ±219ms | 3,006 |

**純 route generation（不含 Python 啟動，pytest-benchmark，min_rounds=20）：**

| Tenants | Median | Rounds | 說明 |
|---------|--------|--------|------|
| 10      | ~38µs  | 27,678 | in-process，無 I/O |
| 50      | ~197µs | 5,415  | 線性增長 |
| 100     | ~394µs | 2,773  | sub-millisecond |

基礎開銷（Python 啟動 + import）佔 CLI wall time 的 ~55-70%。純 route generation 邏輯為 sub-millisecond。Output lines 與 tenant 數嚴格線性（~15 lines/tenant）。

## 8. Alertmanager 通知效能（Idle-State）

量測 Alertmanager 在動態路由配置下的閒置狀態運行時效能。完整負載測試見 §11 Under-Load。

```bash
make benchmark ARGS="--alertmanager-bench"
```

| 指標 | 數值 | 說明 |
|------|------|------|
| Active Inhibit Rules | 3 | 2 條 severity dedup + 1 條預設 |
| Active Alerts | 1 | 穩態下的 sentinel alert |
| Inhibited Alerts | 0 | 閒置時無 warning+critical 同時觸發 |
| Notification Latency p99 | N/A | 閒置時無通知活動 |

> Idle-state 下 Alertmanager 僅持有 sentinel alert（sentinel 模式設計，見 architecture §2.7），無觸發通知故 latency 為 N/A。3 條 inhibit rules 為 severity dedup 機制的固定成本，不隨租戶數增長。完整通知延遲量測需搭配 `make demo-full` 或 `--under-load`（§11）。

## 9. Config Reload E2E 延遲

量測「tenant 改了 routing 設定 → Alertmanager 生效」的端到端延遲。

```bash
make benchmark ARGS="--reload-bench"
```

| 指標 | 數值 (5 輪 median) | 說明 |
|------|-------------------|------|
| `/-/reload` API | **0.3ms** | Alertmanager 自身 reload |
| `--apply` E2E | **763ms** | route generation + `kubectl patch` + reload |

分解：route generation ~94ms、kubectl API server ~600ms、reload <1ms。瓶頸在 API server 交互。生產環境（dedicated etcd）預期 E2E < 500ms。

## 10. v2.0.0 新功能效能基線

v2.0.0 新增功能的核心運算效能（不含 Prometheus 查詢 I/O，20 輪 median）：

### Policy-as-Code 引擎

`evaluate_policies()` 對所有 tenant 評估策略規則。3 條 PolicyRule × N 個 tenant：

| Tenants | Median | 說明 |
|---------|--------|------|
| 10      | 0.032ms | 即時回應 |
| 50      | 0.148ms | 線性增長 |
| 100     | 0.262ms | Sub-millisecond |
| 500     | 1.295ms | 線性擴展 |
| 1000    | 2.605ms | 1000 tenant 仍 < 3ms |

100 tenant × 3 rules 在 0.3ms 以內，可安全納入 CI pipeline 或 pre-commit hook。

### Alert Quality Scoring

`compute_noise_score()` + `compute_stale_score()` 為純計算：

| 操作 | Median (20 輪) | 說明 |
|------|---------------|------|
| noise+stale × 1,000 calls | 1.06ms | ~1.1µs/call |
| noise+stale × 10,000 calls | 4.73ms | ~0.5µs/call（amortized） |

瓶頸在 Prometheus 範圍查詢（~1-3s），非計算本身。

### Cardinality Forecasting

`linear_regression()`（純 Python，無 NumPy 依賴）：

| 操作 | Median (20 輪) | 說明 |
|------|---------------|------|
| 100 資料點 × 100 calls | 2.9ms | ~29µs/call |
| 100 資料點 × 1,000 calls | 28.2ms | ~28µs/call |
| 100 資料點 × 10,000 calls | 286.7ms | ~29µs/call |

線性擴展穩定。100 個 tenant 完整預測（含 Prometheus 查詢）預計 3-5s，瓶頸在網路 I/O。

### validate_config E2E

`da-tools validate-config` 一站式驗證（schema + routing + policy + drift），CLI E2E 含所有 check（10 輪 median ± stddev）：

| Tenants | Wall Time | 說明 |
|---------|-----------|------|
| 2       | 225ms ±5ms | 現有 config（快速路徑） |
| 10      | 305ms ±54ms | 合成 config |
| 50      | 606ms ±289ms | 含 routing + policy |

Python 啟動開銷佔 ~200ms。純驗證邏輯 < 100ms/50 tenants。

### Schema Validation（validate_tenant_keys）

`validate_tenant_keys()` 逐 tenant 驗證 key 合法性（20 輪 median ± stddev）：

| Tenants | Median | 說明 |
|---------|--------|------|
| 10      | 0.010ms | 近乎零成本 |
| 100     | 0.128ms | 線性增長 |
| 500     | 0.498ms | Sub-millisecond |
| 1000    | 0.978ms | 1000 tenant < 1ms |

純 dict 操作，可安全嵌入 hot-reload path。

## 11. Under-Load 基準（100 Synthetic Tenants）

注入 100 個合成 tenant 至 ConfigMap，等待 exporter hot-reload + Prometheus scrape，量測負載下的系統行為。

```bash
make benchmark ARGS="--under-load --tenants 100"
```

**Kind 單節點叢集實測（3 輪獨立執行，102 tenants = 2 existing + 100 synthetic）：**

| 指標 | Round 1 | Round 2 | Round 3 | 說明 |
|------|---------|---------|---------|------|
| Prometheus RSS (before) | 148.6MB | 168.2MB | 171.0MB | 注入前基線 |
| Prometheus RSS (after) | 150.8MB | 168.6MB | 168.7MB | 注入後穩態 |
| **Memory Delta** | **+2.2MB** | **+0.4MB** | **-2.3MB** | GC 波動 |
| Scrape Duration (after) | 104.2ms | 6.3ms | 19.9ms | 抓取時間 |
| Eval Time (after) | 65.3ms | 29.8ms | 86.6ms | 規則評估時間 |
| Active Series | 7,338 | 7,378 | 7,378 | 穩定 |
| user_threshold Series | 8→408 | 408→408 | 408→408 | = 102 tenants × 4 |

**結論：**

100 synthetic tenants 對 Prometheus 記憶體影響極小（median delta ~+0.4MB，GC 噪音範圍內）。Active series 穩定在 ~7,360，user_threshold series 精確符合 `102 × 4 = 408`，驗證 per-tenant 4 series 的線性模型。Eval time 與 scrape duration 受 Prometheus 快取狀態影響而有波動，但均在可接受範圍（< 105ms）。Round 1 為乾淨啟動（user_threshold 8→408），Round 2-3 為連續 session（408→408）。

> **注意：** 連續多輪 benchmark 存在 port-forward 重連不穩定問題，詳見 [Benchmark Playbook](internal/benchmark-playbook.md)。建議每輪獨立執行。

## 12. pytest-benchmark 基線

`pytest -m benchmark`（min_rounds=20，warmup=on）。用於版本間趨勢偵測。

| 測試 | Median | Rounds | 說明 |
|------|--------|--------|------|
| `test_parse_integer` | ~102ns | 100,161 | parse_duration_seconds 最快路徑 |
| `test_parse_seconds` | ~634ns | 164,555 | 含字串解析 |
| `test_parse_minutes` | ~624ns | 168,039 | 含字串解析 |
| `test_parse_hours` | ~619ns | 168,663 | 含字串解析 |
| `test_format_seconds` | ~128ns | 80,167 | format_duration |
| `test_format_minutes` | ~160ns | 59,443 | format_duration（分鐘） |
| `test_format_hours` | ~147ns | 70,872 | format_duration（小時） |
| `test_within_bounds` | ~796ns | 131,303 | validate_and_clamp（無 clamp） |
| `test_clamped` | ~1.2µs | 85,129 | validate_and_clamp（含 clamp） |
| `test_10_tenants` (routes) | ~38µs | 27,678 | 10 tenant route generation |
| `test_50_tenants` (routes) | ~197µs | 5,415 | 50 tenant route generation |
| `test_100_tenants` (routes) | ~394µs | 2,773 | 100 tenant route generation |
| `test_100_tenants` (inhibit) | ~32µs | 31,718 | 100 tenant inhibit rules |
| `test_10_tenants_from_disk` | ~5.4ms | 197 | 10 tenant 含 YAML I/O |

---

## 方法論

完整方法論與踩坑記錄詳見 [Benchmark Playbook](internal/benchmark-playbook.md)。

**統計要求：**
- pytest-benchmark：min_rounds=20，warmup 開啟，報告 median
- benchmark.sh（K8s idle-state）：5 輪，間隔 30s，報告 median ± stddev
- benchmark.sh（K8s under-load）：每輪獨立執行（避免 port-forward 不穩定）
- Go micro-bench：`-count=5`，報告 median
- v2.0.0 功能基線：20 輪 in-process，報告 median
- CLI E2E：10 輪 subprocess，報告 median ± stddev

---

> 本文件從 [`architecture-and-design.md`](architecture-and-design.md) 獨立拆分。

## 相關資源

| 資源 | 相關性 |
|------|--------|
| ["Performance Analysis & Benchmarks"] | ⭐⭐⭐ |
| ["Threshold Exporter API Reference"](api/README.md) | ⭐⭐ |
| ["BYO Alertmanager 整合指南"](./byo-alertmanager-integration.md) | ⭐⭐ |
| ["Bring Your Own Prometheus (BYOP) — 現有監控架構整合指南"](./byo-prometheus-integration.md) | ⭐⭐ |
| ["da-tools CLI Reference"](./cli-reference.md) | ⭐⭐ |
| ["Grafana Dashboard 導覽"](./grafana-dashboards.md) | ⭐⭐ |
| ["進階場景與測試覆蓋"](scenarios/advanced-scenarios.md) | ⭐⭐ |
| ["Shadow Monitoring SRE SOP"](./shadow-monitoring-sop.md) | ⭐⭐ |
