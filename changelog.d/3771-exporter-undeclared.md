---
section: Added
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:31:00+00:00
---
- **新告警 `TenantExporterUndeclared`：有 tenant-exporters target、卻沒宣告 `db_type` 的租戶（rule-packs；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：exporter 存活告警只涵蓋在 `_metadata` 宣告 `db_type` 的租戶，沒宣告的租戶 exporter 全掛也不會有告警。這條規則對「被 `job="tenant-exporters"` scrape、卻沒宣告」的租戶持續 15 分鐘即響，`severity: warning`、`maintenance: mute`。threshold-exporter 自己掛掉時不響（以 `tenant_metadata_info` 是否存在守門），不會對所有租戶齊響。**升級須知**：既有未宣告 `db_type` 的租戶升級後會收到這條 warning，在 `_metadata.db_type` 補上它使用的資料庫種類即可；`scaffold_tenant` 目前不寫 `db_type`，新建租戶需手動補。
