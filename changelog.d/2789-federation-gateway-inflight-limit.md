---
section: Added
topic: federation
issues: [2789]
created: 2026-11-22T12:00:00+08:00
---
- **federation-gateway 加上每個 token 的並發上限（helm；federation-gateway chart 0.9.0；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：實作 ADR-020 已決定的每 token 同時進行查詢數上限。新值 `inflightLimit.maxPerToken`（預設 4，`inflightLimit.enabled` 預設開）：同一個 token 在**一個 gateway 實例**上已有 4 個查詢在跑時，下一個回 429，稽核日誌記 `status: 429`、指標計入 `rate_limited`。上限是每實例：2 個副本合計 8、擴到 8 個副本合計 32。計數在每個 Envoy worker 各自進行、平均分配，`maxPerToken` 必須是 `concurrency` 的倍數，否則 render 失敗；預設 2 個 worker 時，同一個 worker 上最多 2 個。回應結束即釋放；client 中途斷線的查詢，名額要到 35 秒後才釋放。⚠️ 行為變更：原本會放行的並行查詢現在可能收到 429。
