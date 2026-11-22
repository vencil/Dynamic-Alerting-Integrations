---
section: Added
topic: federation
issues: [2789]
created: 2026-11-22T12:00:00+08:00
---
- **federation-gateway 加上每個 token 的並發上限（helm；federation-gateway chart 0.9.0；[#2789](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2789)）**：實作 ADR-020 已決定的每 token 同時進行查詢數上限。新值 `inflightLimit.maxPerToken`（預設每實例 4，`inflightLimit.enabled` 預設開），超過回 429，稽核日誌記 `status: 429`、指標計入 `rate_limited`。計數在每個 Envoy worker 各自進行：每個 worker 分到 `maxPerToken ÷ concurrency`（捨去、至少 1），單一連線在預設設定下只有 2 個，整個實例最多 `concurrency ×` 每 worker 名額（預設 4；`concurrency` 為 8 時是 8）；多副本再乘上副本數。gateway 自己回的 403／503 不佔名額。回應結束即釋放；client 中途斷線的名額 35 秒後才釋放。⚠️ 行為變更：原本會放行的並行查詢現在可能收到 429。
