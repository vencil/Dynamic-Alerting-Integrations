---
section: Changed
topic: exporter
issues: [3046]
created: 2026-11-05T09:57:56+00:00
---
- **⚠️ BREAKING：閾值鍵寫 `disable` 連帶關掉同族的 `_critical` 與維度鍵（[#3046](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3046)）**：`X: disable` 之後，`X_critical`、租戶的 `X{…}` 與根 `defaults:` 給的 `X{…}` 預設值都不再送出，不論同族鍵來自哪一層；排程式 `disable` 只在那段時間連帶；根 `defaults:` 自己給值的 `X_critical` 是獨立閾值，不連帶。過去寫 `X: disable` 加 `X_critical: N` 想只留 critical 的，改寫成 `X: "N:critical"`，並**刪掉** `X_critical`（兩者並存會送出同一條 series，整份 `/metrics` 失敗）。`/effective` 的 `not_served` 與 `da-guard served-values` 的 `keys`／`schedules` 以新原因 `base_disabled` 標出被連帶關掉的鍵；共用判決視這些鍵為已讀，不報。過去照建議寫成 `X: disable` 加 `X_critical: disable` 的，`X_critical` 那行現在什麼也不關，報 `disable_noop`（warning），可刪掉。`disable_partial`（warning）與 served-values 的 `partial` 效果、`served_family`／`served_from` 欄位一併移除。
