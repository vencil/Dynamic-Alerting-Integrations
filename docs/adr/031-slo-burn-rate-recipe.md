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
updated_at: 2026-07-12
---
# ADR-031: slo_burn_rate recipe — 宣告式 SLO 告警編譯

## 狀態

✅ **Accepted**（2026-07-12）

> **決策一句話**：新增第 7 個平台自撰 recipe `slo_burn_rate`——一條宣告在編譯期展開成多窗 SLI recording rules ＋ multi-window AND 的 fast/slow burn-rate 告警；objective 走**既有** `user_threshold{severity}` 通道（exporter resolve 期算好係數）；v1 範圍＝availability/error-ratio 型 only；custom 子樹 outbound delivery 列硬前置。

設計歷經內部對抗 critic pass（9 findings 全數 triage）與外部 adversarial review（3 銳角修入）——完整對抗軌跡與 triage 表見 feature issue [#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092)（decision trail 進 issue、本 ADR 留決策＋理由）。

> 依語言政策（自 ADR-019 預設 ZH-only），本 ADR 不另製 `.en.md`。

## TL;DR

- **新增第 7 個平台自撰 recipe `slo_burn_rate`**：一條宣告 → 編譯期展開成多窗 SLI recording rules + multi-window AND 的 fast/slow burn-rate 告警。租戶**零 PromQL**（ADR-008/024 declarative-only 不動）。
- **v1 範圍 = availability/error-ratio SLI only**；latency-quantile 與 freshness 型 **defer-with-trigger**（宣告介面表達不了，硬塞會產生「永不觸發且靜默」的告警）。
- **objective 不開新資料平面**：exporter 在 resolve 期算好 `14.4×(1−obj)`／`6×(1−obj)`，以**既有** `user_threshold{severity}` 通道餵值 → 規則端零新表達式類別、落回現行 per-severity core join。
- **硬前置**：custom 子樹 outbound delivery（現況 firehose receiver 無 notifier——沒有它，「critical/page」page 不到任何人）。
- **主推模式 = domain-inherited**（domain 定義 recipe、tenant 填 objective；不吃 cap、自動向量化）。
- **明確不做**：raw-PromQL 逃生門（ADR-029 T1）；平台代管錯誤預算政策。

## 背景

### 問題

SLO burn-rate 告警（SRE Workbook ch.5）是「該設多嚴、何時吵醒人」的業界標準答案，但從「SLO 表格」到「正確的 Prometheus 規則」的最後一哩（多窗 recording、倍率換算、AND 合成、reset 語意、低流量防誤報）對缺乏概念的團隊是斷崖。本平台的核心形狀（config-driven、租戶宣告 YAML、編譯器 own PromQL）正是這一哩的載體。

### Spike 實證（能力邊界）

| burn-rate 需求 | 現況 |
|---|---|
| ratio-of-rates SLI | ✅ 原生（`ratio` recipe；promtool fixture 已驗；`(den > 0)` 除零守衛） |
| recording rule 輸出 | ✅ 預設架構（每 recipe 展開 recording + alerts；forecast 已有一 shape 多 recording 先例） |
| per-severity 閾值比較 | ✅ 原生（`user_threshold{severity}` core join） |
| **多視窗 AND** | ❌ **編譯器唯一缺口**——每 instance 單 window、無合成原語。非底層限制（`and on(tenant)` 複合先例：mariadb pack 4 處、全 packs 9 處） |
| **latency/freshness SLI** | ❌ 宣告介面表達不了（selector 同時套分子分母 → latency 的 `le` 會套到 `_count` 上 → 分母空向量、告警靜默不觸發；freshness 是 timestamp 差值、非 counter ratio）→ **v1 排除** |
| **custom 告警通知出口** | ❌ **MVP 缺**——`component="custom"` 全數 `continue:false` 進 `custom-alerts-firehose`，該 receiver 無 outbound notifier（僅 AM UI 可見）→ **硬前置** |

## 決策

### 1. 宣告形狀（v1：availability/error-ratio only）

```yaml
_custom_alerts:
  - recipe: slo_burn_rate
    name: checkout-availability          # 必填（沿用既有 required；入 slug）
    metric: <壞事件 counter>              # 如 http_requests_errors_total
    denominator_metric: <總事件 counter>   # 如 http_requests_total
    selectors: { ... }                     # 既有機制；v1 同時套用於分子分母（此即 latency 型排除的原因）
    objective: "99.9"                      # SLO 目標（百分比）；"disable" 沿用三態關閉慣例
    slo_period: 30d                        # 預算窗（enum：28d|30d）；倍率語意的錨、Phase 2 儀表板的分母
    min_events: 10                         # N_min：fast 短窗（5m）內壞事件絕對數下限；低流量防誤報
    # 進階（有預設）：tiers: two_tier（預設）| four_tier
```

- **`objective` 取代 `threshold` 欄位**（schema if/then 第三分支）：值域驗證 `(0,100)` **開區間**（`objective=100` → 閾值 0 → 恆 fire，直接 reject）；`"disable"` 保留三態關閉語意。severity **不再**來自 threshold 尾巴慣例——由 recipe 固定（fast→critical、slow→warning），為刻意偏離、於 recipe 文件明示。
- **`min_events` 是編譯期宣告參數**（shape-affecting）：主推 domain-inherited 模式下由 domain 統一設定→單一 shape；tenant 覆寫＝shape 分岔（可接受：改動罕見）。slow 檔的下限由**編譯器按窗長線性換算**（30m = 5m×6）寫成字面值，換算歸屬明確在編譯側。
  - ⚠️ 此處推翻早期草案「`N_min` 走資料平面」：那是為「per-tenant 各異 + 共 shape」優化，但 domain-first 模式下不成立；編譯期字面值換得零新管線。
- **latency-quantile／freshness 型 → Deferred**（見下）；模板文件仍教 instrumentation 路徑（pushgateway／textfile／Vector `log_to_metric`），但 recipe 支援不假裝存在。

### 2. 編譯輸出（兩段預設檔位）

- Recording：`custom:metric:{rid}:<win>` — SLI error-ratio × 4 窗（1h／5m／6h／30m）+ 壞事件絕對數 × 2 短窗（5m／30m）。
- Fast-burn core（critical）：`(ratio:1h > thr_crit) and on(tenant) (ratio:5m > thr_crit) and on(tenant) (bad:5m > N_min)`。
- Slow-burn core（warning）：`(ratio:6h > thr_warn) and on(tenant) (ratio:30m > thr_warn) and on(tenant) (bad:30m > N_min×6)`。
- **`thr_crit`／`thr_warn` = 既有 `user_threshold{severity}` series**：exporter 在 resolve 期算好倍率×(1−obj) 餵入——**規則端零新表達式類別**，比較 RHS 維持裸 threshold series（現行所有 core 同形）；promtool fixture 落在既有類別。
  - **倍率不 hardcode、由 `slo_period` 動態導出**：常數的真正定義是**預算消耗語意**——fast＝「1h 燒掉 2% 預算」、slow＝「6h 燒掉 5% 預算」；倍率 M＝消耗比 × period ÷ 偵測窗（30d→14.4／6；28d→13.44／5.6）。golden vectors 兩種 period 都涵蓋。
  - **好性質（明寫）**：此案下 `slo_period` 只影響 exporter 算出的**值**、不進規則文本 → **不是 shape 成分**——換 period 不 re-slug、不分岔規則。
  - **原始 objective 的可見性 = exporter 原生 gauge**：`user_slo_objective{tenant, recipe_id} 99.9`（命名落在既有 `user_*` 家族，最終名照 OQ-B 與 maintainer 定），供 Phase 2 儀表板與除錯。**禁用 recording rule 反推**：反推式（`1 − thr/M`）必須把 period 相依倍率寫進規則文本 → `slo_period` 變 shape-affecting，毀掉上一條性質。
  - 代價（明列）：倍率導出邏輯進 Go resolver、與 Python 編譯器的窗選擇跨語言 lockstep（golden vector 必涵蓋）。被拒的表達式側方案見選項表 E′。
- Alert 標籤：沿用 custom 家族標籤 + **補 `metric_group: "slo_{name}"`**——沒有它 Severity Dedup 的 inhibit（source/target 均要求 `metric_group=~".+"`）對 custom alerts 永不匹配，fast+slow 同時 firing（大事故常態）會雙發通知；補上後 critical 抑制 warning 落回既有 dedup 機制，零新 inhibit 規則。
- 每 shape 規則數 ≈ 11（1 threshold + 4 ratio 窗 + 2 事件數窗 + 1 info + 2 core + 2 alert；現行 recipe 約 4–5）——rule-group `limit` 餘裕需在實作期實測列帳（實作 checklist 項）。

### 3. 護欄

1. **Cap 帳**：一條宣告固定展開 fast(critical)+slow(warning) → **loader 計 2**（沿用 distinct (tenant, recipe_id, severity) 計數，明定不另開特例）。domain-inherited 不吃 cap（既有行為）。**覆寫的代價誠實告知**：租戶覆寫 domain 預設的 `min_events`（或任何 shape 成分）＝自有宣告 → **計入自身 cap 額度（2）且 shape 分岔（+~11 條規則）**——架構上可接受（改動罕見），但 recipe 文件必須明寫，防「免費覆寫」的錯誤預期。
2. **Cardinality**：`group_by` 白名單、rule-group limit、promtool hard gate、fail-soft quarantine 全數繼承。
3. **通知風暴（infra 全局震盪）**：**量測永不抑制**；抑制通知 fan-out——`vibe_slo_burn="true"` 標籤（命名照 pack 標籤慣例實作期覆核）+ 文件化 infra-outage sentinel inhibit pattern。
4. **維護窗（三態）交互——刻意取捨**：SLO **alert** 沿用 pack 慣例，維護窗期間 `unless` 抑制（三態對租戶的承諾就是「維護窗零告警干擾」）；SLI **recording rules 不受 unless 影響、持續評估**——預算燒蝕在 TSDB 全程留痕，「量測永不抑制」哲學由 recording 層承擔，alert 層的抑制是通知承諾、不是量測缺口。此取捨明寫進 recipe 文件。
5. **表達力邊界**：編譯期展開、輸入仍為受限參數（bare metric name、enum、已驗純量）——**不新增租戶查詢表達力類別、不觸發 ADR-029 T1**（T1 定義=「超出固定 recipe 模板+已驗純量參數」；objective/min_events 為新增已驗純量，經 ParseFloat 驗證、永不進規則文本）。objective/min_events 依 ADR-029 Consequences 慣例**於 Python/Go 兩側各自加驗證**。
6. **誠實邊界**：SLI 源指標必須已存在——平台編譯宣告、不發明量測。v1 只接 counter-ratio 形；latency/freshness 的 instrumentation 指引在文件、recipe 支援在 deferred。

## 選項與取捨

| 方案 | 取 | 捨 | 採用 |
|---|---|---|---|
| **A：第 7 recipe、編譯期展開** | 落在既有 emit_shape 模式；零新表達力；cap 友善 | 檔位 opinionated | ✅ |
| B：raw-PromQL / bounded-DSL | 表達力全開 | ADR-029 T1（跨租戶隔離重評）成本階躍 | ❌ |
| C：租戶手拼多條 `ratio` | 零開發 | 無 AND 合成（失 reset 語意）；cap ~8 條/SLO；門檻沒降 | ❌ |
| D：adopt Sloth／Pyrra 引擎 | 現成慣例 | 靜態 per-SLO 規則：無租戶向量化、繞過 conf.d/三態/dedup/canary 管線 | ❌（**慣例✅**） |
| **E：係數 exporter 側算（resolve 期）** | 走既有 `user_threshold{severity}` 通道；規則端零新形狀；砍掉整條新資料平面管線 | 倍率常數跨語言 lockstep；原始 objective 需原生 gauge 補可見性 | ✅ |
| E′：係數 PromQL 側算（`> group_left 14.4×(1−obj_series)`） | 常數留在編譯器單側 | **全新表達式類別**（現行 RHS 全為裸 series）、疊上 version fallback 雙分支後表達式面積倍增、需新 promtool fixture 類別、需新 objective series 家族（新 resolver/collector 觸點） | ❌ |

## 後果

**正面**：租戶零 PromQL 得到含低流量防護的 burn-rate 告警；domain→tenant 分工（domain 定 shape、tenant 填 objective）；規則 O(shapes)；**objective 改值不 re-slug**（editable 友善——objective 是 series 值、非 shape 成分）。

**負面／待審視**：

- recipe 家族 6→7，Python/Go lockstep 面積 +1，且 E 案把倍率常數也納入 lockstep。
- **preview 缺口**：ratio 預覽本就延後 → 出貨時 would-fire 預覽不可用，UI 誠實標示（既有慣例）。
- 兩段檔位對 Workbook 的**刻意偏離**：Workbook 中 14.4× 與 6× 均為 page 檔（ticket 為 3×/1×）；本設計把 6× 降為 warning/ticket 是入門簡化（6× = 5 天燒光 30d 預算，判斷為「今天處理、不必吵醒」尚可辯護），`four_tier` 參數還原全譜。此偏離於 recipe 文件明示。
- v1 只覆蓋 availability 型——SLO 故事不完整，靠 deferred triggers 誠實管理預期。

## 分階段交付與 gates

| Phase | 內容 | Gate |
|---|---|---|
| **0-pre（硬前置）** | **custom 子樹 outbound delivery**（per-tenant 子路由、沿用既有 `tenant-<name>` receiver；定案見 OQ-C）——否則 fast-burn critical 無人收到 | 本 ADR accepted 即做；**獨立工作項，現有 custom alerts 同受益** |
| 0 | recipe（availability 型）+ 模板文件 + inhibit pattern 文件 + 公開文（告警系列篇二）功能段補寫 | ADR accepted + 0-pre 完成 |
| 1 | Portal 精靈（兩題定位→建議檔位；歷史 SLI 回查建議 objective——獨立 range query；**主動詢問 min_events**）+ ratio preview 解凍（前置） | 首客戶實際使用 Phase 0 模板 |
| 2 | Grafana SLO 儀表板 pack（預算餘額=f(objective, slo_period)、burn rate、趨勢；讀 `user_slo_objective`） | Phase 1 上線且 ≥1 租戶有活 recipe |

## Deferred（附 trigger）

- **latency-quantile 型**：需 per-side selectors + good-ratio 語意開關（比較方向反轉）——trigger＝首個客戶提出 latency SLO 需求。
- **freshness 型**：timestamp 差值形狀、非 ratio——trigger＝首個客戶具備 completion-timestamp instrumentation 且提出需求。
- **四段全譜檔位**：`tiers: four_tier` 參數位保留——trigger＝首個客戶要求更細分級。
- **歷史回查引擎泛化**：trigger＝第二個真實消費者（n=2；已三度拒 pre-build）。
- **錯誤預算政策自動化**：**永久平台範圍外**。

## 同步清單（doc-as-code cascade）

`RECIPES` enum（`shape.py`+`custom_alert.go` lockstep）→ 倍率常數 lockstep（Go resolver 算值×Python 選窗，golden vector 涵蓋）→ `recipe_id_vectors.json` → schema：enum + `objective`/`slo_period`/`min_events` 欄位 + **if/then 第三分支**（slo_burn_rate → objective 必填、threshold 禁用）→ `rule-packs/recipes/` 文件 + count 同步 → promtool fixture（含低流量情境：2 請求 1 錯不觸發）→ CHANGELOG/CLAUDE.md/README 計數。

## Open Questions

- OQ-A `min_events` 預設值（保守起點候選 5–10；配低流量 fixture 定案）。
- OQ-B `metric_group: "slo_{name}"` 與 `vibe_slo_burn` 標籤命名——照 pack 標籤慣例（現行無前綴家族）實作期與 maintainer 定。
- ~~OQ-C 0-pre（custom 子樹 delivery）的 receiver 形狀~~——已定案（2026-07-12）：custom 子樹內 per-tenant 子路由、沿用既有 `tenant-<name>` receiver、fallback firehose；本 ADR 只立依賴，設計細節見 feature issue [#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092) 與 0-pre PR。

## 關聯

- [ADR-008](008-operator-native-integration-path.md)/[ADR-024](024-version-aware-threshold-via-dimensional-label.md)（declarative-only、custom-alerts 能力線）、[ADR-025](025-alerting-plane-self-liveness.md)（編譯 canary，新 recipe 自動納入）、[ADR-029](029-custom-alert-cross-tenant-query-scoping.md)（表達力邊界；objective/min_events 驗證義務）。
- feature 追蹤 issue [#1092](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1092)（TRK-333；含完整對抗軌跡 triage 表）。
- [#657](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/657) recipe-preview（ratio 預覽解凍為 Phase 1 前置）、[#1008](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1008) fail-soft quarantine（繼承）。
- 告警驗證能力階梯的 deferred 端：[#1090](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1090)（chaos-for-alerts）、[#1091](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1091)（公開驗證 guide）。
