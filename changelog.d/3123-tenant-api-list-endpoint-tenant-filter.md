---
section: Security
topic: tenant-api
issues: [3123]
created: 2026-10-17T13:03:06+00:00
---
- **BREAKING — tenant-api 列表／串流端點逐租戶過濾（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）**：路由對 `"*"` 的讀取檢查擋不住 `tenants: ["*"]` 加 org-scope（或 environments／domains）的規則，以下端點改為逐租戶判斷。`GET /events` 每個訂閱者只收到讀得到的租戶的事件，送出當下以最新 RBAC 判斷；groups、views、federation policy 等不屬單一租戶的 state 事件只送給讀得到全部租戶的呼叫者。`GET /prs` 的批次 PR/MR 只給讀得到它涵蓋的每個租戶的呼叫者：`tenant_id` 改為空、新增 `tenants` 欄位；重啟前開的批次 PR 不知涵蓋哪些租戶，只給讀得到全部租戶的呼叫者；`batch` 保留為租戶 id，不能寫入。`DELETE /federation/tokens/{id}` 無權撤銷時改回與 token 不存在相同的 404（原為帶租戶 id 的 403）。
