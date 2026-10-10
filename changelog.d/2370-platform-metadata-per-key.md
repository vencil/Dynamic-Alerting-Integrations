---
section: Changed
topic: confd-family
issues: [2370]
created: 2026-10-10T08:30:00+08:00
---
- **平台檔 `tenants:` 的 `_metadata` 逐鍵繼承（exporter／tenant-api；[#2370](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2370)）**：根目錄平台檔的 `tenants.<id>._metadata` 與租戶檔的 `_metadata` 逐鍵合併，同鍵租戶檔贏、多個平台檔依檔名後者贏，只有一邊寫的鍵保留（例如平台給 `environment`／`domain`／`owner`、租戶只寫 `db_type`，四個都在）。`/metrics` 的 `tenant_metadata_info`／`tenant_expected_exporter` 過去在租戶檔有任何 `_metadata` 時整塊捨棄平台層；tenant-api 租戶清單與搜尋的 `environment`／`domain`／`db_type`／`owner` 等欄位過去只讀租戶檔。兩邊現在都照這條合併規則讀這兩層；tenant-api 的清單、搜尋與寫入判定（現有檔案與新提交內容）用同一個合併結果。tenant-api 讀不到根目錄平台檔時：清單與搜尋把每一列的 metadata 視為未知——列上只有租戶檔自己的值並標 `metadata_incomplete: true`，可見性比照 degraded 列（只有 RBAC 規則不限制 environment／domain 的 caller 看得到），搜尋的 metadata 篩選不命中；寫入判定則只用租戶檔的 `_metadata`。`/effective` 仍不帶 `_metadata`。規則見 [config-driven](docs/design/config-driven.md)。
