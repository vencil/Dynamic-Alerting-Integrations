# Custom Alert Recipes（ADR-024 能力 B，#741）

平台 authored 的**參數化告警 recipe 庫**。各階層（platform / domain / tenant）填表即可自訂告警，**永不寫 PromQL**（守住宣告式地基）。

## 七個核心 recipe

| recipe | 用途 | 關鍵參數 |
|---|---|---|
| [`threshold`](threshold.yaml) | gauge 越過閾值 | `metric` `op` `window` `threshold` |
| [`rate`](rate.yaml) | counter 每秒增率越界 | `metric` `op` `window` `threshold` |
| [`ratio`](ratio.yaml) | 兩 counter 增率比值越界（除零安全） | `metric` `denominator_metric` `op` `window` `threshold` |
| [`absence`](absence.yaml) | 指標在時間窗內缺席（自我圈定） | `metric` `window` `threshold` |
| [`p99_latency`](p99_latency.yaml) | histogram 分位數延遲越界 | `metric` `quantile` `op` `window` `threshold` |
| [`forecast`](forecast.yaml) | 線性預測 gauge/餘量比例在提前量內越界（趨勢/耗盡） | `metric` `capacity_metric` `op` `horizon` `threshold` |
| [`slo_burn_rate`](slo_burn_rate.yaml) | SLO 錯誤預算 burn-rate（multi-window AND；fast→critical、slow→warning） | `metric` `denominator_metric` `objective` `slo_period` `min_events` |

每個 `.yaml` 是該 recipe 的**治理契約**（參數、emitted PromQL shape、範例）；可執行形式在
[`scripts/tools/dx/custom_alerts/recipes.py`](../../scripts/tools/dx/custom_alerts/recipes.py)（兩者由
[`tests/dx/test_compile_custom_alerts.py`](../../tests/dx/test_compile_custom_alerts.py) drift-guard）。

## 宣告語法（`_custom_alerts`）

宣告在 `conf.d/` 的哪一層決定 **scope**：

```yaml
# tenant leaf（只作用該租戶）
tenants:
  shop-a:
    _custom_alerts:
      - recipe: rate
        name: http_5xx_spike        # 租戶自訂名（scope 內唯一，顯示用）
        metric: http_requests_total
        selectors_re: {status: "5.."}   # 正則 label 過濾（=~）
        selectors: {method: "POST"}     # 精確 label 過濾（=）
        op: ">"
        window: 5m
        threshold: "50:warning"     # value:severity（severity 預設 warning）

# _defaults.yaml 頂層（platform L0 / domain L1 → 蓋整子樹，租戶不可覆寫）
_custom_alerts:
  - {recipe: absence, name: heartbeat_gone, metric: app_heartbeat_total, window: 10m, threshold: "0:critical"}
```

## 安全與向量化

- **防注入**：`metric` 嚴格 `^[a-zA-Z_][a-zA-Z0-9_]*$`（禁冒號/括號/運算子）；label 過濾只能走 `selectors`/`selectors_re`，由編譯器組裝 + value 跳脫。保留 label（`tenant`/`version`/`severity`/…）禁設。
- **向量化（O(M)）**：相同 shape signature `(recipe, metric, op, window/horizon, quantile, denominator/capacity, selectors, for, min_events)` 的多租戶**共用一條規則**（`on(tenant) group_left`）；規則數 = shape 數，非租戶數。`for` 入 signature（控制平面靜態屬性）；`forecast` 用 `horizon`（推導 lookback）取代 `window`；`slo_burn_rate` 無 `window`（burn 窗固定屬 recipe 語意）、`objective`/`slo_period` 走資料平面**不入 shape**（改值不 re-slug），`min_events` 入 shape（字面值寫進規則）。
- **shape slug = `recipe_id`**：去重鍵 + recording-rule 名 + alertname + `user_threshold` 上的選擇 label，是 Go↔Python 跨語言契約（見 [`tests/dx/fixtures/recipe_id_vectors.json`](../../tests/dx/fixtures/recipe_id_vectors.json)）。

## slo_burn_rate 補充

### 運營：通知風暴抑制與吵度回顧

- **infra 事故時的通知風暴抑制（選用）**：`slo_burn="true"` 標籤是為此準備的 inhibit target discriminator——可操作範本與「為什麼不預設出貨」的取捨見[設計文件 §Alertmanager inhibit_rules 範本](../../docs/design/config-driven.md#27-三態運營模式-operational-modes)。量測永不抑制：inhibit 只擋通知，recording 與 `ALERTS` 評估照常。
- **告警吵度回顧**：讀 `ALERTS` 歷史的三條 documented queries（吵度排行／episode 數／風暴群聚）見[告警系列篇三 §6.1](../../docs/alerting-best-practices.md#61-告警吵度回顧三條-documented-queries)；SLO burn 告警可用 `{slo_burn="true"}` 過濾單看。

### latency / freshness 型 SLI：instrumentation 先行（recipe 支援 deferred）

v1 的 `slo_burn_rate` 只接 availability／error-ratio 型（兩個 counter 的比率）。latency 與 freshness 型的 **recipe 支援是 defer-with-trigger**（首個客戶需求觸發，見 [ADR-031 §Deferred](../../docs/adr/031-slo-burn-rate-recipe.md#deferred附-trigger)），但 instrumentation 可以現在就鋪——等 recipe 到位時資料已在：

- **latency 型（如「95% 請求 < 300ms」）**：宣告介面的 `selectors` 同時套用分子分母，histogram 的 `le` 會污染分母——正解不是等 recipe 吃 histogram，而是**在源頭產出 counter 對**：instrument 一個 `*_slow_requests_total`（超過門檻的請求數）counter，與既有 `*_requests_total` 配對。**這其實是今天就能用的 workaround**：把「慢」定義成「壞事件」，availability 型宣告（`metric: *_slow_requests_total` / `denominator_metric: *_requests_total`、`objective: "95"`）現在就能算 latency SLO 的 burn rate——代價是門檻（300ms）燒死在 instrumentation、改門檻要改 code。
- **freshness 型（如「資料不超過 N 分鐘未更新」）**：本質是 timestamp 差值、非 counter 比率。鋪法三選一：K8s 內 batch job 完成時寫 completion-timestamp gauge 到 node-exporter **textfile collector**（job 寫 `.prom` 檔）；無常駐 exporter 的短命 job 用 **Pushgateway**；log-only 系統用 **Vector `log_to_metric`** transform 從完成 log 派生 gauge。**誠實邊界：completion-timestamp gauge 今天接不上任何 recipe**——「太舊」語意需要 `time() - gauge > N` 的時間差運算，recipe 宣告介面沒有它（這正是 freshness 型被 deferred 的原因；epoch timestamp 對靜態閾值恆真或恆假＝死告警）。過渡期要告警，得由平台側 raw rule 承接 `time() - gauge > N`，或改鋪「持續刷新的 age gauge」（值＝現在−完成時刻，需常駐程序計算）再配 `threshold` recipe。先鋪 timestamp gauge 的價值在於**資料先落 TSDB**——未來 freshness recipe 會直接消費同一 gauge。

## 編譯

```bash
make custom-alerts-compile   # _custom_alerts 宣告 → 向量化規則（編譯器）
```

行為驗證（promtool fire/no-fire/隔離 golden）由 pytest 驅動：
`tests/dx/test_custom_alerts_promtool.py` 編譯 [`examples/conf.d/`](examples/conf.d/) → temp pack → 跑 `promtool test rules`（promtool 不在時自動 skip）。

> **S1+S2 範圍 = 編譯器本身**（compiler + recipe 庫 + schema + 測試）。**刻意不 commit 已部署的 pack**：
> repo 的 #731 closed-label 契約（`rulepack_contract_test.go` 只准 `user_threshold` selector 用
> `{component,metric,severity,version,tenant}`）證明「committed pack」與「exporter emit
> `user_threshold{recipe_id=...}`」結構性耦合——故**產生並部署 pack（configmap + operator CRD）
> + exporter emission + 最終 label 形態整包留 S3**（連同 Go `validReservedKeys` 註冊 + 契約更新）。
> 完整 epic 拆解見 [#741](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/741) / [ADR-024](../../docs/adr/024-version-aware-threshold-via-dimensional-label.md)。
