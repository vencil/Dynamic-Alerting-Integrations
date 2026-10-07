---
section: Changed
topic: confd-family
issues: [2115]
created: 2026-10-07T14:03:45+00:00
---
- **租戶元資料（`generate_tenant_metadata.py`、`platform-data.json` 內嵌的 `tenant_metadata`）改讀 exporter 實際發出的值（[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：租戶清單、`_metadata` 與各欄推斷改用 `da-guard served-values`：子目錄的租戶、平台檔 `tenants:` 寫的 owner 都會出現。⚠️ 行為變更：`rule_packs` 推斷與 `metric_count` 改看 Go 發出的鍵（含從 `defaults:` 繼承的），結果會變——`rule_packs` 的意思是「哪些 pack 對這個租戶有閾值」，根 defaults 的鍵每個租戶都算；`db_type` 不再從鍵名推斷，只取 `_metadata.db_type` 的宣告值（與 exporter 的存活監測、tenant-api 同一個定義），沒宣告就是空字串；`operational_mode` 改看 exporter 的判定（過期的維護時間盒不算維護）。exporter 丟掉／讀不到的檔、找不到 da-guard 時結束碼 2 並轉出 da-guard 的 stderr，不再照讀或產出缺租戶的檔。從此需要 da-guard：`make platform-data`／`make lint-docs` 在 `DA_GUARD_BINARY` 未設時先建 `.build/da-guard`，pre-commit 的 `platform-data-check` 也是（沒有 Go 則 ERROR）。
