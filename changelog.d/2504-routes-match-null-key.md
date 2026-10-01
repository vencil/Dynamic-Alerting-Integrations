---
section: Fixed
topic: alertmanager-routing
issues: [2504]
created: 2026-10-01T14:42:57+00:00
---
- **`routes[].match` 裡的 null key 不再被靜默丟掉、讓 route 變寬（[#2504](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2504)）**：`match` 寫了 `~:`、`null:` 或空 key 時，`generate-routes` 先前把那個 key 丟掉，`{~: x, severity: critical}` 因此產成只比對 `severity="critical"` 的子路由（比寫的還寬）。行為變更：現在這類 key 與 `a-b` 同樣判成不合法的 label name，整條 route 略過並印 `WARN: <tenant>: routes[<i>]: match label None is not a valid label name, skipping`（`--validate` 與 validate-config 的 routes 列為阻擋），tenant `_routing` 與 routing profile 的 routes 皆同；只有一個 null key 時報的也是這句。結果與 da-guard 的 `invalid_route_entry` 一致；這條被略過的 route 也不算 `require_critical_escalation` 的 PagerDuty 升級路徑。其他位置（例如 tenants 層）的 null key 讀法不變。
