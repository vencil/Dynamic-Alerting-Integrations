---
section: Fixed
topic: alertmanager-routing
issues: [2758]
created: 2026-10-09T13:08:42+00:00
---
- **receiver type 約束的項目是集合時不再讓產生器崩潰（[#2758](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2758)）**：`_domain_policy.yaml` 的 `forbidden_receiver_types`／`allowed_receiver_types` 若有項目是 mapping、list 或 `!!set`（例如 `[slack, {a: 1}]`），`generate-routes` 與 `explain-route --trace` 先前直接 traceback、結束碼 1；`allowed` 混有 null 與字串項目時（`[!!null x, email]`），違規訊息也會 traceback。現在不再 traceback：這種項目不是 receiver 類型，產生器在所有模式拒收整棵樹並點名該項目（finding kind `domain_policy_unusable`，規則見 [#3116](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3116)）；da-guard 不判 policy，把 `_domain_policy.yaml` 列為未判定證據（exit 4）。
