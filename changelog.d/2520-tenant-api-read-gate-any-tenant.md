---
section: Changed
topic: tenant-api
issues: [2520]
created: 2026-10-17T14:06:00+00:00
---
- **BREAKING — tenant-api 列表類端點的路由閘門改為「至少能讀一個租戶」（[#2520](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2520)）**：`/me`、`/tenants`、`/tenants/search`、`/groups`、`/tasks/{id}`、`/prs`、`/events`、`GET /federation/policy`、`/federation/tokens`、`POST /tenants/batch` 不再要求規則寫了 `tenants: ["*"]`；只有單租戶或前綴授權的呼叫者進得來，內容照舊由 handler 逐租戶過濾。`PUT`／`DELETE /groups/{id}` 同理改為至少能寫一個租戶，逐成員寫入檢查不變。判斷只看呼叫者自己的規則，不看租戶是否存在，200／403 不透露任何租戶。仍要求 `"*"`：saved views、`PUT /federation/policy`、帳號 backfill、`/audit/...`。零條規則照舊 fail-closed。閘門的 403 訊息改為 `insufficient permissions: no tenant grants read`；org-scoped 規則在呼叫者沒有 org claim 且 org 軸 enforce 時改回 403（先前回空列表）。
