---
section: Changed
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:30:00+00:00
---
- **⚠️ breaking：exporter 存活改以「租戶 × db_type」判斷，`_metadata.db_type` 可寫成列表（rule-packs、exporter、tenant-api；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：`db_type` 可寫成字串或不重複的非空列表（如 `[mariadb, redis]`），字串等同一元素列表；租戶檔的值整份取代平台檔的，不聯集。exporter 對每個宣告的種類各吐一條 `tenant_expected_exporter`；`TenantExporterAbsent` 依各 exporter 自己的存在指標（`mysql_up`、`redis_up`……，寫在告警運算式裡，不另設 recording rule，各 pack 單獨載入或 federation 分層都不受影響）逐 db_type 判斷：同租戶 redis 健康、mariadb 唯一副本掛時照樣 critical。`MassExporterOutage` 的門檻改數「有缺席的租戶」（單一租戶多種全掛不觸發），比例改以「租戶 × db_type」組計。⚠️ Kafka 的存活判定需要 danielqsj/kafka_exporter（讀 `kafka_brokers`），只部署 JMX exporter 的 Kafka 租戶宣告 `kafka` 會永久響 Absent。**遷移**：tenant-api 清單／搜尋的 `db_type` 由字串改為字串陣列，讀它的整合要改；`db_type` 篩選比對任一種類。
