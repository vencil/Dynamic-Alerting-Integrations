---
section: Added
topic: tenant-api
issues: [2341]
created: 2026-10-02T14:09:33+00:00
---
- **tenant-api 的 batch 新增 `unset`，可刪除 key、重新啟用路由（[#2341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2341)）**：`POST /tenants/batch` 的每筆 op 與 `POST /groups/{id}/batch` 可帶 `unset: ["_routing"]`，從租戶 block 刪掉 `_routing`（含 mapping 型），回到 `_routing_defaults` 與 routing profile。這等於重新啟用路由，所以和 `_routing` patch 一樣受 domain policy 判定，PR 模式也會疊上同租戶前面已納入的 op。目前只接受 `_routing`；key 不存在為 no-op；同一 key 不可重複或同時出現在 `patch`。行為變更：`patch` 與 `unset` 同時為空改回 400 `INVALID_BODY`——`/tenants/batch` 的空 patch 原本當成 no-op 回 200，`/groups/{id}/batch` 原本回 400 `BAD_REQUEST`。
