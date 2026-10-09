---
section: Security
topic: tenant-api
issues: [1529, 1530, 1531]
created: 2026-10-09T13:18:55+00:00
---
- **group 端點不再透露 caller 讀不到的成員，PUT 也不再繞過 DELETE 的成員閘門（tenant-api；[#1529](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1529)、[#1530](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1530)、[#1531](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1531)）**：行為變更，原本會過的請求可能改回 403／404。`PUT /groups/{id}` 更新既有 group 時，也要對它既有的每個成員有 write（含 `members: []`）。`POST /groups/{id}/batch` 的 `results` 與 `summary`（同步、非同步 `GET /tasks/{id}`、PR 模式）只列、只算 caller 讀得到的成員。group 的 403 只點名 caller 讀得到的成員，其餘只說「另有無法檢視的成員」，不給數字。caller 看不到的 group，在 GET、DELETE、batch 一律回與不存在相同的 404；沒有成員的 group 改為對所有人可見：先前只要設了 `_rbac.yaml`，它在 GET／LIST 對所有人都是 404，連平台管理員也看不到，這一點所有啟用 RBAC 的部署都會看到差異。其餘變更只在讀取或寫入權限依租戶而不同的部署（org-scope 規則標記了租戶，或寫入受 environments／domains 限定）才看得到差異。
