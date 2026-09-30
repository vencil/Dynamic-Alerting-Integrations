---
section: Fixed
topic: confd-reader-consistency
issues: [2433]
created: 2026-09-30T12:14:06+00:00
---
- **`/effective`、tenant-api、da-guard 與 merged_hash 以原始文字讀 `_profile`，與 `/metrics` 一致（exporter；[#2433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2433)）**：`_profile: 010` 沒加引號時，`/metrics` 套用 profile `010`，`/effective` 卻讀成整數 `8`，沒有套到任何 profile；`_profile: !!binary MDEw` 則反過來。含 `default:` 的寫法（`_profile: {default: '010'}`）`/metrics` 會套用，`/effective` 卻沒套。現在這些讀取端都依 `/metrics` 選中的名稱選 profile，`/effective` 回傳的 `_profile` 就是那個名稱（`"010"`）。租戶檔與根平台檔 `tenants:` 區塊都適用。加引號的值與一般名稱不受影響；null 維持原本的解讀。`describe_tenant.py` 對 mapping 形的 `_profile` 尚未對齊（[#2515](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2515)）。以 merge key 帶入 `default:` 的寫法（`_profile: {<<: {default: '010'}}`）在 `/metrics` 仍不套用，兩邊一致。⚠️ 已這樣寫的租戶，升級後 merged_hash 會變一次，觸發一次 reload 事件；其中 `{default: ~}` 的 `_profile` 由 map 變成 `""`、merge key 寫法由 map 變成 YAML 文字，這兩種選到的 profile 不變。
