---
section: Added
topic: tenant-api
issues: [3157]
created: 2026-10-20T14:55:00+00:00
---
- **租戶列表標示 API 管理或 git 管理（tenant-api；[#3157](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3157)）**：`GET /tenants` 與 `/tenants/search` 的每一列多了 `managed_by`（`api`／`git`），`git` 時另帶 `git_managed_reason`，與同一份檔的 `GET /tenants/{id}` 判定、代碼相同。檔名與宣告的 id 不同的檔，過去在列表裡是一列不帶任何標記的空資料，現在標 `id_mismatch`；列表仍不回宣告的 id。讀不到檔的降級列不帶這兩個欄位。細節見 [API 硬化 §3.1](docs/api/tenant-api-hardening.md)。
