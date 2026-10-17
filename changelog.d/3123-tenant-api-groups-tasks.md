---
section: Security
topic: tenant-api
issues: [3123]
created: 2026-10-17T13:03:07+00:00
---
- **BREAKING — tenant-api 群組與 async task 不再透露別人的東西（[#3123](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3123)）**：`PUT /groups/{id}` 的 `members` 至少一個（清空請用 DELETE）；id 已被呼叫者看不到的群組佔用時回固定內容的 409 `GROUP_ID_UNAVAILABLE`（換 id，勿重試），只透露「id 已被使用」。既有的空群組改為只有規則不受任何範圍限制的 platform admin 看得到、改得到、刪得掉，其他人一律當它不存在（open mode 不變）。Task id 改為 128-bit 隨機值（含同步與 PR 模式回應的 `task_id`）；`GET /tasks/{id}` 只給提交者，其他人一律回與 task 不存在相同的 404，原本的 403 拿掉。同日同筆數的兩個 async 批次不再共用 id、後者被靜默丟棄。`config_derivation.parse_failed_hidden` 移除。
