---
section: Fixed
topic: alertmanager-routing
issues: [2839]
created: 2026-10-11T08:34:24+00:00
---
- **`test-notification` 遇到非字串的 `receiver.type` 不再 traceback（[#2839](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2839)）**：租戶主 route、`overrides` 或 `routes` 的 `receiver.type` 寫成 mapping 或 list（例如 `{a: 1}`、`[webhook]`）時，`notification_tester` 先前以 `TypeError: unhashable type` 中止，連 `--dry-run` 也一樣，其他租戶都沒被測到。現在讀 type 一律經路由產生器同一個 `receiver_type()`（[#2780](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2780)），該 receiver 回報 `INVALID_CONFIG`、明細為 `receiver type must be a string, got <型別>`，其餘 receiver 與租戶照常測試。`0`、`false`、`{}`、`[]` 這類非字串的 type 先前被當成沒寫 type 而略過（`--ci` 照樣綠），現在同樣回報 `INVALID_CONFIG`；沒寫 type、`type: ~` 與 `type: ""` 仍視為沒有 receiver。⚠️ `type: 5` 這類整數先前報 `unknown receiver type '5'`，現在改報同一則「必須是字串」訊息。
