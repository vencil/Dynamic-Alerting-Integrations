---
section: Added
topic: confd-family
issues: [3877]
created: 2026-11-14T03:43:17+00:00
---
- **`da-guard served-values --schedules-only`：整天的逐時段閾值，結果不隨執行時刻改變（da-guard；[#3877](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3877)）**：每個租戶只輸出 `schedules`，不輸出任何只在 `--at` 那一刻讀的欄位。`--at`（預設現在）落在 `/metrics` 無法 `Gather` 的時段時，`--schedules` 仍以 exit 2 結束，`--schedules-only` 則照樣輸出、該段帶 `error`，所以只判整天的讀取端不會因為在那段時間執行而變成「未判」。Python 端對應 `_lib_tenant_values.load_served_schedules`。不能與 `--disable-effects` 併用。
