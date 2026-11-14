---
section: Added
topic: confd-family
issues: [3877]
created: 2026-11-14T03:43:17+00:00
---
- **`da-guard served-values --schedules-only`：整天的逐時段閾值，不因 `--at` 是一天中的幾點而改變（da-guard；[#3877](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3877)）**：每個租戶只輸出 `schedules`，不輸出 `values` 這類只在 `--at` 那一刻讀的租戶欄位（文件頂層的 `at` 照舊）。`--at`（預設現在）落在 `/metrics` 無法 `Gather` 的時段時，`--schedules` 仍以 exit 2 結束，`--schedules-only` 則照樣輸出、該段帶 `error`，所以只判整天的讀取端不會因為在那段時間執行而變成「未判」。`--at` 仍決定 `expires:` 是否已過期：到期時刻落在兩次執行之間，兩次的結果就會不同。Python 端對應 `_lib_tenant_values.load_served_schedules`。不能與 `--schedules` 或 `--disable-effects` 併用（exit 2）。
