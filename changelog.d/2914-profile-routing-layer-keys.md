---
section: Changed
topic: confd-family
issues: [2914]
created: 2026-10-13T13:23:52+00:00
---
- **⚠️ breaking：閾值 profile 不准帶 `_severity_dedup` 與 `_routing*`，exporter 不再套用（exporter；[#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)）**：路由產生器從不讀閾值 profile 的這些鍵，exporter 卻把它們補進租戶，`/effective`、`user_severity_dedup`、`da-guard served-values` 與 config-diff 因此說出 Alertmanager 實際沒有的通道或 dedup 設定。現在兩邊一致：exporter 不補，每次 load 印 ERROR 並計入 `da_config_profile_routing_keys`；da-guard 的 `routing_in_unread_location`（error）在 profile 裡擴及 `_severity_dedup` 與所有 `_routing*` 鍵，validate-config 的 profiles 列判 FAIL（`_metadata` 不再警告），schema 拒收。實際送出的 route 與 inhibit rule 不變。遷移：`_routing` 改寫到 `_routing_profiles.yaml` 並在租戶設 `_routing_profile`（或寫進租戶自己的 `_routing`），`_severity_dedup`、`_routing_profile` 寫進租戶自己的條目，再從 profile 刪掉。
