---
section: Fixed
topic: confd-reader-consistency
issues: [2830]
created: 2026-10-10T14:59:59+00:00
---
- **portal 的租戶清單不再依租戶名猜 environment（`generate_tenant_metadata`；[#2830](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2830)）**：`_metadata` 沒寫 environment 的租戶，過去會依名稱（`prod-`、`staging-`、`dev-` 開頭，或含 `production`／`staging`／`development`）補上 environment，並分進對應的 `tenant_groups`。exporter 的 `tenant_metadata_info` 與 tenant-api 的清單只讀 `_metadata`，而 portal 在 tenant-api 不可用時會退回讀 `platform-data.json`，同一個租戶因此在兩處顯示不同的 environment。現在與 `db_type` 一樣只取 `_metadata` 的值，沒寫就是空的。
