---
section: Changed
topic: exporter
issues: [3046]
created: 2026-11-05T09:57:56+00:00
---
- **⚠️ BREAKING：閾值鍵寫 `disable` 連帶關掉同族的 `_critical` 與維度鍵（[#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)）**：`X: disable` 之後，`X_critical`、租戶的 `X{…}` 與根 `defaults:` 給的 `X{…}` 預設值都不再送出，不論同族鍵來自哪一層；排程式 `disable` 只在那段時間連帶；只在 `dimensions:` 宣告的 `X` 也算 base，子目錄寫的 `disable` 會傳到租戶。根 `defaults:` 自己給值的 `X_critical` 是獨立閾值，不連帶。想只留 critical：改寫成 `X: "N:critical"` 並**刪掉** `X_critical`（兩者並存整份 `/metrics` 失敗）。租戶自己檔裡的同族鍵被連帶關掉時報 `disable_cascade`（warning）；舊建議的 `X_critical: disable` 現在報 `disable_noop`。被連帶關掉的鍵在 `not_served` 與 served-values 標 `base_disabled`、判為已讀；`disable_partial` 與 `partial`、`served_family`／`served_from` 移除。
