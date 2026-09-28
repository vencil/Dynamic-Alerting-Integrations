---
section: Changed
topic: exporter-config
issues: [944, 1339, 1568, 1911, 1957, 1976, 1982, 2019, 2191]
created: 2026-09-26T17:00:00+00:00
---
- **平台檔 `tenants:` 語意統一、設定合併規則收緊（exporter、tenant-api、tools；⚠️ BREAKING）**：根目錄 `_` 平台檔的 `tenants:` 改為「平台值先套、租戶檔同鍵後蓋」，與檔名排序無關，且不得建立租戶——只靠平台檔宣告的租戶會從 `/metrics` 與路由消失並 WARN（`check_routing_profiles --strict` 此時 rc 1）。`/effective`、`describe_tenant`（新增 `platform_overlay`）、da-guard（含排程形式的平台值）與 merged_hash 套用同一層，受影響租戶 merged_hash 變一次。⚠️ gauge `da_config_hierarchy_divergent_tenants` 改名 `da_config_subtree_undeliverable_tenants`，dashboard／alert 需跟改。⚠️ base 不在 defaults 的 `<metric>_critical` 改為驗證錯誤（tenant-api 寫入被擋）。`/effective` 對閾值鍵 `null` 改與 `/metrics` 一致（維持繼承；停用用 `"disable"`），`_routing` 分組／時間欄位可寫 `null`。⚠️ flat 模式每次掃描也 observe `da_config_scan_duration_seconds`，壞檔增量重載每輪計數 1→2。詳 [#1982](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1982)、[#2019](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2019)、[#1339](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1339)。
