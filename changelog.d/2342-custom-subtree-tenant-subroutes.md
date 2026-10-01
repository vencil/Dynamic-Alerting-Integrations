---
section: Changed
topic: alertmanager-routing
issues: [2342]
created: 2026-09-30T23:46:03+00:00
---
- **custom 告警改照租戶的 `_routing.routes` 與 overrides 分流（`generate_alertmanager_routes.py`；[#2342](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2342)）**：`component="custom"` 隔離子樹裡每個租戶的子路由，現在帶著該租戶主路由底下的同一批子路由（overrides 在前、`routes` 在後）。先前 custom 告警一律落到租戶的主 receiver，例如 `routes: [{match: {severity: critical}, receiver: …}]` 對 custom 告警不生效。`--output-configmap` 與 `--apply` 兩條路徑都適用。主路由的 grouping 與 timing 不繼承，仍用 custom 子樹自己的設定，但子路由自己宣告的 timing／`group_by` 會一起帶進來；沒有子路由的租戶，產出與先前相同。
