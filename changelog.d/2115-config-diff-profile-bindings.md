---
section: Fixed
topic: confd-reader-consistency
issues: [2115]
created: 2026-10-09T14:36:02+00:00
---
- **`config-diff` 的 profile 爆炸半徑改問 da-guard（tools；[#2115](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2115)）**：以前只認字串寫法的 `_profile`，`_profile: {default: std}` 這種 exporter 會綁 `std` 的租戶，在 `std` 改動時的 `affected_tenants` 是 `[]`；根目錄平台檔 `tenants:` 裡寫的 `_profile` 也看不到。現在名稱取 `da-guard effective`（exporter 的讀法），只算租戶自己這一層寫的，從子目錄 `_defaults.yaml` 繼承來的不算；仍以名稱比對，所以移除 profile 時還指名它的租戶照樣列出。⚠️ 有 profile 變更時需要 da-guard（映像內建）；找不到、失敗，或 `--new-dir` 有 exporter 解不出來的檔時結束碼 2，stderr 帶 da-guard 的原因。沒有 profile 變更的比對不呼叫 da-guard。
