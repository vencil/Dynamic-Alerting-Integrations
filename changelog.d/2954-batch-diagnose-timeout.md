---
section: Fixed
topic: da-tools
issues: [2954]
created: 2026-10-13T16:56:57+00:00
---
- **`batch-diagnose --timeout` 真的會中斷單一租戶的檢查（[#2954](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2954)）**：先前這個參數沒有被讀取，卡住的租戶檢查只受內部各次呼叫自己的上限約束（kubectl 每次 120 秒），整批跟著等。現在每個租戶的檢查有自己的期限，到期仍未完成就記為 `error`（`timed out after …s`），整批結束碼為 `2`（沒能完成檢查，與缺少 `kubectl` 同類），不再等它。預設值由 30 秒改為 60 秒，大於 diagnose 最多 4 次 Prometheus 查詢的總和，Prometheus 卡住時仍回報具體原因。`--timeout` 須大於 0、可帶小數，其餘以結束碼 `2` 拒絕。
