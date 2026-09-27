---
section: Fixed
topic: confd-reader-consistency
issues: [2114]
created: 2026-09-27T16:50:00+08:00
---
- **路由、policy、診斷類工具以原始文字讀租戶 id，與 exporter 一致（tools；[#2114](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2114)）**：這些工具過去以 YAML 1.1 型別讀 `tenants:` 的 key（`010`→`8`、`yes`→`True`），exporter 則以原始文字為 id。改為原始文字的有：`generate-routes` / `explain-route` 的 conf.d 讀取、`check_routing_profiles.py`、`diagnose`、`describe_tenant.py`、`evaluate-policy` 與 `validate-config` 的 policy 檢查，以及共用的 `load_tenant_configs`（`opa-evaluate`、`config-diff`、`test-notification`、`threshold-recommend` 等經由它讀）。這些工具輸出 `"010"` 而非 `8`，JSON 的租戶 id 為字串；不同檔的 `123` 與 `"123"` 算同一租戶，平台檔 `tenants:` 區塊因此對得上。`generate-routes` 遇到不加引號的 `yes`、`010` 不再以 `TypeError` 中止；`exclude_tenants` 與 domain policy 的 `tenants` 清單改以文字比對。其餘讀租戶的工具另於 [#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115) 處理。
