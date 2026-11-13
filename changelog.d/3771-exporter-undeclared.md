---
section: Added
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:31:00+00:00
---
- **新告警 `TenantExporterUndeclared`：有某種資料庫 exporter、卻沒宣告那一種 `db_type`（rule-packs；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：exporter 存活告警只涵蓋 `_metadata.db_type` 宣告了的種類。這條規則（liveness pack，`severity: warning`、`maintenance: mute`）逐 (租戶, db_type) 判斷：1 小時內在 `job="tenant-exporters"` 下出現過 schema 可宣告的資料庫 exporter（`mysql_up`、`redis_up` 等）、而租戶沒宣告那一種，持續 15 分鐘即響——整個租戶沒宣告，或宣告了 `[mariadb]` 卻同時跑著 redis exporter 都算。只有 nginx／jvm 等 exporter 的租戶不響；exporter 全掛時 1 小時內不會自己解除；threshold-exporter 自己掛掉或租戶已退租時不響。**升級須知**：既有租戶有沒宣告的資料庫 exporter 時升級後會收到這條 warning，把該種類補進 `_metadata.db_type` 即可；`scaffold_tenant` 不寫 `db_type`，新建租戶需手動補。
