---
section: Changed
topic: da-tools
issues: [3304, 3331]
created: 2026-10-24T08:00:00+00:00
---
- **Prometheus 回應的形狀照規格讀：樣本值必須是字串、時間戳必須是數字；rule group 必須帶 `rules`（[#3304](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3304)、[#3331](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3331)）**：`_lib_prometheus` 的 `sample_value` 只收 JSON 字串（Prometheus、VictoriaMetrics、Mimir 都這樣寫），新增 `sample_timestamp` 只收非 bool 的 JSON 數字；instant 與 range 查詢的每個樣本（含 native histogram 的時間戳）都經這兩支檢查，不符即 `prometheus_query_error`。`rules_groups` 要求每個 group 帶字串 `name` 與 `rules` 這個 key（Prometheus 一定會寫），`rules: null` 讀成空的 group（Go 以 nil slice 輸出的空 group，例如 Thanos），rule 的 `lastError` 存在時須為字串、`alerts` 存在時須為陣列。⚠️ 行為變更：回應中出現數字樣本值或非數字時間戳，讀取的工具 `0` → `2`（byo-check Step 2 由 pass 改為讀取失敗）；rule group 缺 `rules`，byo-check／federation-check central／runtime-audit `0`（runtime-audit `--ci` `1`）→ `2`；`rules: null` 由 `2` 改為 `0`（runtime-audit `--ci` 依結果）；group `name` 不是字串、rule `lastError` 不是字串、`alerts` 不是陣列，三支工具一律 `2`（先前依工具是 crash、或讀成 `0`／`1`）。
