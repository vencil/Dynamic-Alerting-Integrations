---
title: "ADR-031: slo_burn_rate recipe — 宣告式 SLO 告警編譯"
tags: [adr, threshold-exporter, custom-alerts, alerting, slo]
audience: [platform-engineers, sre, domain-experts]
version: v2.9.0
lang: zh
id: ADR-031
tracking_kind: adr
status: accepted
domain: threshold-exporter
created_at: 2026-07-12
updated_at: 2026-11-24
---
# ADR-031: slo_burn_rate recipe — 宣告式 SLO 告警編譯

## 狀態

✅ **Accepted**（2026-07-12）。recipe 已上線。

2026-10-16 修訂：一組租戶共用的 SLO 宣告寫在 profile；`_defaults.yaml` 裡的 `_custom_alerts` exporter 不送出，不能用來共用（[ADR-024](024-version-aware-threshold-via-dimensional-label.md) §3）。

2026-10-17 修訂：每租戶 cap 由 tenant-api 寫入檢查、da-guard 與 validate-config 計算，profile 的 recipe 不計；壞宣告由 exporter 逐條拒收。

2026-10-21 修訂：燒錯窗口與倍數寫在 Go 的 recipe 契約檔，編譯器也在 Go（[ADR-024](024-version-aware-threshold-via-dimensional-label.md) §9），不再有 Python／Go 兩份要同步。

2026-11-24 修訂：改寫為現行行為；標籤名照實作（`slo_burn`），`tiers` 參數未實作、列為延後項。

## 決策

**新增平台自撰的 recipe `slo_burn_rate`：一條宣告在編譯時展開成多窗口的 SLI recording rule，以及 fast／slow 兩檔、多窗口 AND 的 burn-rate 告警。** 租戶不寫 PromQL。

- **只支援 availability／error-ratio 型 SLI**（兩個 counter 的比率）。latency 分位數與 freshness 型延後：宣告介面表達不了，硬塞會產生永遠不觸發、也不出聲的告警。
- **objective 走既有的 `user_threshold{severity}` 通道。** exporter 在解析設定時算好 `倍數 × (1 − objective/100)` 當閾值送出，規則端不新增任何表達式類別。
- **嚴重度由 recipe 固定**：fast 檔 critical、slow 檔 warning；這個 recipe 不接受 `threshold`。
- **建議用 profile 共用**：profile 定義 recipe 與預設 objective，租戶綁定該 profile，同一個 shape、規則數不隨租戶數成長。
- **不做**：raw PromQL 逃生門（[ADR-029](029-custom-alert-cross-tenant-query-scoping.md)）、平台代管錯誤預算政策。

### 範例

宣告（租戶檔或 profile 的 `_custom_alerts`）：

```yaml
tenants:
  shop-a:
    _custom_alerts:
      - recipe: slo_burn_rate
        name: checkout_availability
        metric: http_requests_errors_total        # 壞事件 counter
        denominator_metric: http_requests_total   # 總事件 counter
        objective: "99.9"                         # SLO 目標（百分比，字串）
        slo_period: 30d                           # 預算窗：28d 或 30d，預設 30d
        min_events: 10                            # fast 檔 5m 窗內壞事件數下限，預設 10
```

編譯器產生的形狀（節錄自 recipe 的契約說明）：

```text {.quote src="rule-packs/recipes/slo_burn_rate.yaml"}
  custom:metric:<id>:<win> = sum by(tenant,da_cluster)(rate(<bad><sel>[<win>]))
                             / (sum by(tenant,da_cluster)(rate(<total><sel>[<win>])) > 0)
                             # <win> ∈ 1h / 5m / 6h / 30m (fixed recipe semantics)
  custom:bad:<id>:<win>    = sum by(tenant,da_cluster)(increase(<bad><sel>[<win>]))   # 5m / 30m
# ...
  custom:<id>:critical:core =  (ratio:1h > thr_crit) and on(tenant, da_cluster)
                               (ratio:5m > thr_crit) and on(tenant, da_cluster)
                               (bad:5m  > <min_events>)          # fast burn
  custom:<id>:warning:core  =  (ratio:6h > thr_warn) and on(tenant, da_cluster)
                               (ratio:30m > thr_warn) and on(tenant, da_cluster)
                               (bad:30m > <min_events>*6)        # slow burn
```

同一個租戶在多個叢集時，比值與壞事件數逐叢集計算，三個條件要在同一個叢集同時成立；`da_cluster` 是叢集身分的 label，見 [ADR-006](./006-tenant-mapping-topologies.md)。序列沒有這個 label 時（單一 Prometheus）與 `by(tenant)`／`on(tenant)` 相同。

以 `objective: "99.9"`、`slo_period: 30d` 為例：exporter 送出 `thr_crit = 14.4 × 0.001 = 0.0144`、`thr_warn = 6 × 0.001 = 0.006`，兩者都是 `user_threshold{severity}` 序列。

## 背景

SLO burn-rate 告警（Google SRE Workbook 第 5 章）是「該設多嚴、何時吵醒人」的業界標準答案，但從一張 SLO 表格到正確的 Prometheus 規則，還要經過多窗口 recording、倍數換算、AND 合成與低流量防誤報，對沒有這些概念的團隊是一道斷崖。本平台的形狀（租戶宣告 YAML、編譯器產生 PromQL）正好接得住這一段。

決定之前檢查過既有能力：

| burn-rate 需要的 | 既有能力 |
|---|---|
| 兩個 counter 的比率 | ✅ `ratio` recipe，含除零守衛 |
| recording rule 輸出 | ✅ 每個 recipe 都展開 recording 與告警 |
| 依嚴重度比較閾值 | ✅ `user_threshold{severity}` |
| **多窗口 AND** | ❌ 編譯器缺的唯一一塊：每個 recipe 只有一個窗口。不是底層限制，rule pack 裡已有 `and on(tenant)` 的寫法 |
| latency／freshness SLI | ❌ 宣告介面表達不了：selector 同時套在分子分母，latency 的 `le` 會套到 `_count` 上，分母成了空向量；freshness 是時間戳差值，不是 counter 比率 |
| custom 告警的通知出口 | ❌ 當時所有 custom 告警都進一個沒有通知出口的 receiver，「critical」叫不醒任何人 → 列為硬前置，已完成（見「分階段交付」） |

## 決定的細節

### 1. 宣告形狀

- **`objective` 取代 `threshold`**：字串，開區間 (0, 100)。`100` 代表預算為 0、永遠觸發，`0` 也拒收；`"disable"` 是三態的關閉。objective 不是 shape 的一部分，改它不會產生新規則。
- **`slo_period`**：`28d` 或 `30d`，預設 `30d`。它只決定 exporter 算閾值用的倍數，不進規則文字，也不是 shape 的一部分。
- **`min_events`**：fast 檔 5 分鐘窗內壞事件數的下限，防低流量誤報；正整數，預設 10。slow 檔的下限由編譯器按窗長換算（30m = 5m × 6）寫成字面值。它**是** shape 的一部分：改它會產生另一組規則。
- **selector 同時套在分子與分母**，這正是 latency 型無法表達的原因。這個 recipe 不接受 `group_by`。

### 2. 編譯輸出

- **recording**：四個窗口（1h／5m／6h／30m）的 SLI 錯誤率，加上兩個短窗（5m／30m）的壞事件數。這一層不受維護窗抑制：維護期間預算燒蝕照樣記在 TSDB。
- **兩檔 core**：fast 檔 `1h 與 5m 的錯誤率都超過 thr_crit，且 5m 壞事件數超過 min_events`；slow 檔 `6h 與 30m 都超過 thr_warn，且 30m 壞事件數超過 min_events × 6`。
- **倍數由預算消耗率導出，不寫死**：fast 檔是「1 小時燒掉 2% 預算」，slow 檔是「6 小時燒掉 5% 預算」；倍數 = 消耗比例 × 預算窗 ÷ 偵測窗。30d 是 14.4／6，28d 是 13.44／5.6，寫在 Go 的 recipe 契約檔（`recipe_contract.json`），`tests/dx/fixtures/slo_burn_multiplier_vectors.json` 釘住兩種預算窗的數值。
- **原始 objective 另有自己的 gauge**：`user_slo_objective{tenant, recipe_id}`，供儀表板與除錯。不在規則裡從閾值反推：反推要把倍數寫進規則文字，`slo_period` 就會變成 shape 的一部分。
- **告警標籤**：沿用 custom 告警的標籤，另加 `metric_group: "slo_{name}"` 與 `slo_burn: "true"`。前者讓既有的嚴重度去重（同一個 `metric_group` 的 critical 抑制 warning）生效：大事故時 fast 與 slow 常同時觸發，沒有它會通知兩次。後者是基礎設施整體故障時 inhibit 的目標（護欄 3）。

### 3. 護欄

1. **cap 計 2，覆寫有代價。** 一條宣告固定產生兩條告警，計入租戶自訂告警上限的 2 個名額；tenant-api 寫入檢查、da-guard 與 validate-config 用同一個數。profile 裡的 recipe 是綁定它的租戶共用的 shape，不計入。租戶要不同的 objective 或 `min_events`，得寫自己的 `_custom_alerts`：花掉自己的 2 個名額，而且**整份取代** profile 的清單（profile 裡其他 recipe 要一併照抄）；改 `min_events` 還會多一組約 12 條規則，只改 objective 不會。recipe 文件寫明這一點。
2. **壞宣告逐條拒收。** exporter 逐條判定，編譯器不為壞宣告產生規則，其餘照常編譯；規則群組上限與 promtool 閘門照舊。
3. **基礎設施整體故障時的通知風暴：量測不抑制，只抑制通知。** 告警帶 `slo_burn: "true"`，可作為 inhibit 的目標；「整體故障時 SLO 告警該不該靜音」是組織決策，所以平台只把 inhibit pattern 寫進文件（[設定驅動設計](../design/config-driven.md)的三態運營模式一節），不預設出貨。
4. **維護窗只抑制告警，不抑制量測。** 告警照 rule pack 慣例在維護期間不觸發；recording 不受影響。
5. **不擴大租戶的查詢表達力。** 輸入仍是受限參數：metric 名、列舉值、驗證過的數值。objective 永遠不進規則文字；`min_events` 經嚴格整數驗證後才寫成字面值，沒有注入面。所以不觸發 [ADR-029](029-custom-alert-cross-tenant-query-scoping.md) 的跨租戶隔離重評。
6. **平台不發明量測。** SLI 的來源指標必須已存在。

## 後果與已知限制

- **租戶不寫 PromQL 就有含低流量防護的 burn-rate 告警**；規則數隨 shape 成長，不隨租戶數；改 objective 不產生新規則。
- **兩檔是刻意偏離 SRE Workbook**：Workbook 裡 14.4× 與 6× 都叫人，本設計把 6× 降為 warning（6× 約 5 天燒光 30 天的預算，判斷為「今天處理、不必半夜叫醒」）。四檔全譜延後。
- **每個 shape 約 12 條規則**，比其他 recipe（4–5 條）多。
- **只涵蓋 availability 型**，SLO 的故事不完整，以延後項管理預期。
- **portal 的 recipe-builder 不提供表單**：它對 `slo_burn_rate` 只顯示「直接寫 YAML」的提示，因為 objective 不是它的表單欄位。

## 考慮過的替代方案

| 方案 | 為什麼不採用 |
|---|---|
| raw PromQL 或受限 DSL | 表達力全開，觸發 ADR-029 的跨租戶隔離重評，成本跳一階 |
| 租戶自己拼多條 `ratio` | 沒有 AND 合成，一個 SLO 吃掉約 8 個 cap 名額，門檻沒有降低 |
| 採用 Sloth 或 Pyrra | 靜態的逐 SLO 規則，無法依租戶向量化，也繞過 conf.d、三態、嚴重度去重與 canary 管線（但沿用它們的慣例） |
| 在 PromQL 裡算係數（`> group_left 14.4 × (1 − objective_series)`） | 新的表達式類別（現有規則右側全是裸 series），疊上版本 fallback 後面積倍增，還要一個新的 objective series 家族 |

## 分階段交付

| 階段 | 內容 | 狀態 |
|---|---|---|
| 前置 | custom 告警依租戶送出：custom 告警的路由下，每個有主路由的租戶一條子路由，指向既有的 `tenant-<name>` receiver；沒有的才落到 `custom-alerts-firehose` | ✅ 已完成 |
| 0 | recipe（availability 型）、recipe 文件、inhibit pattern 文件 | ✅ 已完成 |
| 1 | portal 引導精靈：以歷史 SLI 建議 objective、主動詢問 `min_events` | 未做；觸發條件是第一個客戶實際使用 recipe |
| 2 | Grafana SLO 儀表板（預算餘額、burn rate、趨勢；讀 `user_slo_objective`） | 未做；觸發條件是階段 1 上線且至少一個租戶有在用的 recipe |

## 延後的項目

- **latency 分位數型**：需要分子分母各自的 selector，以及比較方向反轉的「好事件比率」。觸發條件：第一個客戶提出 latency SLO 需求。
- **freshness 型**：時間戳差值，不是比率。觸發條件：第一個客戶有完成時間戳的 instrumentation 並提出需求。
- **四檔全譜（`tiers` 參數）**：未實作。觸發條件：第一個客戶要求更細的分級。
- **歷史回查引擎泛化**：觸發條件：第二個真實的使用者。
- **錯誤預算政策自動化**：永久不在平台範圍內。

## 相關

- [ADR-024](024-version-aware-threshold-via-dimensional-label.md) —— 自訂告警與 recipe 契約
- [ADR-025](025-alerting-plane-self-liveness.md) —— 編譯 canary，新 recipe 自動納入
- [ADR-029](029-custom-alert-cross-tenant-query-scoping.md) —— 租戶查詢表達力的邊界
- [`rule-packs/recipes/slo_burn_rate.yaml`](https://github.com/vencil/Dynamic-Alerting-Integrations/blob/main/rule-packs/recipes/slo_burn_rate.yaml) —— recipe 的人讀契約
- [#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092) —— 功能追蹤與設計討論
