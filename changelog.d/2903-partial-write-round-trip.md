---
section: Changed
topic: tenant-api
issues: [2903]
created: 2026-10-15T13:58:02+00:00
---
- **部分更新不再重寫無法無損往返的租戶檔（ADR-036 §4；[#2903](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2903)）**：批次 patch 與 `PUT .../custom-alerts` 把變更合併進現有租戶檔再整份重新輸出。檔裡有多份 YAML 文件（重新輸出只寫第一份），或最上層、`tenants`、該租戶 block 裡有別名當鍵或 `<<` merge key（合併依鍵的原文找鍵，例如刪掉 `_routing` 後 `<<` 帶來的那一個還在）時，改為合併前就拒收：`TENANT_CONFIG_NOT_LOADABLE`（custom-alerts 回 409，批次是該筆結果），`config_error` 為 `multiple_documents`／`alias_or_merge_key`。以整檔 `PUT /tenants/{id}` 或在 git 改寫該檔即可。
