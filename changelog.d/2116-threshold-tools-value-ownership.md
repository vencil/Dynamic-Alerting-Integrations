---
section: Changed
topic: confd-family
issues: [2116]
created: 2026-10-06T23:29:37+00:00
---
- **`threshold-recommend`、`threshold-govern`、`config-diff` 改讀生效值與來源檔（[#2116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2116)）**：⚠️ 行為變更：三支都經 `da-guard effective` 讀樹，看得到 `defaults:`、平台 `tenants:`、profile 與子目錄租戶。`threshold-recommend` 把繼承的 key 另列為「只供參考」；`--export-patch` 每行標出目前值的來源檔，繼承的 key 只以註解列出、不寫成值行。`threshold-govern` 只對租戶檔自己寫的 key 開 PR，寫回用檔案裡的拼法（含舊拼法 `mysql_cpu`）；繼承的 key 只列 INFO，不再觸發連續錯誤斷路；子目錄的租戶檔 tenant-api 讀不到，改記 `skipped_nested`、不送請求。`config-diff` 比對（值, 來源檔），`_defaults.yaml` 的變更列在每個繼承它的租戶下，值不變但換了來源檔報 `moved`（rc 1）。沒有 `tenants:` 的檔不再當租戶；找不到 da-guard、或有檔 exporter 載不進來時 exit 2 並轉出 da-guard 的 stderr。
