---
section: Changed
topic: alertmanager-routing
issues: [2137]
created: 2026-09-27T05:20:38+00:00
---
- **receiver 必填欄位在 schema、路由產生器與 Go guard 三處統一（alertmanager-routing；[#2137](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2137)）**：PagerDuty 改為 `service_key` 與 `routing_key` **恰好一個**——只給 `routing_key`（Events API v2）的 receiver 以前整段路由被 WARN 並略過，現在合法；兩個都給以前會通過，現在 schema、`generate_alertmanager_routes` 與 guard 都擋下（Alertmanager 收到兩個會走 v1、默默忽略 `routing_key`）。email 缺 `from` 以前只在產生路由時才被略過，現在 `tenant-config.schema.json` 驗證就擋下。schema 補上管線本來就會轉給 Alertmanager 的 email `text`、slack `title_link`／`icon_emoji`；`routing-profiles.schema.json` 的 receiver 改為引用同一份定義；`check_confd_schema` 的 receiver 錯誤訊息改為指出該 type 的規則。
