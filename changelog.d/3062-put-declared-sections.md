---
section: Security
topic: tenant-api
issues: [3062]
created: 2026-10-17T04:10:00+00:00
---
- **⚠️ Breaking：`PUT /tenants/{id}` 只替換 body 宣告的租戶段落，同檔其他段落原樣保留（tenant-api；[#3062](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3062)）**：過去對 `{id}` 有寫入權就能刪改同檔其他租戶的段落。現在 body 宣告的每個租戶都要有寫入權（否則固定文字 403；PR 模式在最新 base 重判），宣告段落在寫入鎖內拼進現有檔，其他段落、root key 與註解保留；保留不住回 409 `not_round_trippable`，無法拼接的檔沿用 #2405。省略某段不再刪掉它。讀不到檔內全部租戶的呼叫者，anchor／alias 改以不比值的結構規則拒收。`/diff`、`/validate` 與 PUT 共用同一套拒收判定與順序。另一個行為改變：只能讀 `{id}`、不能寫的呼叫者，`/validate` 一律 `valid: false`（與其 PUT 被拒一致）；其 `/diff` 仍是遮罩預覽，檔內有讀不到的租戶時各種檔案形狀的拒收都回 422 `TENANT_FILE_WITHHELD`。細節見 [API 硬化 §3.1](docs/api/tenant-api-hardening.md)。
