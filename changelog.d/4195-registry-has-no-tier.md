---
section: Changed
topic: tools
issues: [4195]
created: 2026-11-22T07:38:31+00:00
---
- **⚠️ breaking：閾值登錄表拿掉 `tier` 欄位，平台值改由 `arming` 決定（[#4195](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4195)）**：`rule-packs/threshold-registry.yaml` 的鍵不再寫 `tier`，schema 也不再接受它；`scaffold_tenant.RULE_PACKS` 每個規則包只有一個 `thresholds` 對照表（原本的 `defaults`／`optional_overrides` 兩格合併），讀它的外部腳本要跟著改。`platform-data.json` 的 `rulePacks[*].defaults` 只剩有平台值的鍵（`arming: default`），其餘的鍵移到 `declaredKeys`，列也帶 `metricClass`；portal 的離線 fallback 一併生成 `declaredKeys`，不再手抄。`_registry_lib.is_shipped_optional_key` 更名為 `is_flat_key`。
