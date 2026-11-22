---
section: Changed
topic: rule-packs
issues: [3771, 4097]
created: 2026-11-13T10:30:00+00:00
---
- **⚠️ breaking：exporter 存活改以「租戶 × exporter 種類」判斷（rule-packs、exporter；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：租戶在頂層的 `_expected_exporters` 列出它跑的 exporter 種類（如 `[mariadb, redis]`；租戶檔的值整份取代平台檔的，不聯集）。exporter 對每個宣告的種類各吐一條 `tenant_expected_exporter{tenant, pack}`；`TenantExporterAbsent` 依各 exporter 自己的存在指標（`mysql_up`、`redis_up`……，寫在告警運算式裡，不另設 recording rule，各 pack 單獨載入或 federation 分層都不受影響）逐種類判斷：同租戶 redis 健康、mariadb 唯一副本掛時照樣 critical。`MassExporterOutage` 的門檻改數「有缺席的租戶」（單一租戶多種全掛不觸發），比例改以「租戶 × 種類」組計。告警帶 `pack` label（種類），外部 Alertmanager 可用它分派。⚠️ Kafka 的存活判定需要 danielqsj/kafka_exporter（讀 `kafka_brokers`），只部署 JMX exporter 的 Kafka 租戶宣告 `kafka` 會永久響 Absent。宣告的寫法與遷移見 `_expected_exporters` 一條（[#4097](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4097)）。
