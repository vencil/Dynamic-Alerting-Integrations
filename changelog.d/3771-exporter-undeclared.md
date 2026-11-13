---
section: Added
topic: rule-packs
issues: [3771]
created: 2026-11-13T10:31:00+00:00
---
- **新告警 `TenantExporterUndeclared`、`ExporterPresenceRulesNotLoaded`（rule-packs；[#3771](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/3771)）**：`TenantExporterUndeclared`（liveness pack，warning、`maintenance: mute`）對「有 schema 可宣告的資料庫 exporter 在 `job="tenant-exporters"` 下回應、卻沒宣告 `db_type`」的租戶持續 15 分鐘即響；只有 nginx／jvm 等 exporter 的租戶不響；threshold-exporter 自己掛掉時不響。`ExporterPresenceRulesNotLoaded`（平台 pack，warning）：存活判定的存在規則沒載入（只掛平台包，或 liveness 包被拆開）時，`TenantExporterAbsent` 與 `MassExporterOutage` 以哨兵 `tenant:exporter_presence_loaded` 守門而不響、不會對每個租戶齊響 critical，此時沒有任何 exporter 缺席會告警，由這條指出。**升級須知**：有 DB exporter 卻沒宣告 `db_type` 的既有租戶會收到 warning，補上即可；`scaffold_tenant` 不寫 `db_type`。liveness 包的存在規則與告警同在 `liveness-alert.yml`，`liveness-recording.yml` 不再存在；平台包要與 liveness 包一起掛。
