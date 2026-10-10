---
section: Fixed
topic: confd-family
issues: [1549]
created: 2026-10-10T02:45:29+00:00
---
- **`describe_tenant` 與 `tenant-verify` 的 `merged_hash` 改用 da-guard 的值，純搬檔不再算爆炸半徑（tools；[#1549](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1549)）**：`--show-sources` / `--all` 與 `tenant-verify` 印出的 `merged_hash` 讀自 `da-guard effective`，與 tenant-api `/effective` 相同；先前對帶 `_custom_alerts` 的租戶與 Go 不同，且搬檔就會變。沒有 da-guard 時 `describe_tenant` 印 `null` 並附 `merged_hash_error`，`tenant-verify` 回 1。`blast_radius` 以 `merged_hash` 判斷設定值是否變更，自訂告警另比 recipe 內容、名稱與是否為租戶自己的，不比宣告所在的檔：只搬檔時 `affected_tenants` 為 0，平台 recipe 改門檻時繼承它的租戶都列出。⚠️ 帶 `_custom_alerts` 的租戶，升級前存下的 `tenant-verify` 快照值會對不上，請重拍。
