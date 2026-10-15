---
section: Fixed
topic: alertmanager-routing
issues: [2989]
created: 2026-10-15T16:44:09+00:00
---
- **BREAKING — `_routing_enforced.match` 不是非空字串的 list 時改為拒收，不再靜默變成收全部告警（[#2989](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2989)）**：寫成 `routes[i].match` 的 mapping 寫法 `match: {severity: critical}`（或單一字串、數字）時，產生器原本丟掉整個 `match`，NOC route 收全部告警，`--validate --strict`、validate-config 與 da-guard 都回 0；list 裡放 `1`、`~`、mapping 時 `--validate` 直接 traceback。現在該區塊不啟用、不產出 enforced route，印 `WARN … skipping`，`--validate` 與 validate-config 失敗；render 結束碼不變，但 NOC 路由完全不存在，部署前務必跑 `--validate`。較早的根目錄 `_` 檔已啟用的區塊照常生效。da-guard 新增 `routing_enforced_match_invalid`（error）。修法：`match: ['severity="critical"']`。schema 改正為 AND 語意。
