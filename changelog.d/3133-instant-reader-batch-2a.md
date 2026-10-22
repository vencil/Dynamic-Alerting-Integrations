---
section: Changed
topic: da-tools
issues: [3133]
created: 2026-10-22T18:00:00+00:00
---
- **⚠️ 結束碼變更：instant 查詢沒得到回答時，threshold-recommend、baseline-discovery、diagnose、validate-migration 不再當成「沒有資料」（[#3133](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3133) 第二批）**：這幾支改用 `_lib_prometheus.prometheus_instant`，連不上、HTTP 錯誤、`error` 狀態、回應形狀不對或樣本值讀不懂，都是沒量到。threshold-recommend 把該 key 標 `query_error`、其他 key 照常推薦，報告 `status: partial`，結束碼 0 → 2；baseline-discovery 失敗的點記空值、報告與 CSV 照出，摘要 CSV 的 `note` 與 stdout 寫「查詢失敗 N/M 次，本列不完整」而不是「樣本不足」，結束碼 0 → 2；diagnose 的維護／靜默模式查詢失敗時不再報 `normal`，`operational_mode` 為 `unknown`、`issues` 含 `Prometheus query failed`，結束碼 0 → 2，batch-diagnose 隨之在任一租戶如此時結束碼 0 → 2；validate-migration 遇到讀不懂的樣本值不再報成單邊缺值，結束碼 1 → 2。cardinality-forecasting 移除沒有呼叫端的 `query_scrape_series_added`。
