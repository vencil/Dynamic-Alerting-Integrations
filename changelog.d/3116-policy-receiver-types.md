---
section: Changed
topic: alertmanager-routing
issues: [3116]
created: 2026-10-17T03:46:23+00:00
---
- **⚠️ Breaking：domain policy 的 receiver 類型清單只收產生器做得出來的類型，否則所有模式拒收整棵樹（alertmanager-routing；[#3116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3116)）**：`forbidden_receiver_types`／`allowed_receiver_types` 的每一項必須是 `email`、`pagerduty`、`rocketchat`、`slack`、`teams`、`webhook` 之一。過去拼錯的 forbidden 類型（`[webhok]`）什麼都不禁止、結束碼 0；拼錯的 allowed 類型（`[slak]`）讓每個 receiver 都違規，訊息卻不指出清單寫錯。現在未知名稱、`opsgenie`、非字串項目（`yes`、`1`、`~`、mapping）在 render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、`--strict` 都回 1、不寫出不套用，finding kind `domain_policy_unusable`，訊息列出不認得的值與可用類型。`opsgenie` 已從 `domain-policy.schema.json` 的 enum 移除（產生器做不出 opsgenie receiver）。清單是 null 或沒寫照舊不約束。
