---
section: Changed
topic: alertmanager-routing
issues: [3116]
created: 2026-10-17T03:46:23+00:00
---
- **⚠️ Breaking：domain policy 寫不成可執行的約束時，路由產生器所有模式拒收整棵樹（alertmanager-routing；[#3116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3116)）**：凡是報 `domain_policy_unusable` 的情況——policy 或 `constraints` 不是 mapping、`tenants` 不是 list 或有項目不是 tenant id、list 型 constraint 不是 list、receiver 類型不是 `email`／`pagerduty`／`rocketchat`／`slack`／`teams`／`webhook`（拼錯、`opsgenie`、`yes`、`1`、`~`、mapping）、`max_repeat_interval`／`min_group_wait` 讀不出、`require_critical_escalation` 不是布林、整份 `_domain_policy.yaml` 不能用——render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、`--strict` 都回 1、不寫出不套用，訊息列出原因。過去多數只在 `--strict` 擋，非 strict 時該約束或整份 policy 靜默不生效、結束碼 0。null 的 policy、`constraints` 與 constraint 值仍是「沒有約束」。`opsgenie` 已從 `domain-policy.schema.json` 的 enum 移除（產生器做不出 opsgenie receiver）。
