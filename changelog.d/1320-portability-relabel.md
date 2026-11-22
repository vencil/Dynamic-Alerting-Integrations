---
section: Changed
topic: rule-packs
issues: [1320]
created: 2026-11-21T23:59:00+00:00
---
- **`oracle_sessions_active`、`mongodb_connections_current`、`clickhouse_active_connections` 不再標「待改比值」（rule-packs；[#1320](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1320)）**：三個 key 仍要部署方自己給值，行為不變；pack 檔頭與 Helm values 註解上的說明由「待改比值」改為「依部署而定」。Oracle 與 MongoDB：佔上限的比例早已各有一條不必填值的告警（`OracleHighSessionUtilization` 85%、`MongoDBConnectionSaturation` 80%），這兩個 key 量的是絕對數。ClickHouse：內建 `/metrics` 不匯出 `max_connections`（讀 v25.8.1.5101-lts 原始碼確認），沒有可以算比例的分母。
