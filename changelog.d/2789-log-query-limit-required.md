---
section: Changed
topic: log-federation
issues: [2789]
created: 2026-11-22T12:00:00+00:00
---
- **⚠️ 租戶日誌查詢 `/select/logsql/query` 現在必須帶 `limit`（helm；federation-gateway chart 0.9.0；ADR-021；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：這是租戶看得到的行為變更，先前會被轉送的請求現在會被 gateway 拒絕。`victorialogs` mode 要求剛好一個 `limit`，值為 `1` 到新 chart 值 `victorialogs.maxQueryLimit`（預設 `1000`），URL 與 form-urlencoded POST body 合計計算；沒帶、空值、`0`、非整數、超過上限或重複出現回 `400`，不補值也不調降。對 `/query` 的 POST body 若不是 form-urlencoded 或帶 `Content-Encoding` 回 `415`。其他端點與其他 mode 不變。升級前請通知租戶，並確認 Grafana 等工具送出的筆數在上限內。
