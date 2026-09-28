---
section: Fixed
topic: alertmanager-routing
issues: [2243]
created: 2026-09-28T02:07:00+00:00
---
- **domain policy 也檢查 `_routing.overrides` 產出的子路由（`generate_alertmanager_routes.py`；[#2243](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2243)）**：先前只檢查 tenant 主路由，某條 override 換成被禁止的 receiver 類型（例如 finance 禁 slack）時 `--validate --strict` 仍 exit 0 並產出該 receiver。現在每條會產出 receiver 的 override 都套用 `allowed_receiver_types` / `forbidden_receiver_types` / `max_repeat_interval` / `min_group_wait` / `enforce_group_by`，訊息點名 `override[N] (alertname=…|metric_group=…)`；`--strict` 下 exit 1，非 strict 為 WARN。override 沒寫的 timing / `group_by` 以 tenant 主路由的值檢查（它在 Alertmanager 裡繼承的就是主路由的值，見 [#2252](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2252)），訊息標明值是繼承來的。
