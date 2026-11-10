---
section: Removed
topic: rule-packs
issues: [3618, 3682, 1200]
created: 2026-11-07T02:07:42+00:00
---
- **⚠️ breaking：移除 `ClickHouseDown`，ClickHouse 停機改由 `TenantExporterAbsent` 負責（[#3618](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3618)、[#3682](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3682)）**：舊規則讀 `up{job="clickhouse"}`，但出貨的 scrape 設定沒有這個 job，預設部署下永遠不會觸發。ClickHouse 自己提供 /metrics，沒有另外的 exporter，所以 target 停掉就等於 server 停掉。`TenantExporterAbsent`（critical、`page`，需在 `_metadata` 宣告 `db_type: clickhouse`）在 3 分鐘後觸發；target 整個消失時也會觸發。`ClickHouseDown` 直接移除、不留過渡期；外部 Alertmanager 以它為 matcher 的 routing 請改用 `TenantExporterAbsent`。BYO 自訂 `job="clickhouse"` 的客戶舊規則原本會觸發，要把 scrape job 改名或 relabel 成 `tenant-exporters` 並宣告 `db_type`，否則沒有替代告警。
