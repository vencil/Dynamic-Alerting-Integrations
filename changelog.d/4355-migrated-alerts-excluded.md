---
section: Changed
topic: tools
issues: [4355]
created: 2026-11-28T13:00:00+00:00
---
- **⚠️ Breaking：migrated 告警不命中平台路由、不當 inhibit source（tools；[#4355](https://github.com/vencil/Dynamic-Alerting-Integrations/issues/4355)）**：身分 label 改為 `da_migrated="true"`，序列保留自己的 `component`。`generate-routes` 替 migrated 影子路由以外的每條平台路由加 `da_migrated=""`，名叫 `Watchdog` 或帶 `component=custom`／`sentinel`／`synthetic-probe` 的 migrated 告警只進影子路由，切換後照租戶路由；每條 inhibit rule 的 source 都要排除它，base 自帶的 Silent Mode 規則已改，`--strict` 下沒排除的 source 結束碼 1（不帶時 WARN）。`lint` 對寫 `da_migrated` 的租戶規則報 ERROR，migrate 的產出用 `--migrated`。`MigratedRulesExpiring` 不帶 `da_migrated`，影子期也送得到。已產出的 migrated 規則檔要用新版 `migrate` 重跑，再重跑 `generate-routes`；自己寫的 inhibit rule 在 source 加 `da_migrated=""`。
