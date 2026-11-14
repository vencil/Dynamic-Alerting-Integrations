---
section: Changed
topic: rule-packs
issues: [3778]
created: 2026-11-13T07:03:34+00:00
---
- **⚠️ BREAKING：MariaDB 連線閾值改成佔 `max_connections` 的 %，key 改名且不設 alias（[#3778](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3778)）**：`mysql_connections`／`mysql_connections_critical`（絕對連線數）退役，改用 `mysql_connection_usage_percent`（預設 80）／`_critical`（建議 90）。比值逐 instance 計算、取最滿的那台，recording rule `tenant:mysql_connection_usage:percent` 取代 `tenant:mysql_threads_connected:max` 與 `tenant:mysql_connection_usage:ratio`。單位變了，舊值不能照搬，所以不走 `deprecated_aliases`，改登記為 registry 的 `retired_keys`：舊 key（含 `_critical`）不論寫在租戶檔或你自己的 `_defaults.yaml`，validate-config、`da-guard write` 與 tenant-api 都報 `retired` 並點名新 key，請改成新 key 並換算成百分比；tenant-api 可用 batch `unset` 刪除舊 key。alertname 不變，`metric_group` 改為 `mysql_connection_usage_percent`；自建查詢用到被移除的 recording rule 的，請改讀新的那條。分母 `mysql_global_variables_max_connections` 來自 mysqld_exporter 的 `global_variables` collector（預設開啟）：缺它或讀到 0 的 instance 不列入比值，連線告警不會響；`da-tools byo-check prometheus` 對這種 instance 報 warn。
