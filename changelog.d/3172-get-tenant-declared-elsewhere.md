---
section: Added
topic: tenant-api
issues: [3172]
created: 2026-10-20T14:30:17+00:00
---
- **`GET /api/v1/tenants/{id}` 新增 `declared_elsewhere`（tenant-api；[#3172](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3172)）**：伺服器目前讀到的樹裡，除了租戶自己的檔還有別的檔宣告同一個 id 時為 `true`，也就是 409 `TENANT_DECLARED_ELSEWHERE` 判的條件；先前 `GET` 看不出這件事，只有 `/effective`、`/diff` 回 409。自己的檔也宣告時是重複宣告，threshold-exporter 拒收整棵樹的重載；只宣告在別處時 exporter 照常載入，但寫入守衛不讓寫進自己的檔。判定與 `/diff` 共用同一支函式與同一份讀取 walk，不保證寫入的回應（寫入可能先因別的原因被拒，PR 模式以最新 base 判）。只回布林，不點名檔案或其他租戶。walk 跑不了時欄位不出現，不是 `false`。
