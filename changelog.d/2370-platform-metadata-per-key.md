---
section: Changed
topic: confd-family
issues: [2370]
created: 2026-10-10T08:30:00+08:00
---
- **平台檔 `tenants:` 的 `_metadata` 在各平面一致地逐鍵繼承（exporter／tenant-api；[#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370)）**：根目錄平台檔的 `tenants.<id>._metadata` 與租戶檔的 `_metadata` 逐鍵合併，同鍵租戶檔贏、多個平台檔依檔名後者贏，只有一邊寫的鍵保留（例如平台給 `environment`／`domain`／`owner`、租戶只寫 `db_type`，四個都在）。`/metrics` 的 `tenant_metadata_info`／`tenant_expected_exporter` 過去在租戶檔有任何 `_metadata` 時整塊捨棄平台層；tenant-api 租戶清單與搜尋的 `environment`／`domain`／`db_type`／`owner` 等欄位過去只讀租戶檔。兩者現在給出同一份值，tenant-api 所有讀取 `_metadata` 的地方（清單、搜尋、寫入時讀的現有與新提交內容）都用同一個合併結果；平台檔讀不到時，這些地方只用租戶檔的 `_metadata`。`/effective` 仍不帶 `_metadata`。規則見 [config-driven](docs/design/config-driven.md)。
