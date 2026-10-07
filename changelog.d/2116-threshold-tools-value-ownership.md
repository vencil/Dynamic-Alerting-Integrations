---
section: Changed
topic: confd-family
issues: [2116]
created: 2026-10-06T23:29:37+00:00
---
- **`threshold-recommend`、`threshold-govern` 改讀生效值與來源檔（[#2116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2116)）**：⚠️ 行為變更：兩支都經 `da-guard effective` 讀樹，看得到 `defaults:`、平台 `tenants:`、profile 與子目錄租戶。租戶檔自己寫的 key 照常推薦；繼承的 key 只列目前值與來源檔，不查 Prometheus、不產生推薦值。`--export-patch` 每行標出目前值的來源檔，繼承的 key 只以註解列出。`threshold-govern` 只對租戶檔自己寫的 key 開 PR，寫回用檔案裡的拼法（含舊拼法 `mysql_cpu`）；繼承的 key 不再觸發連續錯誤斷路；子目錄的租戶檔 tenant-api 讀不到，改記 `skipped_nested`、不送請求。沒有 `tenants:` 的檔不再當租戶；找不到 da-guard、或有檔 exporter 載不進來時 exit 2 並轉出 da-guard 的 stderr。已知限制：根 `defaults:` 與租戶同時寫 `X_critical` 的樹，`da-guard served-values` 會拒收，兩支都 exit 2。
