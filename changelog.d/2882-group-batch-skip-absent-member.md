---
section: Fixed
topic: tenant-api
issues: [2882]
created: 2026-10-11T10:54:28+00:00
---
- **群組 batch 不再建回已不存在的成員（tenant-api；[#2882](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2882)）**：退租只刪 conf.d 的租戶檔、不清群組成員，先前 `POST /api/v1/groups/{id}/batch` 帶 `patch` 會把這種成員的租戶檔重新建出來並回成功，exporter 隨之重新送它的 series。現在在寫入鎖內、以寫入所依據的 base（PR 模式為最新 origin base）判定，不存在的成員結果為 `status: skipped`、`message: "tenant does not exist"`，不寫入、不進 PR；`summary` 另計 skipped（`1 succeeded, 1 skipped`），async 的 `GET /tasks/{id}` 同一說法。`POST /tenants/batch` 指名新租戶照常建立。
