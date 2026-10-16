---
section: Changed
topic: confd-family
issues: [2914]
created: 2026-10-13T13:23:52+00:00
---
- **⚠️ breaking：閾值 profile 裡的 `_severity_dedup` 與 `_routing*` 不再套用，並一律報錯（exporter；[#2914](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2914)）**：exporter 不再把它們補進租戶，`/effective`、`user_severity_dedup`、`da-guard served-values` 與 config-diff 不再出現這些值；每次 load 印 ERROR 並計入 `da_config_profile_routing_keys`。`generate-routes`（`--validate` 擋）報 `routing_in_unread_location`（da-guard 回 exit 4「未判定」，#2904），validate-config 的 profiles 列取產生器的判定、判 FAIL（`_metadata` 不再警告），schema 拒收。實際送出的 route 與 inhibit rule 不變。遷移：`_routing` 改寫到 `_routing_profiles.yaml` 並在租戶設 `_routing_profile`（或寫進租戶自己的 `_routing`），`_severity_dedup`、`_routing_profile` 寫進租戶自己的條目，再從 profile 刪掉。原因見 [`routing_in_unread_location`](docs/cli-reference.md#guard)。
