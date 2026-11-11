---
section: Removed
topic: rule-packs
issues: [3682, 869]
created: 2026-11-10T15:14:06+00:00
---
- **⚠️ breaking：移除 `MariaDBExporterAbsent`、`PostgreSQLExporterAbsent`、`KafkaExporterAbsent`、`RabbitMQExporterAbsent`（[#3682](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3682)）**：不留過渡期，由逐租戶的 `TenantExporterAbsent` 取代；外部 Alertmanager 以舊 alertname 寫的 matcher 請改成 `alertname="TenantExporterAbsent"` 再加上對應的 `db_type` matcher（`mariadb`／`postgresql`／`kafka`／`rabbitmq`）——新告警四種 DB 共用一個名稱，只換 alertname 會讓原本按 DB 分派給不同團隊的 routing 混在一起。舊規則是全域 `absent()`，任一租戶活著就不觸發、`tenant` 永遠空白。沒有在 `_metadata` 宣告 `db_type` 的租戶不受 `TenantExporterAbsent` 納管：「某一種 DB 的 exporter 全滅、其他種還活著」時不再有告警，只剩整個 job 消失時的 `TenantExporterJobAbsent`；請宣告 `db_type`。BYO 自訂 scrape job 名的客戶，要改名或 relabel 成 `tenant-exporters`。
