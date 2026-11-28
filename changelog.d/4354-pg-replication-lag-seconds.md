---
section: Fixed
topic: rule-packs
issues: [4354, 3833]
created: 2026-11-28T10:53:00+00:00
---
- **PostgreSQL 複寫延遲告警在 postgres_exporter v0.13.0 以後讀不到資料（rule-packs；[#4354](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4354)）**：`PostgreSQLHighReplicationLag`／`…Critical` 只讀 `pg_replication_lag`，那是 v0.12.1 以前範例 `queries.yaml` 的名稱；v0.13.0 起 exporter 內建的 replication collector 供應的是 `pg_replication_lag_seconds`，所以用新版 exporter 的租戶這兩條告警（`pg_replication_lag` 是平台預設啟用的 key）從來不會響。現在兩個名稱都讀（同一個量，單位都是秒），同一個 exporter 兩個都有時取 collector 的值。⚠️ 升級後，新版 exporter 上真的有複寫延遲的租戶會開始收到這條告警。兩個名稱有一處差異：collector 自 v0.14.0 起在 standby 已重放完收到的 WAL 時回報 0，舊查詢在 primary 閒置時延遲會持續上升。
