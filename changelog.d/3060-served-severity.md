---
section: Fixed
topic: exporter
issues: [3060]
created: 2026-10-16T08:22:40+00:00
---
- **`da-guard served-values --schedules`：有值的時段一律寫出 `severity`（[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)）**：先前 `schedules` 的時段遇到空字串 severity 會把 `severity` 欄位整個省略，與「`value` 為 `null` 時才省略」的契約不符，讀取端判定輸出格式不對，讀 `--schedules` 的三支工具（`backtest --config-dir`、`config-diff`、`evaluate-policy`）以 exit 2 結束。現在有值的時段一律帶 `severity`，結束碼由各工具自己的判定決定。空字串 severity 原本來自 `"70:"` 這種空後綴；[#3071](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3071) 起空後綴讓整個值讀不懂、改送平台預設值與 `warning`，所以 `"70"` → `"70:"` 在 `config-diff` 是 70 → 預設值的閾值變更（exit 1），`backtest --config-dir` 也照這個值的變更回測。`value` 為 `null` 的時段與帶 `error` 的時段仍不輸出 `severity`。
