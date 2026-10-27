---
section: Removed
topic: confd-family
issues: [2388, 3341]
created: 2026-10-03T07:15:19+00:00
---
- **⚠️ BREAKING：子目錄 `_defaults.yaml` 的 defaults 不再帶保留鍵，exporter 不套用、da-guard 報 `subtree_default_reserved_key` error；租戶這一側把保留鍵寫成 null 改為回到平台預設（threshold-exporter / da-guard；[#2388](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/2388)、[#3341](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3341)）**：子目錄 defaults 的 `_state_*`、`_silent_mode`、`_severity_dedup`、`_metadata` 等保留鍵（`_routing`／`_routing_*` 除外，由產生器報）不論值都不套用，先前生效的 `disable` 與數值也失效（例如子目錄寫 `_state_maintenance: disable`，filter 改依根目錄 `default_state`），`/effective` 不顯示子目錄的值，`maintenance_scheduler` 與 policy 規則同樣讀不到；`/effective`、`not_served` 依租戶這一側而定的情形見 ADR-017 決策 4 的表。da-guard 對每個寫了非 null 值的（租戶, key）報 error（結束碼 1），訊息列出寫了該 key 的檔與值：要保留就把值搬進租戶 `tenants:` 條目，再從子目錄檔刪掉。租戶檔、根目錄平台檔 `tenants:` 或 profile 把保留鍵寫成 `~`、空值或 `{default: null}`，一律刪除繼承值：`_state_*` 依 `default_state`（先前即使 `disable` 也會開啟）、`_severity_dedup` 開啟且不再印 WARN、`_silent_mode` 不靜音。
