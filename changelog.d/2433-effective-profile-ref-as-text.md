---
section: Fixed
topic: confd-reader-consistency
issues: [2433]
created: 2026-09-30T12:14:06+00:00
---
- **`/effective`、tenant-api、da-guard 與 merged_hash 以原始文字讀 `_profile`，與 `/metrics` 一致（exporter；[#2433](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2433)）**：`_profile: 010` 沒加引號時，`/metrics` 套用 profile `010`，`/effective` 卻讀成整數 `8`，沒有套到任何 profile；`_profile: !!binary MDEw` 則反過來，`/metrics` 回報 unknown profile `MDEw`，`/effective` 卻套用了 `010`。現在這些讀取端都依 `_profile` 的原文選 profile，`/effective` 回傳的 `_profile` 也是原文（`"010"`）。租戶檔與根平台檔 `tenants:` 區塊裡的 `_profile` 都適用。加引號的值與一般名稱不受影響；`_profile` 為 null、mapping 或 list 時維持原本的解讀。
