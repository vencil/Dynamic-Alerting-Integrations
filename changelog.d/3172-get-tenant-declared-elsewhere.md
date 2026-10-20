---
section: Added
topic: tenant-api
issues: [3172]
created: 2026-10-20T14:30:17+00:00
---
- **`GET /api/v1/tenants/{id}` 新增 `declared_elsewhere`（tenant-api；[#3172](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3172)）**：conf.d 裡除了租戶自己的檔，還有別的檔也宣告同一個 id 時為 `true`。這時所有寫入回 409 `TENANT_DECLARED_ELSEWHERE`，threshold-exporter 也拒收整棵樹的重載，修改不會生效；先前 `GET` 看不出這件事，只有 `/effective`、`/diff` 回 409。判定與 `/diff` 共用同一支函式與同一份讀取 walk，只回布林，不點名檔案或其他租戶（這個 bit 早已由 `/effective` 的 409 公開）。walk 跑不了時欄位不出現，不是 `false`。
