---
section: Changed
topic: exporter
issues: [3209]
created: 2026-10-21T15:30:00+00:00
---
- **custom alert 的門檻被拒收時，da-guard 與 validate-config 報錯（[#3209](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3209)，併入 [#3140](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3140)）**：⚠️ 行為變更。`_custom_alerts` 某條目的 `threshold:` 寫成 exporter 讀不懂或非有限的值（`abc`、`NaN`、`-Inf:critical`、`70:high` 等）時，exporter 本來就拒收該條目、不送 series，但 da-guard 與 validate-config 都是結束碼 0，只有 `da-guard served-values` 標 `rejected`。現在與閾值列拒收同一種 finding：da-guard 報 `value_not_served`（error，Field `_custom_alerts[<index>]`），validate-config 的 `guard_errors` 列照列、FAIL，兩者由 0 變 1。`da-guard served-values` 的 `custom_alerts[]` 在這類條目另帶 `refused`（`non_finite`／`unparsed`，與 `keys[].refused` 同一組值；schema 仍為 `da-guard.served-values/v1`）。因其他欄位（`window`、`name` 等）被拒收的條目不在此列。
