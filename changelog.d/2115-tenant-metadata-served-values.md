---
section: Changed
topic: confd-family
issues: [2115, 4109, 4141]
created: 2026-10-07T14:03:45+00:00
---
- **租戶元資料（`generate_tenant_metadata.py`、`platform-data.json` 內嵌的 `tenant_metadata`）改讀 exporter 實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：租戶清單、`_metadata` 與各欄推斷改用 `da-guard served-values`：子目錄的租戶、平台檔 `tenants:` 寫的 owner 都會出現。⚠️ 行為變更：`metric_count` 改看 Go 在一天中任一時段發出的鍵（含從 `defaults:` 繼承的），根 defaults 的鍵每個租戶都算（`rule_packs` 已拿掉，#4141）；`db_type` 改名 `expected_exporters`（列表），不再從鍵名推斷，只取租戶 `_expected_exporters` 的宣告（與 exporter 的存活監測、tenant-api 同一個定義），沒宣告就是 `[]`；⚠️ `operational_mode` 拿掉：維護（含 `recurring:` 時窗）、靜音是某一刻的狀態，由 tenant-api 的 `config_derived` 在被問時推算，portal 離線時顯示 `unknown`（#4109）。只剩 `expires:` 依時刻判，檔內 `generated` 記下這個時刻，`--check` 以它重算——檔案沒變，drift gate 不會只因時間經過而轉紅；有 `/metrics` 收集不了的時段時不論幾點都結束碼 2 並點名。exporter 丟掉／讀不到的檔、找不到 da-guard 時結束碼 2 並轉出 da-guard 的 stderr，不再照讀或產出缺租戶的檔。從此需要 da-guard：`make platform-data`／`make lint-docs` 在 `DA_GUARD_BINARY` 未設時先建 `.build/da-guard`，pre-commit 的 `platform-data-check` 也是（沒有 Go 則 ERROR）。
