---
section: Fixed
topic: confd-reader-consistency
issues: [2115]
created: 2026-10-09T14:36:02+00:00
---
- **`config-diff` 對 profile 變更報出的租戶，就是 exporter 綁定的租戶（tools；[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：以前只認字串寫法的 `_profile`，`_profile: {default: std}` 這種 exporter 會綁 `std` 的租戶，在 `std` 改動時不會列出；根目錄平台檔 `tenants:` 裡寫的 `_profile` 也看不到。現在比的是 exporter 送出的值，profile 改動會在每個受影響租戶的閾值表中出現；從子目錄 `_defaults.yaml` 繼承、exporter 不綁的 `_profile` 不算。指向不存在的 profile、不改變任何值的 `_profile` 變更不列出。
