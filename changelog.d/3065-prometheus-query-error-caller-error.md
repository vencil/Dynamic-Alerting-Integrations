---
section: Fixed
topic: da-tools
issues: [3065, 3074, 3133]
created: 2026-10-16T08:20:02+00:00
---
- **`alert-quality` 查詢 Prometheus 沒量成改為結束碼 2，`cardinality-forecast` 分得出沒量成與沒有資料（tools；[#3065](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3065)、[#3074](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3074)）**：⚠️ 破壞相容——`alert-quality` 的 `ALERTS` 查詢失敗（連不上、HTTP 非 2xx、`status` 非 success、回應不是 JSON 物件，或回 `success` 但形狀不是 matrix，例如缺 `data`、某一列既沒有 `values` 也沒有 `histograms`）以前當成期間內沒有警報，報告全 GOOD、rc 0；現在不論有無 `--ci` 都是 rc 2，`--json` 印 `status: caller_error`、`reason: prometheus_query_error` 與 `error` 原文。`cardinality-forecast` 同樣情況以前在 `--json` 與沒有資料同為 `no_data`／`no_cardinality_data`（rc 2），形狀不對的列則被略過（rc 0）；⚠️ 現在一律 `status: caller_error`、`reason: prometheus_query_error`、rc 2。只有回空 matrix 才是 `no_data`／`no_cardinality_data`。
