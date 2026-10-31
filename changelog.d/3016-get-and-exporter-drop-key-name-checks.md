---
section: Changed
topic: tenant-api
issues: [3016]
created: 2026-10-31T09:00:00+00:00
---
- **⚠️ breaking：GET 與 exporter 不再判「這個鍵有沒有人讀」（ADR-038 決定 11）**：`GET /tenants/{id}` 的 `validation_warnings` 不再含 `unknown key … not in defaults`、`unknown reserved key`、`unknown routing key`、`unknown base metric`、懸空的 `_critical` 這幾類，也不即時判；要看用 `da-tools explain-keys` 或 CI 的 validate-config，寫入時由鍵的判決擋下。平台檔與 profile 層的 notice 同樣拿掉這幾類（判決自己報平台側）。exporter 載入時不再印這幾類 WARN，改在 `config keys no row of this exporter reads` 一行列出它自己讀不到的鍵（`reason=unconsumed` 沒有任何一列讀、`reason=unregistered` 登錄表沒有讀取端的 `_` 鍵），不帶嚴重度，同一組沒變就不重印。值的檢查（`expires:`、保留鍵的值、`_profile`、`version` 標籤）照舊。
