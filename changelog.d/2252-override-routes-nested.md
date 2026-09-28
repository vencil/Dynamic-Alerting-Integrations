---
section: Changed
topic: alertmanager-routing
issues: [2252]
created: 2026-09-28T03:34:40+00:00
---
- **`_routing.overrides` 產生的路由改成 tenant 主路由的子路由（`generate_alertmanager_routes.py`；[#2252](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2252)）**：override 沒寫的 `group_wait` / `group_interval` / `repeat_interval` / `group_by` 改為沿用該 tenant `_routing` 的值；先前 override 與主路由同層，沒寫的值用的是 root route 的值（例如 `repeat_interval` 12h）。產出的 Alertmanager 設定結構跟著改變：override 路由不再出現在 `route.routes` 頂層，而是放在 tenant 主路由的 `routes` 底下，只帶自己的 `alertname` / `metric_group` matcher，`tenant` matcher 留在主路由。告警送到哪個 receiver 不變；`_routing_enforced` 與其他平台路由的位置、`continue` 語意也不變。自己解析產出、假設 override 在頂層的工具要改成往下走一層。
