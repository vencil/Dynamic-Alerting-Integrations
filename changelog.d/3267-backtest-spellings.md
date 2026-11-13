---
section: Changed
topic: da-tools
issues: [3267]
created: 2026-11-01T03:37:22+00:00
---
- **`backtest --tenant` 的數字只由 exporter 讀，拿掉工具自己的拼法表（[#3267](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3267)）**：數字的讀法只剩 exporter 的 `parseThresholdNumber`（經 `da-guard served-values`），工具不再另外判斷哪些寫法算數字。exporter 讀得懂的十進位寫法（`1e3`、`+.5`、`70:critical`）照它送的數字回測，JSON 的 `new_value`／`old_value` 是送出數字的文字（如 `"1000"`，型別仍是字串）；exporter 拒收的寫法（`1_000`、`0x1p4`、`1__0`、`0x10`、數字超過 20 位，見 [#3457](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3457)）照寫法列一列、不回測（UNKNOWN、結束碼 0），寫在舊值側（`--old-value`）或帶 `:critical` 後綴也一樣。
