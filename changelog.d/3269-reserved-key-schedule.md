---
section: Changed
topic: exporter
issues: [3269]
created: 2026-10-22T16:07:56+00:00
---
- **⚠️ Breaking：保留鍵不再接受時間窗與 expires 的排程形式（exporter／tenant-api／generator／schema；[#3269](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3269)）**：`_silent_mode`、`_severity_dedup`、`_state_<filter>`、`_profile`、`_routing_profile` 等保留鍵寫成 `{default: …, overrides: […]}`（或帶 `expires:`／`reason:` 的 `{default: …}`）時，讀取端一向只讀 `default`，時間窗從未生效，卻沒有任何訊號。現在 exporter 仍只用 `default`（租戶檔的送出結果不變；子目錄 `_defaults.yaml` 的變化見 #3295 那一則），載入時印 WARN 點名檔案、租戶與 key；`da-guard served-values` 與 `/effective` 不再把時段當成切點。tenant-api 的寫入拒收（400／batch 該筆 422），檔案裡原樣未改的既有值只回 NOTICE；JSON schema 補上 `_state_*` 且收緊：`_state_<filter>` 只收 `enable` 與 `disable`（或 `disabled`／`off`／`false`），exporter 讀成開啟的 `on`、`enabled` 等值現在 schema lint 報錯，請改寫 `enable`；路由產生器對 mapping 形式的 `_routing_profile` 改為整棵樹拒收（`routing_profile_invalid`）。週期性維護請用 `_state_maintenance` 的 `recurring:`。
