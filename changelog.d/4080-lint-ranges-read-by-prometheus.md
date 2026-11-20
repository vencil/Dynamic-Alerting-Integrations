---
section: Changed
topic: tools
issues: [4080]
created: 2026-11-20T00:27:45+00:00
---
- **`lint` 的範圍與函式檢查改照 Prometheus 的讀法：換個寫法不再繞得過 `max_range_duration`／`denied_functions`（tools；[#4080](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4080)）**：⚠️ breaking。運算式改由 da-read 的新子命令 `promql-reads` 讀，不再用正規式掃文字。先前 `[1w]`、`[1y]`、`[1h30m]`、`[ 2h ]`、`[(30d)]` 與所有子查詢都不檢查，`topk by (a) (…)` 也繞得過禁用清單；標籤值裡的 `[2h]` 與字串、註解裡的函式名則會誤報。現在比的是每個 selector 讀取的時間窗：自身 range 加上外層子查詢的 range（帶 `@` 的 selector 只計自身 range，帶 `@` 的子查詢不計它外層的子查詢），`offset` 與 lookback delta 不計。所以在 1h 上限下，`max_over_time(rate(x[5m])[1h:1m])`（1h5m）會被擋。`denied_functions` 以名字比對函式與聚合運算子。Prometheus 無法求值的運算式（例如 `rate(x[step()])`）列為 ERROR。
