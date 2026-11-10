---
section: Added
topic: platform-self-monitoring
issues: [869, 880]
created: 2026-09-26T17:00:00+00:00
---
- **每租戶 exporter 存活偵測取代全域 `absent()`（threshold-exporter、rule packs；[#869](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/869)、[#880](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/880)）**：舊的 per-db `*ExporterAbsent` 只要任一租戶的 exporter 還活著就不會 fire。新增 `tenant_expected_exporter` 與 `TenantExporterAbsent`，逐租戶判斷 exporter 是否真的 `up == 1`，涵蓋面擴及原本沒有存活告警的 DB；另有整個 job 消失時的 `TenantExporterJobAbsent` 與以比例＋下限判定的 `MassExporterOutage`。只有在 `_metadata` 宣告 `db_type` 的租戶會被納管。⚠️ breaking：舊的四條 `MariaDBExporterAbsent`／`PostgreSQLExporterAbsent`／`KafkaExporterAbsent`／`RabbitMQExporterAbsent` 同時移除、不留過渡期（[#3682](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3682)），以它們為 matcher 的外部 routing 請改用 `TenantExporterAbsent`。⚠️ `_metadata` 改為拒收未知鍵、`db_type` 限定列舉值，並新增 `check_confd_schema.py` 在 pre-commit／CI 驗證；退租時只刪 K8s target 卻留下 conf.d 設定也會被 gate 擋下。
