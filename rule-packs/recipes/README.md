# Custom Alert Recipes（ADR-024 能力 B，#741）

平台 authored 的**參數化告警 recipe 庫**。各階層（platform / domain / tenant）填表即可自訂告警，**永不寫 PromQL**（守住宣告式地基）。

## 五個核心 recipe

| recipe | 用途 | 關鍵參數 |
|---|---|---|
| [`threshold`](threshold.yaml) | gauge 越過閾值 | `metric` `op` `window` `threshold` |
| [`rate`](rate.yaml) | counter 每秒增率越界 | `metric` `op` `window` `threshold` |
| [`ratio`](ratio.yaml) | 兩 counter 增率比值越界（除零安全） | `metric` `denominator_metric` `op` `window` `threshold` |
| [`absence`](absence.yaml) | 指標在時間窗內缺席（自我圈定） | `metric` `window` `threshold` |
| [`p99_latency`](p99_latency.yaml) | histogram 分位數延遲越界 | `metric` `quantile` `op` `window` `threshold` |

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
- **向量化（O(M)）**：相同 shape signature `(recipe, metric, op, window, quantile, denominator, selectors)` 的多租戶**共用一條規則**（`on(tenant) group_left`）；規則數 = shape 數，非租戶數。
- **shape slug = `recipe_id`**：去重鍵 + recording-rule 名 + alertname + `user_threshold` 上的選擇 label，是 Go↔Python 跨語言契約（見 [`tests/dx/fixtures/recipe_id_vectors.json`](../../tests/dx/fixtures/recipe_id_vectors.json)）。

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
