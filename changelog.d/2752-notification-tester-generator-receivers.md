---
section: Fixed
topic: confd-family
issues: [2752]
created: 2026-10-08T18:24:32+00:00
---
- **`test-notification` 改測路由產生器解析出的 receiver（[#2752](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2752)）**：⚠️ 行為變更：每租戶的 `_routing` 改由 `generate-routes` 的讀取器解析（`_routing_defaults` 鏈、routing profile、平台檔 `tenants:` 覆蓋、子目錄租戶檔），不再自己讀根目錄租戶檔，所以寫在這些地方的 receiver 不再回報成「沒有 receiver」、rc 0。路由產生器拒收的樹（檔案讀不到、同一租戶宣告於兩檔、routing-tree 錯誤、租戶 ID 不合法）exit 2，stderr 帶產生器自己的拒收訊息，`--json` 印 `status: caller_error`、`reason: routing_tree_refused`。
