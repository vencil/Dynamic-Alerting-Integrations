---
section: Deprecated
topic: rule-packs
issues: [3618, 1200]
created: 2026-11-07T02:07:42+00:00
---
- **`ClickHouseDown` 改為 deprecation shim，ClickHouse 停機改由 `TenantExporterAbsent` 負責（[#3618](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3618)）**：舊規則讀 `up{job="clickhouse"}`，但出貨的 scrape 設定沒有這個 job，預設部署下永遠不會觸發。ClickHouse 自己提供 /metrics，沒有另外的 exporter，所以 target 停掉就等於 server 停掉。`TenantExporterAbsent`（critical、`page`，需在 `_metadata` 宣告 `db_type: clickhouse`）在 3 分鐘後觸發；target 整個消失時也會觸發。`ClickHouseDown` 保留一個 release，改讀 `up{job="tenant-exporters"}` 並只限 ClickHouse 租戶；這段期間同一次停機會 page 兩次。下一個 release 移除，外部 Alertmanager 的 matcher 請改用 `TenantExporterAbsent`。
