---
section: Added
topic: exporter-config
issues: [656, 658, 1189, 1308, 1310, 1311]
created: 2026-09-26T17:00:00+00:00
---
- **`optional_overrides` 宣告層與設定 schema 接上編輯器（threshold-exporter、helm、da-tools、schema）**：新增第三種閾值狀態——平台宣告 key 但不給值，租戶設了才發射，不設就沒有 series（無平台回退、`expires:` 不適用）。清單只能由平台檔宣告（租戶檔內列出會被剝除），出貨的 chart（`thresholdConfig.optional_overrides`）、`conf.d/_defaults.yaml` 與 `da-tools init`／`scaffold_tenant` 產生的 `_defaults.yaml` 都帶上 9 個平鍵，租戶可經 tenant-api 寫入；`diagnose --show-inheritance` 新增 `declared` 欄位。schema：`tenant-config.schema.json` 接進 dev container 編輯器即時驗證，新增 `platform-defaults.schema.json` 擋 `_defaults.yaml` 頂層 key 打錯；`_severity_dedup` enum 更正為 `enable`／`disable`，從未接線的 `_operator` 自 schema 移除。詳 [#1189](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1189)、[#658](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/658)。
