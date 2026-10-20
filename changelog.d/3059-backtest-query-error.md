---
section: Fixed
topic: da-tools
issues: [3059, 3074, 3133]
created: 2026-10-16T08:19:07+00:00
---
- **`backtest`：查詢沒量成不再報成「沒資料」，三條 rc 2 路徑在 `--json` 下補上 envelope（[#3059](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3059)、[#3074](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3074)）**：Prometheus 連得上、但查詢失敗（HTTP 400／503、逾時），或回 `success` 但形狀不是 matrix（缺 `data`、某一列既沒有 `values` 也沒有 `histograms` 等）的變更改標 `query_error`（訊息帶錯誤說明），其他變更照跑；有任何一筆就結束碼 2（優先於 HIGH 的 1，`--skip-if-unavailable` 也不轉成 0），`--json` 頂層帶 `status: "partial"`、`reason: "query_error"`，報告多一個 `query_error` 計數。⚠️ 行為變更：先前這種變更是 `no_data`（或該列被略過）、結束碼 0。Prometheus 連不上、`--tenant` 缺參數、沒有指定變更來源三條 rc 2 路徑，`--json` 下改輸出 `caller_error` envelope；後兩者改在 Prometheus 檢查之前判，帶 `--skip-if-unavailable` 又剛好連不上時，結束碼由 0 改為 2。
