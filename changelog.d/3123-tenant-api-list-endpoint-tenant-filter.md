---
section: Security
topic: tenant-api
issues: [3123]
created: 2026-10-17T13:03:06+00:00
---
- **BREAKING — tenant-api `/events`、`/prs` 逐租戶過濾（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）**：路由對 `"*"` 的讀取檢查擋不住 `tenants: ["*"]` 加 org-scope 的規則。兩個端點改用所有列表共用的讀取平面述詞逐租戶判斷：標了別的 org 的租戶一律濾掉，沒標 org 的要開 org-scope enforce 才濾；environments／domains 不是讀取平面的邊界。`GET /events` 每個訂閱者只收到讀得到的租戶的事件，送出當下以最新 RBAC 判斷；groups、views 等不屬單一租戶的事件只送給讀得到全部租戶的呼叫者。`GET /prs` 的批次 PR/MR 只給讀得到它涵蓋的每個租戶的呼叫者，`tenant_id` 改為空、新增 `tenants`；重啟前開的只給讀得到全部租戶的呼叫者。`PUT /tenants/{id}` 的 409 `pending_pr_exists` 只在呼叫者看得到那支 PR 時才附 URL 與編號。
