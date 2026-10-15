---
section: Fixed
topic: config-diff
issues: [3022]
created: 2026-10-15T00:32:57+00:00
---
- **config-diff 報出「開始／停止出列」的閾值，即使寫入的數字沒變（[#3022](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3022)）**：租戶寫了但 `/metrics` 沒出列的 key（served-values 的 `unserved`），以前和出列的數值直接比，`'85'` 和 `85` 被當成相同，於是 base 補上或拿掉、告警因而開始或停止生效時，閾值表一列都不報。現在一側出列、另一側沒出列的 key 報成 `added`／`removed`；沒出列的那側在表裡標 `(not served)`，JSON 該筆多一個 `not_served` 欄位（新增欄位，既有欄位不變）。
