---
section: Changed
topic: exporter
issues: [3638]
created: 2026-11-12T03:40:00+00:00
---
- **`da-guard served-values` 的 `routing` 依 `_routing` 寫下的結構讀，純量不再被解析成路由（[#3638](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3638)）**：以前 Go 端的 routing 讀取把值的文字重新解析，所以字串 `_routing: "receiver: {type: webhook, …}"`（或區塊純量 `|`）會在 `served-values` 的 `routing` 裡出現一筆路由；而擁有 routing 判決的 route generator 一向把它讀成「既不是映射、也不是停用字串」，不產生路由。現在兩邊對「讀不讀成路由」一致：純量不是路由，da-guard 印 `WARN: invalid _routing config for tenant=…: written as a scalar, not a mapping`；停用字串（`_routing: disable` 等）與空字串也不是路由，不印這則 WARN（以前停用字串會印一則 YAML 解析錯誤；空字串由 route generator 判為拒收，da-guard 照舊不報）。這種寫法改列在該租戶的 `unserved`（原樣），`routing` 不再出現；讀 `routing` 的 config-diff 與 platform-data 因此不再為它顯示 receiver。至此 exporter 端不再有從保留鍵文字重新解碼的地方，`structureOf` 是唯一一處；守衛的凍結基準因此移除，新出現的重新解碼仍會被擋。
