---
section: Fixed
topic: alertmanager-routing
issues: [2989]
created: 2026-10-15T16:44:09+00:00
---
- **`_routing_enforced.match` 不是 list 時不再靜默變成收全部告警（[#2989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2989)）**：寫成 `routes[i].match` 的 mapping 寫法 `match: {severity: critical}`（或單一字串、數字）時，產生器原本丟掉整個 `match`，NOC route 收全部告警，`--validate --strict`、validate-config 與 da-guard 都回 0。行為變更：該 `_routing_enforced` 區塊不啟用、不產出 enforced route，印 `WARN … skipping`，`--validate` 與 validate-config 因此失敗；較早的根目錄 `_` 檔已啟用的區塊照常生效。da-guard 新增 `routing_enforced_match_invalid`（error）。修法：寫成 list，例如 `match: ['severity="critical"']`。config-diff 的 `_routing_enforced` 改回完整涵蓋。
