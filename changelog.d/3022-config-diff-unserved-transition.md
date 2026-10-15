---
section: Fixed
topic: config-diff
issues: [3022]
created: 2026-10-15T00:32:57+00:00
---
- **config-diff 報出「開始／停止出列」的閾值，即使寫入的數字沒變（[#3022](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3022)）**：租戶寫了但 `/metrics` 沒出列的 key（served-values 的 `unserved`），以前用寫入原文和出列的數值比，`'85'` 和 `85` 被當成相同，於是 base 補上或拿掉、告警因而開始或停止生效時，閾值表一列都不報。現在這類 key 讀作 `85 (not served)`，轉換以 `modified` 列出；值為 `disable` 的照原文呈現。
