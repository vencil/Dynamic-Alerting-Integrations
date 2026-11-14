---
section: Removed
topic: rule-packs
issues: [1204, 1200]
created: 2026-11-07T01:26:37+00:00
---
- **⚠️ BREAKING：刪除 11 條沒有任何消費者的 recording rule（[#1204](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/1204)）**：`tenant:clickhouse_merges_rate:rate5m`、`tenant:es_indexing:rate5m`、`tenant:mongodb_connections_available:min`、`tenant:mongodb_document_ops:rate5m`、`tenant:mysql_buffer_pool_usage:ratio`、`tenant:mysql_uptime:hours`、`tenant:pg_connections:max`、`tenant:pg_transactions:rate5m`、`tenant:pg_uptime:hours`、`tenant:redis_commands:rate5m`、`tenant:redis_memory_usage:ratio`。平台的告警、dashboard、observed map 都不讀它們；自建查詢或 dashboard 若用到，請改用原始指標。
