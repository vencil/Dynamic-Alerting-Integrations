---
title: "性能分析與基準測試 (Performance Analysis & Benchmarks)"
tags: [performance, benchmarks]
audience: [platform-engineer, sre]
version: v2.0.0-preview.3
lang: zh
---
# 性能分析與基準測試 (Performance Analysis & Benchmarks)

> **Language / 語言：** | **中文（當前）**

> 相關文件：[Architecture](architecture-and-design.md) · [Testing Playbook](internal/testing-playbook.md)

---

## 向量匹配複雜度分析

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

## 實際基準數據 (Kind 叢集量測)

**v1.12.0 設置：2 個租戶，237 個規則（15 Rule Packs），43 個規則群組**

> 以下數據取自 Kind 單節點叢集量測。v1.11.0（13 packs）與 v1.12.0（15 packs）對比。

```
v1.12.0（15 Rule Packs）：
  總評估時間（per cycle）: 23.2ms
  p50 per-group: 0.39ms
  p99 per-group: 4.89ms

v1.11.0（13 Rule Packs，5 輪 mean ± stddev）：
  總評估時間（per cycle）: 20.3 ± 1.9ms  (range: 17.7–22.8ms, n=5)
  p50: 1.23 ± 0.28ms per group
  p99: 6.89 ± 0.44ms per group
```

**擴展性對比：**

| 指標 | 現有（2 租戶） | 傳統方案（100 租戶） | 動態方案（100 租戶） |
|------|-------|-------------------|------------------|
| 警報規則數 | 96（固定） | 9,600（96×100） | 96（固定） |
| 記錄規則數 | 141（正規化） | 0（嵌入在警報中） | 141（固定） |
| **規則總數** | **237** | **9,600** | **237** |
| 評估複雜度 | O(M) | O(N×M) | O(M) |
| **估計評估時間** | **~23ms** | **~1,100ms+** | **~23ms** |

**結論：**
- 傳統方案在 100 租戶時評估時間增加 **~48 倍**
- 動態方案評估時間 **恆定**，線性擴展

## 空向量零成本 (Empty Vector Zero-Cost)

所有規則包預加載（benchmark 時為 9 個，v1.8.0 已擴展至 15 個）。未部署匯出器的包針對空向量評估。

**Kind 叢集實際測量：**

| Rule Pack | 狀態 | 規則數 | 評估時間 | 備註 |
|-----------|------|--------|---------|------|
| MariaDB | ✓ 活躍 | 7 | **2.12ms** | 有匯出器 |
| MongoDB | ✗ 無匯出器 | 7 | **0.64ms** | 空向量 |
| Redis | ✗ 無匯出器 | 7 | **0.41ms** | 空向量 |
| Elasticsearch | ✗ 無匯出器 | 7 | **1.75ms** | 複雜 PromQL，仍低成本 |

**結論：**
- 空向量操作近似 O(1)
- 預加載未使用的規則包開銷 **可忽視**（< 1ms）
- 新租戶上線時，所有規則自動適用，**無需重新部署**

## 記憶體效率

> 以下數據取自 **5 輪獨立量測** mean ± stddev。

```
單個 threshold-exporter Pod（實測）：
- Heap 記憶體：2.4 ± 0.4MB（YAML 解析 + 指標生成）
- 輸出指標：~8 user_threshold series（2 個租戶）
- Scrape Duration：4.1 ± 1.2ms

× 2 HA Replicas：~4.8MB 合計
+ Prometheus RSS：142.7 ± 1.4MB（含 9 Rule Packs、141 條規則）
= 叢集開銷：~148MB

vs. 傳統方案 (5,600 規則 @ 100 租戶)：
- Prometheus 規則快取：~500MB+
- 總開銷：~600MB+（單樞紐）
```

## 資源使用基準 (Resource Usage Baseline)

以下為 Kind 單節點叢集實測數據（2 個租戶）：

| 指標 | 元件 | v1.11.0 (13 packs, n=5) | v1.12.0 (15 packs) | 用途 |
|------|------|------|------|------|
| CPU（5m 均值） | Prometheus | ~0.014 ± 0.003 cores | 0.004 cores | 容量規劃 |
| RSS Memory | Prometheus | 142.7 ± 1.4MB | 112.6MB | 記憶體預算 |
| Heap Memory | threshold-exporter (per pod) | 2.4 ± 0.4MB | 2.2MB | Pod resource limits |
| Scrape Duration | Prometheus → exporter | 4.1 ± 1.2ms | 2.7ms | 抓取效能基線 |

**自動化收集：**

```bash
make benchmark          # 完整報告（人類可讀）
make benchmark ARGS=--json  # JSON 輸出（CI/CD 消費）
```

## 儲存與基數分析 (Storage & Cardinality)

**為什麼基數（Cardinality）比磁碟更重要？**

Prometheus 的效能瓶頸在於 **活躍時間序列數（Active Series）**，而非磁碟空間。每個 series 佔用約 2KB 記憶體，series 數直接決定：查詢延遲、記憶體用量、compaction 頻率。

**Kind 叢集實測：**

| 指標 | 數值 | 說明 |
|------|------|------|
| TSDB 磁碟用量 | 8.9 ± 0.2MB (v1.11.0) / 0.5MB (v1.12.0) | 含所有規則與指標 |
| 活躍 Series 總數 | ~6,037 (v1.11.0) / 6,239 (v1.12.0) | 包含所有 exporter + recording rules |
| `user_threshold` Series | 8 | threshold-exporter 輸出的閾值指標 |
| 每租戶 Series 增量 | ~4 | 新增 1 個租戶的邊際成本 |

**擴展估算公式：**

```
新增 N 個租戶的邊際成本：
  Series 增量 = N × (每租戶 series 數)
  記憶體增量 ≈ Series 增量 × 2KB

範例（100 租戶）：
  user_threshold series = 100 × 4 = 400
  記憶體增量 ≈ (400 - 8) × 2KB ≈ 0.8MB
  總 series ≈ 6,239 - 8 + 400 = 6,631
```

**結論：** 動態架構的 series 增量極小（每租戶 ~4 series），100 個租戶僅增加 ~0.8MB 記憶體。v1.12.0 新增 JVM + Nginx Rule Packs（+96 rules），活躍 series 僅增加 ~200（從 6,037 到 6,239），確認 Rule Pack 擴展的 series 開銷可控。

## Under-Load 基準測試 (Benchmark Under-Load Mode)

v0.13.0 新增 `--under-load` 模式，在合成租戶負載下驗證平台擴展性。idle-state 基準只量測空閒效能，under-load 模式則模擬真實的多租戶環境。

**測試方法論：**
```bash
make benchmark ARGS="--under-load --tenants 1000"
```

1. **合成租戶生成**：動態建立 N 個 synthetic tenant 配置（scalar + mixed + night-window 組合）
2. **ConfigMap Patch**：將合成配置注入 `threshold-config` ConfigMap
3. **量測維度**：
   - **Reload Latency**：ConfigMap 變更到 exporter 完成重載的時間
   - **Memory Delta**：新增 N 個租戶後的 RSS 記憶體變化
   - **Scrape Duration**：Prometheus 抓取 threshold-exporter 的時間
   - **Evaluation Time**：Recording rules + Alert rules 的評估時間
4. **清理**：自動移除合成租戶，回到原始狀態

**Go Micro-Benchmark：**

`config_bench_test.go` 提供精確的 Go 層面效能量測（Intel Core 7 240H，`-count=5` 取中位數）：

**v1.12.0（含 Tenant Profiles 支援）：**

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

<details>
<summary>v1.11.0 對照數據</summary>

| Benchmark | ns/op (median) | ns/op (stddev) | B/op | allocs/op |
|-----------|------:|------:|-----:|----------:|
| Resolve_10Tenants_Scalar | 11,570 | 237 | 26,032 | 58 |
| Resolve_100Tenants_Scalar | 107,346 | 4,315 | 196,080 | 511 |
| Resolve_1000Tenants_Scalar | 2,215,080 | 113,589 | 3,739,792 | 5,019 |
| ResolveAt_10Tenants_Mixed | 39,487 | 1,720 | 39,491 | 268 |
| ResolveAt_100Tenants_Mixed | 419,960 | 18,120 | 454,366 | 2,612 |
| ResolveAt_1000Tenants_Mixed | 4,882,962 | 105,810 | 5,160,416 | 26,038 |
| ResolveAt_NightWindow_1000 | 4,887,959 | 123,943 | 5,123,590 | 25,037 |

</details>

**結論：** v1.12.0 新增 `applyProfiles()` 後效能基本持平（Scalar 1000 租戶 1.95ms vs 2.22ms），Mixed 略增（5.34ms vs 4.88ms）。新增 `ResolveSilentModes_1000` benchmark（86µs/1000 租戶）。10→100→1000 租戶呈線性增長，1000 租戶完整 resolve 仍在 5.5ms 以內。

> **與[實際基準數據](#實際基準數據-kind-叢集量測)的關係：** [實際基準數據](#實際基準數據-kind-叢集量測)量測的是 **Prometheus 規則評估**——由於規則數固定為 O(M)，評估時間不隨租戶數增長（2 租戶 ~20ms ≈ 100 租戶 ~20ms）。本節量測的是 **threshold-exporter 設定解析**——每多一個租戶就多一份設定要 resolve，因此成本為 O(N) 線性增長。兩者互補：平台最關鍵的瓶頸（規則評估）恆定不變，次要成本（設定解析）雖線性增長，但 1000 租戶仍僅 ~5ms，遠低於 Prometheus 15 秒抓取週期，對端到端效能影響可忽略。

## Rule Evaluation Scaling Curve

量測 Rule Pack 數量對 Prometheus rule evaluation 時間的邊際影響。透過逐步移除 Rule Pack（9→6→3）並量測 `prometheus_rule_group_last_duration_seconds`，可觀察 evaluation 成本是否呈線性增長。

**測試方法：**
```bash
make benchmark ARGS="--scaling-curve"
```

1. **Tier 3 (9 packs)**：完整狀態（mariadb, kubernetes, redis, mongodb, elasticsearch, oracle, db2, clickhouse, platform）
2. **Tier 2 (6 packs)**：移除 oracle, db2, clickhouse
3. **Tier 1 (3 packs)**：僅保留 mariadb, kubernetes, platform

每個階段等待 Prometheus 完成至少 2 個 evaluation cycle 後取樣。測試結束自動還原所有 Rule Pack。

**Kind 叢集實測：**

| Rule Packs | Rule Groups | Total Rules | Eval Time (median) | Range | 版本 |
|------------|-------------|-------------|-----------|-------|------|
| 3          | 9           | 34          | 7.7ms     | 3.3–15.3ms | v1.11.0 |
| 6          | 18          | 85          | 17.3ms    | 14.3–18.6ms | v1.11.0 |
| 9          | 27          | 141         | 22.7ms    | 8.7–26.0ms | v1.11.0 |
| **15**     | **43**      | **237**     | **23.2ms** | — | **v1.12.0** |

> **量測說明：** v1.11.0 數據取自 3 輪量測 median（每輪刪除 Rule Pack → 重啟 Prometheus → 穩定 → 取樣）。v1.12.0 數據取自 idle-state 量測（15 packs 全部掛載）。

**結論：** Rule Pack 從 3→9→15，eval time 從 7.7→22.7→23.2ms。9→15 packs（+96 rules）eval time 僅增加 0.5ms，因為新增的 JVM/Nginx Rule Pack 在無對應 exporter 數據時觸發[空向量零成本](#空向量零成本-empty-vector-zero-cost)。每個 group 的平均 eval time（23.2ms / 43 groups = 0.54ms）維持穩定。Projected Volume 架構的水平擴展性得到驗證。

## Route Generation Scaling（Alertmanager 路由產出效能）

`generate_alertmanager_routes.py` 將所有 tenant YAML 轉換為 Alertmanager route + receiver + inhibit_rules fragment。隨租戶數增加，產出的 route tree 線性增長。此 benchmark 量測 route generation 的 wall time，確認 CI pipeline 和 `--apply` 的即時性不受租戶規模影響。

**測試方法：**
```bash
make benchmark ARGS="--routing-bench --tenants 100"
```

1. 以 `scaffold_tenant.py` 的 YAML 結構為基礎，產出 N 個合成 tenant 配置（含 6 種 receiver type 輪替、severity_dedup、每 5 個 tenant 帶 routing overrides）
2. 對每個 N（2, 10, 50, 100）執行 5 輪 `generate_alertmanager_routes.py --dry-run`，取 median wall time
3. 同步記錄產出的 YAML 行數、route 數、inhibit rule 數

**實測數據：**

| Tenants | Wall Time (v1.11.0) | Wall Time (v1.12.0) | Output Lines | Routes | Inhibit Rules |
|---------|-------------------|-------------------|--------------|--------|---------------|
| 2       | 94ms              | 181ms             | 72           | 3      | 2             |
| 10      | 118ms             | 196ms             | 209          | 8      | 10            |
| 50      | 245ms             | 248ms             | 994          | 41     | 50            |
| 100     | 298ms             | 327ms             | 1,943        | 80     | 100           |
| 200     | 397ms             | —                 | 3,884        | 161    | 200           |

> **合成 tenant 規格：** 6 種 receiver type 輪替（webhook/email/slack/teams/rocketchat/pagerduty），所有 tenant 啟用 `_severity_dedup`，每 5 個 tenant 帶 1 個 routing override。Wall time 包含 Python 啟動 + YAML 載入 + route 產出。v1.11.0 為 10 輪 median，v1.12.0 為單次量測。

**結論：** 基礎開銷 ~80–180ms（Python 啟動 + import），之後每增加 100 個 tenant 約 +150–200ms。100 個 tenant 在 330ms 以內，遠低於 CI pipeline 容忍度（秒級）。Output lines 與 tenant 數嚴格線性（~19 lines/tenant），inhibit rules 數 = tenant 數（每個啟用 dedup 的 tenant 1 條 severity dedup rule）。

## Alertmanager 通知效能（Notification Performance）

量測 Alertmanager 在動態路由配置下的運行時效能，重點在於 inhibit rule 評估和通知延遲。

**測試方法：**
```bash
make benchmark ARGS="--alertmanager-bench"
```

從 Prometheus 和 Alertmanager API 收集以下指標：

| 指標 | 來源 | 說明 |
|------|------|------|
| Notification Latency p99 | `alertmanager_notification_latency_seconds` | 從收到 alert 到發出通知的 99th percentile |
| Alerts Received (5m) | `alertmanager_alerts_received_total` | 5 分鐘內收到的 alert 數 |
| Notifications Sent (5m) | `alertmanager_notifications_total` | 5 分鐘內成功發送的通知數 |
| Notifications Failed (5m) | `alertmanager_notifications_failed_total` | 失敗的通知數 |
| Inhibited Alerts | `/api/v2/alerts` | 目前被 inhibit 的 alert 數（severity dedup + enforced routing） |
| Active Inhibit Rules | `/api/v2/status` | 配置中的 inhibit rule 總數 |

**Kind 叢集 Idle-State 實測（2 個 tenant，3 條 inhibit rules）：**

| 指標 | 數值 | 說明 |
|------|------|------|
| Active Inhibit Rules | 3 | 2 條 severity dedup（per-tenant）+ 1 條預設 |
| Active Alerts | 1 | 穩態下的 sentinel alert |
| Inhibited Alerts | 0 | 閒置時無 warning+critical 同時觸發 |
| Notification Latency p99 | N/A | 閒置時無通知活動（需搭配 `--under-load` 觸發 alert 量測） |

> **觀測說明：** Idle-state 下 Alertmanager 無通知活動，notification latency histogram 為空。完整的通知延遲量測需搭配 `make demo-full`（composite load → 觸發 alert → 觀察通知延遲）或 `--under-load` 模式。

**關鍵觀察：** Inhibited alerts / Alerts received 的比率反映 severity dedup 的有效性。正常運營下，每個啟用 dedup 的 tenant-metric_group 對，同時觸發 warning + critical 時，warning 應被 inhibit。3 條 inhibit rules（2 tenant × 1 severity dedup + 1 default）對 Alertmanager 的 route matching 效能影響可忽略。

## Config Reload E2E 延遲

量測 Alertmanager 配置變更生效的端到端延遲。這條路徑決定了「tenant 改了 routing 設定後，多快生效」。

**測試方法：**
```bash
make benchmark ARGS="--reload-bench"
```

**量測路徑：**

```
Tenant YAML 變更
  → generate_alertmanager_routes.py --apply
    → kubectl patch ConfigMap
      → configmap-reload sidecar 偵測檔案變更
        → POST /-/reload
          → 新 route 生效
```

**Kind 叢集實測（5 輪取 median）：**

| 指標 | 數值 (median) | 說明 |
|------|--------------|------|
| `/-/reload` API | **0.3ms** | Alertmanager 自身 config reload（sub-millisecond） |
| `--apply` E2E | **763ms** | 完整路徑：route generation + `kubectl patch` + `/-/reload` |

**`--apply` E2E 5 輪明細：** 676ms, 707ms, **763ms**, 858ms, 956ms

**分解：**
- Route generation（2 tenants）：~94ms（見[Route Generation Scaling](#route-generationscaling-alertmanager-路由產出效能)）
- `kubectl patch` ConfigMap + API server 回應：~500–700ms
- `/-/reload` API：~0.3ms
- 總和與量測一致（~763ms）

> **configmap-reload sidecar 說明：** sidecar 監聽的是 Projected Volume 的**檔案內容變更**，而非 ConfigMap annotation。`--apply` 模式直接更新 ConfigMap `data` 區段 + 觸發 `/-/reload`，因此不依賴 sidecar 的輪詢週期。若僅修改 annotation 而不改變 data，sidecar 不會偵測到變更。

**結論：** 完整的「tenant 改了 routing → Alertmanager 生效」路徑在 Kind 環境下約 760ms（sub-second）。瓶頸在 kubectl API server 交互（~600ms），而非 route generation（~94ms）或 Alertmanager reload（<1ms）。生產環境中 API server 回應通常更快（dedicated etcd），預期 E2E < 500ms。

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
