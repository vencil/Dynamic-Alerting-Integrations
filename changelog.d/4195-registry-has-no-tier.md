---
section: Changed
topic: tools
issues: [4195]
created: 2026-11-22T07:38:31+00:00
---
- **⚠️ breaking：閾值登錄表拿掉 `tier` 欄位，平台值改由 `arming` 決定（[#4195](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4195)）**：`rule-packs/threshold-registry.yaml` 的鍵不再寫 `tier`，schema 也不再接受它；`scaffold_tenant.RULE_PACKS` 每個規則包只有一個 `thresholds` 對照表（原本的 `defaults`／`optional_overrides` 兩格合併），讀它的外部腳本要跟著改。`platform-data.json` 的 `rulePacks[*].defaults` 只剩有平台值的鍵（`arming: default`），其餘的鍵移到 `declaredKeys`，列也帶 `metricClass`；portal 的離線 fallback 一併生成 `declaredKeys`，不再手抄；告警預覽、閾值熱圖、規則包選單的鍵數與 YAML 驗證器的 metric 自動完成改讀兩者（宣告鍵用參考值當示範數字），沒有平台值的規則包（redis、mongodb、kafka…）不再一片空白。`da-tools backtest --tenant` 寫出的根檔不再抄登錄表的值（讀取端內建），所以回測 operator 鍵的 `_critical` 時只送 critical 列、不再多一條根層 warning 列，與 exporter 的行為一致（#4173）。`_registry_lib.is_shipped_optional_key` 更名為 `is_flat_key`。
