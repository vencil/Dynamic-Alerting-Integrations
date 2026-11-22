---
section: Changed
topic: tools
issues: [3924]
created: 2026-11-22T04:37:43+00:00
---
- **⚠️ breaking：產生器與範例不再寫登錄表的閾值鍵（da-tools init、scaffold、Helm chart、範例樹；[#3924](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3924)）**：登錄表的鍵都內建在讀取端（平台值或 off），所以 `da-tools init`／`scaffold` 產生的根 `_defaults.yaml` 不再寫任何閾值鍵的值、也不再列 `optional_overrides:`；租戶檔不再預填 `<鍵>_critical`，互動 scaffold 按 Enter 不寫值。Helm chart 的 `thresholdConfig.defaults` 改為空：平台值改由 exporter 內建，**chart 與 exporter 映像要一起升級**（沒有內建層的舊映像讀到空的 defaults 就不送平台值）；同理，新版 `da-tools init`／`scaffold` 產生的樹也要配有內建層的 exporter——舊映像上，平台值不送，沒在根目錄列出的鍵租戶寫了也不送。既有的樹不受影響；沒有平台值的鍵由租戶給值，有哪些看 `validate-config` 的 `platform_defaults` 列。rule pack 標頭的閾值段改按 arming 分兩段。
