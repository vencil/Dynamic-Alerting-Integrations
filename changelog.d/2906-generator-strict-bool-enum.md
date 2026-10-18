---
section: Changed
topic: alertmanager-routing
issues: [2906]
created: 2026-10-17T02:14:06+00:00
---
- **⚠️ Breaking：路由產生器的布林與列舉欄位只依型別與值判斷，型別不對或值不認得時所有模式拒收整棵樹（alertmanager-routing；[#2906](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2906)）**：過去接受、現在拒收的寫法：`_routing_enforced.enabled` 寫加引號的 `"true"`（過去 render 回 0、NOC 路由消失）；domain policy 的 `require_critical_escalation: "true"`（過去非 `--strict` 靜默不執行）；`_severity_dedup` 寫 `"no"`、`"yes"`、空字串或數字（過去當成 enable）；receiver 的 `send_resolved`、`require_tls`、`http_config.proxy_from_environment` 寫加引號的 `"yes"`／`"off"`（過去被換成布林寫出）。render、`--dry-run`、`--output-configmap`、`--apply`、`--validate`、`--strict` 都回 1、不寫出不套用，`validate-config` 的 `schema` 列 FAIL。修法：布林寫未加引號的 `true`／`false`，`_severity_dedup` 寫 `enable`／`disable`。未加引號的 `yes`／`no` 也拒收：產生器改讀 yaml.v3（ADR-036 步驟 4）後它們是字串。
