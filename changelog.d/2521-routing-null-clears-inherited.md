---
section: Fixed
topic: alertmanager-routing
issues: [2521]
created: 2026-10-03T16:50:00+00:00
---
- **schema 接受租戶 `_routing.overrides: ~`／`routes: ~`，以及 `_routing_defaults.overrides`（alertmanager-routing；[#2521](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2521)）**：產生器、`validate-config`、da-guard 與 tenant-api 一直把租戶層的 `overrides: ~`／`routes: ~` 當成 `[]`，清掉從 `_routing_defaults` 或 routing profile 繼承來的整串子路由，但 `tenant-config.schema.json` 拒收，`check_confd_schema` 與編輯器因此報錯；現在 schema 收下並寫明語意。`_routing_defaults.overrides` 產生器照收、租戶會繼承，schema 卻不認得這個鍵，現在補上（條目定義與租戶相同、時長走 `definitions.duration`）。不變的部分：`_routing_defaults.routes` 照舊被拒（產生器丟棄並阻擋），子目錄層 `_routing_defaults.overrides: ~` 照舊是結束碼 2。
