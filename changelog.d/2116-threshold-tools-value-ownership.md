---
section: Changed
topic: confd-family
issues: [2116]
created: 2026-10-06T23:29:37+00:00
---
- **`threshold-recommend`、`threshold-govern` 改讀生效值與來源檔（[#2116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2116)）**：⚠️ 行為變更：兩支改經 `da-guard effective` 讀樹，不再只讀根目錄租戶檔。繼承自 `defaults:`、平台 `tenants:`、profile 的 key 現在列出目前值與來源檔，不查 Prometheus、不產生推薦值、不開 PR（`--json` 的 `inherited[].confidence` 為 `n/a`）；`--export-patch` 每行標出目前值的來源檔，繼承的 key 只以註解列出；租戶名或 key 不是一般名稱（含換行、非 ASCII，或 YAML 會讀成 null／布林／數字／日期的字，如 `null`、`true`、`010`、`2024-01-01`）時不產生值行，改以 `(not written)` 註解列出推薦值，需手動改租戶檔。租戶檔寫舊拼法（如 `mysql_cpu`）時現在會被推薦與治理，寫回用檔案裡的拼法。子目錄的租戶現在看得到；`threshold-govern` 不對它送請求（tenant-api 只讀設定目錄最上層的租戶檔），記為 `skipped_nested`，不算錯誤、不佔 `--max-prs`。沒有 `tenants:` 的檔不再當租戶。以下情況 exit 2（原本 exit 0 或照常執行）：空的 `--config-dir`（排程跑 `threshold-govern` 的 Job 會由成功變失敗）、找不到 da-guard、有檔 exporter 載不進來（轉出 da-guard 的 stderr），以及根 `defaults:` 與租戶同時寫 `X_critical` 的樹（`da-guard served-values` 拒收）。
