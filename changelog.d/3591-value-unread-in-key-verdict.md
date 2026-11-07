---
section: Changed
topic: exporter
issues: [3591]
created: 2026-11-07T10:00:00+08:00
---
- **⚠️ BREAKING：讀不懂的保留鍵值進共用判決（`value_unread`），在寫它的那一層判（[#3591](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3591)）**：`da-guard key-verdict` 新增 finding kind `value_unread`，涵蓋 exporter 讀不懂的 `_silent_mode`、`_state_<filter>`、`_severity_dedup` 值，`_metadata` 裡 `TenantMetadata` 不認得的欄位或解不開的值，以及指到不存在 profile 的 `_profile`。租戶檔裡的是 error（validate-config、產生器 `--validate` 擋整棵樹，`da-guard write` 擋新出現的）；profile 與根平台檔 `tenants:` 裡的是 notice，不算到選用或繼承它的租戶頭上。tenant-api 寫入時另外拒收租戶寫的 `_metadata` 未知欄位與讀不懂的 `_severity_dedup`（原本收下）；檔裡原本就有、沒改動的只回 NOTICE。產生器 `--findings-json` 的對應 kind 是 `key_value_unread`，`da-guard write` 的 `worsened` 是 `value_unread`。`generate_tenant_fixture` 的 synthetic-v2 不再在 `_metadata` 寫 `_overlay_l<N>`（其餘內容同 seed 不變）。
