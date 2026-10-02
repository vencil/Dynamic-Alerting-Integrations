---
section: Changed
topic: alertmanager-routing
issues: [2341]
created: 2026-10-02T00:46:29+00:00
---
- **`_routing` 既不是 mapping 也不是停用字串、`_routing_defaults` 不是 mapping 時改為報錯（[#2341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2341)）**：例如 `_routing: "slack"`、`[x]`、`~` 或未加引號的 `false`／`off`／`no`（YAML 布林），原本靜默改走 `_routing_defaults` 的路由，`--strict` 與 da-guard 都是結束碼 0。行為變更：`generate-routes` 對這個租戶不產出任何 route，印 `WARN … skipping`（`--validate` 與 validate-config 因此失敗），`--strict` 另報 `ERROR`；`_routing_defaults`（根目錄與子目錄）同樣改為阻擋。da-guard 新增 `routing_not_mapping`／`routing_defaults_not_mapping`（error），tenant-api 的 PUT 對 body 寫的這種 `_routing` 回 400 `INVALID_BODY`，schema 改為接受 mapping 或停用字串。要停用路由請加引號：`_routing: 'off'` 或 `disable`。batch 的 `_routing: "on"` 不再重新啟用路由。
