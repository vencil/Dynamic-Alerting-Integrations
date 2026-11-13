---
section: Added
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:31:00+00:00
---
- **新告警 `TenantExporterUndeclared`：有資料庫 exporter、卻沒宣告 `db_type` 的租戶（rule-packs；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：exporter 存活告警只涵蓋在 `_metadata` 宣告 `db_type` 的租戶。這條規則（liveness pack，`severity: warning`、`maintenance: mute`）對 1 小時內在 `job="tenant-exporters"` 下出現過 schema 可宣告的資料庫 exporter（`mysql_up`、`redis_up` 等）、卻沒宣告 `db_type` 的租戶持續 15 分鐘即響；只有 nginx／jvm 等 exporter 的租戶不響。這種租戶的 exporter 全掛時它不會自己解除，1 小時後仍未恢復才解除；threshold-exporter 自己掛掉時不響。**升級須知**：有資料庫 exporter 卻沒宣告 `db_type` 的既有租戶升級後會收到這條 warning，在 `_metadata.db_type` 補上即可；`scaffold_tenant` 不寫 `db_type`，新建租戶需手動補。
