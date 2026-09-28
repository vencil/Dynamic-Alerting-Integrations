---
section: Fixed
topic: exporter
issues: [2280]
created: 2026-09-28T09:15:00+00:00
---
- **da-guard 改判解析後的 routing，開始檢查 `routes` 與 domain policy（[#2280](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2280)）**：先前只讀租戶原始 `_routing`——只靠 `_routing_profile` 的租戶沒被檢查，profile 加租戶 overrides 的租戶被誤報 `missing_receiver_field`，`routes` 與 domain policy 都沒人看。現在與 route generator 同一套三層合併（`_routing_defaults` → profile → 租戶 `_routing`，替換 `{{tenant}}`），主 receiver、`overrides`、`routes` 的 receiver 都判形狀與 domain policy（forbidden 與 allowed 分開判、可同時兩條）。新 finding：`invalid_route_entry`、`domain_policy_violation`、`routing_defaults_routes_ignored`（error）、`domain_policy_unusable`（error）、`unknown_routing_profile`、`routing_profiles_unusable`（warn）。⚠️ 客戶的 guard workflow 會開始擋 policy 違規與 profile 帶來的壞 `routes`；含 `{{tenant}}` 的 receiver URL 不再誤報格式錯。
