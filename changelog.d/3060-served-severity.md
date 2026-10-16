---
section: Fixed
topic: exporter
issues: [3060]
created: 2026-10-16T08:22:40+00:00
---
- **`da-guard served-values --schedules`：有值的時段一律寫出 `severity`，空字串也照寫（[#3060](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3060)）**：租戶值寫成 `"70:"` 時 exporter 送出 severity 為空字串，`severities` 照實回 `""`，`schedules` 各段卻把 `severity` 欄位整個省略，與「`value` 為 `null` 時才省略」的契約不符。讀取端因此判定輸出格式不對，樹裡只要有一個這種值，讀 `--schedules` 的三支工具都以 exit 2 結束；現在可正常讀取，結束碼改由各自的判定決定：`backtest --config-dir` 為 0；`config-diff` 比對 `"70"` → `"70:"` 時列為閾值變更，為 1；`evaluate-policy` 照常評估，全部合規時為 0。`value` 為 `null` 的時段與帶 `error` 的時段仍不輸出 `severity`。
