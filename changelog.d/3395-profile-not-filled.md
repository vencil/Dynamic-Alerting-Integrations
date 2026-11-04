---
section: Changed
topic: exporter
issues: [3395]
created: 2026-11-03T15:49:33+00:00
---
- **`da-guard effective` 與 `/effective` 的 `not_served` 多一個原因 `profile_not_filled`（threshold-exporter / tenant-api / da-guard；[#3395](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3395)）**：租戶綁定的 profile 寫了根目錄只在 `optional_overrides:` 宣告、`defaults:` 沒有值的鍵時，exporter 依 [#1189](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1189) 不讓 profile 填入，過去只在載入 log 留一行 WARN，結構化輸出看不到；現在 `not_served` 逐鍵指名，`file` 為 profile 檔，鍵用正式名（與那行 WARN 相同）。沒有任何一層套用這個值，所以該鍵不在 `effective_config`；租戶檔或平台檔 `tenants:` 條目自己寫了這個鍵時不標。與 `unconsumed` 一樣只在 `not_served` 出現，不是 da-guard `value_not_served` 的原因，也不計入 exporter 的 `da_config_values_not_served`。
