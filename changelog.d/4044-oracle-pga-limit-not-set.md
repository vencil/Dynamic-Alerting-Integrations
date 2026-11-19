---
section: Added
topic: rule-packs
issues: [4044]
created: 2026-11-18T22:40:00+00:00
---
- **Oracle 新增資訊級告警 `OraclePGALimitNotSet`（[#4044](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4044)）**：資料庫的 `PGA_AGGREGATE_LIMIT` 為 0（沒有上限）時，不會計算這台的 PGA 比例。這時就算租戶設了 `oracle_pga_usage_percent`，`OracleHighPGAUsage` 也永遠不會響，而且因為閾值已設，`OracleThresholdNotConfigured` 也不會出聲。新告警在這兩個條件同時成立、持續 30 分鐘時發出，severity 為 `info`、不會 page，維護期間靜音。要結束提醒，可以替資料庫設定 `PGA_AGGREGATE_LIMIT`，或把 `oracle_pga_usage_percent` 寫成 `disable`。
